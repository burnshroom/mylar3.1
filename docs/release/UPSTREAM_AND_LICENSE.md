# Upstream Provenance, Authorship, and GPLv3 Licensing

## 1. Project Provenance & Upstream Lineage

- **Canonical Upstream Repository**: [mylar3/mylar3](https://github.com/mylar3/mylar3) (originally created by evilhero as evilhero/mylar, maintained by MylarComics)
- **Derived Upstream Repository**: [burnshroom/mylar3.1](https://github.com/burnshroom/mylar3.1)
- **Git Ancestry**: Full linear Git history (2,729+ commits originating from commit e82352b9 in Sep 2012) is retained without squashing, rebasing, or rewriting.
- **Project Status**: This is currently an **unofficial derivative / extension preview** and is **not an official upstream release of Mylar or Mylar3**.

## 2. Original Authorship & Copyright Notices

All original authorship and copyright notices are retained in full:
- Original creator and maintainer: **evilhero** (2012–2020)
- Mylar3 core maintainers: **MylarComics team** (2020–present)
- Mylar 3.1 maintainer: **burnshroom** (2024–present)

## 3. License: GNU General Public License v3.0 (GPLv3)

This software is distributed under the terms of the GNU General Public License version 3.0 (GPLv3).
The complete license text is available in the root LICENSE file.

### Compliance Statements (GPLv3 §5):
- **§5a (Modification Notices)**: This work contains modifications developed during **August 2026**. Broad functional areas added or modified include:
  1. **Creator Credit Indexing & Data Model**: Extraction of ComicInfo.xml and CBR/CBZ credits into dedicated database models (ext_creator_name_records, ext_creator_credits).
  2. **Supplemental Provider Integration (Metron)**: Rate-limited, authenticated client and snapshot caching for external creator metadata.
  3. **Candidate Discovery & Scoring**: Automated credit overlap matching and candidate ranking.
  4. **Creator Identity Resolution Engine**: Explicit confirmation, candidate rejection, decision history audit timeline, and identity registry.
  5. **Conflict Resolution & Safe Transfers**: Keep-existing resolution, provider mapping transfer, and safe reversal without orphan cascades.
  6. **Modern Responsive Web Interface**: Mobile-friendly, dark-mode accessible UI templates and interactive inspector dialogs.
  7. **Database Extension Migration Runner**: Versioned, idempotent DDL migration subsystem (mylar/extensions/migrations/).
- **§5b (Copyleft & Terms)**: All added and modified source files are licensed under GPLv3 as part of the combined work.
- **§5c (Source Availability)**: The complete corresponding source code, build scripts, and test suites are provided directly in this repository and packaged with distributed container images.

## 4. Corresponding Source Distribution

In compliance with GPLv3 §6:
- Source repository access: Distributed via Git repository and referenced in container OCI labels (org.opencontainers.image.source).
- Build & Packaging: Local build instructions, Dockerfile, and CI/CD workflow files are included in the source tree.
