"""DR replication benchmark (FR-5, NFR-8). OWNER SCRIPT: talks to a real MiniHCI install and a real CirrOS guest.

Sets the protection policy (interval 60 s, retention rounds+1), then for each round writes --write-mib MiB
of random data in the guest over its console (virsh console through pexpect), waits for the next
succeeded replica and reads GET /vms/{id}/replicas. Prints a table with median and worst size_bytes and
duration_ms for full vs incremental copies. Refuses to run unless MINIHCI_REAL_HOSTS is set.
Nothing is filled in that was not measured.

  MINIHCI_REAL_HOSTS=1 uv run python scripts/bench_dr.py --api http://127.0.0.1:8000 --vm 1 --rounds 5 --write-mib 8
"""

import argparse
import sys
import time

try:
    from scripts import benchlib, guestlib
except ImportError:  # run as `python scripts/bench_dr.py`
    import benchlib  # type: ignore[no-redef]
    import guestlib  # type: ignore[no-redef]

INTERVAL_S = 60


def count_succeeded(rows: list[dict]) -> int:
    return sum(1 for r in rows if r.get("status") in ("succeeded", "pruned"))


def render_table(summary: dict, rounds: int, write_mib: int) -> str:
    method = f"push backup, cirros, {write_mib} MiB/round"
    out = [
        f"Method: {method}; interval {INTERVAL_S} s; {rounds} rounds",
        "| Mode | n | size_bytes median | size_bytes worst | duration_ms median | duration_ms worst |",
        "|---|---|---|---|---|---|",
    ]
    for mode in ("full", "incremental"):
        e = summary[mode]
        cells = [
            e["size_bytes"]["median"],
            e["size_bytes"]["worst"],
            e["duration_ms"]["median"],
            e["duration_ms"]["worst"],
        ]
        out.append(f"| {mode} | {e['count']} | " + " | ".join("n/a" if c is None else str(c) for c in cells) + " |")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--vm", type=int, required=True)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--write-mib", type=int, default=8)
    p.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for each replica")
    args = p.parse_args(argv)

    benchlib.require_real_hosts()
    import httpx

    with httpx.Client(base_url=args.api, timeout=30) as c:
        vm = c.get(f"/vms/{args.vm}").raise_for_status().json()
        host = c.get("/hosts").raise_for_status().json()
        uri = next(h["libvirt_uri"] for h in host if h["id"] == vm["host_id"])
        c.put(
            f"/vms/{args.vm}/protection",
            json={"interval_s": INTERVAL_S, "retention": args.rounds + 1, "enabled": True},
        ).raise_for_status()
        # Only this run's replicas: the VM's older replicas (idle periods, earlier runs) must not
        # mix into the medians, or the table is labelled one method and holds another.
        start_id = benchlib.last_replica_id(c.get(f"/vms/{args.vm}/replicas").raise_for_status().json())
        guest = guestlib.GuestConsole(uri, vm["name"])
        try:
            for i in range(args.rounds):
                before = count_succeeded(c.get(f"/vms/{args.vm}/replicas").raise_for_status().json())
                cmd = f"sudo dd if=/dev/urandom of=/root/f bs=1M count={args.write_mib} conv=fsync"
                guest.run(cmd, timeout=120)
                deadline = time.monotonic() + args.timeout
                while count_succeeded(c.get(f"/vms/{args.vm}/replicas").raise_for_status().json()) <= before:
                    if time.monotonic() > deadline:
                        raise SystemExit(f"round {i + 1}: no new replica within {args.timeout} s")
                    time.sleep(2)
                print(f"round {i + 1}/{args.rounds} replicated", file=sys.stderr)
        finally:
            guest.close()
        rows = benchlib.rows_since(c.get(f"/vms/{args.vm}/replicas").raise_for_status().json(), start_id)
    print(render_table(benchlib.summarise_replicas(rows), args.rounds, args.write_mib))
    return 0


if __name__ == "__main__":
    sys.exit(main())
