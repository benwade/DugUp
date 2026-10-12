---
type: Brainmap
title: extractors
scope: digupwin-app
status: active
created: 2026-10-12
updated: 2026-10-12
---
<!-- BRAINMAP:v04:2026-10-12T01:46:15 -->
# extractors

## Status
Active | 8 files | Last modified 2026-10-12

## Key Files
- __init__.py — Extractors: one per family of file, and the set that picks between them (CONTRACTS.md part 3).
- chunking.py — Cleaning text and cutting it into overlapping chunks (DigUp's 1800 characters with 200 overlap).
- common.py — What every extractor shares: opening a file safely and turning trouble into a stated reason.
- image.py — Images: nothing to read without OCR, so the answer is "one page, needs OCR" (CONTRACTS.md part 3).
- office.py — Native text readers for Office and Open/LibreOffice files (guide 5d item 4, the last link of the chain).
- pdf.py — PDF text layers, with a verdict for every page (guide 5d item 8, CONTRACTS.md part 3).
- sandboxed.py — SandboxedExtractor: the ordinary readers, run in a worker process.
- textfile.py — Plain text files (and HTML with its tags removed).

## Carry Forward — Preserve This Section

<!-- BRAINMAP_GENERATED:2026-10-12 — No Claude notes yet -->

<!-- END CARRY FORWARD -->