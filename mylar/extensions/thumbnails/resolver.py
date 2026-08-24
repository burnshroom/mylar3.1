"""
Issue Archive Path Resolution.

Safely resolves comic archive files from database location fields,
enforcing strict basename-only containment within approved library directories
and handling cross-platform staging mappings.
"""

import os
import re


def resolve_issue_file(comic_location, issue_location):
    """
    Safely resolves a comic archive file from database location fields.
    - Treats issue_location strictly as a filename / basename.
    - Rejects path traversal (..), path separators (/ and \\), absolute paths,
      drive letters (C:), UNC prefixes (\\\\ or //), and null bytes.
    - Normalizes comic directory and file path with abspath/normpath.
    - Verifies directory containment using path-aware commonpath comparisons.
    - Falls back to staging UNC mapping for Linux database paths on Windows host.
    - Returns canonical file path if file exists, else None.
    """
    if not comic_location or not isinstance(comic_location, str):
        return None
    if comic_location == "None" or not comic_location.strip():
        return None

    if not issue_location or not isinstance(issue_location, str):
        return None
    if issue_location == "None" or not issue_location.strip():
        return None

    # Reject null bytes
    if "\0" in issue_location or "\0" in comic_location:
        return None

    # Reject directory traversal attempts
    if ".." in issue_location:
        return None

    # Reject any path separators (must be a basename only)
    if "/" in issue_location or "\\" in issue_location:
        return None

    # Reject absolute paths
    if os.path.isabs(issue_location):
        return None

    # Reject drive-qualified paths (e.g. C:...)
    drive, _ = os.path.splitdrive(issue_location)
    if drive or re.match(r"^[a-zA-Z]:", issue_location):
        return None

    # Reject UNC paths
    if issue_location.startswith("\\\\") or issue_location.startswith("//"):
        return None

    # 1. Direct path check with commonpath containment
    try:
        norm_comic_dir = os.path.abspath(os.path.normpath(comic_location))
        direct_candidate = os.path.abspath(os.path.normpath(os.path.join(norm_comic_dir, issue_location)))
        if os.path.commonpath([norm_comic_dir, direct_candidate]) == norm_comic_dir:
            if os.path.isfile(direct_candidate):
                return direct_candidate
    except (ValueError, Exception):
        pass

    # 2. Linux-to-UNC mapping for Windows preview/staging
    LINUX_ROOT = "/mnt/local/Media/Comics"
    UNC_ROOT = r"\\shroomserver\mnt\local\Media\Comics"

    norm_loc = comic_location.replace("\\", "/")
    if norm_loc.startswith(LINUX_ROOT):
        rel = norm_loc[len(LINUX_ROOT):].lstrip("/")
        try:
            unc_comic_dir = os.path.abspath(os.path.normpath(os.path.join(UNC_ROOT, rel)))
            unc_candidate = os.path.abspath(os.path.normpath(os.path.join(unc_comic_dir, issue_location)))
            norm_unc_root = os.path.abspath(os.path.normpath(UNC_ROOT))

            # Must be contained within both unc_comic_dir and UNC_ROOT
            if (
                os.path.commonpath([unc_comic_dir, unc_candidate]) == unc_comic_dir
                and os.path.commonpath([norm_unc_root, unc_candidate]) == norm_unc_root
            ):
                if os.path.isfile(unc_candidate):
                    return unc_candidate
        except (ValueError, Exception):
            pass

    return None
