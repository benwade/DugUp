---
type: Brainmap
title: dugup
scope: digupwin-app
status: active
created: 2026-10-12
updated: 2026-10-12
---
<!-- BRAINMAP:v04:2026-10-12T01:46:16 -->
# dugup

## Status
Active | 30 files [L] | Last modified 2026-10-12

## Structure
- extractors/ — Active | 8 files | Last modified 2026-10-12

## Root Files
- __init__.py — DugUp: local file search for Windows and Linux.
- cli.py — Command line: a small client of the Service (guide 5c item 7).
- datadir.py — The one data folder (guide 5a and 5c).
- dirscan.py — DirectoryScan: the default FileSource (guide decision D2). Walks a folder with os.scandir.
- doctor.py — `doctor`: is this machine ready, and if not, what is missing (guide 5c, 5d).
- filesource.py — Pure helpers every FileSource shares: include/exclude rules and the comparison of two listings.
- indexer.py — The indexer: bring the index up to date with the folders, in two resumable steps.
- jobs.py — Jobs a worker can be asked to do, and the plain-data forms that cross the process boundary.
- kinds.py — Which family a file belongs to, decided by its extension (guide 5d item 4).
- launcher.py — The one launcher: every outside program and every worker is started here (CONTRACTS.md, "Stopping").
- lockfile.py — One indexing run at a time (CONTRACTS.md part 6: one writer owns the file).
- ocr.py — OCR: reading words from pixels, in a worker process (CONTRACTS.md part 4).
- ocrqueue.py — The OCR queue: read the pages and pictures that wait for OCR, most useful first.
- protocols.py — The ten contracts as Protocol classes (CONTRACTS.md, draft 2).
- sandbox.py — SubprocessSandbox: the Sandbox contract with real worker processes (CONTRACTS.md part 7).
- service.py — LocalService: the Service contract, in-process (guide 5b, CONTRACTS.md part 9).
- settings.py — Settings: every number the program would otherwise hardcode, with its default and where it came from.
- shapes.py — Shared data shapes (CONTRACTS.md, "Shared words").
- store.py — The Store: one SQLite file (guide section 4, CONTRACTS.md part 6).
- suggest.py — Suggestions for what to leave out of a folder (a proposal for the user, never applied by itself).
- worker.py — The worker: a separate process that does risky work for the Sandbox (CONTRACTS.md part 7).

## Carry Forward — Preserve This Section

<!-- BRAINMAP_GENERATED:2026-10-12 — No Claude notes yet -->

<!-- END CARRY FORWARD -->