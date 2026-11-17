import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def benchmarks_section() -> str:
    m = re.search(r"^## Benchmarks\n(.*?)(?=^## |\Z)", README, re.DOTALL | re.MULTILINE)
    assert m, "README has no Benchmarks section"
    return m.group(1)


def test_relative_links_resolve():
    links = re.findall(r"\]\(([^)\s]+)\)", README)
    assert links
    for link in links:
        if re.match(r"^[a-z]+://", link) or link.startswith("#"):
            continue
        path = link.split("#", 1)[0]
        assert (ROOT / path).exists(), f"broken README link: {link}"


def test_benchmark_numbers_are_sourced():
    sources = (ROOT / "autopilot" / "QUEUE.md").read_text()
    bench_dir = ROOT / "docs" / "benchmarks"
    if bench_dir.is_dir():
        sources += "\n".join(p.read_text() for p in bench_dir.glob("*.md"))
    section = re.sub(r"\]\([^)]*\)", "]", benchmarks_section())  # drop link targets
    section = re.sub(r"`[^`]*`", " ", section)  # drop inline code (script names, commands)
    numbers = re.findall(r"(?<![\w.])\d+(?:\.\d+)?", section)
    assert numbers
    for n in numbers:
        found = re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", sources)
        assert found, f"number {n} not found in QUEUE.md or docs/benchmarks"


def test_benchmarks_are_labelled_preliminary_and_have_tbd_rows():
    section = benchmarks_section()
    assert "preliminary" in section
    assert "TBD" in section
