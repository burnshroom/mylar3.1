# Mylar3 Modularization Audit & Extraction Plan (Phase M0)

**Document Version:** 1.0  
**Status:** Completed Audit & Architectural Plan (No Code Changes)  
**Target Milestone:** Post-Story Arc / CBL Milestone  
**Scope:** Modern theme, Story Arc / CBL import & provenance, Issue thumbnail generation/serving, Shelf rendering & Inspector, Security & cross-platform path resolution.

---

## 1. Executive Summary & Architecture Goals

### 1.1 Context
Over previous development cycles, Mylar3 has been enhanced with:
- A responsive, dark-mode **Modern Interface** (`data/interfaces/modern/`).
- Interactive **Cover Shelf View**, **Issue Inspector**, and **Multi-Issue Selection Toolbars**.
- High-performance, non-destructive **Issue Thumbnail Extraction & Caching** (WebP via Pillow).
- A **Story Arc / CBL Import & Provenance System** supporting direct XML/CBL file uploads, pre-validated staged manifests, heuristic series/issue reconciliation, atomic import transactions, and SHA-256 reference-counted artifact cleanup.
- **Security hardening** (strict cover path allowlists, path traversal protection, Linux-to-UNC path mapping).

### 1.2 The Problem
Currently, these customizations are injected directly into core Mylar monolithic files:
- `mylar/webserve.py` contains ~650 lines of custom CBL parsing, reconciliation, endpoint controllers, and thumbnail dispatching inside the 10,000-line `WebInterface` class.
- `mylar/helpers.py` and `mylar/getimage.py` contain custom thumbnail and path resolution routines appended to core utilities.
- `mylar/maintenance.py` contains custom SQLite schema migrations injected directly into `Maintenance.db_check()`.
- `data/interfaces/modern/` contains 41 template files, 34 of which are exact 1:1 copies of `default/` templates, creating significant divergence debt when upstream updates default views.

### 1.3 Target Architecture
The objective is a **minimally invasive extension architecture**—not a heavy generic plugin runtime. The design isolates custom domain logic into a structured `mylar/extensions/` package and replaces core file modifications with minimal, stable, 1-to-3 line hooks.

