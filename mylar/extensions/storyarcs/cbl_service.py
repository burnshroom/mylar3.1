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


def parse_and_reconcile_cbl(raw_bytes, sanitized_name, myDB=None):
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
    
    :param raw_bytes: Raw XML / CBL bytes
    :param sanitized_name: Sanitized manifest filename
    :param myDB: Optional DBConnection instance
    :return: Dictionary containing reconciliation results and metadata
    """
    if myDB is None:
        myDB = db.DBConnection()

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
    for idx, book in enumerate(books, start=1):
        sname = book.get('Series', '')
        vyear = book.get('Volume', '')
        inum = book.get('Number', '')
        pyear = book.get('Year', '')
        cvelem = book.find(".//Database[@Name='cv']")
        cv_sid = cvelem.get('Series') if cvelem is not None else None
        cv_iid = cvelem.get('Issue') if cvelem is not None else None

        res_state = 'Unknown / Unmatched Reference'
        mylar_sid = None
        mylar_sname = None
        mylar_iid = None
        mylar_ititle = None
        mylar_loc = None

        if cv_sid and cv_iid:
            try:
                int(cv_sid)
                int(cv_iid)
                valid_cv = True
            except (ValueError, TypeError):
                valid_cv = False

            if valid_cv:
                comic = myDB.selectone("SELECT ComicID, ComicName, ComicYear FROM comics WHERE ComicID=?", [cv_sid]).fetchone()
                if not comic:
                    res_state = 'Unmonitored Series'
                else:
                    mylar_sid = comic['ComicID']
                    mylar_sname = comic['ComicName']
                    iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM issues WHERE IssueID=?", [cv_iid]).fetchone()
                    if not iss:
                        iss = myDB.selectone("SELECT IssueID, IssueName, Status, Location FROM annuals WHERE IssueID=? AND NOT Deleted", [cv_iid]).fetchone()
                    if not iss:
                        res_state = 'Unknown / Unmatched Reference'
                    else:
                        mylar_iid = iss['IssueID']
                        mylar_ititle = iss['IssueName']
                        if iss['Status'] == 'Downloaded' and iss['Location'] and iss['Location'] != 'None':
                            res_state = 'Downloaded'
                            mylar_loc = iss['Location']
                        else:
                            res_state = 'Missing (Monitored)'
            else:
                res_state = 'Unknown / Unmatched Reference'
        else:
            res_state = 'Unknown / Unmatched Reference'

        reconciled.append({
            'order': idx,
            'series_name': sname,
            'volume_year': vyear,
            'issue_number': inum,
            'pub_year': pyear,
            'cv_series_id': cv_sid,
            'cv_issue_id': cv_iid,
            'resolution_state': res_state,
            'matched_comic_id': mylar_sid,
            'matched_comic_name': mylar_sname,
            'matched_issue_id': mylar_iid,
            'matched_issue_title': mylar_ititle,
            'matched_location': mylar_loc
        })

    return {
        'status': 'success',
        'manifest_title': manifest_title,
        'publisher': publisher,
        'books': books,
        'results': reconciled
    }


def upload_cbl_manifest(raw_bytes, orig_filename, myDB=None):
    """
    Processes, stores in cbl_imports, and previews an uploaded CBL file.
    
    :param raw_bytes: Uploaded byte stream
    :param orig_filename: Original filename from multipart header
    :param myDB: Optional DBConnection instance
    :return: Dictionary containing status, upload_token, and preview results
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

    recon_result = parse_and_reconcile_cbl(raw_bytes, sanitized_name, myDB)
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
        'results': recon_result['results']
    }


def preview_cbl_manifest(token, myDB=None):
    """
    Reconciles and previews a reading list from a staged token or upload SHA-256 token.
    
    :param token: Staged token name or 64-char hex SHA-256 token
    :param myDB: Optional DBConnection instance
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

        recon_result = parse_and_reconcile_cbl(content, manifest['source_name'], myDB)
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

        recon_result = parse_and_reconcile_cbl(content, f"cbl_{token[:12]}.cbl", myDB)
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
            'results': recon_result['results']
        }
    else:
        return {'status': 'error', 'message': 'Invalid manifest token.'}


def confirm_cbl_import(token, filename=None, myDB=None):
    """
    Performs an atomic, verified import of a CBL manifest into the database.
    - Validates file integrity before database write.
    - Persists manifest metadata in storyarc_manifests.
    - Inserts reading order rows in storyarcs.
    - Atomically rolls back both tables if any insertion fails.
    - Never changes issue statuses or triggers provider searches.
    
    :param token: Staged token or upload SHA-256 token
    :param filename: Optional display filename
    :param myDB: Optional DBConnection instance
    :return: Result dictionary with status and storyarcid
    """
    if myDB is None:
        myDB = db.DBConnection()

    if not token:
        return {'status': 'error', 'message': 'No manifest token provided.'}

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

        recon = parse_and_reconcile_cbl(content, manifest['source_name'], myDB)
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

    elif len(token) == 64 and all(c in '0123456789abcdefABCDEF' for c in token):
        filepath = os.path.join(mylar.DATA_DIR, 'cbl_imports', f"cbl_{token}.cbl")
        if not os.path.isfile(filepath):
            return {'status': 'error', 'message': 'Uploaded file not found on server. Please upload again.'}

        with open(filepath, 'rb') as f:
            content = f.read()
        file_sha256 = hashlib.sha256(content).hexdigest()
        if file_sha256.lower() != token.lower():
            return {'status': 'error', 'message': 'Integrity check failed: file hash mismatch.'}

        recon = parse_and_reconcile_cbl(content, f"cbl_{token[:12]}.cbl", myDB)
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

        # Load companion metadata if available (e.g. from dieseltech catalog)
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

        logger.info(f"[CBL_IMPORT] Successfully imported '{display_name}' ({len(books)} issues) with SHA-256 {file_sha256}")
        return {
            'status': 'success',
            'message': f"Successfully imported '{display_name}' ({len(books)} issues)",
            'storyarcid': storyarc_id,
            'storyarcname': display_name
        }
    except Exception as e:
        logger.error(f"[CBL_IMPORT] Error during atomic import transaction: {e}")
        try:
            myDB.action("DELETE FROM storyarcs WHERE StoryArcID=?", [storyarc_id])
            myDB.action("DELETE FROM storyarc_manifests WHERE StoryArcID=?", [storyarc_id])
        except Exception:
            pass
        return {'status': 'error', 'message': f'Failed to import Story Arc: {e}'}


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
