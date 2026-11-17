"""Pure helpers shared by the owner benchmark scripts (NFR-5). No I/O besides stderr/exit."""

import math
import os
import sys
from urllib.parse import urlparse

ENV_VAR = "MINIHCI_REAL_HOSTS"


def require_real_hosts() -> None:
    if not os.environ.get(ENV_VAR):
        print(
            f"refusing to run: this script talks to real hosts; set {ENV_VAR}=1 to confirm.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def summarize(ms_list) -> dict:
    """median / p90 (nearest rank) / worst; all None for an empty list."""
    if not ms_list:
        return {"median": None, "p90": None, "worst": None}
    xs = sorted(ms_list)
    n = len(xs)
    mid = n // 2
    median = xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2
    if isinstance(median, float) and median.is_integer():
        median = int(median)
    p90 = xs[max(math.ceil(0.9 * n) - 1, 0)]
    return {"median": median, "p90": p90, "worst": xs[-1]}


def parse_qemu_img_compare(output: str) -> bool:
    """True when `qemu-img compare` reported the two images as identical."""
    return "images are identical" in output.lower()


def qemu_img_compare_argv(src_uri: str, disk: str, other: str) -> list[str]:
    """argv for `qemu-img compare`, run on the source host over ssh (the laptop does not see
    the hosts' /var/lib/minihci path), or locally when src_uri has no host (qemu:///system)."""
    cmd = ["qemu-img", "compare", "-U", "-f", "qcow2", "-F", "qcow2", disk, other]
    parsed = urlparse(src_uri)
    if not parsed.hostname:
        return cmd
    return ["ssh", f"{parsed.username or 'root'}@{parsed.hostname}", *cmd]


def format_probe_line(q: str, ok: bool, detail: str) -> str:
    return f"{q}: {'PASS' if ok else 'FAIL'} {detail}"


def summarise_probe(results: list[dict]) -> dict:
    """[{"q": "Q1", "ok": True, ...}, ...] -> {"Q1": "PASS", ...}"""
    return {r["q"]: "PASS" if r["ok"] else "FAIL" for r in results}


def summarise_replicas(rows: list[dict]) -> dict:
    """GET /vms/{id}/replicas rows -> per mode (full / incremental) count plus median and worst of
    size_bytes and duration_ms. Only succeeded and pruned rows count (a pruned row was a good copy)."""
    out: dict = {}
    for mode in ("full", "incremental"):
        sel = [r for r in rows if r.get("mode") == mode and r.get("status") in ("succeeded", "pruned")]
        entry: dict = {"count": len(sel)}
        for key in ("size_bytes", "duration_ms"):
            s = summarize([r[key] for r in sel if r.get(key) is not None])
            entry[key] = {"median": s["median"], "worst": s["worst"]}
        out[mode] = entry
    return out


def _cell(v) -> str:
    return "-" if v is None else str(v)


def render_markdown(setup: dict, rows: list[dict], summary: dict) -> str:
    out = ["## Setup", ""]
    out += [f"- {k}: {v}" for k, v in setup.items()]
    out += ["", "## Summary", ""]
    if all(v is None for v in summary.values()):
        out.append("No measurements were collected.")
    for k in ("median", "p90", "worst"):
        v = summary.get(k)
        out.append(f"- {k}: {'n/a' if v is None else v}")
    out += ["", "## Raw", ""]
    if rows:
        cols = list(rows[0].keys())
        out.append("| " + " | ".join(cols) + " |")
        out.append("|" + "|".join(" --- " for _ in cols) + "|")
        for r in rows:
            out.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    else:
        out.append("(no rows)")
    return "\n".join(out) + "\n"