```
┌────────────────────────────────────────────────────────────────────────┐
│                          Core Mylar Engine                             │
│                                                                        │
│   mylar/webserve.py         mylar/maintenance.py   mylar/helpers.py    │
│   (WebInterface routes)     (db_check hook)        (core utils)        │
└──────────┬──────────────────────────┬──────────────────────────────────┘
           │ [Minimal Hooks]          │ [Migration Hook]
           ▼                          ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        mylar/extensions/ Layer                         │
│                                                                        │
│  ┌────────────────────┐ ┌────────────────────┐ ┌────────────────────┐  │
│  │   modern/          │ │   thumbnails/      │ │   storyarcs/       │  │
│  │  - template fallback│ │  - service (WebP)  │ │  - parser & recon  │  │
│  │  - security allow  │ │  - resolver (UNC)  │ │  - manifests       │  │
│  │  - modular JS/CSS  │ │  - controller      │ │  - importer & arc  │  │
│  └────────────────────┘ └────────────────────┘ └────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │   migrations/ (Isolated SQLite schema updates)                   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Change Inventory & Evidence Audit

Every modified file, function, and template was audited using `git diff` against upstream `origin/stable`.

| Component / Feature | Modified File(s) | Function / Location | Classification | Upstream Conflict Risk | Movable Now Without Behavior Change? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Issue Thumbnail Generation & Caching** | `mylar/getimage.py` | `get_or_create_issue_thumbnail()` | Shared Backend Logic | **Medium** | **Yes** (Move to `mylar.extensions.thumbnails.service`) |
| **Archive & UNC Path Resolution** | `mylar/helpers.py` | `resolve_issue_file()` | Shared Backend Logic | **Medium** | **Yes** (Move to `mylar.extensions.thumbnails.resolver`) |
| **Cover Cache Path Security Allowlist** | `mylar/helpers.py` | `validate_cache_cover_path()` | Shared Backend Logic / Security | **Medium** | **Yes** (Move to `mylar.extensions.modern.security`) |
| **Story Arc Manifests DB Schema** | `mylar/maintenance.py` | `Maintenance.db_check()` | Database / Migration Logic | **Medium-High** | **Yes** (Move to `mylar.extensions.migrations`) |
| **Series Grid Cover Serialization** | `mylar/webserve.py` | `WebInterface.getJSON()` | Shared Backend Logic | **High** | **Yes** (Keep 1-line security filter call) |
| **Staged Manifests Registry & Path Finder**| `mylar/webserve.py` | `STAGED_CBL_MANIFESTS`, `_get_staged_cbl_path()`, `_sanitize_cbl_filename()` | Shared Backend Logic | **High** | **Yes** (Move to `mylar.extensions.storyarcs.manifests` and `parser`) |
| **CBL Parsing & Reconciliation Engine** | `mylar/webserve.py` | `_parse_and_reconcile_cbl()` | Shared Backend Logic | **High** | **Yes** (Move to `mylar.extensions.storyarcs.reconciler`) |
| **CBL Upload Endpoint** | `mylar/webserve.py` | `WebInterface.cbl_upload()` | Shared Backend Logic / HTTP Route | **High** | **Yes** (Move to `mylar.extensions.storyarcs.controller`) |
| **CBL Preview Endpoint** | `mylar/webserve.py` | `WebInterface.cbl_preview()` | Shared Backend Logic / HTTP Route | **High** | **Yes** (Move to `mylar.extensions.storyarcs.controller`) |
| **CBL Confirm Import Endpoint** | `mylar/webserve.py` | `WebInterface.cbl_confirm_import()` | Shared Backend Logic / HTTP Route | **High** | **Yes** (Move to `mylar.extensions.storyarcs.importer`) |
| **CBL Delete Arc & Artifact Cleanup** | `mylar/webserve.py` | `WebInterface.cbl_delete_arc()` | Shared Backend Logic / HTTP Route | **High** | **Yes** (Move to `mylar.extensions.storyarcs.service`) |
| **Story Arc List View Enrichment** | `mylar/webserve.py` | `WebInterface.storyArc()` | Shared Backend Logic | **High** | **Yes** (Delegate enrichment to `mylar.extensions.storyarcs.service`) |
| **Story Arc Detail View Enrichment** | `mylar/webserve.py` | `WebInterface.detailStoryArc()` | Shared Backend Logic | **High** | **Yes** (Delegate enrichment to `mylar.extensions.storyarcs.service`) |
| **Issue Thumbnail HTTP Route** | `mylar/webserve.py` | `WebInterface.IssueThumbnail()` | Shared Backend Logic / HTTP Route | **High** | **Yes** (Move to `mylar.extensions.thumbnails.controller`) |
| **Modern Shell & Navigation** | `data/interfaces/modern/base.html` | Base layout template | Modern-Theme-Only | **High** | **Yes** (Retain as primary modern template override) |
| **Modern Series & Issue Shelf / Inspector** | `data/interfaces/modern/comicdetails_update.html` | Series detail template | Modern-Theme-Only | **High** | **Yes** (Retain template; extract inline JS to static assets) |
| **Modern Library Table / Shelf Mode** | `data/interfaces/modern/index.html` | Library overview template | Modern-Theme-Only | **Medium-High** | **Yes** (Retain template; extract inline JS to static assets) |
| **Modern Story Arc Management & CBL Modal** | `data/interfaces/modern/storyarc.html` | Story arc list template | Modern-Theme-Only | **High** | **Yes** (Retain template; extract inline JS to static assets) |
| **Modern Story Arc Detail & Hero Banner** | `data/interfaces/modern/storyarc_detail.html` | Story arc detail template | Modern-Theme-Only | **High** | **Yes** (Retain template) |
| **Modern Weekly Pull List Layout** | `data/interfaces/modern/weeklypull.html` | Weekly pull template | Modern-Theme-Only | **Medium** | **Yes** (Retain template) |
| **Static Theme Prototype Mock** | `data/interfaces/modern/preview.html` | Static preview HTML | Dead / Prototype Asset | **None** | **Yes** (Move to documentation / design archive) |
| **Modern Theme Stylesheets** | `data/interfaces/modern/css/*` | CSS stylesheets | Modern-Theme-Only | **Low** | **Yes** (Retain under theme assets) |
| **Duplicated Unmodified Templates (34 files)** | `data/interfaces/modern/*.html` | `config.html`, `logs.html`, `manage.html`, etc. | Redundant Copies | **High** | **Yes** (Remove once Mako fallback lookup is implemented) |

---

## 3. Deep Dive: Technical Trace & Upstream Risk Analysis

### 3.1 Issue Thumbnail Generation & Serving
* **Current Execution Trace:**
  1. Frontend request arrives at `GET /IssueThumbnail?issueid=<id>`.
  2. `WebInterface.IssueThumbnail()` queries `issues` / `annuals` joined with `comics` to extract `ComicID`, `ComicLocation`, and `Location`.
  3. `helpers.resolve_issue_file()` validates path traversal (`..`), tests local file existence, and maps Linux path prefixes (`/mnt/local/Media/Comics`) to Windows UNC roots (`\\shroomserver\mnt\local\Media\Comics`).
  4. `getimage.get_or_create_issue_thumbnail()` reads the CBZ (`zipfile`) or CBR (`rarfile`), extracts the first image into memory, applies a 100MP decompression bomb safety check, converts color modes (`RGBA`/`P`/`LA`/`1` to `RGB`), resizes via `Image.LANCZOS` to 300px width, writes atomically via a `.tmp_` file to `cache/thumbnails/<comicid>/<issueid>.webp`, and returns the path.
  5. CherryPy serves the file with `image/webp` and `Cache-Control: public, max-age=3600`, or responds with 404 and header `X-Mylar-Fallback: vector`.
* **Coupling & Risk:**
  - Logic is scattered across `webserve.py`, `helpers.py`, and `getimage.py`.
  - Upstream updates to `getimage.py` (which handles ComicVine remote images) could inadvertently collide with archive extraction code.
* **Extraction Plan:**
  - Consolidate all thumbnail caching, image processing, path resolution, and HTTP dispatch into `mylar/extensions/thumbnails/`.

### 3.2 Story Arc & CBL Provenance System
* **Current Execution Trace:**
  1. **Schema Check:** On startup, `Maintenance.db_check()` runs `CREATE TABLE IF NOT EXISTS storyarc_manifests (...)` and executes `PRAGMA table_info` checks for `SourceType` and `RawXMLPath`.
  2. **Upload / Staged Handling:**
     - `POST /cbl_upload`: Reads file stream up to 5MB, calculates SHA-256, writes to `data/cbl_imports/cbl_<sha256>.cbl`, calls `_parse_and_reconcile_cbl()`.
     - `GET /cbl_preview`: Retrieves staged manifest from `STAGED_CBL_MANIFESTS` registry or uploaded file by SHA-256 token, re-runs reconciliation.
  3. **Parsing & Reconciliation:**
     - `_parse_and_reconcile_cbl()` parses XML `<Book>` items, extracts series title, volume, issue number, and publication year.
     - Runs heuristic queries against `comics`, `issues`, and `annuals` to categorize each entry into `Downloaded`, `Monitored but not downloaded`, `Unmonitored series`, or `Unknown/unmatched`.
  4. **Import & Persistence:**
     - `POST /cbl_confirm_import`: Executes atomic transaction inserting manifest record into `storyarc_manifests` and reading order sequence into `storyarcs`.
  5. **Deletion & Ref-Counted Cleanup:**
     - `POST /cbl_delete_arc`: Deletes arc from `storyarcs` and `storyarc_manifests`. Checks if SHA-256 is referenced by any other arc; if zero references remain, unlinks raw file from `data/cbl_imports/`.
  6. **UI Enrichment:**
     - `storyArc()` and `detailStoryArc()` query `storyarc_manifests` to inject badges, calculate download percentages, determine span years, and serve `storyarc.html` / `storyarc_detail.html`.
* **Coupling & Risk:**
  - Injects ~650 lines into `webserve.py`.
  - Injects raw SQL DDL into `maintenance.py`.
  - Any upstream rebase of `webserve.py` will experience merge conflicts in `storyArc` and `detailStoryArc`.
* **Extraction Plan:**
  - Move parser, reconciler, manifest registry, importer, and cleanup service into `mylar/extensions/storyarcs/`.
  - Move schema definitions into `mylar/extensions/migrations/`.
  - Keep `storyArc()` and `detailStoryArc()` in `webserve.py` as clean 5-line delegates.

### 3.3 Modern Theme & Template Architecture
* **Current State:**
  - `data/interfaces/modern/` was created as an interface theme directory.
  - Currently contains 41 HTML files and 4 CSS files.
  - 34 of the 41 HTML files are bit-for-bit identical duplicates of `data/interfaces/default/`.
  - Only 6 templates are customized: `base.html`, `comicdetails_update.html`, `index.html`, `storyarc.html`, `storyarc_detail.html`, `weeklypull.html`, plus 1 static mock `preview.html`.
* **Coupling & Risk:**
  - Whenever upstream Mylar fixes bugs in `config.html`, `history.html`, `manage.html`, or `logs.html`, those fixes will NOT appear in the modern interface because CherryPy/Mako finds the stale duplicate in `data/interfaces/modern/`.
* **Extraction Plan:**
  - Modify `serve_template()` in `mylar/webserve.py` (via `mylar.extensions.modern.template_lookup`) to provide layered directory search: `[modern_dir, default_dir]`.
  - Delete all 34 identical duplicated template files from `data/interfaces/modern/`.
  - Retain only the 6 customized templates and stylesheets.

---

## 4. Proposed Extension Package Layout

The recommended modular layout isolates all domain logic while keeping core Mylar intact:

```text
mylar/
├── extensions/
│   ├── __init__.py                         # Extension loader & registry
│   │
│   ├── modern/                             # Modern UI extensions & security
│   │   ├── __init__.py
│   │   ├── template_lookup.py              # Multi-directory fallback lookup ([modern, default])
│   │   ├── security.py                     # validate_cache_cover_path()
│   │   └── static/                         # Static JS/CSS assets
│   │       ├── js/
│   │       │   ├── shelf_view.js           # Shelf rendering & selection logic
│   │       │   ├── issue_inspector.js      # Issue Inspector drawer logic
│   │       │   ├── cbl_import.js           # Drag-and-drop & staged CBL modal logic
│   │       │   └── library_view.js         # Library view mode & sorting
│   │       └── css/
│   │           └── style.css
│   │
│   ├── thumbnails/                         # Issue thumbnail generation & delivery
│   │   ├── __init__.py
│   │   ├── service.py                      # get_or_create_issue_thumbnail() (CBZ/CBR -> WebP)
│   │   ├── resolver.py                     # resolve_issue_file() (Traversal & UNC mapping)
│   │   └── controller.py                   # IssueThumbnail HTTP endpoint handler
│   │
│   ├── storyarcs/                          # Story Arc / CBL import & provenance
│   │   ├── __init__.py
│   │   ├── manifests.py                    # Staged manifests registry & metadata
│   │   ├── parser.py                       # CBL/XML parsing & filename sanitization
│   │   ├── reconciler.py                   # Series / Volume / Issue heuristic reconciliation
│   │   ├── importer.py                     # Atomic database import transaction
│   │   ├── service.py                      # Arc detail enrichment & ref-counted deletion
│   │   └── controller.py                   # cbl_upload, cbl_preview, cbl_confirm_import, cbl_delete_arc
│   │
│   └── migrations/                         # Isolated extension database schema migrations
│       ├── __init__.py
│       ├── runner.py                       # run_extension_migrations()
│       └── versions/
│           ├── __init__.py
│           └── 001_storyarc_manifests.py   # storyarc_manifests table & column migrations
│
data/
└── interfaces/
    └── modern/                             # Streamlined theme (overrides only)
        ├── base.html                       # Base layout & modern navigation
        ├── comicdetails_update.html        # Series details & shelf view
        ├── index.html                      # Library grid/table view
        ├── storyarc.html                   # Story arcs & CBL modal
        ├── storyarc_detail.html            # Story arc detail & hero provenance
        ├── weeklypull.html                 # Weekly pull list view
        └── css/                            # Modern theme stylesheets
```

---

## 5. Stable Core Hook Specifications

To achieve this modularization without altering runtime behavior, only **5 minimal hooks** are placed in core Mylar files.

### Hook 1: Database Migration Hook (`mylar/maintenance.py`)
In `Maintenance.db_check()`:
```python
# Replace hardcoded CREATE TABLE storyarc_manifests with:
from mylar.extensions.migrations.runner import run_extension_migrations
run_extension_migrations(self.dbmylar)
```

### Hook 2: Layered Template Lookup Hook (`mylar/webserve.py`)
In `serve_template()`:
```python
# Replace single-directory template lookup with:
from mylar.extensions.modern.template_lookup import get_interface_directories
template_dirs = get_interface_directories(tmper_dir, interface_dir)
_hplookup = TemplateLookup(directories=template_dirs)
```
*Where `get_interface_directories('modern', interface_dir)` returns `['data/interfaces/modern', 'data/interfaces/default']`.*

### Hook 3: HTTP Endpoint Delegation (`mylar/webserve.py`)
In `WebInterface`:
```python
# CBL Endpoints
from mylar.extensions.storyarcs import controller as cbl_controller
from mylar.extensions.thumbnails import controller as thumb_controller

def cbl_upload(self, cbl_file=None, **kwargs):
    return cbl_controller.handle_cbl_upload(cbl_file, **kwargs)
cbl_upload.exposed = True

def cbl_preview(self, token=None, **kwargs):
    return cbl_controller.handle_cbl_preview(token, **kwargs)
cbl_preview.exposed = True

def cbl_confirm_import(self, token=None, **kwargs):
    return cbl_controller.handle_cbl_confirm_import(token, **kwargs)
cbl_confirm_import.exposed = True

def cbl_delete_arc(self, storyarcid=None, **kwargs):
    return cbl_controller.handle_cbl_delete_arc(storyarcid, **kwargs)
cbl_delete_arc.exposed = True

def IssueThumbnail(self, issueid, comicid=None):
    return thumb_controller.handle_issue_thumbnail(issueid, comicid)
IssueThumbnail.exposed = True
```

### Hook 4: Cover Path Validation in Series Serialization (`mylar/webserve.py`)
In `WebInterface.getJSON()`:
```python
from mylar.extensions.modern.security import validate_cache_cover_path
# In row comprehension:
validate_cache_cover_path(row.get('ComicImage'))
```

### Hook 5: Story Arc Enrichment Delegation (`mylar/webserve.py`)
In `WebInterface.storyArc()` and `WebInterface.detailStoryArc()`:
```python
from mylar.extensions.storyarcs.service import enrich_storyarc_list, enrich_storyarc_detail

# Inside storyArc:
arclist = enrich_storyarc_list(raw_arclist)

# Inside detailStoryArc:
enriched_data = enrich_storyarc_detail(arcinfo, StoryArcID, StoryArcName)
return serve_template(templatename="storyarc_detail.html", **enriched_data)
```

---

## 6. Template Inheritance & Pruning Strategy

### 6.1 Current Problem
`data/interfaces/modern/` currently contains 41 files totaling ~1.2 MB. Of these, 34 files are exact duplicates of `data/interfaces/default/`.
If upstream updates `manage.html` or `logs.html`, those updates are masked when `CONFIG.INTERFACE = 'modern'`.

### 6.2 Pruning Plan
With the multi-directory Mako lookup enabled:
1. **Templates to Keep in `modern/` (6 active overrides):**
   - `base.html` (Shell, mobile menu, search hotkey, SVG icons)
   - `comicdetails_update.html` (Shelf mode, inspector, multi-select)
   - `index.html` (Cover grid, progress percentage, sort controls)
   - `storyarc.html` (Story Arc grid, CBL upload modal, staged chooser)
   - `storyarc_detail.html` (Arc hero card, reading order table, status badges)
   - `weeklypull.html` (Header action bar, navigation links)
   - `css/*` (`style.css`, `data_table.css`, `jquery-ui.css`, `config.less`)
2. **Prototype to Archive:**
   - `preview.html` -> Move to docs / archive.
3. **Templates to Delete from `modern/` (34 redundant files):**
   - `cblimport.html`, `config.html`, `config_dump.html`, `futurepull.html`, `header.html`, `history.html`, `importlog.html`, `importresults.html`, `importresults_popup.html`, `login.html`, `logs.html`, `maintenance_base.html`, `maintenance_mode.html`, `manage.html`, `managecomics.html`, `managefailed.html`, `manageissues.html`, `opds.html`, `previewrename.html`, `queue_management.html`, `read.html`, `readinglist.html`, `searchfix-2.html`, `searchfix.html`, `searchresults.html`, `shutdown.html`, `storyarc_detail.poster.html`, `torrentinfo.html`, `upcoming.html`, etc.

**Result:** Zero duplicate templates; 100% automatic inheritance of upstream fixes for non-overridden pages.

---

## 7. Migration & Extraction Roadmap (Future Execution Steps)

When extraction execution begins in future phases, the following sequential phases ensure zero regression:

| Phase | Tasks | Verification Gate |
| :--- | :--- | :--- |
| **Phase E1: Core Package & Migrations** | 1. Create `mylar/extensions/` package structure.<br>2. Implement `migrations/runner.py` and `001_storyarc_manifests.py`.<br>3. Replace DDL in `mylar/maintenance.py` with migration runner hook. | Verify clean startup, table creation, and column idempotency on fresh and existing DBs. |
| **Phase E2: Thumbnail Layer Extraction** | 1. Implement `extensions/thumbnails/resolver.py` (`resolve_issue_file`).<br>2. Implement `extensions/thumbnails/service.py` (`get_or_create_issue_thumbnail`).<br>3. Implement `extensions/thumbnails/controller.py` (`handle_issue_thumbnail`).<br>4. Replace implementations in `helpers.py`, `getimage.py`, and `webserve.py` with delegates. | Verify `/IssueThumbnail` endpoint with CBZ, CBR, UNC share fallback, and vector fallback. |
| **Phase E3: Story Arc & CBL Layer Extraction** | 1. Implement `extensions/storyarcs/manifests.py`, `parser.py`, `reconciler.py`, `importer.py`, `service.py`, and `controller.py`.<br>2. Replace inline methods in `webserve.py` with extension calls. | Verify CBL file upload, staged manifest preview, atomic import, arc detail enrichment, and delete ref-counting. |
| **Phase E4: Modern Theme & Template Pruning** | 1. Implement `extensions/modern/template_lookup.py` and `security.py`.<br>2. Update `serve_template()` in `webserve.py`.<br>3. Prune 34 duplicate HTML templates from `data/interfaces/modern/`.<br>4. Extract JavaScript modules into `modern/static/js/`. | Verify all Mylar views render correctly in Modern theme (both overridden views and inherited default views). |
| **Phase E5: Upstream Merge Smoke Test** | 1. Run git diff check to confirm minimal footprint in core Mylar files (`webserve.py`, `helpers.py`, `maintenance.py`, `getimage.py`).<br>2. Execute full functional regression suite. | Zero functional changes; diff against upstream is clean and isolated. |

---

## 8. Summary of Upstream Conflict Reductions

| Metric | Before Extraction | After Extraction | Improvement |
| :--- | :--- | :--- | :--- |
| **Custom lines in `mylar/webserve.py`** | ~650 lines | ~25 lines (delegate hooks) | **96% reduction** |
| **Custom lines in `mylar/helpers.py`** | ~75 lines | 0 lines | **100% elimination** |
| **Custom lines in `mylar/getimage.py`** | ~98 lines | 0 lines | **100% elimination** |
| **Custom lines in `mylar/maintenance.py`** | ~15 lines | 2 lines (migration hook) | **87% reduction** |
| **Files in `data/interfaces/modern/`** | 41 HTML + 4 CSS | 6 HTML + 4 CSS | **85% template reduction** |
| **Upstream Rebase Conflict Probability** | **High** across 4 core files | **Very Low** (isolated to stable hooks) | **Substantial stability improvement** |

*Plan generated and verified against the working codebase. Ready for execution upon review and approval.*
