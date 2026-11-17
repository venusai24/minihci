import pytest

from scripts import benchlib


def test_require_real_hosts_refuses(monkeypatch, capsys):
    monkeypatch.delenv("MINIHCI_REAL_HOSTS", raising=False)
    with pytest.raises(SystemExit) as e:
        benchlib.require_real_hosts()
    assert e.value.code == 2
    assert "MINIHCI_REAL_HOSTS" in capsys.readouterr().err


def test_require_real_hosts_allows(monkeypatch):
    monkeypatch.setenv("MINIHCI_REAL_HOSTS", "1")
    benchlib.require_real_hosts()


def test_summarize_empty_is_none():
    assert benchlib.summarize([]) == {"median": None, "p90": None, "worst": None}


def test_summarize_values():
    s = benchlib.summarize([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert s["median"] == 55
    assert s["p90"] == 90
    assert s["worst"] == 100


def test_summarize_single():
    assert benchlib.summarize([7]) == {"median": 7, "p90": 7, "worst": 7}


def test_render_markdown():
    md = benchlib.render_markdown(
        {"Guest size": "1024 MiB"},
        [{"round": 1, "status": "done", "duration_ms": 1500}, {"round": 2, "status": "failed", "duration_ms": None}],
        benchlib.summarize([1500]),
    )
    assert "Guest size" in md and "1024 MiB" in md
    assert "| round | status | duration_ms |" in md
    assert "| 2 | failed | - |" in md
    assert "median" in md and "1500" in md


def test_render_markdown_empty_never_invents():
    md = benchlib.render_markdown({}, [], benchlib.summarize([]))
    assert "no measurements" in md.lower()
    assert "median: n/a" in md


def test_parse_qemu_img_compare_identical():
    assert benchlib.parse_qemu_img_compare("Images are identical.\n") is True


def test_parse_qemu_img_compare_differs():
    assert benchlib.parse_qemu_img_compare("Content mismatch at offset 65536!\n") is False
    assert benchlib.parse_qemu_img_compare("") is False


def test_summarise_probe():
    results = [{"q": "Q1", "ok": True, "detail": "x"}, {"q": "Q2", "ok": False, "detail": "y"}]
    assert benchlib.summarise_probe(results) == {"Q1": "PASS", "Q2": "FAIL"}


def test_summarise_probe_empty():
    assert benchlib.summarise_probe([]) == {}


def test_format_probe_line():
    assert benchlib.format_probe_line("Q3", True, "alloc=1") == "Q3: PASS alloc=1"
    assert benchlib.format_probe_line("Q4", False, "refused") == "Q4: FAIL refused"


def test_compare_argv_ssh_source():
    argv = benchlib.qemu_img_compare_argv("qemu+ssh://root@host1/system", "/d/a.qcow2", "/dr/b.qcow2")
    assert argv[:3] == ["ssh", "root@host1", "qemu-img"] or argv[:2] == ["ssh", "root@host1"]
    assert argv[-2:] == ["/d/a.qcow2", "/dr/b.qcow2"]
    assert "compare" in argv and "-U" in argv


def test_compare_argv_local():
    argv = benchlib.qemu_img_compare_argv("qemu:///system", "/d/a.qcow2", "/dr/b.qcow2")
    assert argv[0] == "qemu-img" and "ssh" not in argv
    assert argv[-2:] == ["/d/a.qcow2", "/dr/b.qcow2"]
