"""
CBL Import, Manifest Parsing & Authoritative Reconciliation Domain Service.

Handles CBL reading list file validation, XML security (XXE prevention),
strict ComicVine ID reconciliation against local library database,
content-addressed artifact retention in data/cbl_imports,
atomic database import transactions, and reference-counted artifact deletion.
"""

import datetime
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET

import mylar
from mylar import db, helpers, logger


# Server-side registry of pre-validated staged CBL manifests
STAGED_CBL_MANIFESTS = {
    'batman_lonely_place_of_dying': {
        'token': 'batman_lonely_place_of_dying',
        'display_name': 'Batman: A Lonely Place of Dying',
        'source_name': '[DC Comics] Batman- A Lonely Place of Dying (WEB-CBRO).cbl',
        'repo_url': 'https://github.com/DieselTech/CBL-ReadingLists',
        'repo_commit': '831371357f08f6bfc13922876841df4a0068940e',
        'repo_path': 'DC/Events/CBRO/1987-1990 - Part 2/[DC Comics] Batman- A Lonely Place of Dying (WEB-CBRO).cbl',
        'expected_sha256': 'c33e762620fefe249015c10d8591e40492edbfde20b47581ddd0e14f1a2f60f6',
        'file_name': '[DC Comics] Batman- A Lonely Place of Dying (WEB-CBRO).cbl',
        'storyarc_id': 'cbl_c33e762620fe',
        'publisher': 'DC Comics',
        'expected_count': 5
    },
    'marvel_secret_invasion': {
        'token': 'marvel_secret_invasion',
        'display_name': 'Secret Invasion (Official)',
        'source_name': '[Marvel] (2007-08) Secret Invasion (Official).cbl',
        'repo_url': 'https://github.com/DieselTech/CBL-ReadingLists',
        'repo_commit': 'ba7dc4ee82f2cd0fd46d1f2048943ae4dad2d1fc',
        'repo_path': 'Marvel/Events/Official/[Marvel] (2007-08) Secret Invasion (Official).cbl',
        'expected_sha256': '81f59876e25586d5462ebb45e42b1b308129bfac9a34b9c3ce21c34092fb7a50',
        'file_name': '[Marvel] (2007-08) Secret Invasion (Official).cbl',
        'storyarc_id': 'cbl_81f59876e255',
        'publisher': 'Marvel',
        'expected_count': 98
    }
}


def get_staged_cbl_path(filename):
    """
    Finds staged CBL file in configured data directories or staging fallback locations.

    :param filename: Staged manifest filename
    :return: Absolute file path if found and readable, otherwise None
    """
    data_dir = getattr(mylar, 'DATA_DIR', None) or ''
    candidate_dirs = [
        os.path.join(data_dir, 'cbl_staging'),
        os.path.join(data_dir, 'scratch', 'cbl_staging'),
        os.path.join(os.path.dirname(data_dir), 'scratch', 'cbl_staging') if data_dir else '',
        r'C:\Users\spike\.gemini\antigravity\brain\fd578093-18e1-4b0d-8e49-152f70d60671\scratch\cbl_staging',
        r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\cbl_staging'
    ]
    for cdir in candidate_dirs:
        if cdir:
            p = os.path.join(cdir, filename)
            if os.path.isfile(p):
                return p
    return None


def sanitize_cbl_filename(filename):
    """
    Sanitizes user-provided CBL/XML filenames to avoid directory traversal.

    :param filename: Filename from user upload header
    :return: Safe basename string
    """
    if not filename:
        return 'uploaded_manifest.cbl'
    base = os.path.basename(filename)
    clean = re.sub(r'[^\w\s\-\.\(\)\[\]]', '_', base).strip()
    if not clean:
        return 'uploaded_manifest.cbl'
    return clean


