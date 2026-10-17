"""A runtime that offers a way to a graphics card is not a card. Only a route that really reaches one counts,
and a report names the device a model really ran on."""

import sys
import types

import pytest

from dugup import embedding, jobs


def _runtime(monkeypatch, tmp_path, offered, reached):
    """Stand-in onnxruntime (offering `offered`, really running on `reached`) and rapidocr (with one model)."""
    (tmp_path / "rapidocr").mkdir()
    (tmp_path / "rapidocr" / "small.onnx").write_bytes(b"\0" * 10)
    made = []

    class Session:
        def __init__(self, model, options=None, providers=()):
            made.append(list(providers))
            self.providers = [reached if reached in providers else "CPUExecutionProvider", "CPUExecutionProvider"]

        def get_providers(self):
            return self.providers

    ort = types.SimpleNamespace(get_available_providers=lambda: list(offered), InferenceSession=Session,
                                SessionOptions=lambda: types.SimpleNamespace(log_severity_level=2),
                                set_default_logger_severity=lambda level: None)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "rapidocr", types.SimpleNamespace(__file__=str(tmp_path / "rapidocr" / "__init__.py")))
    monkeypatch.setattr(jobs, "_card_checked", {})
    return made


def test_a_runtime_that_offers_directml_without_a_card_has_no_card(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path, ["DmlExecutionProvider", "CPUExecutionProvider"], reached="CPUExecutionProvider")
    assert jobs.gpu_provider() == ""
    assert jobs.ocr_device({}) == {"card": "", "provider": ""}


def test_a_card_that_is_really_reached_counts(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path, ["DmlExecutionProvider", "CPUExecutionProvider"], reached="DmlExecutionProvider")
    assert jobs.gpu_provider() == "DmlExecutionProvider"
    assert jobs.ocr_device({})["card"] == "graphics card (DirectML)"


def test_the_card_is_tried_once(monkeypatch, tmp_path):
    made = _runtime(monkeypatch, tmp_path, ["DmlExecutionProvider", "CPUExecutionProvider"], reached="DmlExecutionProvider")
    jobs.gpu_provider()
    jobs.gpu_provider()
    assert len(made) == 1


def test_the_plain_runtime_has_no_way_to_a_card(monkeypatch, tmp_path):
    made = _runtime(monkeypatch, tmp_path, ["CPUExecutionProvider"], reached="CPUExecutionProvider")
    assert jobs.gpu_provider() == "" and made == []


@pytest.mark.parametrize("first,said", [("CPUExecutionProvider", "processor"),
                                        ("DmlExecutionProvider", "graphics card (DirectML)"),
                                        ("CUDAExecutionProvider", "graphics card (CUDA)")])
def test_a_report_names_the_device_really_used(first, said):
    session = types.SimpleNamespace(get_providers=lambda: [first, "CPUExecutionProvider"])
    assert embedding.device_used(session, "graphics card (DirectML)") == said


def test_doctor_reads_only_the_answer_not_a_runtimes_banner(monkeypatch):
    """Seen on a machine without a card: the runtime printed a banner of asterisks after falling back, and
    doctor took the banner for the card's name."""
    from dugup import doctor
    banner = "*************** EP Error ***************\nNo devices detected\n****************************************\n"
    for printed, card in ((banner + "CARD:\n", ""), ("CARD:graphics card (DirectML)\n" + banner, "graphics card (DirectML)"),
                          (banner, "")):
        done = types.SimpleNamespace(returncode=0, stdout=printed)
        monkeypatch.setattr("subprocess.run", lambda *a, _done=done, **k: _done)
        assert doctor.find_ocr_card() == card


def test_doctor_says_none_found_when_the_card_runtime_is_already_installed():
    from dugup.doctor import Environment, run_checks
    yes = lambda name: True
    with_runtime = {c.name: c for c in run_checks(Environment(can_import=yes, ocr_card=lambda: "", directml=lambda: True))}
    assert "none found" in with_runtime["ocr: graphics card"].detail
    assert "pip install" not in with_runtime["ocr: graphics card"].detail, "no advice to install what is there"
    plain = {c.name: c for c in run_checks(Environment(can_import=yes, ocr_card=lambda: "", directml=lambda: False))}
    assert "onnxruntime-directml" in plain["ocr: graphics card"].detail
