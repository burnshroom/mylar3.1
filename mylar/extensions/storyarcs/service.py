"""
Story Arc Business Logic & Detail Service.

Handles Story Arc catalog queries, detail generation, reading order enrichment,
and banner metadata calculation.
Re-exports CBL domain routines from cbl_service for backwards compatibility.
"""

import datetime
import hashlib
import json
import os
import re

import mylar
from mylar import db, helpers, logger
from mylar.extensions.storyarcs.cbl_service import (
    STAGED_CBL_MANIFESTS,
    get_staged_cbl_path,
    sanitize_cbl_filename,
    parse_and_reconcile_cbl,
    upload_cbl_manifest,
    preview_cbl_manifest,
    confirm_cbl_import,
    reconcile_existing_storyarc,
    execute_entry_action,
    delete_cbl_arc,
)


def get_storyarc_catalog(arcid=None, myDB=None):
    """
    Queries and enriches the Story Arc catalog list.

    :param arcid: Optional single StoryArcID or CV_ArcID filter
    :param myDB: Optional DBConnection instance
    :return: List of enriched story arc dictionaries
    """
    if myDB is None:
        myDB = db.DBConnection()

    arclist = []
    if arcid is None:
        alist = myDB.select("SELECT * from storyarcs WHERE ComicName is not Null GROUP BY StoryArcID")
    else:
        alist = myDB.select("SELECT * from storyarcs WHERE ComicName is not Null AND (StoryArcID=? OR CV_ArcID=?) GROUP BY StoryArcID", [arcid, arcid])

    for al in alist:
        arc_id = al['StoryArcID']
        arc_issues = myDB.select("SELECT * from storyarcs WHERE StoryArcID=? AND NOT Manual is 'deleted' ORDER BY ReadingOrder ASC", [arc_id])
        totalarc = len(arc_issues)

        havearc = 0
        for it in arc_issues:
            iid = it['IssueID']
            if iid:
                iss = myDB.selectone("SELECT Status, Location FROM issues WHERE IssueID=?", [iid]).fetchone()
                if not iss:
                    iss = myDB.selectone("SELECT Status, Location FROM annuals WHERE IssueID=? AND NOT Deleted", [iid]).fetchone()
                if iss and iss['Status'] == 'Downloaded' and iss['Location'] and iss['Location'] != 'None':
                    havearc += 1

        try:
            percent = int((havearc * 100.0) / totalarc) if totalarc > 0 else 0
        except (ZeroDivisionError, TypeError):
            percent = 0
        manifest = myDB.selectone("SELECT * FROM storyarc_manifests WHERE StoryArcID=?", [arc_id]).fetchone()
        span_years = helpers.spantheyears(arc_id) if arc_id else al['SeriesYear']
        if not span_years or span_years in ('0000-00-00', '0000-00', '0000', 'None') or str(span_years).startswith('0000'):
            span_years = '—'

        series_yr = al['SeriesYear']
        if not series_yr or series_yr in ('0000-00-00', '0000-00', '0000', 'None') or str(series_yr).startswith('0000'):
            series_yr = '—'

        arclist.append({
            "StoryArcID":       arc_id,
            "StoryArc":         al['StoryArc'],
            "TotalIssues":      totalarc,
            "SeriesYear":       series_yr,
            "StoryArcDir":      al['StoryArc'],
            "Status":           al['Status'],
            "percent":          percent,
            "Have":             havearc,
            "SpanYears":        span_years,
            "Total":            totalarc,
            "CV_ArcID":         al['CV_ArcID'],
            "Publisher":        al['Publisher'] or 'DC Comics',
            "Manifest":         dict(manifest) if manifest else None
        })

    return arclist


def _clean_date(d):
    """Formats unknown / zero dates as None."""
    if not d:
        return None
    d_str = str(d).strip()
    if d_str in ('0000-00-00', '0000-00', '0000', 'None', 'null', '0', ''):
        return None
    if d_str.startswith('0000'):
        return None
    return d_str


def _row_val(r, k):
    """Safely retrieves a row column value."""
    if not r:
        return None
    try:
        return r[k]
    except Exception:
        return None


