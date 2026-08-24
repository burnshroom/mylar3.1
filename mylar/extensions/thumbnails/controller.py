"""
Issue Thumbnail Controller.

Handles HTTP requests for issue thumbnails, querying database records,
invoking resolver and thumbnail services, and serving WebP assets via CherryPy.
"""

import os

import cherrypy
from cherrypy.lib.static import serve_file

from mylar import db
from mylar.extensions.thumbnails.resolver import resolve_issue_file
from mylar.extensions.thumbnails.service import get_or_create_issue_thumbnail


def handle_issue_thumbnail(issueid, comicid=None):
    """
    HTTP handler for /IssueThumbnail route.
    
    :param issueid: Issue identifier (int or numeric string)
    :param comicid: Optional Comic identifier
    :return: WebP file stream via serve_file or HTTP error response
    """
    try:
        val_issueid = int(str(issueid).strip())
    except (ValueError, TypeError):
        cherrypy.response.status = 400
        return "Invalid issue ID"

    myDB = db.DBConnection()
    row = myDB.selectone(
        "SELECT a.ComicID, a.ComicLocation, b.Location FROM comics a JOIN issues b ON a.ComicID=b.ComicID WHERE b.IssueID=?",
        [val_issueid],
    ).fetchone()
    if not row:
        row = myDB.selectone(
            "SELECT a.ComicID, a.ComicLocation, b.Location FROM comics a JOIN annuals b ON a.ComicID=b.ComicID WHERE b.IssueID=? AND NOT b.Deleted",
            [val_issueid],
        ).fetchone()

    if not row or not row["Location"]:
        cherrypy.response.status = 404
        cherrypy.response.headers["X-Mylar-Fallback"] = "vector"
        return "No local archive"

    cid = row["ComicID"]
    cloc = row["ComicLocation"]
    iloc = row["Location"]

    resolved_path = resolve_issue_file(cloc, iloc)
    if not resolved_path:
        cherrypy.response.status = 404
        cherrypy.response.headers["X-Mylar-Fallback"] = "vector"
        return "File not found"

    thumb_path = get_or_create_issue_thumbnail(cid, val_issueid, resolved_path)
    if not thumb_path or not os.path.isfile(thumb_path):
        cherrypy.response.status = 404
        cherrypy.response.headers["X-Mylar-Fallback"] = "vector"
        return "Thumbnail unavailable"

    cherrypy.response.headers["Cache-Control"] = "public, max-age=3600"
    return serve_file(thumb_path, content_type="image/webp")
