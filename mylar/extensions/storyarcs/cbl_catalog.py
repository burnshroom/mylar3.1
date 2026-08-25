"""
DieselTech CBL Catalog Repository Adapter and Cache Manager.

Handles explicit user-initiated discovery, retrieval, indexing, local search,
and raw manifest staging from the public DieselTech/CBL-ReadingLists repository.

Security & Safety Principles:
- Zero background polling or automatic network calls on page load.
- Outbound network requests occur solely upon explicit user action.
- Pinned commit SHA resolution ensures immutability and provenance tracking.
- Strict path sanitization prevents directory traversal and URI injection.
- Rejects incomplete / truncated GitHub trees.
- Local catalog snapshots are stored in isolated data/cbl_catalogs/ without DB schema dependencies.
- Retained raw files are staged in data/cbl_imports/ for strict ComicVine reconciliation.
"""

import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import mylar
from mylar import logger
from mylar.extensions.storyarcs import cbl_service


REPO_OWNER = "DieselTech"
REPO_NAME = "CBL-ReadingLists"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
RAW_BASE_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
API_BASE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
USER_AGENT = "Mylar3/1.0 (Comic Management Software)"
TIMEOUT_SECONDS = 15


def get_catalog_dir():
    """Returns the isolated directory path for DieselTech catalog snapshots."""
    data_dir = getattr(mylar, 'DATA_DIR', None) or ''
    return os.path.join(data_dir, 'cbl_catalogs', 'dieseltech')


def get_snapshot_path():
    """Returns the file path of current.json catalog snapshot."""
    return os.path.join(get_catalog_dir(), 'current.json')


