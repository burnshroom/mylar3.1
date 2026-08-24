"""
Mylar Thumbnails Extension Package.

Provides archive path resolution, image extraction, WebP scaling/caching,
and thumbnail HTTP delivery.
"""

from mylar.extensions.thumbnails.controller import handle_issue_thumbnail
from mylar.extensions.thumbnails.resolver import resolve_issue_file
from mylar.extensions.thumbnails.service import get_or_create_issue_thumbnail

__all__ = [
    "handle_issue_thumbnail",
    "resolve_issue_file",
    "get_or_create_issue_thumbnail",
]
