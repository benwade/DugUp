# DugUp

**Windows: [Download the installer](https://github.com/benwade/DugUp/releases/latest)** — choose `DugUp-Setup-….exe` under **Assets**. The green **Code** button downloads the source code, so you can read it, understand how DugUp works, and adapt it to your needs.

Find files by what you remember about them: words in a document, text on a scanned receipt, the meaning of a passage, or what a picture shows. Choose your folders, let DugUp index them, then search from its window or with **Win+Shift+F**.

Your files stay on your computer, where all indexing and search processing happens. No account, telemetry or crash reporting — and no cloud service needed to search your files.

**Beta 0.2.8 · Windows 11 · Linux supported, with less desktop testing**

## What you can do

- **Find documents by the words they contain.** Search file names and text in PDFs, Office and OpenDocument files, saved e-mails (.eml), plain text and HTML. Older Office files (.doc, .xls, .ppt, .rtf) are read too when the free LibreOffice is installed.
- **Read words in images.** Find receipts, screenshots and scanned pages by the text on them, using OCR.
- **Search by meaning.** Ask in your own words, or combine meaning with word matches.
- **Find pictures by appearance.** Describe a scene or find pictures like one you already have.
- **Organize pictures and copies.** Preview descriptive photo names, review similar pictures, and find identical files before removing extras.
- **Search from anywhere.** Keep DugUp in the tray and press **Win+Shift+F** to bring up a small search box.
- **Let Claude search for you (optional).** Turn on cloud search access, choose the folders it may see, and ask Claude to find files in a conversation. It can search, never change anything.

## Get started

1. [Download and run the Windows installer](https://github.com/benwade/DugUp/releases/latest). It installs for your account without administrator rights, with Python included.
2. Choose where to keep the program and its index. **Keep the index in a private folder that your cloud-sync or backup apps do not upload.**
3. Choose optional downloads for meaning search, picture search and photo naming — about **2 GB together**. **Download everything now** is selected by default; you can defer the downloads until you need those features.
4. Open **DugUp** from the Start menu. The setup helps you choose folders and indexing settings. You can search what has been indexed while the rest is still running, and stop and continue indexing later.

The installer also offers desktop and start-at-login options. **DugUp Remote** in the Start menu controls the tray, opens search, and lets you release model memory when another program needs it.

The installer is unsigned, so Windows may show an *unknown publisher* warning. If you choose to proceed, Windows offers **More info → Run anyway**.

For Linux or a source checkout, see [source installation](docs/technical.md#source-installation).

## Everyday use

**Search:** type words in the search box and press Enter. Use quotes for a phrase, or **Exact form** to match the word as typed. Narrow results by folder, file type, date or size. Double-click a result to open it.

**Try a different search:** under the search box, **Search by** offers **Words**, **Meaning**, **Words + meaning** and **Pictures**. **Pictures like this** finds images similar to a selected picture. Meaning and picture search need their optional models.

**Describe your filters:** the **In plain words** box turns a request such as “PDFs about nursing from last month over 1 MB” into a search form for you to check. Press Enter when it is ready. This currently understands English.

**Keep folders current:** use the **Index** tab to add folders and update them. The tray can update the index in the background, usually once an hour; on battery power it waits by default. Missing files remain listed and flagged for 30 days; **Only files that are here now** hides unavailable results.

**Ask Claude to search:** tick **Allow cloud search access** in the search box, the tray menu, the **File** menu or **DugUp Remote**. DugUp explains what will leave your computer and asks before it turns on. Then ask Claude, in a conversation linked to your computer, to find what you need. [How cloud search works](docs/user-guide.md#let-claude-search-for-you).

**Rename or clean up:** review names in the **Rename** tab, or use **Find near-duplicates** in the **Pictures** tab. Check the proposed changes before applying them. Similarity is a suggestion: two distinct photos can look almost identical to the model.

More help: [searching, folders and settings](docs/user-guide.md) · [renaming, trash and duplicate cleanup](docs/file-changes.md)

## Privacy and your files

**Local processing keeps you in control.** DugUp searches your documents, reads scans and analyzes pictures on your own computer, without uploading your files to a service. The index stays on your computer too, under your control like the files it helps you find.

As with other unencrypted files, someone with access to your computer and permission to read the data folder can read the index. **The index is not encrypted yet**, and it contains text copied from your files, including files you may later move or remove. If that matters for your use, keep the data folder on an encrypted drive, exclude sensitive folders, or index their names only. DugUp checks for several common sync services, but cannot detect every sync or backup app.

Indexing and searching read your files. Renaming, trashing and duplicate cleanup change files only when requested, with previews or confirmation and records to support undo. Keep backups and retain DugUp’s data folder if you may need its undo records. On Windows, a drive without a Recycle Bin may trigger a shell prompt for permanent deletion; read that dialog before accepting. [Details of file checks and limitations](docs/file-changes.md#file-checks).

**Cloud search access is off unless you turn it on.** When it is on, Claude can search your index, read-only, in the folders you choose. What Claude is shown, file names, paths and the passages that matched, leaves your computer, as a file you attach to a conversation would. Turn it off at any time from the same switch; it takes effect immediately.

Search, OCR and image processing run locally. Optional models are downloaded during setup or when needed. If you deliberately enable a remote server for the experimental plain-words reader, it receives your query and indexed folder paths, but not your files. [Network and storage details](docs/technical.md#network-and-privacy).

## Status and help

DugUp is a beta with limited real-world use. Windows has had the most desktop use; automated tests also run on Ubuntu with Python 3.10, 3.12 and 3.13. OCR works best on clear printed text; handwriting and blurred photos are often missed. The practical limit for near-duplicate scans above about 100,000 pictures has not been established.

**Something wrong? Choose Report a problem.** It is in the Start menu, the search box, the tray menu, DugUp Remote and the main window, and it works even when DugUp is stuck. It gathers what helps find the problem, shows you all of it, and lets you choose how to send it. [How reporting works](docs/user-guide.md#report-a-problem). See [recent changes](docs/changes.md) for release notes and performance work.

## Technical information

- **[Source installation](docs/technical.md#source-installation):** set up Python and optional dependencies on Windows or Linux.
- **[Data storage](docs/technical.md#data-storage):** find or move the folder holding the index, settings, models and undo records.
- **[Models and search](docs/technical.md#models-and-search):** understand model sizes, passage indexing and how word and meaning results are combined.
- **[Picture search](docs/technical.md#picture-search):** see how visual models are indexed and compared.
- **[Cloud search for assistants](docs/technical.md#cloud-search-for-assistants-mcp):** see what the MCP server offers Claude, the rules it follows, and how it connects.
- **[File checks](docs/file-changes.md#file-checks):** understand change records, file identity checks, protected folders and filesystem limitations.
- **[Architecture](docs/technical.md#architecture), [tests](docs/technical.md#tests) and [source layout](docs/technical.md#source-layout):** explore the shared service interface and contribute to the code.

DugUp was inspired by the macOS app [DigUp](https://github.com/ARahim3/DigUp). It is a separate Python implementation, written from scratch and sharing no code with it.

## License

Copyright Ben Wade. DugUp uses the [PolyForm Noncommercial 1.0.0 license](LICENSE): you may use, copy, modify and share it for noncommercial purposes with the required notices. Commercial use needs a separate agreement with the author.

Like much independently published software, DugUp is provided “as is,” without warranty. The license’s **No Liability** section limits the author’s liability as far as the law allows; see the [full terms](LICENSE).
