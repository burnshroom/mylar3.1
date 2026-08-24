# Phase M4 — Template Resolution & Extension Ownership Audit

**Document Version:** 1.0 (Read-Only Architectural Audit)  
**Date:** August 23, 2026  
**Status:** Discovery and Planning Only — No Code, Template, or Database Changes

---

## 1. Executive Summary

This audit establishes the template architecture, ownership boundaries, and fallback resolution mechanisms across all three Mylar web interfaces: **Modern**, **Default (Classic)**, and **Carbon**.

### Key Findings
1. **Directory Inventory**:
   - `data/interfaces/default/`: **35** HTML templates + 9 CSS/LESS files. Serves as the authoritative upstream baseline.
   - `data/interfaces/carbon/`: **0** HTML templates + 9 CSS/LESS files + 15 images + 1 JS file. Relies 100% on fallback to `default/`.
   - `data/interfaces/modern/`: **36** HTML templates + 4 CSS/LESS files.
2. **Modern Template Classification**:
   - **6 Custom Modified Templates**: `base.html`, `comicdetails_update.html`, `index.html`, `storyarc.html`, `storyarc_detail.html`, `weeklypull.html`.
   - **1 Modern-Only New Template**: `preview.html` (Issue Inspector / Preview overlay).
   - **29 Unchanged Copies of Default**: Byte-for-byte identical copies of upstream `default/` templates.
3. **Fallback Resolution Reality**:
   - `serve_template()` implements a two-stage `try / except` lookup.
   - When a template is missing from a custom theme, it catches `mako.exceptions.TopLevelLookupException` and falls back to `data/interfaces/default/`.
   - **Critical Inheritance Caveat**: Because `TemplateLookup` is initialized with a single directory in each stage, a template resolved via fallback inherits `default/base.html` (Classic layout) rather than `modern/base.html`.
   - Direct pruning of unchanged templates is **not recommended** until template lookup chaining (`TemplateLookup(directories=[modern_dir, default_dir])`) or shared layout encapsulation is formally introduced in a future phase.

---

## 2. Template Resolution Flow

`mylar/webserve.py :: serve_template(templatename, **kwargs)` governs template resolution across all routes:

```
                  ┌───────────────────────────────┐
                  │ Request Route Handler         │
                  │ (e.g. storyarc_main, index)   │
                  └───────────────┬───────────────┘
                                  │
                                  ▼
                  ┌───────────────────────────────┐
                  │ serve_template(templatename)  │
                  └───────────────┬───────────────┘
                                  │
          ┌───────────────────────┴───────────────────────┐
          ▼                                               ▼
┌───────────────────────────────────┐   ┌───────────────────────────────────┐
│ mylar.CONFIG.INTERFACE == 'default'│   │ mylar.CONFIG.INTERFACE in         │
│ (or None)                         │   │ ('modern', 'carbon', etc.)        │
└─────────────────┬─────────────────┘   └─────────────────┬─────────────────┘
                  │                                       │
                  ▼                                       ▼
    tmper_dir = 'default'                   tmper_dir = mylar.CONFIG.INTERFACE
    icons from '/images/...'                icons from '/interfaces/carbon/images/...'
                  │                                       │
                  │                                       ▼
                  │                     ┌───────────────────────────────────┐
                  │                     │ Primary TemplateLookup:           │
                  │                     │ directories = [interfaces/{theme}]│
                  │                     └─────────────────┬─────────────────┘
                  │                                       │
                  │                        Lookup Success?│
                  │                        ├── YES ───────┼──────────┐
                  │                        │              │          │
                  │                        └── NO (error) │          │
                  │                                       ▼          ▼
                  │                     ┌───────────────────────┐ ┌───────────────┐
                  │                     │ Fallback Lookup:      │ │ Render theme  │
                  │                     │ directories=[default] │ │ template      │
                  │                     └─────────┬─────────────┘ └───────────────┘
                  │                               │
                  │                Lookup Success?│
                  │                ├── YES ───────┼──────────────────┐
                  │                │              │                  │
                  │                └── NO (error) │                  │
                  │                               ▼                  ▼
                  │                     ┌───────────────────┐ ┌───────────────────┐
                  │                     │ Render Mako HTML  │ │ Render default    │
                  │                     │ Error Template    │ │ template          │
                  │                     └───────────────────┘ └───────────────────┘
                  ▼                                                  │
┌───────────────────────────────────┐                                │
│ Render data/interfaces/default/   │◄───────────────────────────────┘
│ template                          │
└───────────────────────────────────┘
```

