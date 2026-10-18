# Technical information

Source setup, data storage, model behavior and implementation details for people who want to understand or adapt DugUp.

[Back to the README](../README.md) · [User guide](user-guide.md) · [Renaming and removing files](file-changes.md)

## Source installation

Run DugUp from a Python environment on Windows or Linux, with optional packages for the features you need.

Use Python 3.10 or newer. The Windows installer includes its own Python; these steps are for a source checkout instead. Run them from the repository folder.

On Windows, you can install and run through the virtual environment's Python without changing PowerShell's activation policy:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m dugup window
```

Install optional features as needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
.\.venv\Scripts\python.exe -m pip install -e ".[meaning,tray]"
```

The OCR requirements add text recognition for images and scanned pages. The `meaning` extra adds the model runtime used by meaning and picture search; the `tray` extra includes the tray icon dependencies and Pillow; the `assistant` extra adds the MCP library for [cloud search](#cloud-search-for-assistants-mcp). The corresponding model files are separate downloads.

On Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install -r requirements-ocr.txt
python -m pip install -e '.[meaning,tray]'
python -m dugup window
```

For development and tests, also install `requirements-dev.txt` using that environment's Python.

On Windows, `DugUp.cmd` and `DugUp Remote.cmd` open the window and remote without a console. You can create desktop shortcuts to them. Update a source checkout with `git pull`; if dependencies have changed, rerun the relevant installation commands.

By default, a source checkout stores its data in `data` inside the checkout. **Keep that folder out of cloud uploads**, just as you would the installed program's index. See [data storage](#data-storage).

## Data storage

The index, settings, models, logs and undo records share one configurable data folder.

DugUp chooses the folder in this order:

1. An explicit `--data-dir FOLDER` option or `DUGUP_DATA_DIR` environment variable.
2. The location described by `dugup-data-folder.txt` beside the program. Installed copies and source checkouts include this file; normally it points to a `data` subfolder. The installer can specify a separate index location instead. The marker also identifies program folders to exclude from indexing.
3. The per-user default: `%LOCALAPPDATA%\DugUp` on Windows or `~/.local/share/DugUp` on Linux.

Run this to see the active location and why it was chosen:

```powershell
dugup data where
```

If the normal folder beside the program is detected as cloud-synced, DugUp uses the per-user folder instead and explains that choice when opening. To choose another location through the installer, uninstall while keeping the data, then reinstall and choose the new index location. DugUp can then offer to move the existing data.

### Existing indexes and moves

If DugUp finds an older index in the per-user folder, it continues using it and offers to move it beside the program when appropriate. You can answer in the window or with:

```powershell
dugup data move
dugup data keep
```

Choose one of those commands according to whether you want to move or keep the existing location. A move waits until the tray or indexing process releases the data, copies the contents, checks the copy, and then removes the old folder. The tray's start-at-login entry is updated for the new location.

**Keep the data folder if you may need undo.** Deleting it removes settings, model downloads and change records as well as the rebuildable index. Deleting the active folder does not remove copies made by your backup or sync software.

### Sync detection

DugUp checks registered locations for OneDrive, Google Drive for desktop, Dropbox and Windows' own sync records. It may miss a location, and it does not detect every backup or sync app. If a list cannot be read, DugUp reports that and continues; `dugup doctor` shows the findings.

Choose a data folder that your own backup and sync configuration will not upload. The program's folder and the data folder can be different locations.

## Network and privacy

DugUp processes files locally, with network access for model downloads, an explicitly enabled remote reader, and cloud search when you turn it on.

Indexing, OCR, meaning search, picture comparison and caption generation run on your computer. There is no account, telemetry or crash reporting. Model downloads use pinned addresses and recorded SHA-256 checksums. They send ordinary download requests, not your indexed file contents. Models can also be placed in the expected data-folder locations manually.

The default plain-words reader uses local rules. An optional model reader uses a local server unless `intent_allow_remote` is deliberately enabled. A remote reader receives the query and indexed folder paths so it can interpret folder names; it does not receive the files themselves.

Cloud search access is off by default. When you turn it on, an assistant such as Claude can search the index read-only, and what it is shown (file names, paths and matching passages) goes to the assistant's service. See [Cloud search for assistants](#cloud-search-for-assistants-mcp).

The index is a SQLite database containing text extracted from your files. **It is not encrypted yet.** Like other unencrypted files, it is readable by anyone with access and permission to read its folder. It may retain text from files moved or removed after indexing. Use an encrypted drive, exclude sensitive folders, or index only their names if this matters for your use.

Ordinary logs contain counts and reason codes rather than document text or file names. Rename and removal records include file names because identifying the changed files is necessary for undo.

### Worker isolation

File parsing and image reading run in separate processes with time limits, allowing a stalled or damaged file to be stopped without ending the indexing run. These workers run with your file permissions. Their network restriction is a Python shim, not an operating-system security boundary. Run DugUp with ordinary user rights; it displays a notice if started as administrator.

## Models and search

Meaning search compares numerical representations of passages, optionally combined with word matches.

The model turns each passage into a vector, also called an embedding. DugUp stores these in the index and compares them with the vector for your query. Results are grouped by file, using each file's closest passage.

### Model and storage choices

| Choice | Download | Notes |
|---|---|---|
| Default meaning model | About 547 MB | More accurate, with higher processing and memory costs. |
| `meaning_model = "small"` | About 90 MB | Measured at roughly seven times faster in earlier testing, with less accurate results. |
| `meaning_size = "compact"` | No separate model download | Stores smaller passage vectors, using about one-third of the vector space with some loss in top-ten results. |

Full-size vectors use about 170 MB for 200,000 passages. These figures describe models and vectors, not the total index size. Speed depends on the computer and workload.

Build meaning information and inspect coverage with:

```powershell
dugup meaning fill
dugup meaning status
```

`fill` downloads the chosen model if needed, then processes eligible files, most recently changed first. **Ctrl+C** stops the run; the next run continues with unfinished work. Folder choices control eligibility. Changing the model or vector size requires new vectors; existing ones are replaced as processing continues.

Manual index updates continue into meaning indexing by default once the model is installed. `meaning_after_index = false` or `--no-meaning` skips that stage. The manual update path checks model readiness rather than fetching a missing model itself. The tray's background update calls the fill operation when eligible work is waiting, so it can download a missing model. Download during setup if you want that completed in advance.

`meaning_gpu` enables supported graphics acceleration. Earlier tests found a substantial speedup for the larger model, but the gain depends on the hardware; unsupported configurations use the processor.

### Query and ranking

```powershell
dugup search --meaning "a receipt for a licence I paid online"
dugup search --both "EZDriveMA receipt from 2018"
```

Meaning-only search ranks files by passage similarity. Combined search adds a word-match contribution: the strongest word result receives up to `meaning_word_weight`, which defaults to `0.5`; weaker matches receive proportionally less. A file found by only one search can still appear.

Set the weight to `0` for meaning alone; values above `1` give words greater influence. `meaning_word_pool` sets the number of word candidates. The word side looks for any useful query term, leaving out terms found in a large share of passages. `meaning_word_everywhere` sets that cutoff, defaulting to `0.3`. The command reports terms it omits.

Folder, type, date and size filters apply. Word-specific filters such as exact form and file-name-only search do not apply to meaning-only results. `--offset` pages through results.

### Candidate selection and warm-up

DugUp first compares the initial 96 dimensions of the vectors to select about 800 likely files, with a limited number of candidate passages per file. It then compares full vectors for the final ranking. Limiting passages per file helps prevent a long document from filling the candidate pool.

`meaning_warm_up = "auto"` loads the query model in the background when the index contains meaning vectors. `"yes"` always warms it up; `"never"` defers loading until needed, saving roughly 600 MB of idle memory for the default model. A search started during loading shows a wait window with a Stop control; stopping that search does not cancel model loading.

```powershell
dugup meaning measure --ask "a sentence"
```

This compares both models on a sample of the existing index, reporting time, storage and results without storing new passage vectors.

## Picture search

CLIP models compare image vectors with a description or another image.

Picture search uses a roughly 352 MB image model and 254 MB text model. Both are checksum-checked downloads stored in the data folder. Source installations need the `meaning` extra and Pillow. Each indexed picture adds about 512 bytes of vector data.

```powershell
dugup alike fill
dugup alike status
dugup search --looks "a dog on a beach"
dugup alike like "D:\Photos\example.jpg"
```

`fill` processes eligible pictures without vectors, most recently changed first, and fetches missing models. It resumes after interruption. A picture that fails is recorded with its reason and is not retried until it changes. A reference image supplied to `like` can be outside the index; DugUp reads it for that comparison.

`looks_readers` controls parallel image reading; `0` chooses a value based on available cores. `looks_gpu` controls supported graphics acceleration. Reading and resizing images can take more time than the model itself, so additional readers can help.

Manual index updates process new pictures once the models are installed unless `looks_after_index` is false or `--no-looks` is supplied. As with meaning indexing, the tray's background fill operation can fetch missing models when work is waiting.

Similarity scores are useful for ordering, not certainty. Earlier guidance used around `0.30` for a strong description match and around `0.20` for a weak one; image-to-image scores around `0.9` often show the same scene, while `0.97` is the default near-duplicate threshold. Distinct images can exceed those values. Use OCR to distinguish pages of text, and review [near-duplicate groups](file-changes.md#near-duplicate-pictures) before removing anything.

Thumbnails in the window are made in the background and kept for the session. To compare behavior on your own images without storing new vectors:

```powershell
dugup alike measure --ask "a dog on a beach" --like "D:\Photos\example.jpg"
```

## Caption model

Florence-2 generates local descriptions for picture renaming when requested.

The model consists of four ONNX parts totaling about 1.1 GB, downloaded and checksum-checked on first use if not installed during setup. Captioning runs in a worker process and uses supported graphics acceleration when `caption_gpu` is enabled. On a processor it can take seconds per picture, so it runs on demand rather than as routine background indexing.

`caption_source = "server"` is reserved for a future server integration and is not implemented. Use the local caption model, or `--no-caption` with a naming pattern based on other fields.

## Cloud search for assistants (MCP)

An MCP server lets Claude, or another assistant on this computer, search the index read-only.

`dugup mcp serve` runs the server over standard input and output. The Claude desktop app starts it from its settings file, `%APPDATA%\Claude\claude_desktop_config.json` on Windows; `dugup mcp connect --write` or the switch's **Connect** step adds the entry. Claude conversations linked to the computer reach the same tools. Some installations of the Claude app keep the file elsewhere; **Settings → Developer → Edit Config** in the app shows it, and `--config FILE` writes there instead.

The server offers three tools, each marked read-only:

| Tool | What it returns |
|---|---|
| `search` | Results by words, meaning, both, or looks, with folder, type and date filters; each with path, type, size, date and the passage that matched. At most 50. |
| `passages` | More places in one file where some words appear, up to 10 passages of a few hundred characters. Never the whole file. |
| `status` | The visible folders, whether an update is running, and meaning and picture coverage. |

The rules live in `assistant.py`:

- **Off unless `mcp_enabled` is true.** The setting is read on every request, so turning it off takes effect immediately.
- **Only the folders in `mcp_folders`**, separated by semicolons; empty means every indexed folder. Results, passages and other copies' paths outside them are never returned, and requests for such a folder are refused.
- **Read-only.** No trash, rename, cleanup or settings changes.
- **One thread for the index.** Every request runs on a single worker thread with its own read connection.
- **Models are released when idle.** A model loaded for a meaning or picture search is released after `mcp_idle_minutes` (default `5`; `0` keeps it loaded).

The entry starts the console Python, since `pythonw.exe` has no standard input and output. It names a data folder only when one was chosen by hand, so the server finds the data folder as other commands do, including after a move. The Windows installer includes the MCP library; a source checkout needs the `assistant` extra.

## Experimental plain-words reader

An optional language model can interpret search requests, but built-in rules remain the default.

The smallest tested model was less reliable than the rules for this task. To inspect the optional download before installing:

```powershell
dugup models install
dugup models install --yes
```

The first command previews the download; the second fetches the server and small model, about 300 MB together, checking their recorded checksums. Set `intent_reader = "model"` in `config.toml` to use the model reader. `intent_url` can point to a model server you run yourself, such as LM Studio.

`dugup models install --model larger` selects the approximately 2.2 GB model instead. Its estimated memory requirement is about 3 GB; processor-only response time has not been measured. To compare readers:

```powershell
dugup ask "pdfs about budgets from last month" --reader rules
dugup ask "pdfs about budgets from last month" --reader model
```

These commands show the interpreted search form and elapsed time without changing your files. See [Network and privacy](#network-and-privacy) before configuring a remote server.

## Indexing and resource settings

Worker counts, time limits and storage settings let DugUp fit different computers and collections.

Manual indexing selects up to eight file readers based on cores and free memory. `index_readers = 1` forces serial processing; higher values set the reader count explicitly. Failure-patience counts apply per reader.

OCR uses RapidOCR, with models included in its package. It defaults to one reader for small queues and can switch to parallel reading once `ocr_fast_pages` pages wait, default `200`. Set that threshold to `0` to disable automatic switching. `--gentle` keeps one reader; `--fast` requests parallel reading. Background tray OCR explicitly uses gentle mode.

OCR defaults to the processor. Source installations on Windows can use `onnxruntime-directml` instead of `onnxruntime` for supported NVIDIA, AMD and Intel cards. Select `--gpu` or `ocr_gpu = true`, and inspect `dugup doctor` for availability. Install only the intended runtime in that environment; the Windows installer already includes a graphics-capable runtime.

| Setting | Default | Purpose |
|---|---|---|
| `reader_seconds` | `60` | Base time allowed per file. |
| `reader_seconds_per_mb` | `2` | Additional seconds per MB. |
| `ocr_seconds` | `120` | Time allowed per page or picture. |
| `retry_patience` | `2.0` | Multiplier for a second attempt; `1` disables longer retries. |
| `max_index_mb` | `0` | Index budget in MB; `0` means unlimited. |
| `min_free_mb` | `200` | Stop a run below this much free disk space. |
| `figure_fraction` | `0.1` | Image share of a typed page that triggers OCR too; `0` disables it. |

Text is stored once and compressed with `text_compression = "zlib"`. `"none"` disables compression for new text. Word indexing still uses the original text. Older indexes are converted when opened; large conversions may take time. Run `dugup space --compact` afterward to reclaim unused space.

`content_marks` controls exact-content fingerprints used to group copies. `read_once` lets copies reuse extracted text and OCR results. Empty files are not grouped as copies, and files with the same bytes but different extensions are processed separately. Older indexes gain content marks on the next update. `main_place` influences which copy is displayed; on Windows the default is inferred from the drive holding your user folders.

Folder suggestions can be adjusted through `book_folders`, `book_folder_level`, `picture_heavy_fraction` and `picture_heavy_min`. See [settings.py](../src/dugup/settings.py) for the complete settings and defaults, or use `dugup config list --all`.

## Architecture

The window, command line, tray and search box call a shared service interface.

`Service` in `protocols.py` defines the operations. `LocalService` implements search, indexing, OCR, meaning and picture processing, renaming and trashing. Policy and the single-writer coordination live in this layer. Tests can substitute an implementation of the same interface.

An application can use the local service directly:

```python
from dugup.datadir import DataLayout, resolve_data_dir
from dugup.service import LocalService
from dugup.settings import load

layout = DataLayout(resolve_data_dir())
settings, _ = load(layout.config)
with LocalService(layout, settings) as dugup:
    for row in dugup.search("nursing license", limit=5).rows:
        print(row.path, row.rank)
```

The same service backs the [MCP server](#cloud-search-for-assistants-mcp) for assistants, through a read-only layer that applies the folder and passage rules.

Window searches use a dedicated search worker and a separate SQLite read connection. Requests carry sequence numbers so stale results are discarded. Only the newest waiting request is retained. This keeps the interface responsive while a search runs; it does not by itself make that search faster.

File-changing operations have their own [recording and identity checks](file-changes.md#file-checks).

## Tests

Run automated checks or generate sample files for manual testing from a development checkout.

After installing `requirements-dev.txt`, run these with the checkout's Python environment:

```powershell
python -m pytest
```

The default configuration excludes tests marked `slow`, `gpu` and `private`. Tests requiring external tools or a particular platform may be skipped when those requirements are absent. A default pass is therefore not a claim that every optional integration was exercised.

To generate a manual test collection, supply an empty folder:

```powershell
python tests/make_corpus.py SOME_EMPTY_FOLDER
```

## Source layout

Modules separate storage, indexing, worker processes and user interfaces.

| Module | Responsibility |
|---|---|
| `shapes.py`, `protocols.py` | Shared data structures and interfaces. |
| `store.py` | SQLite storage. |
| `filesource.py`, `dirscan.py` | Folder rules, directory scanning and change detection. |
| `extractors/` | Readers for text, PDF, Office and image files. |
| `indexer.py` | Scan coordination, change recording and queued extraction. |
| `service.py` | Shared service implementation, policy and writer coordination. |
| `cli/` | Command-line clients, with disk-changing commands in `changes_cmds.py`. |
| `window.py`, `desktop.py` | Main interface and opening files in desktop applications. |
| `launcher.py` | Starting external processes and managing their lifetime. |
| `sandbox.py`, `worker.py`, `jobs.py` | Worker limits and process communication. |
| `ocr.py`, `ocrqueue.py` | OCR workers and scheduling. |
| `plainwords.py` | Rule-based interpretation of search requests. |
| `intent.py`, `models.py` | Experimental model reader and model downloads. |
| `suggest.py` | Proposed indexing exclusions. |
| `settings.py`, `kinds.py`, `datadir.py`, `lockfile.py`, `doctor.py` | Configuration, file types, data paths, locking and diagnostics. |
| `datamove.py`, `startnotes.py`, `elevated.py` | Data moves, startup notices and administrator detection. |
| `tray.py`, `popup.py`, `remote.py` | The tray, the Win+Shift+F search box and DugUp Remote. |
| `assistant.py`, `mcpserver.py` | Cloud search rules and the MCP server that presents them. |
| `assistantsetup.py`, `assistantwindow.py`, `cli/mcp_cmds.py` | Turning cloud search on and off, choosing folders, connecting the Claude app. |
