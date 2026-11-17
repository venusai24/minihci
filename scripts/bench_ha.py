"""HA failover benchmark (FR-4.2, NFR-5). OWNER SCRIPT: talks to a real MiniHCI install.

Uses the REST API only. Refuses to run unless MINIHCI_REAL_HOSTS is set.
Prints the cue for destroying the host VM, records t0 when you press Enter, then polls
until every VM that was on that host is running on another host (or the timeout passes).
Writes docs/benchmarks/ha-<date>.md. Nothing is filled in that was not measured.
"""

import argparse
import datetime
import sys
import time
from pathlib import Path

try:
    from scripts import benchlib
except ImportError:  # run as `python scripts/bench_ha.py`
    import benchlib  # type: ignore[no-redef]


def output_path(out_dir: Path, date: str) -> Path:
    return Path(out_dir) / f"ha-{date}.md"


def ha_elapsed(t0: float, observations, affected_ids, dead_host_id: int):
    """Seconds from t0 to the first observation where every affected VM is running on a
    host other than dead_host_id; None if that never happened.

    observations: [(timestamp, [vm dict with id, host_id, actual_state])] in time order.
    """
    affected = set(affected_ids)
    if not affected:
        return None
    for ts, vms in observations:
        by_id = {v["id"]: v for v in vms}
        if all(
            i in by_id and by_id[i]["actual_state"] == "running" and by_id[i]["host_id"] != dead_host_id
            for i in affected
        ):
            return ts - t0
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--host", required=True, help="name of the host to kill (a libvirt domain of the same name)")
    p.add_argument("--timeout", type=float, default=300.0, help="seconds to wait after t0")
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--storage", default="not recorded")
    p.add_argument("--out-dir", type=Path, default=Path("docs/benchmarks"))
    args = p.parse_args(argv)

    benchlib.require_real_hosts()
    import httpx

    with httpx.Client(base_url=args.api, timeout=30) as client:
        hosts = client.get("/hosts").raise_for_status().json()
        dead = next((h for h in hosts if h["name"] == args.host), None)
        if dead is None:
            raise SystemExit(f"unknown host {args.host!r}")
        vms = client.get("/vms").raise_for_status().json()
        affected = [v for v in vms if v["host_id"] == dead["id"] and v["desired_state"] == "running"]
        if not affected:
            raise SystemExit(f"no running VMs on {args.host}; nothing to measure")
        ids = {v["id"] for v in affected}
        print(f"{len(ids)} VM(s) on {args.host}: {', '.join(v['name'] for v in affected)}")
        print("Run this on the laptop, then press Enter AT THE SAME MOMENT:")
        print(f"    virsh destroy {args.host}")
        input("Press Enter right as you run the command... ")
        t0 = time.monotonic()
        obs = []
        while time.monotonic() - t0 < args.timeout:
            obs.append((time.monotonic(), client.get("/vms").raise_for_status().json()))
            if ha_elapsed(t0, obs[-1:], ids, dead["id"]) is not None:
                break
            time.sleep(args.poll)

    elapsed = ha_elapsed(t0, obs, ids, dead["id"])
    last = {v["id"]: v for v in obs[-1][1]} if obs else {}
    rows = [
        {
            "vm_id": v["id"],
            "name": v["name"],
            "final_host_id": (last.get(v["id"]) or {}).get("host_id"),
            "final_state": (last.get(v["id"]) or {}).get("actual_state"),
        }
        for v in affected
    ]
    setup = {
        "date": datetime.date.today().isoformat(),
        "killed host": f"{args.host} (id {dead['id']})",
        "hosts": ", ".join(f"{h['name']} (id {h['id']})" for h in hosts),
        "affected VMs": len(ids),
        "storage": args.storage,
        "timeout (s)": args.timeout,
        "poll interval (s)": args.poll,
        "outcome": "recovered" if elapsed is not None else "NOT recovered within timeout",
        "summary unit": "seconds from pressing Enter until every affected VM is running elsewhere",
    }
    summary = {"median": None, "p90": None, "worst": None}
    if elapsed is not None:
        s = round(elapsed, 2)
        summary = {"median": s, "p90": s, "worst": s}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = output_path(args.out_dir, setup["date"])
    path.write_text(benchlib.render_markdown(setup, rows, summary))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