def get_storyarc_detail(storyarc_id, storyarc_name=None, cv_arc_id=None, myDB=None):
    """
    Queries and builds detailed reading order and provenance data for a Story Arc.
    - Preserves downloaded-arc location refresh behavior via helpers.updatearc_locs.
    - Preserves complete template context for Classic, Carbon, and Modern templates.

    :param storyarc_id: Story Arc ID string (e.g. 'cbl_c33e762620fe' or numeric)
    :param storyarc_name: Optional Story Arc Name
    :param cv_arc_id: Optional ComicVine Arc ID
    :param myDB: Optional DBConnection instance
    :return: Full template context dictionary
    """
    if myDB is None:
        myDB = db.DBConnection()

    if storyarc_id is None and cv_arc_id is not None:
        arcinfo = myDB.select("SELECT * from storyarcs WHERE CV_ArcID=? and NOT Manual IS 'deleted' order by ReadingOrder ASC", [cv_arc_id])
    else:
        arcinfo = myDB.select("SELECT * from storyarcs WHERE StoryArcID=? and NOT Manual IS 'deleted' order by ReadingOrder ASC", [storyarc_id])

    issref = []
    cvarcid = None
    sdir = getattr(mylar.CONFIG, 'GRABBAG_DIR', None)
    arcpub = 'DC Comics'

    if arcinfo:
        try:
            cvarcid = _row_val(arcinfo[0], 'CV_ArcID')
            arcpub = _row_val(arcinfo[0], 'Publisher') or 'DC Comics'
            if storyarc_id is None:
                storyarc_id = _row_val(arcinfo[0], 'StoryArcID')
            if not storyarc_name:
                storyarc_name = _row_val(arcinfo[0], 'StoryArc')
            sdir = helpers.arcformat(storyarc_name, cvarcid=cvarcid, Publisher=arcpub)
        except Exception:
            pass

        for it in arcinfo:
            issref.append({
                'IssueArcID': _row_val(it, 'IssueArcID'),
                'IssueID': _row_val(it, 'IssueID'),
                'StoryArcID': _row_val(it, 'StoryArcID'),
                'Status': _row_val(it, 'Status'),
                'ReadingOrder': _row_val(it, 'ReadingOrder'),
                'Location': _row_val(it, 'Location')
            })

    # Location Refresh: Check for downloaded arc items where Location is missing
    recheck_locs = [x for x in issref if x['Status'] == 'Downloaded' and x['Location'] is None]
    if len(recheck_locs) > 0:
        logger.info('Missing locations for downloaded issues in Story Arc. Refreshing...')
        helpers.updatearc_locs(issref, sdir)
        if storyarc_id is None and cv_arc_id is not None:
            arcinfo = myDB.select("SELECT * from storyarcs WHERE CV_ArcID=? and NOT Manual IS 'deleted' order by ReadingOrder ASC", [cv_arc_id])
        else:
            arcinfo = myDB.select("SELECT * from storyarcs WHERE StoryArcID=? and NOT Manual IS 'deleted' order by ReadingOrder ASC", [storyarc_id])

    if not arcinfo:
        manifest = myDB.selectone("SELECT * FROM storyarc_manifests WHERE StoryArcID=?", [storyarc_id]).fetchone()
        return {
            'found': False,
            'template': 'storyarc_detail.html',
            'readlist': [],
            'storyarcname': storyarc_name or (manifest['StoryArcName'] if manifest else 'Story Arc'),
            'storyarcid': storyarc_id,
            'cvarcid': cvarcid,
            'sdir': sdir,
            'arcdetail': {'percent': 0, 'Have': 0, 'Total': 0, 'SpanYears': '—', 'Publisher': 'Unknown'},
            'storyarcbanner': 'images/banner.png',
            'bannerheight': '280',
            'bannerwidth': '960',
            'manifest': dict(manifest) if manifest else None,
            'have_count': 0,
            'total_count': 0,
            'percent': 0,
            'spanyears': '—',
            'publisher': 'Unknown'
        }

    total_count = len(arcinfo)
    have_count = 0
    enriched_readlist = []

    for al in arcinfo:
        al_dict = dict(al) if not isinstance(al, dict) else al.copy()

        iid = _row_val(al, 'IssueID')
        sid = _row_val(al, 'ComicID')
        order = _row_val(al, 'ReadingOrder') or 0
        sname = _row_val(al, 'ComicName') or ''
        inum = _row_val(al, 'IssueNumber') or ''
        vyear = _row_val(al, 'SeriesYear') or ''
        pyear = _row_val(al, 'IssueYEAR') or ''
        arc_issue_status = _row_val(al, 'Status')

        store_date = _clean_date(_row_val(al, 'StoreDate'))
        issue_date = _clean_date(_row_val(al, 'IssueDate'))
        release_date = _clean_date(_row_val(al, 'ReleaseDate'))

        res_state = 'Unknown / Unmatched Reference'
        is_downloaded = False
        is_monitored = False
        mylar_loc = None
        issue_title = _row_val(al, 'IssueName') or ''

        if iid:
            iss = myDB.selectone("SELECT IssueID, ComicID, IssueName, Status, Location, IssueDate, ReleaseDate FROM issues WHERE IssueID=?", [iid]).fetchone()
            if not iss:
                iss = myDB.selectone("SELECT IssueID, ComicID, IssueName, Status, Location, IssueDate, ReleaseDate FROM annuals WHERE IssueID=? AND NOT Deleted", [iid]).fetchone()

            if iss:
                is_monitored = True
                if not issue_title and _row_val(iss, 'IssueName'):
                    issue_title = _row_val(iss, 'IssueName')

                if not issue_date:
                    issue_date = _clean_date(_row_val(iss, 'IssueDate'))
                if not release_date:
                    release_date = _clean_date(_row_val(iss, 'ReleaseDate'))

                if _row_val(iss, 'Status') == 'Downloaded' and _row_val(iss, 'Location') and _row_val(iss, 'Location') != 'None':
                    res_state = 'Downloaded'
                    is_downloaded = True
                    have_count += 1
                    mylar_loc = _row_val(iss, 'Location')
                else:
                    res_state = 'Missing (Monitored)'
                    if arc_issue_status == 'Downloaded':
                        have_count += 1
                        is_downloaded = True
            elif sid:
                comic = myDB.selectone("SELECT ComicID, ComicName FROM comics WHERE ComicID=?", [sid]).fetchone()
                if comic:
                    res_state = 'Unknown / Unmatched Reference'
                else:
                    res_state = 'Unmonitored Series'
            else:
                res_state = 'Unknown / Unmatched Reference'
        elif sid:
            comic = myDB.selectone("SELECT ComicID, ComicName FROM comics WHERE ComicID=?", [sid]).fetchone()
            if comic:
                res_state = 'Unknown / Unmatched Reference'
            else:
                res_state = 'Unmonitored Series'
        else:
            res_state = 'Unknown / Unmatched Reference'

        if not store_date:
            store_date = release_date or issue_date

        iss_status_val = _row_val(iss, 'Status') if iss else None
        is_unmonitored = (sid is not None and not is_monitored)
        can_mark_wanted = bool(is_monitored and iss_status_val in ('Skipped', 'Archived', 'Ignored', 'Wanted', 'Loading'))
        can_add_series = bool(is_unmonitored and sid)

        al_dict['ReadingOrder'] = order
        al_dict['StoreDate'] = store_date
        al_dict['IssueDate'] = issue_date
        al_dict['ReleaseDate'] = release_date
        al_dict['ResolutionState'] = res_state
        al_dict['IsDownloaded'] = is_downloaded
        al_dict['Location'] = mylar_loc or _row_val(al, 'Location')
        al_dict['IsMonitored'] = is_monitored
        al_dict['IsUnmonitoredSeries'] = is_unmonitored
        al_dict['CanMarkWanted'] = can_mark_wanted
        al_dict['CanAddSeries'] = can_add_series
        al_dict['IssueStatus'] = iss_status_val
        al_dict['ComicName'] = sname
        al_dict['IssueNumber'] = inum
        al_dict['IssueName'] = issue_title
        al_dict['Volume'] = vyear
        al_dict['IssueYEAR'] = pyear
        al_dict['SeriesYear'] = vyear
        al_dict['IssueArcID'] = _row_val(al, 'IssueArcID') or f"{storyarc_id}_{order}"
        al_dict['ComicID'] = sid
        al_dict['IssueID'] = iid
        al_dict['Publisher'] = _row_val(al, 'Publisher') or arcpub

        enriched_readlist.append(al_dict)

    enriched_readlist.sort(key=lambda x: (x['ReadingOrder'] is None, x['ReadingOrder'] if x['ReadingOrder'] is not None else 999999))

    try:
        percent = int((have_count * 100.0) / total_count) if total_count > 0 else 0
    except (ZeroDivisionError, TypeError):
        percent = 0

    manifest = myDB.selectone("SELECT * FROM storyarc_manifests WHERE StoryArcID=?", [storyarc_id]).fetchone()
    try:
        spanyears = helpers.spantheyears(storyarc_id) if storyarc_id else None
    except Exception:
        spanyears = None

    if not spanyears or spanyears in ('0000-00-00', '0000-00', '0000', 'None') or str(spanyears).startswith('0000'):
        years = [x.get('SeriesYear') or x.get('IssueYEAR') for x in enriched_readlist if x.get('SeriesYear') or x.get('IssueYEAR')]
        years = [y for y in years if y and str(y).isdigit() and int(y) > 1900]
        if years:
            min_y, max_y = min(years), max(years)
            spanyears = f"{min_y} - {max_y}" if min_y != max_y else str(min_y)
        else:
            spanyears = _row_val(arcinfo[0], 'SeriesYear') if arcinfo else '—'

    storyarc_name = _row_val(arcinfo[0], 'StoryArc') or (manifest['StoryArcName'] if manifest else 'Story Arc')
    publisher = _row_val(arcinfo[0], 'Publisher') or (manifest['SourceName'] if manifest else 'Unknown')

    arcdetail = {
        'percent': percent,
        'Have': have_count,
        'Total': total_count,
        'SpanYears': spanyears,
        'Publisher': publisher
    }

    # Banner / poster dimensions and template selection
    bannerheight = '280'
    bannerwidth = '960'
    template = 'storyarc_detail.html'
    storyarcbanner = f"images/banner.png?v={datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    filepath = os.path.join(mylar.DATA_DIR, 'cache', 'storyarcs', f"{storyarc_id}.png")
    if os.path.exists(filepath):
        storyarcbanner = f"cache/storyarcs/{storyarc_id}.png?v={datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
        if cvarcid:
            fname = f"{cvarcid}.png"
            if 'H' in fname:
                bannerwidth = '263'
                bannerheight = str(fname[fname.find('H')+1:fname.find('.')])
                template = 'storyarc_detail.html'
            elif 'W' in fname:
                bannerheight = '400'
                bannerwidth = str(fname[fname.find('W')+1:fname.find('.')])
                template = 'storyarc_detail.poster.html'
        else:
            try:
                import get_image_size
                image = get_image_size.get_image_metadata(filepath)
                imageinfo = json.loads(get_image_size.Image.to_str_json(image))
                if imageinfo['width'] > imageinfo['height']:
                    template = 'storyarc_detail.html'
                    bannerheight = '280'
                    bannerwidth = '960'
                else:
                    template = 'storyarc_detail.poster.html'
                    bannerwidth = '263'
                    bannerheight = '400'
            except Exception:
                bannerheight = '280'
                bannerwidth = '960'

    return {
        'found': True,
        'template': template,
        'readlist': enriched_readlist,
        'storyarcname': storyarc_name,
        'storyarcid': storyarc_id,
        'cvarcid': cvarcid,
        'sdir': sdir,
        'arcdetail': arcdetail,
        'storyarcbanner': storyarcbanner,
        'bannerheight': bannerheight,
        'bannerwidth': bannerwidth,
        'manifest': dict(manifest) if manifest else None,
        'have_count': have_count,
        'total_count': total_count,
        'percent': percent,
        'spanyears': spanyears,
        'publisher': publisher
    }
