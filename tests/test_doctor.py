import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dugup import cli, doctor
from dugup.doctor import FAIL, INFO, OK, WARN, Environment, has_failure, render, run_checks
from helpers import CJK, E_ACUTE

IMPORTS_ALL = {"tkinter", "rapidocr", "pypdfium2", "watchdog", "rapidfuzz"}


def fake_env(imports=IMPORTS_ALL, programs=("tesseract", "soffice"), files=(), platform="linux",
             python=(3, 12), fts5=True, program_dirs=()):
    on_path = {name: f"/usr/bin/{name}" for name in programs}
    return Environment(
        which=lambda name: on_path.get(name),
        can_import=lambda name: name in imports,
        exists=lambda path: path in set(files),
        platform=platform,
        python_version=python,
        program_dirs=list(program_dirs),
        sqlite_version="3.45.1",
        has_fts5=lambda: fts5,
    )


def by_name(checks):
    return {c.name: c for c in checks}


def test_a_fully_equipped_machine_has_no_failure_and_no_warning():
    checks = run_checks(fake_env())
    assert not has_failure(checks)
    assert [c.name for c in checks if c.status == WARN] == []


def test_missing_fts5_is_a_failure():
    checks = by_name(run_checks(fake_env(fts5=False)))
    assert checks["sqlite fts5"].status == FAIL


def test_old_python_is_a_failure_with_both_versions_named():
    check = by_name(run_checks(fake_env(python=(3, 8))))["python"]
    assert check.status == FAIL and "3.8" in check.detail and "3.10" in check.detail


def test_one_ocr_engine_missing_is_only_a_warning():
    checks = by_name(run_checks(fake_env(programs=("soffice",))))   # no tesseract
    assert checks["ocr: tesseract"].status == WARN
    assert checks["ocr engine"].status == WARN
    assert not has_failure(checks.values())
    checks = by_name(run_checks(fake_env(imports=IMPORTS_ALL - {"rapidocr"})))
    assert checks["ocr: rapidocr"].status == WARN and not has_failure(checks.values())


def test_no_ocr_engine_at_all_is_a_failure():
    checks = by_name(run_checks(fake_env(imports=IMPORTS_ALL - {"rapidocr"}, programs=("soffice",))))
    assert checks["ocr engine"].status == FAIL


def test_tesseract_is_found_in_the_program_files_folder_when_not_on_the_path():
    exe = os.path.join("PF", "Tesseract-OCR", "tesseract.exe")
    env = fake_env(programs=("soffice",), files=[exe], platform="win32", program_dirs=["PF"])
    check = by_name(run_checks(env))["ocr: tesseract"]
    assert check.status == OK and check.detail == exe


@pytest.mark.parametrize("program", ["soffice", "libreoffice"])
def test_libreoffice_is_found_on_the_path_under_either_name(program):
    assert by_name(run_checks(fake_env(programs=("tesseract", program))))["libreoffice"].status == OK


def test_libreoffice_in_program_files_counts():
    exe = os.path.join("PF", "LibreOffice", "program", "soffice.exe")
    env = fake_env(programs=("tesseract",), files=[exe], platform="win32", program_dirs=["PF"])
    assert by_name(run_checks(env))["libreoffice"].status == OK


def test_missing_libreoffice_is_a_warning_that_names_the_fallback():
    check = by_name(run_checks(fake_env(programs=("tesseract",))))["libreoffice"]
    assert check.status == WARN and "native" in check.detail


def test_microsoft_office_is_only_informational_and_windows_only():
    assert by_name(run_checks(fake_env()))["microsoft office"].status == INFO
    word = os.path.join("PF", "Microsoft Office", "root", "Office16", "WINWORD.EXE")
    env = fake_env(platform="win32", files=[word], program_dirs=["PF"])
    assert by_name(run_checks(env))["microsoft office"].status == OK
    env = fake_env(platform="win32", program_dirs=["PF"])
    assert by_name(run_checks(env))["microsoft office"].status == INFO


