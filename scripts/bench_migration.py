"""Migration benchmark (FR-3.1, FR-3.5, NFR-5). OWNER SCRIPT: talks to a real MiniHCI install.

Uses the REST API only. Refuses to run unless MINIHCI_REAL_HOSTS is set.
Writes docs/benchmarks/migration-<date>.md; failed rounds are listed, not timed.
"""

import argparse
import datetime
import sys
import time
from pathlib import Path

try:
    from scripts import benchlib
except ImportError:  # run as `python scripts/bench_migration.py`
    import benchlib  # type: ignore[no-redef]


def other_host(current: int, host_ids: list[int]) -> int:
    return next(h for h in host_ids if h != current)


def output_path(out_dir: Path, date: str) -> Path:
    return Path(out_dir) / f"migration-{date}.md"


def build_rows(results, migrations):
    """results: [(task_id, task_state)] in round order. Only succeeded rounds contribute timings."""
    by_task = {m["task_id"]: m for m in migrations}
    rows, durations, downtimes = [], [], []
    for i, (task_id, state) in enumerate(results, 1):
        m = by_task.get(task_id) if state == "succeeded" else None
        dur = m["duration_ms"] if m else None
        down = m["downtime_ms"] if m else None
        rows.append(
            {
                "round": i,
                "task_id": task_id,
                "task_state": state,
                "src_host_id": (by_task.get(task_id) or {}).get("src_host_id"),
                "dst_host_id": (by_task.get(task_id) or {}).get("dst_host_id"),
                "duration_ms": dur,
                "downtime_ms": down,
            }
        )
        if dur is not None:
            durations.append(dur)
        if down is not None:
            downtimes.append(down)
    return rows, durations, downtimes


def wait_task(client, task_id: int, timeout: float, poll: float = 1.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/tasks/{task_id}").raise_for_status().json()["state"]
        if state in ("succeeded", "failed", "rolled_back"):
            return state
        time.sleep(poll)
    return "timeout"


def ensure_vm(client, args, host_ids):
    for v in client.get("/vms").raise_for_status().json():
        if v["name"] == args.vm_name and v["desired_state"] != "absent":
            return v
    body = {
        "name": args.vm_name,
        "host_id": host_ids[0],
        "vcpus": args.vcpus,
        "mem_mb": args.vm_mem,
        "image": args.image,
    }
    r = client.post("/vms", json=body)
    r.raise_for_status()
    state = wait_task(client, r.json()["task_id"], args.timeout)
    if state != "succeeded":
        raise SystemExit(f"VM create task ended in state {state}")
    return client.get(f"/vms/{r.json()['target_id']}").raise_for_status().json()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--vm-mem", type=int, default=1024, help="guest memory in MiB")
    p.add_argument("--vcpus", type=int, default=1)
    p.add_argument("--rounds", type=int, default=10, help="host1->host2->host1 round trips")
    p.add_argument("--vm-name", default="bench-mig")
    p.add_argument("--image", required=True)
    p.add_argument("--timeout", type=float, default=600.0, help="per task, seconds")
    p.add_argument("--cpu-mode", default="not recorded", help="describe guest CPU mode for the report")
    p.add_argument("--storage", default="not recorded", help="describe storage for the report")
    p.add_argument("--out-dir", type=Path, default=Path("docs/benchmarks"))
    args = p.parse_args(argv)

    benchlib.require_real_hosts()
    import httpx

    with httpx.Client(base_url=args.api, timeout=30) as client:
        hosts = [h for h in client.get("/hosts").raise_for_status().json() if h["status"] == "ready"]
        host_ids = [h["id"] for h in hosts][:2]
        if len(host_ids) < 2:
            raise SystemExit("need two ready hosts")
        names = {h["id"]: h["name"] for h in hosts}
        vm = ensure_vm(client, args, host_ids)
        results = []
        for _ in range(args.rounds * 2):
            cur = client.get(f"/vms/{vm['id']}").raise_for_status().json()["host_id"]
            r = client.post(f"/vms/{vm['id']}/migrate", json={"host_id": other_host(cur, host_ids)})
            r.raise_for_status()
            tid = r.json()["task_id"]
            results.append((tid, wait_task(client, tid, args.timeout)))
        migs = client.get(f"/vms/{vm['id']}/migrations").raise_for_status().json()

    rows, durations, downtimes = build_rows(results, migs)
    setup = {
        "date": datetime.date.today().isoformat(),
        "guest memory (MiB)": vm["mem_mb"],
        "guest vcpus": vm["vcpus"],
        "hosts": ", ".join(f"{names[i]} (id {i})" for i in host_ids),
        "CPU mode": args.cpu_mode,
        "storage": args.storage,
        "migrations attempted": len(results),
        "failed or timed out": sum(1 for _, s in results if s != "succeeded"),
        "summary unit": "ms; duration_ms table below, downtime summarised separately",
    }
    dsum = benchlib.summarize(durations)
    hsum = benchlib.summarize(downtimes)
    text = benchlib.render_markdown(setup, rows, dsum)
    text = text.replace(
        "## Raw",
        "Downtime (ms): " + ", ".join(f"{k} {'n/a' if v is None else v}" for k, v in hsum.items()) + "\n\n## Raw",
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = output_path(args.out_dir, setup["date"])
    path.write_text(text)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