def validate_catalog_path(rel_path):
    """
    Strictly validates a relative repository path.
    - Must be a non-empty string.
    - Must not contain null bytes.
    - Must not contain backslashes (\\).
    - Must not contain directory traversal (..).
    - Must not contain drive qualifiers or URI schemes (:).
    - Must not start with a slash (/).
    - Must end with .cbl.
    - Path normalization must equal the original relative path.
    
    :param rel_path: Repository file path
    :return: (is_valid: bool, error_message: str)
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        return False, "Empty or non-string repository path."
    if '\x00' in rel_path:
        return False, "Path contains null byte."
    if '\\' in rel_path:
        return False, "Path contains invalid backslash separator."
    if '..' in rel_path.split('/'):
        return False, "Path contains directory traversal segment."
    if ':' in rel_path:
        return False, "Path contains invalid drive letter or URI scheme separator."
    if rel_path.startswith('/'):
        return False, "Path must not start with a leading slash."
    if not rel_path.lower().endswith('.cbl'):
        return False, "Path must end with .cbl extension."
    
    norm = os.path.normpath(rel_path).replace('\\', '/')
    if norm != rel_path:
        return False, "Path is not canonical normalized POSIX relative path."

    return True, None


def generate_entry_id(rel_path):
    """Generates an opaque, deterministic entry ID from a repository relative path."""
    h = hashlib.sha256(rel_path.encode('utf-8')).hexdigest()[:16]
    return f"cbl_dt_{h}"


def extract_entry_metadata(rel_path, size_bytes=0, git_blob_sha=None):
    """
    Extracts publisher, category, era, and title metadata from a standard repository path.
    
    :param rel_path: Relative repository path (e.g. 'Marvel/Events/Official/[Marvel] (2007-08) Secret Invasion (Official).cbl')
    :param size_bytes: File size in bytes
    :param git_blob_sha: Git object SHA
    :return: Dictionary with parsed catalog entry attributes
    """
    parts = rel_path.split('/')
    filename = parts[-1]
    publisher = parts[0] if len(parts) > 1 else 'Unknown'
    category = '/'.join(parts[1:-1]) if len(parts) > 2 else 'General'

    # Extract clean name from filename
    base_name = os.path.splitext(filename)[0]
    clean_name = base_name

    # Check for [Publisher] prefix
    if clean_name.startswith('[') and ']' in clean_name:
        clean_name = clean_name[clean_name.find(']')+1:].strip()

    # Extract year/era pattern (e.g. (2007-08) or (1989))
    era = None
    era_match = re.search(r'\((\d{4}(?:-\d{2,4})?)\)', clean_name)
    if era_match:
        era = era_match.group(1)
        clean_name = clean_name[:era_match.start()].strip() + ' ' + clean_name[era_match.end():].strip()
        clean_name = clean_name.strip()

    # Extract curation tag (e.g. (Official), (WEB-CBRO), (CMRO))
    curation = None
    curation_match = re.search(r'\(([A-Za-z0-9\-_\s]+)\)$', clean_name)
    if curation_match:
        curation = curation_match.group(1).strip()
        clean_name = clean_name[:curation_match.start()].strip()

    entry_id = generate_entry_id(rel_path)

    return {
        'id': entry_id,
        'name': base_name,
        'clean_title': clean_name,
        'publisher': publisher,
        'category': category,
        'era': era,
        'curation': curation or 'Community',
        'path': rel_path,
        'filename': filename,
        'size': size_bytes,
        'git_blob_sha': git_blob_sha
    }


def load_catalog_snapshot():
    """
    Loads the current cached catalog snapshot from disk.
    
    :return: Snapshot dictionary or None if not cached / invalid
    """
    spath = get_snapshot_path()
    if not os.path.isfile(spath):
        return None
    try:
        with open(spath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('repository') == f"{REPO_OWNER}/{REPO_NAME}" and 'entries' in data:
            return data
    except Exception as e:
        logger.error(f"[CBL_CATALOG] Failed to load catalog snapshot {spath}: {e}")
    return None


def get_catalog_status():
    """
    Returns the current local catalog snapshot status metadata.
    
    :return: Dictionary containing cached status, commit SHA, entry count, fetch timestamp
    """
    snapshot = load_catalog_snapshot()
    if not snapshot:
        return {
            'cached': False,
            'repository': REPO_URL,
            'commit_sha': None,
            'fetched_at': None,
            'total_count': 0,
            'stale': False
        }

    fetched_at = snapshot.get('fetched_at')
    stale = False
    if fetched_at:
        try:
            dt = datetime.datetime.strptime(fetched_at, '%Y-%m-%d %H:%M:%S')
            if (datetime.datetime.now() - dt).days > 30:
                stale = True
        except Exception:
            pass

    return {
        'cached': True,
        'repository': snapshot.get('repository', REPO_URL),
        'commit_sha': snapshot.get('commit_sha'),
        'commit_short': snapshot.get('commit_sha')[:7] if snapshot.get('commit_sha') else None,
        'fetched_at': fetched_at,
        'total_count': snapshot.get('total_count', len(snapshot.get('entries', []))),
        'stale': stale
    }


def refresh_dieseltech_catalog(custom_fetcher=None):
    """
    Performs an explicit, user-initiated refresh of the DieselTech CBL repository index.
    - Resolves main branch commit SHA via GitHub API.
    - Fetches the recursive Git tree for that commit.
    - Validates that the tree was not truncated by GitHub.
    - Filters and strictly validates all .cbl blob paths.
    - Atomically writes the snapshot to data/cbl_catalogs/dieseltech/current.json.
    
    :param custom_fetcher: Optional callable for testing (url, headers) -> bytes
    :return: Result dictionary with status, message, commit_sha, and entry count
    """
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/vnd.github.v3+json'
    }

    def _http_get(url):
        if custom_fetcher:
            return custom_fetcher(url, headers)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read()

    # Step 1: Resolve main branch commit SHA
    commit_url = f"{API_BASE_URL}/commits/main"
    try:
        commit_bytes = _http_get(commit_url)
        commit_data = json.loads(commit_bytes.decode('utf-8'))
        commit_sha = commit_data.get('sha')
        if not commit_sha or len(commit_sha) != 40 or not all(c in '0123456789abcdefABCDEF' for c in commit_sha):
            return {'status': 'error', 'message': f'Invalid commit SHA resolved from GitHub: {commit_sha}'}
    except urllib.error.HTTPError as e:
        logger.error(f"[CBL_CATALOG] GitHub API error resolving commit: {e}")
        if e.code == 403:
            return {'status': 'error', 'message': 'GitHub API rate limit reached. Please wait a few minutes before trying again.'}
        return {'status': 'error', 'message': f'GitHub API error resolving main branch commit (HTTP {e.code}).'}
    except Exception as e:
        logger.error(f"[CBL_CATALOG] Network error connecting to GitHub: {e}")
        return {'status': 'error', 'message': f'Network error connecting to GitHub: {e}'}

    # Step 2: Fetch recursive Git tree
    tree_url = f"{API_BASE_URL}/git/trees/{commit_sha}?recursive=1"
    try:
        tree_bytes = _http_get(tree_url)
        tree_data = json.loads(tree_bytes.decode('utf-8'))
    except urllib.error.HTTPError as e:
        logger.error(f"[CBL_CATALOG] GitHub API error fetching tree: {e}")
        if e.code == 403:
            return {'status': 'error', 'message': 'GitHub API rate limit reached during tree fetch.'}
        return {'status': 'error', 'message': f'GitHub API error fetching repository tree (HTTP {e.code}).'}
    except Exception as e:
        logger.error(f"[CBL_CATALOG] Network error fetching tree from GitHub: {e}")
        return {'status': 'error', 'message': f'Network error fetching repository tree: {e}'}

    # Step 3: Verify tree integrity
    if tree_data.get('truncated') is True:
        logger.error("[CBL_CATALOG] GitHub reported truncated tree results. Catalog refresh rejected for safety.")
        return {'status': 'error', 'message': 'GitHub reported truncated tree results. Catalog refresh rejected for safety.'}

    raw_tree = tree_data.get('tree', [])
    if not isinstance(raw_tree, list):
        return {'status': 'error', 'message': 'Malformed tree response from GitHub.'}

    # Step 4: Parse, sanitize, and extract CBL entries
    safe_entries = []
    for item in raw_tree:
        if not isinstance(item, dict):
            continue
        if item.get('type') != 'blob':
            continue
        path = item.get('path', '')
        if not path.lower().endswith('.cbl'):
            continue

        is_valid, err_msg = validate_catalog_path(path)
        if not is_valid:
            logger.warn(f"[CBL_CATALOG] Skipping unsafe or invalid catalog path '{path}': {err_msg}")
            continue

        size = item.get('size', 0)
        blob_sha = item.get('sha')
        entry = extract_entry_metadata(path, size_bytes=size, git_blob_sha=blob_sha)
        safe_entries.append(entry)

    # Sort entries by publisher and name
    safe_entries.sort(key=lambda x: (x['publisher'].lower(), x['name'].lower()))

    # Step 5: Save atomic snapshot to disk
    fetched_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    snapshot_data = {
        'repository': f"{REPO_OWNER}/{REPO_NAME}",
        'repo_url': REPO_URL,
        'commit_sha': commit_sha,
        'fetched_at': fetched_at,
        'truncated': False,
        'total_count': len(safe_entries),
        'entries': safe_entries
    }

    catalog_dir = get_catalog_dir()
    os.makedirs(catalog_dir, exist_ok=True)
    target_path = get_snapshot_path()
    tmp_path = f"{target_path}.tmp"

    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot_data, f, indent=2)
        if os.path.exists(target_path):
            os.remove(target_path)
        os.rename(tmp_path, target_path)
        logger.info(f"[CBL_CATALOG] Successfully refreshed DieselTech catalog snapshot ({len(safe_entries)} lists) at commit {commit_sha[:7]}")
    except Exception as e:
        logger.error(f"[CBL_CATALOG] Failed to write catalog snapshot: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return {'status': 'error', 'message': f'Failed to write local catalog snapshot: {e}'}

    return {
        'status': 'success',
        'message': f"Catalog refreshed successfully. Indexed {len(safe_entries)} reading lists from commit {commit_sha[:7]}.",
        'commit_sha': commit_sha,
        'commit_short': commit_sha[:7],
        'total_count': len(safe_entries),
        'fetched_at': fetched_at
    }


def search_catalog(query="", publisher="", category="", limit=200):
    """
    Performs local search and filtering against the cached DieselTech catalog snapshot.
    
    :param query: Optional free-text search string
    :param publisher: Optional publisher filter (e.g. 'Marvel', 'DC')
    :param category: Optional category filter (e.g. 'Events')
    :param limit: Maximum entries to return (default 200)
    :return: Result dictionary containing matching entries and snapshot status
    """
    snapshot = load_catalog_snapshot()
    if not snapshot:
        return {
            'status': 'not_cached',
            'message': 'No local catalog snapshot found. Please refresh catalog.',
            'entries': [],
            'total_matches': 0,
            'catalog_status': get_catalog_status()
        }

    q_clean = (query or '').strip().lower()
    pub_clean = (publisher or '').strip().lower()
    cat_clean = (category or '').strip().lower()

    entries = snapshot.get('entries', [])
    filtered = []

    for entry in entries:
        if pub_clean and entry.get('publisher', '').lower() != pub_clean:
            continue
        if cat_clean and cat_clean not in entry.get('category', '').lower():
            continue
        if q_clean:
            name = entry.get('name', '').lower()
            clean_title = entry.get('clean_title', '').lower()
            path = entry.get('path', '').lower()
            era = (entry.get('era') or '').lower()
            curation = (entry.get('curation') or '').lower()

            if (q_clean not in name and 
                q_clean not in clean_title and 
                q_clean not in path and 
                q_clean not in era and 
                q_clean not in curation):
                continue

        filtered.append(entry)

    total_matches = len(filtered)
    results = filtered[:limit]

    # Extract distinct publishers and categories for filter dropdowns
    distinct_publishers = sorted(list(set(e.get('publisher') for e in entries if e.get('publisher'))))
    distinct_categories = sorted(list(set(e.get('category') for e in entries if e.get('category'))))

    return {
        'status': 'success',
        'entries': results,
        'total_matches': total_matches,
        'returned_count': len(results),
        'publishers': distinct_publishers,
        'categories': distinct_categories,
        'catalog_status': get_catalog_status()
    }


def fetch_and_stage_catalog_cbl(entry_id, myDB=None, custom_fetcher=None, import_mode='apply_library', issuesonly=None, ignorearchived=None):
    """
    Fetches the exact raw CBL bytes for a selected catalog entry at the snapshot's pinned commit.
    - Resolves entry_id from the local snapshot.
    - Validates path security.
    - Retrieves bytes from Raw GitHub CDN.
    - Validates size (5MB ceiling) and XML structure.
    - Stages file in data/cbl_imports/cbl_<sha256>.cbl with companion provenance metadata.
    - Runs strict ComicVine ID reconciliation against the database.
    
    :param entry_id: Opaque catalog entry ID (e.g. 'cbl_dt_3d040326a914')
    :param myDB: Optional DBConnection instance
    :param custom_fetcher: Optional custom HTTP fetcher callable
    :param import_mode: 'apply_library' or 'reading_list_only'
    :param issuesonly: Overrides autowant_all when adding new volume
    :param ignorearchived: Skips marking archived issues wanted
    :return: Reconciliation preview dictionary with provenance metadata
    """
    if not entry_id or not isinstance(entry_id, str):
        return {'status': 'error', 'message': 'No valid catalog entry ID provided.'}

    snapshot = load_catalog_snapshot()
    if not snapshot:
        return {'status': 'error', 'message': 'No local catalog snapshot found. Please refresh catalog first.'}

    commit_sha = snapshot.get('commit_sha')
    if not commit_sha:
        return {'status': 'error', 'message': 'Corrupted catalog snapshot: missing commit SHA.'}

    entries = snapshot.get('entries', [])
    selected_entry = next((e for e in entries if e.get('id') == entry_id), None)
    if not selected_entry:
        return {'status': 'error', 'message': f'Catalog entry ID not found in current snapshot: {entry_id}'}

    repo_path = selected_entry['path']
    is_valid, err_msg = validate_catalog_path(repo_path)
    if not is_valid:
        return {'status': 'error', 'message': f'Invalid repository path for catalog entry: {err_msg}'}

    # Build safe Raw GitHub URL using exact commit SHA and URL-encoded path segments
    encoded_path = '/'.join(urllib.parse.quote(seg) for seg in repo_path.split('/'))
    raw_url = f"{RAW_BASE_URL}/{commit_sha}/{encoded_path}"

    headers = {'User-Agent': USER_AGENT}
    try:
        if custom_fetcher:
            raw_bytes = custom_fetcher(raw_url, headers)
        else:
            req = urllib.request.Request(raw_url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                raw_bytes = resp.read(5 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as e:
        logger.error(f"[CBL_CATALOG] Failed to fetch raw CBL from GitHub CDN ({raw_url}): {e}")
        return {'status': 'error', 'message': f'Failed to retrieve CBL file from GitHub (HTTP {e.code}).'}
    except Exception as e:
        logger.error(f"[CBL_CATALOG] Network error fetching raw CBL ({raw_url}): {e}")
        return {'status': 'error', 'message': f'Network error retrieving CBL file: {e}'}

    if not raw_bytes:
        return {'status': 'error', 'message': 'Downloaded CBL file is empty.'}
    if len(raw_bytes) > 5 * 1024 * 1024:
        return {'status': 'error', 'message': 'Downloaded CBL file exceeds 5 MB limit.'}

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    filename = selected_entry.get('filename', os.path.basename(repo_path))

    # Stage raw file in cbl_imports
    imports_dir = os.path.join(mylar.DATA_DIR, 'cbl_imports')
    try:
        os.makedirs(imports_dir, exist_ok=True)
        stored_cbl_path = os.path.join(imports_dir, f"cbl_{sha256}.cbl")
        stored_meta_path = os.path.join(imports_dir, f"cbl_{sha256}.json")

        with open(stored_cbl_path, 'wb') as f:
            f.write(raw_bytes)

        meta_info = {
            'source_type': 'dieseltech',
            'source_name': filename,
            'repo_url': REPO_URL,
            'repo_commit': commit_sha,
            'repo_path': repo_path,
            'sha256': sha256,
            'catalog_entry_id': entry_id
        }
        with open(stored_meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta_info, f, indent=2)
    except Exception as e:
        logger.error(f"[CBL_CATALOG] Failed to stage raw CBL in imports directory: {e}")
        return {'status': 'error', 'message': f'Failed to store downloaded manifest on server: {e}'}

    # Run authoritative reconciliation
    recon_result = cbl_service.parse_and_reconcile_cbl(raw_bytes, filename, myDB, import_mode=import_mode, issuesonly=issuesonly, ignorearchived=ignorearchived)
    if recon_result.get('status') != 'success':
        return recon_result

    existing = None
    if myDB:
        existing = myDB.selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [sha256]).fetchone()
    else:
        from mylar import db
        existing = db.DBConnection().selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [sha256]).fetchone()

    return {
        'status': 'success',
        'upload_token': sha256,
        'source_type': 'dieseltech',
        'repo_url': REPO_URL,
        'repo_commit': commit_sha,
        'repo_commit_short': commit_sha[:7],
        'repo_path': repo_path,
        'filename': filename,
        'filesize': len(raw_bytes),
        'sha256': sha256,
        'manifest_title': recon_result['manifest_title'],
        'publisher': recon_result['publisher'],
        'total_issues': len(recon_result['results']),
        'is_already_imported': bool(existing),
        'existing_arc_id': existing['StoryArcID'] if existing else None,
        'summary': recon_result['summary'],
        'results': recon_result['results']
    }
