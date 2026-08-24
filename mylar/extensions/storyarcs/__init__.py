"""
Mylar Story Arcs Extension Package.

Provides Story Arc catalog/detail handling, CBL manifest uploading,
strict ComicVine-ID reconciliation, atomic imports, deletion, and DieselTech catalog browsing.
"""

from mylar.extensions.storyarcs import controller, service, cbl_service, cbl_catalog
from mylar.extensions.storyarcs.controller import (
    handle_storyarc_main,
    handle_detail_storyarc,
    handle_cbl_upload,
    handle_cbl_preview,
    handle_cbl_confirm_import,
    handle_cbl_delete_arc,
    handle_cbl_catalog_status,
    handle_cbl_catalog_refresh,
    handle_cbl_catalog_search,
    handle_cbl_catalog_preview,
)
from mylar.extensions.storyarcs.cbl_service import (
    STAGED_CBL_MANIFESTS,
    get_staged_cbl_path,
    sanitize_cbl_filename,
    parse_and_reconcile_cbl,
    upload_cbl_manifest,
    preview_cbl_manifest,
    confirm_cbl_import,
    delete_cbl_arc,
)
from mylar.extensions.storyarcs.service import (
    get_storyarc_catalog,
    get_storyarc_detail,
)

__all__ = [
    "controller",
    "service",
    "cbl_service",
    "cbl_catalog",
    "handle_storyarc_main",
    "handle_detail_storyarc",
    "handle_cbl_upload",
    "handle_cbl_preview",
    "handle_cbl_confirm_import",
    "handle_cbl_delete_arc",
    "handle_cbl_catalog_status",
    "handle_cbl_catalog_refresh",
    "handle_cbl_catalog_search",
    "handle_cbl_catalog_preview",
    "STAGED_CBL_MANIFESTS",
    "get_staged_cbl_path",
    "sanitize_cbl_filename",
    "parse_and_reconcile_cbl",
    "get_storyarc_catalog",
    "get_storyarc_detail",
    "upload_cbl_manifest",
    "preview_cbl_manifest",
    "confirm_cbl_import",
    "delete_cbl_arc",
]
