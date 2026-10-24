# Renaming and removing files

DugUp can help organize pictures and remove extra copies. You choose what changes and review the proposal before it acts. Removing files asks you to confirm; renaming, which **Undo** reverses, happens with one click.

[Back to the README](../README.md) · [User guide](user-guide.md) · [Technical information](technical.md)

## Before changing files

**Keep backups of files that matter, and keep DugUp's data folder if you may need its undo records.** The search index can be rebuilt; a lost change record cannot be recreated from it.

Protected folders are excluded from proposed changes. Use the `cleanup_protect` setting to protect folders, especially a live sync or backup of another computer. Removing a file from a synced folder can also remove it elsewhere.

On Windows, removal uses the shell's trash operation. A file on a cloud drive may go to that service's trash. **On a drive without a Recycle Bin, Windows may ask to delete permanently. Read that dialog before accepting.** For exact-copy cleanup on other systems, DugUp uses `send2trash` where available, or moves files into a **DugUp removed** folder on that drive.

Undo depends on the records, the files still available, and the checks described under [File checks](#file-checks). For files sent to the trash, keep the trash contents as well as the records if you may need to restore them.

## Trashing selected files

Move selected search results to the trash after reviewing the confirmation.

1. Select files in the result list or picture grid. Use **Ctrl-click** to add or remove selections, **Shift-click** for a range, or **Ctrl+A** for all displayed rows.
2. Choose **To trash** beside the open-file controls.
3. Review the named files and confirm if the selection is right.

DugUp skips protected files, records what happened under `removed` in its data folder, and removes successfully trashed files from the current results.

For a manual picture cleanup, select a picture, choose **Pictures like this**, then select the extras you want to trash. Similar-looking pictures may contain differences you want to keep, so review them first.

## Near-duplicate pictures

Find visually similar images, choose which ones to keep, and review the extras before removing them.

Near-duplicates can be resized copies, re-saved pictures or separate shots of the same scene. This feature compares appearance; it does not require identical bytes.

1. Open the **Pictures** tab and choose **Find near-duplicates**.
2. Review the similarity threshold. The default is `0.97`.
3. Look through each group. DugUp marks the proposed keeper **KEEP** in green and the extras in red.
4. To choose a different keeper, select it and use **Keep this one instead**.
5. Choose **Trash the extras**, review the confirmation, and proceed if the choices are right.

The default keeper is chosen in this order: a picture in a preferred folder, then the larger file, then the older one, then the shorter path. The displayed reason explains the choice. Set `cleanup_prefer` to name preferred folders. Protected pictures are shown separately and are not proposed for removal; a group needs at least two eligible pictures.

A score of `0.97` is a useful starting point, not a verdict. Two shots taken a second apart may score that highly. Screenshots and scanned pages may also cluster because their layouts look alike. The practical scale limit above about 100,000 pictures has not been established.

Pictures changed since indexing are marked **changed** and excluded as both keepers and extras until indexed again. DugUp rechecks each extra and its keeper before removal. Choosing a different keeper does not bypass those checks.

### Command line

This previews groups without moving files:

```powershell
dugup alike duplicates --threshold 0.97
```

Use `--protect FOLDER` and `--prefer FOLDER` to apply folder choices to a run. To remove the extras, add `--remove --record FILE`; DugUp shows the list and asks for confirmation. Choose a record path you will keep.

## Naming pictures

Turn camera-generated names into descriptions you can review and edit before applying.

For example, `IMG_4471.jpg` might become a name based on “Green car parked in front of yellow building” and the date taken. Captions are generated locally using the optional captioning model.

### In the window

1. Select pictures and open the **Rename** tab. It previews the selected picture as you change your selection.
2. Use **Preview** for the current results, or choose a folder. When previewing results, only the previously selected pictures start checked.
3. Review each thumbnail, caption and proposed name. Edit names or change the checkboxes to choose which pictures will be renamed.
4. Choose **Rename as shown**. The pictures are renamed at once and the status line says how many. A window appears only if a picture could not be renamed, saying which and why.

Captioning runs in the background with a **Stop** button. Applied renames are recorded under `renames` in the data folder. **Undo last rename** puts the names back using that record, also with one click. The index follows a successful rename, so the new name can be searched without rereading the file.

By default, DugUp proposes names only for pictures with camera or automatic names, such as `IMG_4471`, `DSC_0012`, timestamps, screenshots or numbers. Choose **Also pictures with real names** to include pictures already given descriptive names.

### Choose a naming pattern

The default `rename_pattern` is `{caption} {taken}`. You can combine these fields:

| Field | Meaning |
|---|---|
| `{caption}` | A description made safe for a file name, normally shortened to about ten words at a phrase boundary. |
| `{taken}` | Date taken from the picture's metadata, or its saved date when unavailable. |
| `{time}` | Time taken, or the file's modification time when unavailable. |
| `{camera}` | Camera information from metadata. |
| `{folder}` | Folder name. |
| `{original}` | Current file name without its extension. |
| `{n}` | Sequence number. |
| `{ext}` | File extension. The extension is kept when the pattern does not specify it. |

If several proposed names match, DugUp adds numbers. A destination name taken by another file is checked again when applying the rename; see [filesystem limitations](#file-checks).

### Command line

Replace the folder and record paths with your own. The first command previews; the second applies the proposals after confirmation:

```powershell
dugup rename "D:\Photos"
dugup rename "D:\Photos" --apply --record "D:\rename-record.txt"
```

To attempt to restore the previous names:

```powershell
dugup rename --undo "D:\rename-record.txt"
```

Undo processes the record in reverse order and skips files that fail its checks. Use `--pattern` to try another pattern, `--all` to include descriptive names, `--longer` for longer captions, or `--no-caption` to use only the other fields. See [caption models](technical.md#caption-model) for download and performance details.

## Exact-copy cleanup

Find identical files on one drive and decide where each kept copy belongs.

This is different from near-duplicate picture search: DugUp compares the bytes of each extra and its keeper again before moving the extra. The report is limited to one drive, so a file on your computer and its backup on another drive are not treated as a pair to clean up.

### Review before removing

1. Add and index the drive. Names-only folders do not have content fingerprints and do not participate.
2. Optionally run `dugup roots copies G:\` for an overview of folders that look like copies, based on names and sizes. This is an initial estimate, not proof of identical contents.
3. Set `cleanup_protect` for folders that should be left alone. Separate multiple folder paths with `;`. Check any warning that a protected path has no indexed files; the path may be wrong.
4. Set `cleanup_prefer` for folders where kept copies should live. Otherwise, DugUp prefers the copy in the larger group of files, then the one higher in the folder structure.
5. Review the report and adjust those choices until the proposed keepers make sense.

For example, this reports duplicates on drive G without moving them:

```powershell
dugup duplicates G:\
```

Add `--save FILE` to save the report. `--protect FOLDER` and `--prefer FOLDER` set folder choices for that run.

### Check the trash, then remove extras

Use an expendable file to check how the target drive handles removal:

```powershell
dugup duplicates G:\ --try-trash "G:\throwaway-file.txt"
```

**This moves the named file. Use a file you created for this check.** Confirm where it went before continuing with a cleanup.

When the report is right, choose a record path and run:

```powershell
dugup duplicates G:\ --remove --record "D:\duplicate-cleanup-record.txt"
```

DugUp displays the proposal and asks you to type `yes`. It leaves an extra alone if its keeper is missing or their bytes no longer match. The record lists moved and skipped files with reasons. **Ctrl+C** stops between files.

Run `dugup index` afterward to update the index. Keep the record and any removed files you may want to restore.

## File checks

DugUp records planned changes and rechecks files and folder protections before acting.

These checks apply to trashing, renaming, undo and duplicate cleanup:

- **Record before acting.** The planned change is written and flushed to disk before the file is touched. If that fails, DugUp leaves the file alone and stops the operation.
- **Report recording failures.** If the file changed but the completion record cannot be written, DugUp reports the change and stops further work. The earlier planned-change record remains available.
- **Recheck file identity.** Size, modification time and, where available, file ID are compared with the indexed and previewed file. A changed or replaced file is skipped. These checks also apply when selecting a different keeper and when undoing changes.
- **Recheck protections.** Protected folders are checked when the change happens, including during undo.
- **Check destination names.** On Windows and common Linux filesystems, renaming uses an operation that refuses an existing destination atomically. On filesystems without either supported safe operation, the fallback has a brief race window in which another program could create the same name.
- **Read older records.** Undo supports records from earlier versions, subject to the same file and protection checks.

These are specific checks, not a promise that every possible change to a file will be detected. Power loss during a file change has not been tested.

The index and change records live in [DugUp's data folder](technical.md#data-storage). Sync detection has limits, so **choose a private data folder that your sync or backup apps do not upload**. Ordinary logs contain counts and reason codes; change records contain file names because they are needed to identify and undo operations.
