"""Folders that look like copies of other folders, from names and sizes alone."""

from dugup import copies, picker


def files(folder, count, start=0, size=1000):
    return [(f"{folder}/sub{i % 3}/file{i}.txt", size + i) for i in range(start, start + count)]


def test_names_that_say_backup_and_names_that_do_not():
    for name in ("Backup", "backups", "docs_bak", "Photos - Copy", "Copy of Taxes", "Taxes copy", "Work (copy)", "My Backup 2021", "Letters - Copy (2)"):
        assert copies.backup_name(name), name
    for name in ("Old", "Archive", "Bakery", "Copywriting", "Photocopy shop", "Backupper", "Documents"):
        assert not copies.backup_name(name), name


def test_a_folder_with_the_same_names_and_sizes_is_found_once_at_the_top():
    found = copies.find(files("Docs", 60) + files("Backup/Docs", 60) + files("Other", 60, start=500))
    assert [(p.first, p.second) for p in found] == [("Backup/Docs", "Docs")], "not the sub-folders too, and not Backup itself"
    assert found[0].share > 0.95 and found[0].files == 60
    assert copies.the_copy(found[0]) == "Backup/Docs", "a folder above it is named like a backup"


def test_an_older_backup_with_fewer_files_is_still_found_and_different_sizes_are_not():
    found = copies.find(files("Docs", 300) + files("Docs old", 250))
    assert len(found) == 1 and found[0].share >= 0.8 and copies.the_copy(found[0]) == "", "no name says which is the backup"
    assert copies.find(files("Docs", 60) + files("Edited", 60, size=5000)) == [], "same names, other sizes: not copies"
    assert copies.find(files("A", 10) + files("B", 10)) == [], "a handful of files says too little"
    assert copies.find([("Only/one/deep.txt", 5)] * 1 + files("Only", 40)) == [], "a folder is not a copy of one inside it"


def test_the_picker_leaves_out_what_is_named_a_backup_and_only_notes_the_rest():
    listed = [(f"/r/{rel}", size, False) for rel, size in
              files("Docs", 60) + files("Backup/Docs", 60) + files("Photos", 60, start=900) + files("Photos2", 60, start=900)]
    tree = picker.build("/r", listed)
    plan = picker.plan_from_guess(tree)
    assert not plan.ticked("Backup/Docs") and plan.ticked("Docs") and plan.ticked("Backup")
    assert 'looks like a backup of "Docs"' in plan.reasons["backup/docs"] and "tick it if you want both" in plan.reasons["backup/docs"]
    assert plan.ticked("Photos") and plan.ticked("Photos2"), "no name says which is the backup: both stay, with a note"
    assert 'looks like a copy of "Photos2"' in plan.reasons["photos"] and 'looks like a copy of "Photos"' in plan.reasons["photos2"]
    assert len(plan.copies) == 2
    assert "/Backup/Docs" in plan.to_root().exclude


def test_the_command_lists_pairs_with_full_locations_and_changes_nothing(tmp_path):
    import io
    from dugup.cli import main
    docs = tmp_path / "docs"
    for folder in ("Stuff", "Backup/Stuff"):
        (docs / folder).mkdir(parents=True)
        for i in range(25):
            (docs / folder / f"note{i}.txt").write_text("x" * (i + 1), encoding="utf-8")
    out = io.StringIO()
    code = main(["roots", "copies", str(docs), "--data-dir", str(tmp_path / "data")], stdout=out)
    text = out.getvalue()
    assert code == 0 and "1 set(s) of folders look like copies" in text and "Roughly 25 file(s) are extra copies" in text
    assert str(docs / "Stuff") in text and str(docs / "Backup" / "Stuff") in text, "every copy with its whole location"
    assert "named like a backup" in text.split(str(docs / "Backup" / "Stuff"))[1].splitlines()[0]
    out = io.StringIO()
    assert main(["roots", "list", "--data-dir", str(tmp_path / "data")], stdout=out) in (0, 1) and str(docs) not in out.getvalue()
    out = io.StringIO()
    (docs / "Lone").mkdir()
    assert main(["roots", "copies", str(docs / "Lone"), "--data-dir", str(tmp_path / "data")], stdout=out) == 0
    assert "No folders here look like copies" in out.getvalue()


def test_a_folder_is_paired_with_its_copy_and_not_with_the_larger_folder_that_holds_the_copy():
    """Seen on a real drive: a project and the whole of "Other computers", which held a copy of it."""
    listed = files("Project", 80) + files("Other/PC3/Project", 80) + files("Other/PC3/Rest", 400, start=2000) \
        + files("Other/PC2/More", 300, start=5000)
    found = copies.find(listed)
    assert [(p.first, p.second) for p in found] == [("Other/PC3/Project", "Project")]


def test_tool_data_is_not_compared_at_all():
    """Every Python environment looks like every other; on a real drive that was all 200 pairs."""
    package = lambda where: [(f"{where}/Lib/site-packages/torch/m{i}.py", 500 + i) for i in range(120)]
    listed = [(f"/r/{rel}", size, False) for rel, size in
              package("A/venv") + package("B/.venv") + package("C/env") + files("Docs", 60) + files("Docs copy", 60)]
    assert len(copies.find([(path[3:], size) for path, size, _ in listed])) >= 1
    tree = picker.build("/r", listed)
    assert [(p.first, p.second) for p in tree.copies] == [("Docs", "Docs copy")], "only the folders a person made"
    assert copies.find(package("A/venv") + package("B/venv"), skip=["A/venv", "b/VENV"]) == []


def test_a_folder_kept_in_four_places_is_one_family_and_saved_web_pages_are_not_copies():
    """From a real drive: Desktop in four places came out as six pairs, and the folders a browser writes
    beside saved pages (all holding the same scripts) as dozens."""
    places = ["Desktop", "Personal/Desktop", "Other/PC2/Desktop", "Other/PC3/Desktop"]
    listed = [row for place in places for row in files(place, 60)] + files("Alone", 60, start=3000)
    pages = [(f"Travel/Mail {n}_files/asset{i}.js", 700 + i) for n in range(6) for i in range(40)]
    found = copies.find(listed + pages)
    assert len(found) == 6 and not any("_files" in p.first for p in found)
    groups = copies.families(found)
    assert len(groups) == 1 and groups[0].folders == ("Desktop", "Personal/Desktop", "Other/PC2/Desktop", "Other/PC3/Desktop")
    assert groups[0].files == 60 and copies.named_backups(groups[0].folders) == []
    assert copies.named_backups(("Docs", "Backup/Docs", "Docs - Copy")) == ["Backup/Docs", "Docs - Copy"]
    assert copies.named_backups(("Backup/A", "Backup/B")) == [], "when every name says backup, the names do not choose"
    tree = picker.build("/r", [(f"/r/{rel}", size, False) for rel, size in listed])
    plan = picker.plan_from_guess(tree)
    assert all(plan.ticked(place) for place in places) and len(plan.copies) == 1
    note = plan.reasons["desktop"]
    assert '"Personal/Desktop"' in note and '"Other/PC2/Desktop"' in note and "all are included" in note
