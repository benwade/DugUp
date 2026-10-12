# DugUp

Local file search for Windows and Linux: find your files by the words inside them. Everything stays on
your machine.

DugUp was inspired by the macOS app [DigUp](https://github.com/ARahim3/DigUp) but is a separate design,
written from scratch in Python; it shares no code with it.

Status: early. Words in files, and words in pictures and scanned pages (OCR), work and are tested on
Windows 11 and Linux, from a search window or the command line. Search by meaning and photo search are
planned and not built yet.

## What works today

- Choose folders, index them, search by the words in files and in file names.
- Reads plain text and HTML, PDF text layers, docx/xlsx/pptx and odt/ods/odp.
- Suggests what to skip (downloaded requirements, tool caches, trash, build output) and never skips by itself.
- Reads the words in pictures and scanned PDF pages (`dugup ocr`), so a photo can be found by the sign in it.
  Document pages are read first, then scans and screenshots, then photos, and tiny graphics last.
- A typed PDF page that holds a sizeable picture is read by OCR as well, for the words inside the figure.
- A file or page that runs out of time is read once more, after everything else, with double the time.
  A run of crashes or timeouts pauses the run instead of writing off the queue.
- Disk guards: a run stops when the drive holding the index is nearly full, and you can give the index a
  spending limit (`dugup space --limit MB`). Searching keeps working either way.
- Every file ends in a stated state. A scan or photo is `partial` ("pages wait for OCR") until `dugup ocr`
  has read it; a file that cannot be read says why.
- Stop with Ctrl+C and run again to continue. Deleted files stay findable, flagged, for 30 days.
- Files are parsed, and pictures are read, in separate worker processes with a time limit, so a damaged or
  hostile file can cost one worker and never the index. The workers have no network access.

## Set up

Python 3.10 or newer. From the folder you cloned into:

Windows (PowerShell):

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements-dev.txt
    pip install -e .
    pip install -r requirements-ocr.txt     (optional: to read pictures and scanned pages)

Linux:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    pip install -e .
    pip install -r requirements-ocr.txt     (optional: to read pictures and scanned pages)

The OCR engine is RapidOCR. Its models come inside the package, so nothing is downloaded when it runs.
It reads printed words well. Drawn or handwritten lettering and blurred photos are often missed.

## Use

The window:

    dugup window        (or: dugup-window, which opens without a console)

Type words and press Enter. Narrow by folder, by where the words are (text, file name, text read by OCR),
by kind of file, by date and by size. Words are matched by their stem, so "education" also finds
"educate"; tick "Exact form" (or use `dugup search --exact`) for the very form you typed. Put a phrase in double
quotes to find it as typed. Without quotes, files where your words stand together rank above files where
they are far apart. Click a column header to sort; click up to three and the earlier
ones become the second and third sort keys. Double-click a result to open it; the buttons under the list
open it for editing, show it in its folder, or copy its path. The Index tab adds folders, brings the index
up to date and reads pictures and scans, with progress and a Stop button.

### Search in plain words

The window's "In plain words" box turns a sentence into the search form. Type "PDFs about nursing from
last month over 1 MB" and it fills in the words, the type, the dates and the size for you to check; nothing
is searched until you press Enter. It understands kinds of file, sizes, times (yesterday, last week, in
March, since 2026-01-15, in 2024), "in the file name", "on a sign" or "in a picture", "the exact word",
quoted phrases, and the names of your indexed folders. English only.

This is done by rules built into the program: nothing to install, and the same answer every time.

Experimental: a small language model can read the sentence instead. `dugup models install` shows what
would be downloaded (a model server and a model file, about 300 MB, each checked against a known
checksum) and `dugup models install --yes` fetches them; that download is the only time DugUp uses the
internet. Set `intent_reader = "model"` in `config.toml` to use it. On the smallest model, tried without
any training for this job, the rules were clearly more reliable, so the rules are the default.
`intent_url` points at a model server you run yourself (LM Studio, for example) instead.

`dugup models install --model larger` fetches a mid-size model instead (2.2 GB, MIT licence). It needs about
3 GB of memory and, without a graphics card, probably several seconds a sentence (an estimate, not yet
measured). `dugup ask "pdfs about budgets from last month"` shows how a sentence fills in the form and how
long that took; add `--reader model` or `--reader rules` to compare the two.

The command line:

    dugup doctor
    dugup roots add "D:\Some Folder"
    dugup roots suggest "D:\Some Folder"              (what may not be worth indexing; changes nothing)
    dugup roots suggest "D:\Some Folder" --apply-safe (skip downloaded requirements and tool data)
    dugup roots exclude "D:\Some Folder" /Trash Temp  (skip anything else; --undo takes it back)
    dugup index
    dugup ocr                                         (read pictures and scanned pages; --minutes N to limit it)
    dugup search invoice 2024
    dugup status --attention
    dugup space                                       (what the index uses; --limit MB sets a spending limit)
    dugup space --compact                             (give back space left by removed and re-read text)
    dugup ask "word files about rent, over 1 MB"      (how a sentence is read; changes nothing)

The index keeps each piece of text once, compressed (`text_compression = "zlib"` in `config.toml`; `"none"`
turns it off for new text). Searching is unaffected: the word index is built from the text as written. An
index from an earlier version is converted the first time it is opened, which can take a minute on a
large one; run `dugup space --compact` afterwards to hand the freed space back to the disk.

`python -m dugup ...` does the same. The index lives in one per-user data folder (`%LOCALAPPDATA%\DugUp`
on Windows, `~/.local/share/DugUp` on Linux); add `--data-dir FOLDER` to any command to keep it somewhere
else. Deleting that folder removes everything DugUp stored. Settings go in `config.toml` in the data
folder; see `src/dugup/settings.py` for the names and defaults. The ones most likely to matter:

    reader_seconds = 60        # time for one file, plus reader_seconds_per_mb (2) for each megabyte
    ocr_seconds = 120          # time for one page or picture
    retry_patience = 2.0       # the second try gets this many times the limit; 1 turns second tries off
    max_index_mb = 0           # spending limit for the index; 0 means none (dugup space --limit sets it)
    min_free_mb = 200          # a run stops when the drive has less than this free
    figure_fraction = 0.1      # a picture this share of a typed page gets the page read by OCR too; 0: off

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
| `window.py`, `desktop.py` | The search window (tkinter), another client of the service, and handing a file to the desktop. |
| `launcher.py` | The one place outside programs and workers are started; ends a program with everything it started. |
| `sandbox.py`, `worker.py`, `jobs.py` | The Sandbox: risky work in a worker process with limits, and what crosses the boundary. |
| `ocr.py`, `ocrqueue.py` | The OcrBackend (the engine, in a worker) and the queue that decides what is read first. |
| `plainwords.py` | A sentence to a filled-in search form, by rules (the default). |
| `intent.py`, `models.py` | Experimental: the same through a small language model, and fetching that model and its runtime. |
| `suggest.py` | Suggestions for what to skip (a proposal for the user, never applied by itself). |
| `settings.py`, `kinds.py`, `datadir.py`, `lockfile.py`, `doctor.py` | Settings, file kinds, the data folder, the run lock, the health report. |

## License

MIT. See `LICENSE`.
