"""
Creator Archive Indexer and ComicInfo.xml Parser.

Handles safe, read-only extraction of ComicInfo.xml from .cbz and .cbr archives,
change detection signature computation, and conservative tag parsing.
"""

import hashlib
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

from mylar import logger
from mylar.extensions.creators.normalizer import map_role, parse_creator_names
from mylar.extensions.creators.schema import (
    MAX_COMICINFO_BYTES,
    SCAN_STATUSES,
)

# Import rarfile support from bundled lib
try:
    from rarfile import rarfile
except ImportError:
    lib_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'lib')
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)
    try:
        from rarfile import rarfile
    except ImportError:
        rarfile = None


TAGS_TO_PARSE = [
    ('Writer', 'writer', False),
    ('Penciller', 'penciller', False),
    ('Inker', 'inker', False),
    ('Colorist', 'colorist', False),
    ('Letterer', 'letterer', False),
    ('Editor', 'editor', False),
    ('CoverArtist', 'cover_artist', True),
]


def compute_file_signature(file_path):
    """
    Get file modification time in integer nanoseconds and size in bytes.

    :param file_path: Absolute or resolved path to comic archive
    :return: tuple of (mtime_ns: int, size_bytes: int) or (0, 0) if inaccessible
    """
    try:
        st = os.stat(file_path)
        mtime_ns = getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))
        return mtime_ns, st.st_size
    except Exception as e:
        logger.fdebug(f"[CREATOR-INDEXER] Unable to stat file {file_path}: {e}")
        return 0, 0


def compute_xml_hash(xml_bytes):
    """
    Compute SHA256 hex digest of raw XML bytes.
    """
    if not xml_bytes:
        return None
    return hashlib.sha256(xml_bytes).hexdigest()


def read_archive_comicinfo(file_path):
    """
    Safely extract ComicInfo.xml content from a .cbz or .cbr archive in read-only mode.

    Returns:
        tuple: (raw_xml_text, raw_xml_bytes, scan_status, error_message)
    """
    if not file_path or not os.path.isfile(file_path):
        return None, None, 'inaccessible_or_unsupported', f"File not found or not accessible: {file_path}"

    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.cbz':
        return _read_comicinfo_zip(file_path)
    elif ext == '.cbr':
        return _read_comicinfo_rar(file_path)
    else:
        return None, None, 'inaccessible_or_unsupported', f"Unsupported archive extension: {ext}"


def _read_comicinfo_zip(file_path):
    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            target_name = None
            for name in zf.namelist():
                if os.path.basename(name).lower() == 'comicinfo.xml':
                    target_name = name
                    break

            if not target_name:
                return None, None, 'no_comicinfo', None

            info = zf.getinfo(target_name)
            if info.file_size > MAX_COMICINFO_BYTES:
                return None, None, 'inaccessible_or_unsupported', f"ComicInfo.xml exceeds 1MB limit ({info.file_size} bytes)"

            raw_bytes = zf.read(target_name)
            if len(raw_bytes) > MAX_COMICINFO_BYTES:
                return None, None, 'inaccessible_or_unsupported', "ComicInfo.xml byte stream exceeds 1MB limit"

            # Security check: reject DOCTYPE / ENTITY declarations to prevent XXE
            if b'<!DOCTYPE' in raw_bytes.upper() or b'<!ENTITY' in raw_bytes.upper():
                return None, None, 'inaccessible_or_unsupported', "Unsafe XML: DOCTYPE or ENTITY declarations detected"

            raw_text = raw_bytes.decode('utf-8', errors='replace')
            return raw_text, raw_bytes, 'scanned_with_credits', None

    except zipfile.BadZipFile as e:
        return None, None, 'inaccessible_or_unsupported', f"Corrupt or invalid zip archive: {e}"
    except Exception as e:
        return None, None, 'inaccessible_or_unsupported', f"Error reading zip archive: {e}"


def _read_comicinfo_rar(file_path):
    if rarfile is None:
        # Check if CBR is actually a renamed ZIP
        try:
            return _read_comicinfo_zip(file_path)
        except:
            return None, None, 'inaccessible_or_unsupported', "rarfile library not available to read CBR"

    try:
        with rarfile.RarFile(file_path, 'r') as rf:
            target_name = None
            for name in rf.namelist():
                if os.path.basename(name).lower() == 'comicinfo.xml':
                    target_name = name
                    break

            if not target_name:
                return None, None, 'no_comicinfo', None

            info = rf.getinfo(target_name)
            if info.file_size > MAX_COMICINFO_BYTES:
                return None, None, 'inaccessible_or_unsupported', f"ComicInfo.xml exceeds 1MB limit ({info.file_size} bytes)"

            raw_bytes = rf.read(target_name)
            if len(raw_bytes) > MAX_COMICINFO_BYTES:
                return None, None, 'inaccessible_or_unsupported', "ComicInfo.xml byte stream exceeds 1MB limit"

            if b'<!DOCTYPE' in raw_bytes.upper() or b'<!ENTITY' in raw_bytes.upper():
                return None, None, 'inaccessible_or_unsupported', "Unsafe XML: DOCTYPE or ENTITY declarations detected"

            raw_text = raw_bytes.decode('utf-8', errors='replace')
            return raw_text, raw_bytes, 'scanned_with_credits', None

    except Exception as e:
        # Attempt fallback to zip reader in case CBR is a misnamed zip
        try:
            res_text, res_bytes, res_status, res_err = _read_comicinfo_zip(file_path)
            if res_status in ('scanned_with_credits', 'no_comicinfo'):
                return res_text, res_bytes, res_status, res_err
        except:
            pass
        return None, None, 'inaccessible_or_unsupported', f"Error reading CBR archive: {e}"


def extract_credits_from_xml(raw_xml_text):
    """
    Parse ComicInfo.xml content and extract structured creator credits.

    Returns:
        list of dicts: [
            {
                'role': 'writer',
                'raw_role_text': 'Writer',
                'raw_credit_name': 'Fabian Nicieza',
                'is_cover': 0,
                'sort_order': 0
            },
            ...
        ]
    """
    if not raw_xml_text or not raw_xml_text.strip():
        return []

    try:
        root = ET.fromstring(raw_xml_text)
    except Exception as e:
        logger.fdebug(f"[CREATOR-INDEXER] XML parse error: {e}")
        return []

    credits = []

    for tag_name, role_default, is_cover in TAGS_TO_PARSE:
        elem = root.find(tag_name)
        if elem is not None and elem.text and elem.text.strip():
            raw_val = elem.text.strip()
            names = parse_creator_names(raw_val)
            for idx, name in enumerate(names):
                credits.append({
                    'role': role_default,
                    'raw_role_text': tag_name,
                    'raw_credit_name': name,
                    'is_cover': 1 if is_cover else 0,
                    'sort_order': idx
                })

    return credits