### Trace by Interface Theme:
1. **Default / Classic (`interface = 'default'`)**:
   - Primary lookup directory: `data/interfaces/default/`.
   - Icons: `/images/...`.
   - Zero fallback needed.
2. **Carbon (`interface = 'carbon'`)**:
   - Primary lookup directory: `data/interfaces/carbon/`.
   - Carbon directory contains **0** `.html` files; primary lookup immediately raises `TopLevelLookupException`.
   - Fallback lookup directory: `data/interfaces/default/`.
   - Context: `interface='carbon'` passes Carbon CSS stylesheets (`interfaces/carbon/css/style.css`) over the Default HTML structure.
3. **Modern (`interface = 'modern'`)**:
   - Primary lookup directory: `data/interfaces/modern/`.
   - If present in `modern/` (36 templates): renders Modern HTML with Modern CSS.
   - If missing from `modern/`: falls back to `data/interfaces/default/`.

---

## 3. Complete Template Inventory & Classification

All 36 templates currently present in `data/interfaces/modern/` compared against `data/interfaces/default/` and `data/interfaces/carbon/`:

| # | Template Name | Default | Carbon | Modern | Classification | Rationale / Content Summary |
|---|---|:---:|:---:|:---:|---|---|
| 1 | `base.html` | YES | NO | YES | **Modified copy of Default** | Modern sidebar navigation, SVG icon sets, top navbar, responsive header, modal hooks. |
| 2 | `comicdetails_update.html` | YES | NO | YES | **Modified copy of Default** | Modern series hero card, issue cards grid, `/getIssueThumbnail` cover art integration, issue inspector. |
| 3 | `index.html` | YES | NO | YES | **Modified copy of Default** | Modern Comic Library with Grid/Shelf view, progress indicators, publisher filters. |
| 4 | `preview.html` | NO | NO | YES | **Modern-only new page** | Dedicated issue preview and metadata inspector overlay modal. |
| 5 | `storyarc.html` | YES | NO | YES | **Modified copy of Default** | Modern Story Arc catalog, List/Shelf toggle, reading order badges (`#1`, `#2`), CBL upload/staging modal. |
| 6 | `storyarc_detail.html` | YES | NO | YES | **Modified copy of Default** | Modern Story Arc detail view, provenance card, 4 resolution states, reading order pills, safe arc deletion. |
| 7 | `weeklypull.html` | YES | NO | YES | **Modified copy of Default** | Modern Weekly Pull list view with card layout, pull status chips, quick-add actions. |
| 8 | `cblimport.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream legacy CBL import page (SHA-256 match). |
| 9 | `config.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream settings tabs and form fields (SHA-256 match). |
| 10 | `config_dump.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream configuration debug dump (SHA-256 match). |
| 11 | `futurepull.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream upcoming pull list calendar (SHA-256 match). |
| 12 | `header.html` | YES | NO | YES | **Unchanged copy of Default** | Legacy subhead menu helper (SHA-256 match). |
| 13 | `history.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream download and snatch history log table (SHA-256 match). |
| 14 | `importlog.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream folder import transaction log (SHA-256 match). |
| 15 | `importresults.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream search and import match resolver (SHA-256 match). |
| 16 | `importresults_popup.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream popup dialog for import choices (SHA-256 match). |
| 17 | `login.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream authentication login form (SHA-256 match). |
| 18 | `logs.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream system logging viewer (SHA-256 match). |
| 19 | `maintenance_base.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream maintenance mode layout (SHA-256 match). |
| 20 | `maintenance_mode.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream database maintenance progress splash (SHA-256 match). |
| 21 | `manage.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream management dashboard (SHA-256 match). |
| 22 | `managecomics.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream series mass-edit management table (SHA-256 match). |
| 23 | `managefailed.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream failed downloads management (SHA-256 match). |
| 24 | `manageissues.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream issue status mass-editor (SHA-256 match). |
| 25 | `opds.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream OPDS feed configuration (SHA-256 match). |
| 26 | `previewrename.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream file rename preview modal (SHA-256 match). |
| 27 | `queue_management.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream download client queue management (SHA-256 match). |
| 28 | `read.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream reading client synchronization (SHA-256 match). |
| 29 | `readinglist.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream legacy reading list manager (SHA-256 match). |
| 30 | `searchfix-2.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream comic search disambiguation v2 (SHA-256 match). |
| 31 | `searchfix.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream comic search disambiguation v1 (SHA-256 match). |
| 32 | `searchresults.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream search result table (SHA-256 match). |
| 33 | `shutdown.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream server restart / shutdown countdown (SHA-256 match). |
| 34 | `storyarc_detail.poster.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream poster-format story arc detail layout (SHA-256 match). |
| 35 | `torrentinfo.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream torrent peer/seed debug info (SHA-256 match). |
| 36 | `upcoming.html` | YES | NO | YES | **Unchanged copy of Default** | Upstream upcoming releases list (SHA-256 match). |

### Summary Statistics
- **Total Modern Templates**: `36`
- **Custom Modern Implementations / Overrides**: `7` (6 modified + 1 modern-only)
- **Unchanged Upstream Copies**: `29` (80.5% of the template tree)

---

## 4. Python Route & Template Context Contracts

The following table documents all Python route/controller contracts supporting Modern UI features:

### 1. Library View
- **Route**: `GET /index` (or `/`)
- **Handler**: `mylar.webserve.WebInterface.index`
- **Template**: `index.html`
- **Context Contract**:
  - `title`: `"Comic List"`
  - `comiclist`: Query result list of all monitored comics (`ComicID`, `ComicName`, `ComicYear`, `Publisher`, `Status`, `Have`, `Total`, `percent`, `Location`, etc.).
  - `delete_type`: `0`

### 2. Series Detail View
- **Route**: `GET /comicDetails`
- **Handler**: `mylar.webserve.WebInterface.comicDetails`
- **Template**: `comicdetails_update.html`
- **Context Contract**:
  - `ComicID`: Primary key string.
  - `comic`: Series dictionary (`ComicName`, `ComicYear`, `ComicPublisher`, `ComicImage`, `Description`, `Status`, etc.).
  - `issues`: List of issue rows (`IssueID`, `Issue_Number`, `IssueName`, `IssueDate`, `ReleaseDate`, `Status`, `Location`, `Type`, etc.).
  - `annuals`: List of annual issue rows.
  - `navigation`: Dictionary with previous/next comic IDs.
  - `stats`: Computed dictionary (`have_count`, `total_count`, `percent`).

### 3. Story Arc Catalog
- **Route**: `GET /storyarc_main`
- **Handler**: `WebInterface.storyarc_main` → `mylar.extensions.storyarcs.controller.handle_storyarc_main`
- **Template**: `storyarc.html`
- **Context Contract**:
  - `title`: `"Story Arcs"`
  - `arclist`: List of enriched story arc dictionaries (`StoryArcID`, `StoryArc`, `SpanYears`, `TotalIssues`, `Have`, `Total`, `percent`, `Status`, `Publisher`, `CV_ArcID`, `Manifest`).
  - `delete_type`: `0`

### 4. Story Arc Detail
- **Route**: `GET /detailStoryArc`
- **Handler**: `WebInterface.detailStoryArc` → `mylar.extensions.storyarcs.controller.handle_detail_storyarc`
- **Template**: `storyarc_detail.html` (or `storyarc_detail.poster.html`)
- **Context Contract**:
  - `storyarcid`: Arc ID string (`cbl_...` or numeric).
  - `storyarcname`: Arc title.
  - `cvarcid`: ComicVine Arc ID (if present).
  - `sdir`: Destination folder path formatted via `helpers.arcformat`.
  - `arcdetail`: Arc summary dict (`percent`, `Have`, `Total`, `SpanYears`, `Publisher`).
  - `storyarcbanner`: Banner/poster cache URL with timestamp.
  - `bannerheight`: String (`'280'` for banner, `'400'` for poster).
  - `bannerwidth`: String (`'960'` for banner, `'263'` for poster).
  - `manifest`: Manifest provenance dict (`SourceName`, `SourceType`, `SHA256`, `ImportTime`, `RepoURL`, `RepoCommit`, `RepoPath`, `TotalIssues`).
  - `have_count`: Integer count of downloaded issues.
  - `total_count`: Integer count of total arc issues.
  - `percent`: Integer completion percentage.
  - `spanyears`: Publication year span string (`'1989-1990'` or `'—'`).
  - `publisher`: Publisher name string.
  - `readlist`: List of enriched issue rows with both modern resolution attributes (`ResolutionState`, `IsDownloaded`, `Location`, `IsMonitored`, `StoreDate`) and legacy database attributes (`Status`, `IssueArcID`, `Volume`, `IssueNumber`, `IssueName`, `IssueDate`, `ReleaseDate`, `issueYEAR`, `Publisher`, `SeriesYear`).

### 5. CBL Reading List Import & Deletion Endpoints
- **Upload**: `POST /cbl_upload` (multipart `cbl_file`) → JSON `{ status, upload_token, manifest_title, publisher, total_issues, is_already_imported, results }`.
- **Preview**: `GET /cbl_preview?token=...` → JSON `{ status, token, source_type, manifest_title, publisher, total_issues, results }`.
- **Confirm Import**: `POST /cbl_confirm_import` (`token`, `filename`) → JSON `{ status, message, storyarcid, storyarcname }`.
- **Delete Arc**: `POST /cbl_delete_arc` (`storyarcid`) → JSON `{ status, message, storyarcid }` (safely prunes unreferenced raw `.cbl` artifacts).

### 6. Issue Thumbnail Endpoint
- **Route**: `GET /IssueThumbnail?issueid=<id>&comicid=<optional-id>`
- **Handler**: `WebInterface.IssueThumbnail` → `mylar.extensions.thumbnails.controller.handle_issue_thumbnail`
- **Response Contract**:
  - HTTP 200: Content-Type `image/webp;charset=utf-8` (or `image/jpeg`), `Cache-Control: public, max-age=604800, immutable`.
  - HTTP 404: `X-Mylar-Fallback: vector` (when archive/cover is missing).
  - HTTP 400: Error string on missing or invalid `issueid`.

---

## 5. Experimental Fallback Behavior & Proof

An isolated test was executed using mocked interface directories outside production/staging data (`scratch/test_template_fallback_behavior.py`):

```
=================================================================
=== Phase M4: Isolated Template Fallback Resolution Test ===
=================================================================