@pytest.mark.parametrize("module,name", [("tkinter", "tkinter"), ("watchdog", "watchdog"), ("rapidfuzz", "rapidfuzz")])
def test_optional_libraries_missing_are_warnings(module, name):
    checks = by_name(run_checks(fake_env(imports=IMPORTS_ALL - {module})))
    assert checks[name].status == WARN
    assert not has_failure(checks.values())


def test_missing_pdf_reader_is_a_failure():
    checks = by_name(run_checks(fake_env(imports=IMPORTS_ALL - {"pypdfium2"})))
    assert checks["pdf reader"].status == FAIL


def test_data_folder_check_passes_refuses_and_can_be_skipped(tmp_path):
    assert by_name(run_checks(fake_env()))["data folder"].status == INFO
    assert by_name(run_checks(fake_env(), tmp_path / "ok"))["data folder"].status == OK
    bad = by_name(run_checks(fake_env(), tmp_path / "backup" / "x", never_write=[tmp_path / "backup"]))["data folder"]
    assert bad.status == FAIL and "never write" in bad.detail


def test_doctor_does_not_create_the_data_folder(tmp_path):
    target = tmp_path / "should-not-appear"
    run_checks(fake_env(), target)
    assert not target.exists()


def test_report_uses_plain_status_words_and_lists_every_check():
    checks = run_checks(fake_env(programs=("soffice",)))
    text = render(checks)
    assert len(text.splitlines()) == len(checks)
    assert all(c.name in text for c in checks)
    assert chr(0x2713) not in text and chr(0x2717) not in text   # no symbols a legacy console cannot show


# ---- the command line --------------------------------------------------------------------

def run_cli(argv, env, environ=None):
    out = io.StringIO()
    code = cli.main(argv, env=env, stdout=out, environ=environ if environ is not None else {})
    return code, out.getvalue()


def test_exit_code_is_0_when_ready_and_1_when_something_failed(tmp_path):
    code, text = run_cli(["doctor", "--data-dir", str(tmp_path / "d")], fake_env())
    assert code == 0 and "[  ok]" in text
    code, text = run_cli(["doctor", "--data-dir", str(tmp_path / "d")], fake_env(fts5=False))
    assert code == 1 and "FAIL" in text


def test_a_refused_data_folder_is_reported_not_raised(tmp_path):
    code, text = run_cli(["doctor", "--data-dir", str(tmp_path / "b" / "d"), "--never-write", str(tmp_path / "b")], fake_env())
    assert code == 1 and "never write" in text


def test_never_write_also_comes_from_the_environment(tmp_path):
    environ = {"DUGUP_NEVER_WRITE": str(tmp_path / "b"), "DUGUP_DATA_DIR": str(tmp_path / "b" / "d")}
    code, text = run_cli(["doctor"], fake_env(), environ)
    assert code == 1 and "never write" in text


def test_a_legacy_console_cannot_crash_the_report_when_the_folder_name_has_japanese(tmp_path):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
    folder = tmp_path / f"caf{E_ACUTE} {CJK}"
    code = cli.main(["doctor", "--data-dir", str(folder)], env=fake_env(), stdout=console, environ={})
    console.flush()
    text = raw.getvalue().decode("cp1252")
    assert code == 0
    assert "\\u65e5" in text          # shown as an escape, not a crash
    assert f"caf{E_ACUTE}" in text    # what the console can show is shown as is


def test_ctrl_c_during_the_checks_stops_cleanly(monkeypatch, tmp_path):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_checks", interrupted)
    code, text = run_cli(["doctor", "--data-dir", str(tmp_path / "d")], fake_env())
    assert code == 130 and "Interrupted" in text


def test_the_real_environment_can_be_probed_without_error():
    checks = run_checks(Environment())
    assert checks and all(c.status in {OK, WARN, FAIL, INFO} for c in checks)


def test_running_the_module_prints_a_report_and_no_traceback(tmp_path):
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = dict(os.environ, PYTHONPATH=src, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "-m", "dugup", "doctor", "--data-dir", str(tmp_path / "d")],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    assert "Traceback" not in done.stderr
    assert done.returncode in (0, 1)
    assert "python" in done.stdout and "sqlite fts5" in done.stdout
