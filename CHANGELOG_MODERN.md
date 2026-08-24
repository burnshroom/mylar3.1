# Modern Creator Identity Extension Changelog

All notable changes introduced by the Modern Creator Identity extension project are documented in this file.

## [Creator Identity Preview 1] - 2026-08-24

### Added
- **Creator Credit Indexing Subsystem**:
  - Implemented ext_creator_name_records, ext_creator_credits, and indexing background worker.
  - Archive metadata extraction from ComicInfo.xml and CBR/CBZ archives with role parsing.
- **Supplemental Metadata Provider (Metron)**:
  - Rate-limited and authenticated Metron REST API client with exponential backoff.
  - Comparison caching and credit match scoring.
- **Candidate Discovery & Identity Scoring**:
  - Heuristic match ranking with confidence tiers and candidate discovery service.
- **Decision Engine & Mutation Security**:
  - Explicit confirmation, candidate rejection, and safe reversal endpoints.
  - Strict POST-only method enforcement, CSRF token validation, and parameter sanitization.
  - Server-derived reversal targets protecting against spoofed client rejection identifiers.
- **Decision History & Audit Trail**:
  - Complete immutable audit logging (ext_creator_resolution_audit) with before/after state snapshots.
  - Decision timeline view and event summaries.
- **Creator Identity Registry**:
  - Full-text search, role filters, status counters (confirmed, rejected, conflicted, transferred, unresolved), and pagination.
- **Conflict Resolution Engine**:
  - Keep-existing resolution (keep_existing_reject_competing).
  - Provider mapping transfer (	ransfer_mapping).
  - Safe transfer reversal (
everse_transfer) restoring pre-transfer state without cascading deletes.
- **Modern Responsive Web Interface**:
  - Modern templates (ase.html, comicdetails_update.html, creators.html, creator_detail.html, creator_registry.html).
  - Responsive stylesheets, modal dialogs, and dark mode support.
- **Extension Schema Migration Runner**:
  - Automated, idempotent versioned DDL migrations in mylar/extensions/migrations/.
- **Authoritative Regression Suite**:
  - 17 test suites covering 265 automated test cases in 	ests/extensions/.
