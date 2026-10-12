# DugUp

Local file search for Windows and Linux: find your files by the words inside them. Everything stays on
your machine.

DugUp was inspired by the macOS app [DigUp](https://github.com/ARahim3/DigUp) but is a separate design,
written from scratch in Python; it shares no code with it.

Status: early. Phase 1 (words in files) works and is tested on Windows 11 and Linux. OCR for scans and
photos, search by meaning, photo search and a window are planned and not built yet.

## What works today

- Choose folders, index them, search by the words in files and in file names.
- Reads plain text and HTML, PDF text layers, docx/xlsx/pptx and odt/ods/odp.
- Suggests what to skip (downloaded requirements, tool caches, trash, build output) and never skips by itself.
- Every file ends in a stated state. A scan or photo is `partial` ("pages wait for OCR") until the OCR
  step exists; a file that cannot be read says why.
- Stop with Ctrl+C and run again to continue. Deleted files stay findable, flagged, for 30 days.
- Files are parsed in a separate worker process with a time and a memory limit, so a damaged or hostile file
  can cost one worker and never the index. The worker has no network access.

## Set up

Python 3.10 or newer. From the folder you cloned into:

Windows (PowerShell):

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements-dev.txt
    pip install -e .

Linux:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    pip install -e .

## Use

    dugup doctor
    dugup roots add "D:\Some Folder"
    dugup roots suggest "D:\Some Folder"              (what may not be worth indexing; changes nothing)
    dugup roots suggest "D:\Some Folder" --apply-safe (skip downloaded requirements and tool data)
    dugup roots exclude "D:\Some Folder" /Trash Temp  (skip anything else; --undo takes it back)
    dugup index
    dugup search invoice 2024
    dugup status --attention

`python -m dugup ...` does the same. The index lives in one per-user data folder (`%LOCALAPPDATA%\DugUp`
on Windows, `~/.local/share/DugUp` on Linux); add `--data-dir FOLDER` to any command to keep it somewhere
else. Deleting that folder removes everything DugUp stored. Settings go in `config.toml` in the data
folder; see `src/dugup/settings.py` for the names and defaults.

## Tests

    python -m pytest

`python tests/make_corpus.py SOME_EMPTY_FOLDER` writes the generated test tree, to try the program by hand.

## Layout

| Module | What it is |
|---|---|
| `shapes.py`, `protocols.py` | Plain data and the contracts between parts. Nothing here does any work. |
| `store.py` | The Store: one SQLite file. |
| `filesource.py`, `dirscan.py` | The FileSource: rules, change detection, the directory scan. |
| `extractors/` | One reader per family (text, PDF, Office, image) and the set that picks between them. |
| `indexer.py` | Scan, record changes, read what is waiting. Talks only to the three contracts above. |
| `service.py` | LocalService: what every client talks to. Policy and the single writer live here. |
| `cli.py` | The command line, a client of the service. |
| `launcher.py` | The one place outside programs and workers are started; ends a program with everything it started. |
| `sandbox.py`, `worker.py`, `jobs.py` | The Sandbox: risky work in a worker process with limits, and what crosses the boundary. |
| `suggest.py` | Suggestions for what to skip (a proposal for the user, never applied by itself). |
| `settings.py`, `kinds.py`, `datadir.py`, `lockfile.py`, `doctor.py` | Settings, file kinds, the data folder, the run lock, the health report. |

## License

MIT. See `LICENSE`.