--- Test 1: Carbon Interface Native Fallback to Default ---
  [PASS] Carbon 'history.html' rendered via fallback to Default (50085 bytes).

--- Test 2: Isolated Mocked Theme Missing-Template Behavior ---
  Case A (Theme Override exists): <html><body>MOCK_THEME_BASE: SAMPLE_THEME_CONTENT</body></html>
  Case B: Primary lookup raised TopLevelLookupException: Can't locate template for uri 'default_only.html'
  Case B Fallback Render: <html><body>DEFAULT_BASE: DEFAULT_ONLY_CONTENT</body></html>
  [PASS] Missing theme template cleanly falls back to Default directory.
  Case C: Both primary and fallback failed as expected (TopLevelLookupException).

--- Test 3: Unchanged Template Render Comparison (Modern vs Default) ---
  Modern config.html length: 17226
  Default config.html length: 17226
  [PASS] Template comparison verified.
```

### Architectural Finding: Inheritance Trapping
When Mako evaluates a template resolved from `default/` (e.g. `default/config.html`), any directive `<%inherit file="base.html"/>` is resolved within the `directories=[default_dir]` lookup path. Consequently:
- It inherits `data/interfaces/default/base.html` (Classic layout with table wrappers).
- It injects `${interface}` as `'modern'`, loading `interfaces/modern/css/style.css` over Classic HTML classes.
- While it avoids a 500 error, the visual presentation on that page reverts to Classic layout styled with Modern CSS.

---

## 6. Recommended Future Extraction & Pruning Strategy

To make Modern UI maintainable as an add-on layer without breaking upstream updates:

### Phase 1: Modern-Owned Core (Must Remain in `modern/`)
The 7 custom templates must remain explicitly owned by Modern:
1. `data/interfaces/modern/base.html`
2. `data/interfaces/modern/comicdetails_update.html`
3. `data/interfaces/modern/index.html`
4. `data/interfaces/modern/storyarc.html`
5. `data/interfaces/modern/storyarc_detail.html`
6. `data/interfaces/modern/weeklypull.html`
7. `data/interfaces/modern/preview.html`

### Phase 2: Upstream Sync / Chained Lookup Prerequisite
Before pruning the 29 unchanged template copies from `data/interfaces/modern/`, the template resolution mechanism in `serve_template()` must be updated to use chained directory lookups:
```python
# Future enhancement: chained lookup preserves modern/base.html while falling back to default/
_hplookup = TemplateLookup(directories=[theme_dir, default_dir])
```
With chained lookup, an un-overridden page (such as `logs.html`) can inherit `base.html` from `modern/` while reading its body content from `default/logs.html`.

### Phase 3: Pruning Candidates (Once Chained Lookup is Implemented)
All 29 unchanged copies can then be safely pruned from `data/interfaces/modern/`, reducing file duplication by over 80%:
`cblimport.html`, `config.html`, `config_dump.html`, `futurepull.html`, `header.html`, `history.html`, `importlog.html`, `importresults.html`, `importresults_popup.html`, `login.html`, `logs.html`, `maintenance_base.html`, `maintenance_mode.html`, `manage.html`, `managecomics.html`, `managefailed.html`, `manageissues.html`, `opds.html`, `previewrename.html`, `queue_management.html`, `read.html`, `readinglist.html`, `searchfix-2.html`, `searchfix.html`, `searchresults.html`, `shutdown.html`, `storyarc_detail.poster.html`, `torrentinfo.html`, `upcoming.html`.

---

## 7. Explicit Rollback Plan

If any future template change causes a regression:
1. **Per-Template Rollback**: Because every template is tracked in git, any individual template in `data/interfaces/modern/` can be restored from the baseline commit `0dcf7c265b343be8b24cde96d17c3086488ed975` via `git checkout <commit> -- data/interfaces/modern/<template>.html`.
2. **Interface Fallback Rollback**: Switching `mylar.CONFIG.INTERFACE = 'default'` or `'carbon'` in `config.ini` instantly returns the application to 100% upstream presentation without modifying code.
3. **Database Independence**: Zero database migrations or schema alterations depend on the template files.

---

## 8. Verification & Read-Only Safety Confirmation

- **Application Source Code**: `0` lines modified during M4.
- **Templates / CSS / JS**: `0` lines modified during M4.
- **Database Status**: `Downloaded: 2978`, `Skipped: 10718`, `Wanted: 0` (100% invariant, 0 mutations).
- **Git Status**: `git diff --check` and `git status` confirm no tracked source files modified.
- **Safety Statement**: Template fallback via the existing `try/except` works for simple pages, but chained lookup is a strict prerequisite before pruning unchanged templates to avoid layout inheritance degradation.