def parse_and_reconcile_cbl(raw_bytes, sanitized_name, myDB=None, import_mode='apply_library', issuesonly=None, ignorearchived=None):
    """
    Parses and authoritatively reconciles a CBL/XML reading list against the database.

    Safety & Security:
    - Strictly rejects XXE (DOCTYPE / custom ENTITY).
    - Enforces 1,000 book maximum node ceiling.
    - Resolves entries strictly by ComicVine Series/Issue IDs from <Database Name="cv">.
    - Never falls back to fuzzy or heuristic title/year matching.

    Resolution States:
    - 'Downloaded': Series and Issue monitored; local file exists on disk.
    - 'Missing (Monitored)': Series and Issue monitored; local file missing.
    - 'Unmonitored Series': Series not monitored in local Mylar library.
    - 'Unknown / Unmatched Reference': Missing or unparseable ComicVine IDs.

    Predicted Actions:
    - 'No action needed — Downloaded': Issue is already Downloaded.
    - 'No action needed — Already Wanted': Issue is already Wanted.
    - 'No action needed — Snatched': Issue is already Snatched.
    - 'No action needed': Issue is in Failed state.
    - 'Mark issue Wanted': Monitored skipped issue (or archived when ignorearchived=False) to be marked Wanted.
    - 'Add series and mark issue Wanted': Unmonitored series to be added and issue marked Wanted.
    - 'Skipped because Archived': Monitored archived issue excluded by ignorearchived setting.
    - 'Reading-list entry only': Manifest entry recorded without modifying library.
    - 'Cannot resolve safely': Missing or invalid ComicVine ID or missing issue reference.

    :param raw_bytes: Raw XML / CBL bytes
    :param sanitized_name: Sanitized manifest filename
    :param myDB: Optional DBConnection instance
    :param import_mode: 'apply_library' or 'reading_list_only'
    :param issuesonly: Overrides autowant_all when adding new volume (default from CONFIG)
    :param ignorearchived: Skips marking archived issues wanted (default from CONFIG)
    :return: Dictionary containing reconciliation results, summary metrics, and metadata
    """
    if myDB is None:
        myDB = db.DBConnection()

    if issuesonly is None:
        issuesonly = getattr(mylar.CONFIG, 'CBL_IMPORT_ISSUESONLY', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True
    if ignorearchived is None:
        ignorearchived = getattr(mylar.CONFIG, 'CBL_IMPORT_IGNOREARCHIVED', False) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else False

    raw_str_lower = raw_bytes.decode('utf-8', errors='ignore').lower()
    if '<!doctype' in raw_str_lower or '<!entity' in raw_str_lower:
        return {'status': 'error', 'message': 'XML with DOCTYPE or custom ENTITY declarations is prohibited for security reasons.'}

    try:
        root = ET.fromstring(raw_bytes)
    except Exception as e:
        return {'status': 'error', 'message': f'Malformed XML document: {e}'}

    valid_roots = ('ReadingList', 'ReadingOrder', 'StoryArc', 'readingList', 'readingOrder', 'storyArc')
    if root.tag not in valid_roots and not root.tag.endswith('ReadingList'):
        return {'status': 'error', 'message': f'Invalid root XML element <{root.tag}>. Expected <ReadingList>.'}

    books = root.findall('.//Book')
    if not books:
        return {'status': 'error', 'message': 'No <Book> elements found in reading list manifest.'}

    if len(books) > 1000:
        return {'status': 'error', 'message': f'Reading list exceeds maximum limit of 1,000 books (found {len(books)}).'}

    manifest_title = root.findtext('.//Name') or root.findtext('.//Title') or root.findtext('.//StoryArc') or root.attrib.get('Name') or root.attrib.get('Title')
    if not manifest_title or manifest_title.strip() == '':
        manifest_title = os.path.splitext(sanitized_name)[0]
    manifest_title = manifest_title.strip()

    publisher = root.findtext('.//Publisher') or root.attrib.get('Publisher') or 'Comic'

    reconciled = []
    unmonitored_series_ids = set()
    issues_to_want_count = 0
    unchanged_count = 0
    archived_excluded_count = 0
    unresolved_count = 0

    for idx, book in enumerate(books, start=1):
        sname = book.get('Series', '')
        vyear = book.get('Volume', '')
        inum = book.get('Number', '')
        pyear = book.get('Year', '')
        cvelem = book.find(".//Database[@Name='cv']")
        cv_sid = cvelem.get('Series') if cvelem is not None else None
        cv_iid = cvelem.get('Issue') if cvelem is not None else None

        res_state = 'Unknown / Unmatched Reference'
        pred_action = 'Cannot resolve safely'
        mylar_sid = None
        mylar_sname = None
        mylar_iid = None
        mylar_ititle = None
        mylar_loc = None
        iss_status = None

        if cv_sid and cv_iid:
            try:
                int(cv_sid)
                int(cv_iid)
                valid_cv = True
            except (ValueError, TypeError):
                valid_cv = False

            if valid_cv:
                comic = myDB.selectone("SELECT ComicID, ComicName, ComicYear, Status FROM comics WHERE ComicID=?", [cv_sid]).fetchone()
                if not comic:
                    res_state = 'Unmonitored Series'
                    if import_mode == 'apply_library':
                        pred_action = 'Add series and mark issue Wanted'
                        unmonitored_series_ids.add(cv_sid)
                        issues_to_want_count += 1
                    else:
                        pred_action = 'Reading-list entry only'
                        unchanged_count += 1
                else:
                    mylar_sid = comic['ComicID']
                    mylar_sname = comic['ComicName']
                    iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM issues WHERE IssueID=?", [cv_iid]).fetchone()
                    if not iss:
                        iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM annuals WHERE IssueID=? AND NOT Deleted", [cv_iid]).fetchone()

                    if not iss:
                        res_state = 'Unknown / Unmatched Reference'
                        pred_action = 'Cannot resolve safely'
                        unresolved_count += 1
                    else:
                        mylar_iid = iss['IssueID']
                        mylar_ititle = iss['IssueName']
                        iss_status = iss['Status']

                        if iss['Status'] == 'Downloaded' and iss['Location'] and iss['Location'] != 'None':
                            res_state = 'Downloaded'
                            mylar_loc = iss['Location']
                        else:
                            res_state = 'Missing (Monitored)'

                        if import_mode == 'reading_list_only':
                            pred_action = 'Reading-list entry only'
                            unchanged_count += 1
                        else:
                            if iss_status == 'Downloaded':
                                pred_action = 'No action needed — Downloaded'
                                unchanged_count += 1
                            elif iss_status == 'Wanted':
                                pred_action = 'No action needed — Already Wanted'
                                unchanged_count += 1
                            elif iss_status == 'Snatched':
                                pred_action = 'No action needed — Snatched'
                                unchanged_count += 1
                            elif iss_status == 'Failed':
                                pred_action = 'No action needed'
                                unchanged_count += 1
                            elif iss_status == 'Archived':
                                if ignorearchived:
                                    pred_action = 'Skipped because Archived'
                                    archived_excluded_count += 1
                                    unchanged_count += 1
                                else:
                                    pred_action = 'Mark issue Wanted'
                                    issues_to_want_count += 1
                            else:
                                pred_action = 'Mark issue Wanted'
                                issues_to_want_count += 1
            else:
                res_state = 'Unknown / Unmatched Reference'
                pred_action = 'Cannot resolve safely'
                unresolved_count += 1
        else:
            res_state = 'Unknown / Unmatched Reference'
            pred_action = 'Cannot resolve safely'
            unresolved_count += 1

        reconciled.append({
            'order': idx,
            'series_name': sname,
            'volume_year': vyear,
            'issue_number': inum,
            'pub_year': pyear,
            'cv_series_id': cv_sid,
            'cv_issue_id': cv_iid,
            'resolution_state': res_state,
            'predicted_action': pred_action,
            'matched_status': iss_status,
            'matched_comic_id': mylar_sid,
            'matched_comic_name': mylar_sname,
            'matched_issue_id': mylar_iid,
            'matched_issue_title': mylar_ititle,
            'matched_location': mylar_loc
        })

    summary = {
        'total_entries': len(reconciled),
        'series_to_add': len(unmonitored_series_ids),
        'issues_to_want': issues_to_want_count,
        'unchanged_entries': unchanged_count,
        'archived_excluded': archived_excluded_count,
        'unresolved_entries': unresolved_count,
        'import_mode': import_mode,
        'issuesonly': issuesonly,
        'ignorearchived': ignorearchived
    }

    return {
        'status': 'success',
        'manifest_title': manifest_title,
        'publisher': publisher,
        'books': books,
        'summary': summary,
        'results': reconciled
    }


def upload_cbl_manifest(raw_bytes, orig_filename, myDB=None, import_mode='apply_library', issuesonly=None, ignorearchived=None):
    """
    Processes, stores in cbl_imports, and previews an uploaded CBL file.

    :param raw_bytes: Uploaded byte stream
    :param orig_filename: Original filename from multipart header
    :param myDB: Optional DBConnection instance
    :param import_mode: 'apply_library' or 'reading_list_only'
    :param issuesonly: Overrides autowant_all when adding new volume
    :param ignorearchived: Skips marking archived issues wanted
    :return: Dictionary containing status, upload_token, summary, and preview results
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not raw_bytes:
        return {'status': 'error', 'message': 'Uploaded file is empty.'}

    if len(raw_bytes) > 5 * 1024 * 1024:
        return {'status': 'error', 'message': 'Uploaded file exceeds the maximum allowed size of 5 MB.'}

    sanitized_name = sanitize_cbl_filename(orig_filename)
    if not (sanitized_name.lower().endswith('.cbl') or sanitized_name.lower().endswith('.xml')):
        return {'status': 'error', 'message': 'Invalid file format. Only .cbl and .xml files are supported.'}

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    imports_dir = os.path.join(mylar.DATA_DIR, 'cbl_imports')
    try:
        os.makedirs(imports_dir, exist_ok=True)
        stored_path = os.path.join(imports_dir, f"cbl_{sha256}.cbl")
        with open(stored_path, 'wb') as f:
            f.write(raw_bytes)
    except Exception as e:
        return {'status': 'error', 'message': f'Failed to store uploaded file on server: {e}'}

    recon_result = parse_and_reconcile_cbl(raw_bytes, sanitized_name, myDB, import_mode=import_mode, issuesonly=issuesonly, ignorearchived=ignorearchived)
    if recon_result.get('status') != 'success':
        return recon_result

    existing = myDB.selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [sha256]).fetchone()
    is_already_imported = bool(existing)

    return {
        'status': 'success',
        'upload_token': sha256,
        'source_type': 'upload',
        'filename': sanitized_name,
        'filesize': len(raw_bytes),
        'sha256': sha256,
        'manifest_title': recon_result['manifest_title'],
        'publisher': recon_result['publisher'],
        'total_issues': len(recon_result['results']),
        'is_already_imported': is_already_imported,
        'existing_arc_id': existing['StoryArcID'] if existing else None,
        'summary': recon_result['summary'],
        'results': recon_result['results']
    }


def preview_cbl_manifest(token, myDB=None, import_mode='apply_library', issuesonly=None, ignorearchived=None):
    """
    Reconciles and previews a reading list from a staged token or upload SHA-256 token.

    :param token: Staged token name or 64-char hex SHA-256 token
    :param myDB: Optional DBConnection instance
    :param import_mode: 'apply_library' or 'reading_list_only'
    :param issuesonly: Overrides autowant_all when adding new volume
    :param ignorearchived: Skips marking archived issues wanted
    :return: Preview reconciliation dictionary
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not token:
        return {'status': 'error', 'message': 'No manifest token provided.'}

    if token in STAGED_CBL_MANIFESTS:
        manifest = STAGED_CBL_MANIFESTS[token]
        filepath = get_staged_cbl_path(manifest['file_name'])
        if not filepath or not os.path.isfile(filepath):
            return {'status': 'error', 'message': 'Staged CBL file not found on server'}

        with open(filepath, 'rb') as f:
            content = f.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if file_sha256.lower() != manifest['expected_sha256'].lower():
            return {'status': 'error', 'message': 'CBL file integrity check failed (SHA-256 mismatch)'}

        recon_result = parse_and_reconcile_cbl(content, manifest['source_name'], myDB, import_mode=import_mode, issuesonly=issuesonly, ignorearchived=ignorearchived)
        if recon_result.get('status') != 'success':
            return recon_result

        existing = myDB.selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [file_sha256]).fetchone()
        return {
            'status': 'success',
            'token': token,
            'source_type': 'staged',
            'manifest': manifest,
            'is_already_imported': bool(existing),
            'existing_arc_id': existing['StoryArcID'] if existing else None,
            'total_issues': len(recon_result['results']),
            'summary': recon_result['summary'],
            'results': recon_result['results']
        }

    elif len(token) == 64 and all(c in '0123456789abcdefABCDEF' for c in token):
        filepath = os.path.join(mylar.DATA_DIR, 'cbl_imports', f"cbl_{token}.cbl")
        if not os.path.isfile(filepath):
            return {'status': 'error', 'message': 'Uploaded CBL file not found on server. Please upload again.'}

        with open(filepath, 'rb') as f:
            content = f.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if file_sha256.lower() != token.lower():
            return {'status': 'error', 'message': 'Stored file integrity check failed.'}

        recon_result = parse_and_reconcile_cbl(content, f"cbl_{token[:12]}.cbl", myDB, import_mode=import_mode, issuesonly=issuesonly, ignorearchived=ignorearchived)
        if recon_result.get('status') != 'success':
            return recon_result

        existing = myDB.selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [file_sha256]).fetchone()
        return {
            'status': 'success',
            'token': token,
            'source_type': 'upload',
            'manifest_title': recon_result['manifest_title'],
            'publisher': recon_result['publisher'],
            'sha256': file_sha256,
            'is_already_imported': bool(existing),
            'existing_arc_id': existing['StoryArcID'] if existing else None,
            'total_issues': len(recon_result['results']),
            'summary': recon_result['summary'],
            'results': recon_result['results']
        }
    else:
        return {'status': 'error', 'message': 'Invalid manifest token.'}


