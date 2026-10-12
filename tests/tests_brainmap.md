---
type: Brainmap
title: tests
scope: digupwin-app
status: active
created: 2026-10-12
updated: 2026-10-12
---
<!-- BRAINMAP:v04:2026-10-12T01:46:17 -->
# tests

## Status
Active | 55 files [L] | Last modified 2026-10-12

## Structure
- conformance/ — Active | 6 files | Last modified 2026-10-12
- corpus/ — Active | 6 files | Last modified 2026-10-12
- fakes/ — Active | 5 files | Last modified 2026-10-12

## Root Files
- dirscan_harness.py — A real-folder harness for the FileSource conformance checks: everything happens in a temporary folder.
- extractor_harness.py — The Extractor harness for the real extractors: it writes real small files into a temporary folder
- helpers.py — Shared test constants. Source files stay ASCII; non-ASCII text is built with chr().
- make_corpus.py — Write the generated test tree into a folder, to try the program on by hand.
- sandbox_jobs.py — Jobs for the Sandbox tests. They run inside a worker process, never in the test process.
- test_cli.py — The command line, run in-process on real temporary folders.
- test_conformance_extractor.py — Extractor conformance: the fake must pass every check; each broken fake must be caught.
- test_conformance_filesource.py — FileSource conformance: the fake must pass every check; each broken fake must be caught.
- test_conformance_ocr.py — OcrBackend conformance: the fake must pass every check; each broken fake must be caught.
- test_conformance_store_fake.py — Store conformance on the in-memory fake: it must pass every check; each broken fake must be caught.
- test_conformance_store_real.py — The shared Store checks run against the real SqliteStore.
- test_dirscan.py — DirectoryScan behaviors that the shared checks do not cover.
- test_dirscan_conformance.py — DirectoryScan against the shared FileSource checks, on real temporary folders.
- test_dirscan_private.py — Timing and counts for DirectoryScan on real folders. Prints numbers only: no names, no paths.
- test_end_to_end.py — End to end on the generated tree: real folders, the real scanner, the real readers, the real store.
- test_extractors.py — The real extractors on generated files: encodings, each Office format, PDF verdicts as a table, limits.
- test_extractors_conformance.py — The shared Extractor checks, run on real files read by the real extractors.
- test_filesource_logic.py — The pure FileSource helpers: include/exclude rules and the comparison of two listings.
- test_hardening.py — Second tries, the pause after a run of failures, the disk guards, OCR that survives a re-read,
- test_indexer.py — The indexer against an in-memory disk, with the in-memory store and with the real SQLite store.
- test_lockfile.py — The run lock: one holder at a time, never a wait, and a dead holder never keeps it.
- test_ocr.py — OCR plumbing that needs no engine: engine output to lines, and the backend's talk with its worker.
- test_ocr_cli.py — `dugup ocr` and Service.run_ocr with a scripted engine: what is said, what is returned, what is locked.
- test_ocr_real.py — The real OCR engine, in a real worker process. Skipped, with the reason, where the engine is not installed.
- test_ocrqueue.py — The OCR queue, with a scripted engine and the real store: order, resume, stop, and every kind of failure.
- test_protocols.py — The ten contracts exist as Protocols, the shapes behave, and real and fake parts have the right methods.
- test_sandbox.py — The launcher and the Sandbox with real processes (CONTRACTS.md part 7 and "Stopping").
- test_sandboxed_extractor.py — Readers in a worker process: the same answers as in-process, and every way a worker can fail is a stated failure.
- test_service.py — LocalService on real temporary folders: roots, policy, honest results, one run at a time, background runs.
- test_settings.py — Settings: defaults, the config file, and how mistakes in it are reported.
- test_suggest.py — Suggestions for what to skip: pure logic on made-up listings, then the commands on real folders.

## Carry Forward — Preserve This Section

<!-- BRAINMAP_GENERATED:2026-10-12 — No Claude notes yet -->

<!-- END CARRY FORWARD -->