def _apply_library_mutations(volume_index, monitored_want_ids, issuesonly=True, ignorearchived=False, myDB=None):
    """
    Applies Carbon-equivalent library mutations:
    - Queues unmonitored series via importer.importer_thread with suppress_addall=(issuesonly and AUTOWANT_ALL)
    - Queues new volume issue IDs to importer.issue_watcher_thread
    - Updates monitored skipped/archived issues to 'Wanted' in the database and queues to issue_watcher_thread
    - Saves user CBL import options to configuration
    """
    if myDB is None:
        myDB = db.DBConnection()

    # 1. Update monitored issues in DB directly so state is immediately truthful
    if monitored_want_ids:
        for iid in monitored_want_ids:
            iss_row = myDB.selectone("SELECT IssueID, Status FROM issues WHERE IssueID=?", [iid]).fetchone()
            if iss_row:
                curr_status = iss_row['Status']
                if curr_status in ('Downloaded', 'Wanted', 'Snatched', 'Failed'):
                    logger.fdebug(f"[CBL_SERVICE] Issue {iid} is already {curr_status}; skipping mutation.")
                    continue
                if curr_status == 'Archived' and ignorearchived:
                    logger.fdebug(f"[CBL_SERVICE] Issue {iid} is Archived and ignorearchived=True; skipping mutation.")
                    continue
                myDB.action("UPDATE issues SET Status='Wanted' WHERE IssueID=?", [iid])
            else:
                ann_row = myDB.selectone("SELECT IssueID, Status FROM annuals WHERE IssueID=? AND NOT Deleted", [iid]).fetchone()
                if ann_row:
                    curr_status = ann_row['Status']
                    if curr_status in ('Downloaded', 'Wanted', 'Snatched', 'Failed'):
                        logger.fdebug(f"[CBL_SERVICE] Annual {iid} is already {curr_status}; skipping mutation.")
                        continue
                    if curr_status == 'Archived' and ignorearchived:
                        logger.fdebug(f"[CBL_SERVICE] Annual {iid} is Archived and ignorearchived=True; skipping mutation.")
                        continue
                    myDB.action("UPDATE annuals SET Status='Wanted' WHERE IssueID=?", [iid])

        try:
            from mylar import importer
            importer.issue_watcher_thread(list(monitored_want_ids))
        except Exception as e:
            logger.warn(f"[CBL_SERVICE] Could not queue monitored issues to issue_watcher_thread: {e}")

    # 2. Queue unmonitored volumes
    if volume_index:
        try:
            from mylar import importer
            autowant_all = getattr(mylar.CONFIG, 'AUTOWANT_ALL', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True
            for comic_id, vdata in volume_index.items():
                comic_request = [{
                    "comicid": comic_id,
                    "comicname": vdata.get('VolumeName'),
                    "seriesyear": vdata.get('VolumeYear'),
                    "suppress_addall": bool(issuesonly and autowant_all)
                }]
                importer.importer_thread(comic_request)
                if vdata.get('IssueIDs'):
                    importer.issue_watcher_thread(vdata['IssueIDs'])
            importer.importer_thread([])  # Nudge mass-add thread
        except Exception as e:
            logger.warn(f"[CBL_SERVICE] Could not queue new volumes to importer_thread: {e}")

    # 3. Update saved configuration values
    if hasattr(mylar, 'CONFIG') and mylar.CONFIG:
        try:
            mylar.CONFIG.CBL_IMPORT_ISSUESONLY = issuesonly
            mylar.CONFIG.CBL_IMPORT_IGNOREARCHIVED = ignorearchived
            mylar.CONFIG.writeconfig(values={
                'CBL_IMPORT_ISSUESONLY': issuesonly,
                'CBL_IMPORT_IGNOREARCHIVED': ignorearchived
            })
        except Exception:
            pass


def confirm_cbl_import(token, filename=None, myDB=None, import_mode='apply_library', issuesonly=None, ignorearchived=None):
    """
    Performs an atomic, verified import of a CBL manifest into the database.
    - Validates file integrity before database write.
    - Persists manifest metadata in storyarc_manifests.
    - Inserts reading order rows in storyarcs.
    - Atomically rolls back both tables if any insertion fails.
    - When import_mode == 'apply_library', applies verified Carbon CBL library mutations.
    - When import_mode == 'reading_list_only', performs zero library mutations.

    :param token: Staged token or upload SHA-256 token
    :param filename: Optional display filename
    :param myDB: Optional DBConnection instance
    :param import_mode: 'apply_library' or 'reading_list_only'
    :param issuesonly: Overrides autowant_all when adding new volume
    :param ignorearchived: Skips marking archived issues wanted
    :return: Result dictionary with status, storyarcid, summary, and message
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not token:
        return {'status': 'error', 'message': 'No manifest token provided.'}

    if issuesonly is None:
        issuesonly = getattr(mylar.CONFIG, 'CBL_IMPORT_ISSUESONLY', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True
    if ignorearchived is None:
        ignorearchived = getattr(mylar.CONFIG, 'CBL_IMPORT_IGNOREARCHIVED', False) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else False

    import_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if token in STAGED_CBL_MANIFESTS:
        manifest = STAGED_CBL_MANIFESTS[token]
        filepath = get_staged_cbl_path(manifest['file_name'])
        if not filepath or not os.path.isfile(filepath):
            return {'status': 'error', 'message': 'Staged CBL file not found on server.'}

        with open(filepath, 'rb') as f:
            content = f.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if file_sha256.lower() != manifest['expected_sha256'].lower():
            return {'status': 'error', 'message': 'CBL file integrity check failed (SHA-256 mismatch).'}

        recon = parse_and_reconcile_cbl(content, manifest['source_name'], myDB, import_mode=import_mode, issuesonly=issuesonly, ignorearchived=ignorearchived)
        if recon.get('status') != 'success':
            return recon

        existing = myDB.selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [file_sha256]).fetchone()
        if existing:
            return {
                'status': 'already_imported',
                'message': 'This reading list has already been imported.',
                'storyarcid': existing['StoryArcID'],
                'storyarcname': existing['StoryArcName']
            }

        storyarc_id = manifest['storyarc_id']
        display_name = manifest['display_name']
        publisher = manifest.get('publisher', 'DC Comics')
        source_type = 'staged'
        source_name = manifest['source_name']
        repo_url = manifest['repo_url']
        repo_commit = manifest['repo_commit']
        repo_path = manifest['repo_path']
        raw_path = filepath
        books = recon['books']
        reconciled_items = recon['results']

    elif len(token) == 64 and all(c in '0123456789abcdefABCDEF' for c in token):
        filepath = os.path.join(mylar.DATA_DIR, 'cbl_imports', f"cbl_{token}.cbl")
        if not os.path.isfile(filepath):
            return {'status': 'error', 'message': 'Uploaded file not found on server. Please upload again.'}

        with open(filepath, 'rb') as f:
            content = f.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if file_sha256.lower() != token.lower():
            return {'status': 'error', 'message': 'Integrity check failed: file hash mismatch.'}

        recon = parse_and_reconcile_cbl(content, f"cbl_{token[:12]}.cbl", myDB, import_mode=import_mode, issuesonly=issuesonly, ignorearchived=ignorearchived)
        if recon.get('status') != 'success':
            return recon

        existing = myDB.selectone("SELECT * FROM storyarc_manifests WHERE SHA256=?", [file_sha256]).fetchone()
        if existing:
            return {
                'status': 'already_imported',
                'message': 'This reading list has already been imported.',
                'storyarcid': existing['StoryArcID'],
                'storyarcname': existing['StoryArcName']
            }

        storyarc_id = f"cbl_{token[:12]}"
        display_name = recon['manifest_title']
        publisher = recon['publisher']
        source_type = 'upload'
        source_name = filename or f"{display_name}.cbl"
        repo_url = None
        repo_commit = None
        repo_path = None
        raw_path = filepath
        books = recon['books']
        reconciled_items = recon['results']

        meta_path = os.path.join(mylar.DATA_DIR, 'cbl_imports', f"cbl_{token}.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta_info = json.load(f)
                source_type = meta_info.get('source_type', source_type)
                source_name = meta_info.get('source_name', source_name)
                repo_url = meta_info.get('repo_url', repo_url)
                repo_commit = meta_info.get('repo_commit', repo_commit)
                repo_path = meta_info.get('repo_path', repo_path)
            except Exception as ex:
                logger.warn(f"[CBL_IMPORT] Failed to read companion metadata {meta_path}: {ex}")
    else:
        return {'status': 'error', 'message': 'Invalid or unapproved manifest token.'}

    # Collect library mutations before committing DB transaction
    volume_index = {}
    monitored_want_ids = []

    if import_mode == 'apply_library':
        autowant_all = getattr(mylar.CONFIG, 'AUTOWANT_ALL', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True
        for item in reconciled_items:
            cv_sid = item.get('cv_series_id')
            cv_iid = item.get('cv_issue_id')
            act = item.get('predicted_action')
            sname = item.get('series_name')
            vyear = item.get('volume_year')

            if act == 'Add series and mark issue Wanted' and cv_sid:
                if cv_sid not in volume_index:
                    issue_list = []
                    if (issuesonly and autowant_all) or not autowant_all:
                        if cv_iid:
                            issue_list.append(cv_iid)
                    volume_index[cv_sid] = {
                        'NewVol': True,
                        'VolumeName': sname,
                        'VolumeYear': vyear,
                        'IssueIDs': issue_list
                    }
                else:
                    if (issuesonly and autowant_all) or not autowant_all:
                        if cv_iid and cv_iid not in volume_index[cv_sid]['IssueIDs']:
                            volume_index[cv_sid]['IssueIDs'].append(cv_iid)
            elif act == 'Mark issue Wanted' and cv_iid:
                monitored_want_ids.append(cv_iid)

    try:
        existing_id = myDB.selectone("SELECT StoryArcID FROM storyarc_manifests WHERE StoryArcID=?", [storyarc_id]).fetchone()
        if existing_id:
            storyarc_id = f"cbl_{file_sha256[:16]}"

        myDB.action("INSERT INTO storyarc_manifests (StoryArcID, StoryArcName, SourceName, SourceType, RepoURL, RepoCommit, RepoPath, SHA256, ImportTime, TotalIssues, RawXMLPath) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [storyarc_id, display_name, source_name, source_type, repo_url, repo_commit, repo_path, file_sha256, import_time, len(books), raw_path])

        for idx, book in enumerate(books, start=1):
            sname = book.get('Series', '')
            vyear = book.get('Volume', '')
            inum = book.get('Number', '')
            pyear = book.get('Year', '')
            cvelem = book.find(".//Database[@Name='cv']")
            cv_sid = cvelem.get('Series') if cvelem is not None else None
            cv_iid = cvelem.get('Issue') if cvelem is not None else None

            iss_name = ''
            if cv_iid:
                iss_row = myDB.selectone("SELECT IssueName FROM issues WHERE IssueID=?", [cv_iid]).fetchone()
                if not iss_row:
                    iss_row = myDB.selectone("SELECT IssueName FROM annuals WHERE IssueID=? AND NOT Deleted", [cv_iid]).fetchone()
                if iss_row and iss_row['IssueName']:
                    iss_name = iss_row['IssueName']

            issue_arc_id = f"{storyarc_id}_{idx}"
            myDB.action("INSERT INTO storyarcs (StoryArcID, ComicName, IssueNumber, SeriesYear, IssueYEAR, StoryArc, TotalIssues, Status, IssueArcID, ReadingOrder, IssueID, ComicID, IssueName, Publisher, DateAdded, Type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [storyarc_id, sname, inum, vyear, pyear, display_name, str(len(books)), 'Imported', issue_arc_id, idx, cv_iid, cv_sid, iss_name, publisher, import_time, 'cbl'])

        # Apply library mutations if apply_library mode selected
        if import_mode == 'apply_library':
            _apply_library_mutations(volume_index, monitored_want_ids, issuesonly=issuesonly, ignorearchived=ignorearchived, myDB=myDB)

        logger.info(f"[CBL_IMPORT] Successfully imported '{display_name}' ({len(books)} issues) with SHA-256 {file_sha256} [Mode: {import_mode}]")
        return {
            'status': 'success',
            'message': f"Successfully imported '{display_name}' ({len(books)} issues)",
            'storyarcid': storyarc_id,
            'storyarcname': display_name,
            'summary': recon['summary']
        }
    except Exception as e:
        logger.error(f"[CBL_IMPORT] Error during atomic import transaction: {e}")
        try:
            myDB.action("DELETE FROM storyarcs WHERE StoryArcID=?", [storyarc_id])
            myDB.action("DELETE FROM storyarc_manifests WHERE StoryArcID=?", [storyarc_id])
        except Exception:
            pass
        return {'status': 'error', 'message': f'Failed to import Story Arc: {e}'}


def reconcile_existing_storyarc(storyarc_id, myDB=None, import_mode='apply_library', issuesonly=None, ignorearchived=None, apply_changes=False):
    """
    Reconciles an already-imported Story Arc against the local library.
    Allows previewing predictions or applying Carbon-equivalent library mutations.

    :param storyarc_id: Story Arc ID string
    :param myDB: Optional DBConnection instance
    :param import_mode: 'apply_library' or 'reading_list_only'
    :param issuesonly: Overrides autowant_all when adding new volume
    :param ignorearchived: Skips marking archived issues wanted
    :param apply_changes: If True, executes library mutations; if False, returns preview
    :return: Dictionary containing status, summary, and per-entry reconciliation
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not storyarc_id:
        return {'status': 'error', 'message': 'No StoryArcID provided.'}

    if issuesonly is None:
        issuesonly = getattr(mylar.CONFIG, 'CBL_IMPORT_ISSUESONLY', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True
    if ignorearchived is None:
        ignorearchived = getattr(mylar.CONFIG, 'CBL_IMPORT_IGNOREARCHIVED', False) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else False

    arc_rows = myDB.select("SELECT * FROM storyarcs WHERE StoryArcID=? AND NOT Manual IS 'deleted' ORDER BY ReadingOrder ASC", [storyarc_id])
    if not arc_rows:
        return {'status': 'error', 'message': f'Story Arc not found: {storyarc_id}'}

    manifest = myDB.selectone("SELECT * FROM storyarc_manifests WHERE StoryArcID=?", [storyarc_id]).fetchone()
    arc_name = manifest['StoryArcName'] if manifest else (arc_rows[0]['StoryArc'] if arc_rows else storyarc_id)

    reconciled = []
    volume_index = {}
    monitored_want_ids = []
    unmonitored_series_ids = set()
    issues_to_want_count = 0
    unchanged_count = 0
    archived_excluded_count = 0
    unresolved_count = 0

    autowant_all = getattr(mylar.CONFIG, 'AUTOWANT_ALL', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True

    for row in arc_rows:
        cv_sid = row['ComicID']
        cv_iid = row['IssueID']
        sname = row['ComicName'] or ''
        vyear = row['SeriesYear'] or ''
        inum = row['IssueNumber'] or ''
        order = row['ReadingOrder']
        issue_arc_id = row['IssueArcID'] or f"{storyarc_id}_{order}"

        res_state = 'Unknown / Unmatched Reference'
        pred_action = 'Cannot resolve safely'
        mylar_sid = None
        mylar_sname = None
        mylar_iid = None
        mylar_ititle = None
        mylar_loc = None
        iss_status = None

        if cv_sid and cv_iid:
            try:
                int(cv_sid)
                int(cv_iid)
                valid_cv = True
            except (ValueError, TypeError):
                valid_cv = False

            if valid_cv:
                comic = myDB.selectone("SELECT ComicID, ComicName, ComicYear, Status FROM comics WHERE ComicID=?", [cv_sid]).fetchone()
                if not comic:
                    res_state = 'Unmonitored Series'
                    if import_mode == 'apply_library':
                        pred_action = 'Add series and mark issue Wanted'
                        unmonitored_series_ids.add(cv_sid)
                        issues_to_want_count += 1
                        if cv_sid not in volume_index:
                            issue_list = []
                            if (issuesonly and autowant_all) or not autowant_all:
                                issue_list.append(cv_iid)
                            volume_index[cv_sid] = {
                                'NewVol': True,
                                'VolumeName': sname,
                                'VolumeYear': vyear,
                                'IssueIDs': issue_list
                            }
                        else:
                            if (issuesonly and autowant_all) or not autowant_all:
                                if cv_iid not in volume_index[cv_sid]['IssueIDs']:
                                    volume_index[cv_sid]['IssueIDs'].append(cv_iid)
                    else:
                        pred_action = 'Reading-list entry only'
                        unchanged_count += 1
                else:
                    mylar_sid = comic['ComicID']
                    mylar_sname = comic['ComicName']
                    iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM issues WHERE IssueID=?", [cv_iid]).fetchone()
                    if not iss:
                        iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM annuals WHERE IssueID=? AND NOT Deleted", [cv_iid]).fetchone()

                    if not iss:
                        res_state = 'Unknown / Unmatched Reference'
                        pred_action = 'Cannot resolve safely'
                        unresolved_count += 1
                    else:
                        mylar_iid = iss['IssueID']
                        mylar_ititle = iss['IssueName']
                        iss_status = iss['Status']

                        if iss['Status'] == 'Downloaded' and iss['Location'] and iss['Location'] != 'None':
                            res_state = 'Downloaded'
                            mylar_loc = iss['Location']
                        else:
                            res_state = 'Missing (Monitored)'

                        if import_mode == 'reading_list_only':
                            pred_action = 'Reading-list entry only'
                            unchanged_count += 1
                        else:
                            if iss_status == 'Downloaded':
                                pred_action = 'No action needed — Downloaded'
                                unchanged_count += 1
                            elif iss_status == 'Wanted':
                                pred_action = 'No action needed — Already Wanted'
                                unchanged_count += 1
                            elif iss_status == 'Snatched':
                                pred_action = 'No action needed — Snatched'
                                unchanged_count += 1
                            elif iss_status == 'Failed':
                                pred_action = 'No action needed'
                                unchanged_count += 1
                            elif iss_status == 'Archived':
                                if ignorearchived:
                                    pred_action = 'Skipped because Archived'
                                    archived_excluded_count += 1
                                    unchanged_count += 1
                                else:
                                    pred_action = 'Mark issue Wanted'
                                    issues_to_want_count += 1
                                    monitored_want_ids.append(cv_iid)
                            else:
                                pred_action = 'Mark issue Wanted'
                                issues_to_want_count += 1
                                monitored_want_ids.append(cv_iid)
            else:
                res_state = 'Unknown / Unmatched Reference'
                pred_action = 'Cannot resolve safely'
                unresolved_count += 1
        else:
            res_state = 'Unknown / Unmatched Reference'
            pred_action = 'Cannot resolve safely'
            unresolved_count += 1

        reconciled.append({
            'order': order,
            'issue_arc_id': issue_arc_id,
            'series_name': sname,
            'volume_year': vyear,
            'issue_number': inum,
            'cv_series_id': cv_sid,
            'cv_issue_id': cv_iid,
            'resolution_state': res_state,
            'predicted_action': pred_action,
            'matched_status': iss_status,
            'matched_comic_id': mylar_sid,
            'matched_comic_name': mylar_sname,
            'matched_issue_id': mylar_iid,
            'matched_issue_title': mylar_ititle,
            'matched_location': mylar_loc
        })

    summary = {
        'total_entries': len(reconciled),
        'series_to_add': len(unmonitored_series_ids),
        'issues_to_want': issues_to_want_count,
        'unchanged_entries': unchanged_count,
        'archived_excluded': archived_excluded_count,
        'unresolved_entries': unresolved_count,
        'import_mode': import_mode,
        'issuesonly': issuesonly,
        'ignorearchived': ignorearchived
    }

    if apply_changes:
        # Also backfill issue title in storyarcs if newly available
        for item in reconciled:
            if item.get('matched_issue_title') and item.get('cv_issue_id'):
                try:
                    myDB.action("UPDATE storyarcs SET IssueName=? WHERE StoryArcID=? AND IssueID=?",
                                [item['matched_issue_title'], storyarc_id, item['cv_issue_id']])
                except Exception:
                    pass

        if import_mode == 'apply_library':
            _apply_library_mutations(volume_index, monitored_want_ids, issuesonly=issuesonly, ignorearchived=ignorearchived, myDB=myDB)

        return {
            'status': 'success',
            'message': f"Story Arc '{arc_name}' successfully reconciled with library.",
            'storyarcid': storyarc_id,
            'storyarcname': arc_name,
            'summary': summary,
            'results': reconciled
        }
    else:
        return {
            'status': 'success',
            'storyarcid': storyarc_id,
            'storyarcname': arc_name,
            'summary': summary,
            'results': reconciled
        }


def execute_entry_action(storyarc_id, issue_arc_id, action, issuesonly=None, ignorearchived=None, myDB=None):
    """
    Executes a granular per-entry action on a Story Arc entry.

    Supported Actions:
    - 'add_series': Queues unmonitored series for addition and its issue for watch.
    - 'mark_wanted': Marks a monitored skipped/archived issue as Wanted.
    - 'retry_resolution': Re-evaluates database resolution for the entry.

    :param storyarc_id: Story Arc ID string
    :param issue_arc_id: Issue Arc ID string or IssueID
    :param action: Action name string
    :param issuesonly: Overrides autowant_all
    :param ignorearchived: Skips archived issues
    :param myDB: Optional DBConnection instance
    :return: Action result dictionary
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not storyarc_id or not issue_arc_id or not action:
        return {'status': 'error', 'message': 'Missing required parameters.'}

    if issuesonly is None:
        issuesonly = getattr(mylar.CONFIG, 'CBL_IMPORT_ISSUESONLY', True) if hasattr(mylar, 'CONFIG') and mylar.CONFIG else True

    row = myDB.selectone("SELECT * FROM storyarcs WHERE StoryArcID=? AND (IssueArcID=? OR IssueID=?)",
                         [storyarc_id, issue_arc_id, issue_arc_id]).fetchone()
    if not row:
        return {'status': 'error', 'message': 'Story Arc entry not found.'}

    cv_sid = row['ComicID']
    cv_iid = row['IssueID']
    sname = row['ComicName'] or 'Series'
    vyear = row['SeriesYear'] or ''
    inum = row['IssueNumber'] or ''

    if action == 'add_series':
        if not cv_sid:
            return {'status': 'error', 'message': 'Missing ComicVine Series ID for entry.'}

        comic = myDB.selectone("SELECT ComicID, ComicName FROM comics WHERE ComicID=?", [cv_sid]).fetchone()
        if comic:
            return {'status': 'info', 'message': f"Series '{sname}' is already monitored."}

        volume_index = {
            cv_sid: {
                'NewVol': True,
                'VolumeName': sname,
                'VolumeYear': vyear,
                'IssueIDs': [cv_iid] if cv_iid else []
            }
        }
        _apply_library_mutations(volume_index, [], issuesonly=issuesonly, myDB=myDB)
        return {
            'status': 'success',
            'message': f"Queued series '{sname} ({vyear})' for addition and Issue #{inum} for download."
        }

    elif action == 'mark_wanted':
        if not cv_iid:
            return {'status': 'error', 'message': 'Missing ComicVine Issue ID for entry.'}

        iss = myDB.selectone("SELECT IssueID, IssueName, Status FROM issues WHERE IssueID=?", [cv_iid]).fetchone()
        is_annual = False
        if not iss:
            iss = myDB.selectone("SELECT IssueID, IssueName, Status FROM annuals WHERE IssueID=? AND NOT Deleted", [cv_iid]).fetchone()
            is_annual = True

        if not iss:
            return {'status': 'error', 'message': f"Issue #{inum} is not monitored in library yet. Add series first."}

        if iss['Status'] in ('Downloaded', 'Wanted', 'Snatched'):
            return {'status': 'info', 'message': f"Issue #{inum} is already in state '{iss['Status']}'."}

        if is_annual:
            myDB.action("UPDATE annuals SET Status='Wanted' WHERE IssueID=?", [cv_iid])
        else:
            myDB.action("UPDATE issues SET Status='Wanted' WHERE IssueID=?", [cv_iid])

        try:
            from mylar import importer
            importer.issue_watcher_thread([cv_iid])
        except Exception:
            pass

        return {
            'status': 'success',
            'message': f"Marked {sname} #{inum} as Wanted."
        }

    elif action == 'retry_resolution':
        if not cv_sid or not cv_iid:
            return {'status': 'error', 'message': 'Entry lacks required ComicVine identifiers.'}

        comic = myDB.selectone("SELECT ComicID, ComicName FROM comics WHERE ComicID=?", [cv_sid]).fetchone()
        if not comic:
            return {
                'status': 'success',
                'resolution_state': 'Unmonitored Series',
                'message': f"Series '{sname}' is unmonitored."
            }

        iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM issues WHERE IssueID=?", [cv_iid]).fetchone()
        if not iss:
            iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM annuals WHERE IssueID=? AND NOT Deleted", [cv_iid]).fetchone()

        if not iss:
            return {
                'status': 'success',
                'resolution_state': 'Unknown / Unmatched Reference',
                'message': f"Series monitored, but issue {cv_iid} not yet indexed."
            }

        res_state = 'Downloaded' if (iss['Status'] == 'Downloaded' and iss['Location'] and iss['Location'] != 'None') else 'Missing (Monitored)'
        if iss['IssueName']:
            try:
                myDB.action("UPDATE storyarcs SET IssueName=? WHERE StoryArcID=? AND IssueID=?", [iss['IssueName'], storyarc_id, cv_iid])
            except Exception:
                pass

        return {
            'status': 'success',
            'resolution_state': res_state,
            'message': f"Resolved as '{res_state}' (Issue status: {iss['Status']})."
        }

    else:
        return {'status': 'error', 'message': f'Unknown entry action: {action}'}


def delete_cbl_arc(storyarcid, myDB=None):
    """
    Safely removes a Story Arc and prunes unreferenced raw CBL files.
    - Removes manifest and reading order records from database.
    - Only deletes the stored raw .cbl file if no remaining manifest references the same SHA-256.

    :param storyarcid: Story Arc ID string
    :param myDB: Optional DBConnection instance
    :return: Dictionary with deletion status
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not storyarcid:
        return {'status': 'error', 'message': 'No StoryArcID provided.'}

    manifest = myDB.selectone("SELECT * FROM storyarc_manifests WHERE StoryArcID=?", [storyarcid]).fetchone()
    arc_rows = myDB.select("SELECT * FROM storyarcs WHERE StoryArcID=?", [storyarcid])

    if not manifest and not arc_rows:
        return {'status': 'error', 'message': 'Story Arc not found.'}

    arc_name = manifest['StoryArcName'] if manifest else (arc_rows[0]['StoryArc'] if arc_rows else storyarcid)
    file_sha256 = manifest['SHA256'] if manifest else None
    raw_path = manifest['RawXMLPath'] if (manifest and 'RawXMLPath' in manifest.keys()) else None

    try:
        myDB.action("DELETE FROM storyarcs WHERE StoryArcID=?", [storyarcid])
        myDB.action("DELETE FROM storyarc_manifests WHERE StoryArcID=?", [storyarcid])

        if file_sha256:
            other_ref = myDB.selectone("SELECT count(*) FROM storyarc_manifests WHERE SHA256=?", [file_sha256]).fetchone()
            if other_ref and other_ref[0] == 0:
                if raw_path and os.path.isfile(raw_path):
                    try:
                        os.remove(raw_path)
                        logger.info(f"[CBL_DELETE] Cleaned up raw artifact file: {raw_path}")
                    except Exception as ex:
                        logger.warn(f"[CBL_DELETE] Could not delete raw file {raw_path}: {ex}")

                meta_json = os.path.join(mylar.DATA_DIR, 'cbl_imports', f"cbl_{file_sha256}.json")
                if os.path.isfile(meta_json):
                    try:
                        os.remove(meta_json)
                    except Exception:
                        pass

        logger.info(f"[CBL_DELETE] Successfully removed Story Arc '{arc_name}' (ID: {storyarcid})")
        return {
            'status': 'success',
            'message': f"Successfully deleted Story Arc '{arc_name}'.",
            'storyarcid': storyarcid
        }
    except Exception as e:
        logger.error(f"[CBL_DELETE] Failed to delete Story Arc {storyarcid}: {e}")
        return {'status': 'error', 'message': f'Failed to delete Story Arc: {e}'}
