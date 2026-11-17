"""M5 acceptance: simulated site loss and DR failover (NFR-8). OWNER SCRIPT: destroys the primary host VMs.

Protects the VM, starts a marker writer in the guest (a timestamp per second), waits for >= 2 succeeded
replicas plus a random 0..interval s, records stop_at, then runs `virsh -c qemu:///system destroy <host>`
for each primary host and waits for all of them to be `dead`. Runs `minihci vms failover ID --wait` (timed),
reads the last marker on the DR copy over its console and prints data_lost_s = stop_at - last_mark,
failover_s and PASS when data_lost_s <= interval + 30. Refuses to run unless MINIHCI_REAL_HOSTS is set.

  MINIHCI_REAL_HOSTS=1 uv run python scripts/acceptance_m5.py --api http://127.0.0.1:8000 --vm 1 \\
      --hosts host1,host2,host3 --interval 60
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time

try:
    from scripts import benchlib, guestlib
except ImportError:  # run as `python scripts/acceptance_m5.py`
    import benchlib  # type: ignore[no-redef]
    import guestlib  # type: ignore[no-redef]

LAPTOP_URI = "qemu:///system"
# cirros is not root; the loop runs under sudo (the brief's loop, wrapped)
MARKER_LOOP = "sudo sh -c 'while true; do date +%s >> /root/marks; sync; sleep 1; done' &"
# Fresh deadline for the dead-host wait, independent of the replica wait: heartbeat_dead_after_s is 30 s,
# so 120 s leaves ample margin without being eaten by time already spent before the destroy.
DEAD_WAIT_S = 120.0
SLACK_S = 30  # NFR-8: within target is interval + 30 s


def verdict(stop_at: float, last_mark: float, interval: int) -> tuple[float, bool]:
    """(data_lost_s, passed): the writes lost are those after the last marker the DR copy holds."""
    lost = stop_at - last_mark
    return lost, lost <= interval + SLACK_S


def restart_lines(destroyed: list[str], uncertain: str | None = None) -> list[str]:
    """The exact commands that bring the destroyed host VMs back, plus a note for a host whose destroy
    call itself failed (its state is unknown; `start` on a running domain is harmless)."""
    if not destroyed:
        return ["No host VM was destroyed; nothing to restart."]
    out = ["To bring the primary site back, run:"]
    out += [f"virsh -c {LAPTOP_URI} start {n}" for n in destroyed]
    if uncertain:
        out.append(
            f"Note: `virsh destroy {uncertain}` itself failed, so its state is unknown; check it with `virsh list --all`."
        )
    return out


def wait_all_dead(check, timeout_s: float, sleep=time.sleep, now=time.monotonic, poll_s: float = 2) -> bool:
    """Poll check() until it is true or timeout_s passes (its own fresh deadline)."""
    deadline = now() + timeout_s
    while True:
        if check():
            return True
        if now() > deadline:
            return False
        sleep(poll_s)


def run_site_loss(names, destroy, wait_dead, after, out) -> None:
    """Destroy each host, wait for dead, then run after(). Whatever happens from the first destroy onward,
    the restart commands for exactly the hosts touched are printed last (a host is counted before its
    destroy call, because a failed call leaves its state unknown)."""
    touched: list[str] = []
    uncertain = None
    try:
        for n in names:
            touched.append(n)
            uncertain = n
            destroy(n)
            uncertain = None
        wait_dead()
        after()
    finally:
        for line in restart_lines(touched, uncertain):
            out(line)


def task_id_from_cli(stdout: str) -> int | None:
    """The task id from `minihci --json ... --wait` output: its first line is the 202 JSON, the wait adds text."""
    lines = stdout.strip().splitlines()
    if not lines:
        return None
    try:
        data = json.loads(lines[0])
    except ValueError:
        return None
    return data.get("task_id") if isinstance(data, dict) else None


def failover_numbers(rows: list[dict], task_id: int | None) -> tuple[float | None, int | None]:
    """(data_loss_s, failover_ms) of this run's failover from GET /vms/{id}/failovers; (None, None) when the row
    cannot be identified, never the numbers of another failover."""
    if task_id is None:
        return None, None
    row = next((r for r in rows if r.get("task_id") == task_id), None)
    if row is None:
        return None, None
    return row.get("data_loss_s"), row.get("failover_ms")


def api_value(v) -> str:
    return "n/a (not reported by the API)" if v is None else str(v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--vm", type=int, required=True)
    p.add_argument("--hosts", required=True, help="comma-separated primary host names (libvirt domains on the laptop)")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--timeout", type=float, default=600.0)
    args = p.parse_args(argv)

    benchlib.require_real_hosts()
    import httpx

    names = [h for h in args.hosts.split(",") if h]
    env = {**os.environ, "MINIHCI_API": args.api}
    with httpx.Client(base_url=args.api, timeout=30) as c:
        vm = c.get(f"/vms/{args.vm}").raise_for_status().json()
        hosts = c.get("/hosts").raise_for_status().json()
        by_name = {h["name"]: h for h in hosts}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SystemExit(f"unknown host(s): {', '.join(missing)}")
        uri = by_name_uri(hosts, vm["host_id"])
        c.put(
            f"/vms/{args.vm}/protection", json={"interval_s": args.interval, "retention": 3, "enabled": True}
        ).raise_for_status()

        guest = guestlib.GuestConsole(uri, vm["name"])
        guest.run(MARKER_LOOP)
        guest.close()

        deadline = time.monotonic() + args.timeout
        while sum(r["status"] == "succeeded" for r in c.get(f"/vms/{args.vm}/replicas").raise_for_status().json()) < 2:
            if time.monotonic() > deadline:
                raise SystemExit("fewer than 2 replicas within the timeout")
            time.sleep(2)
        time.sleep(random.uniform(0, args.interval))

        stop_at = time.time()
        state: dict = {}

        def destroy(n):
            subprocess.run(["virsh", "-c", LAPTOP_URI, "destroy", n], check=True)

        def wait_dead():
            ok = wait_all_dead(
                lambda: all(
                    c.get(f"/hosts/{by_name[n]['id']}/health").raise_for_status().json()["status"] == "dead"
                    for n in names
                ),
                DEAD_WAIT_S,
            )
            if not ok:
                raise SystemExit("hosts not all dead within the timeout")

        def after():
            t0 = time.monotonic()
            r = subprocess.run(
                ["uv", "run", "minihci", "--json", "vms", "failover", str(args.vm), "--wait"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            state["failover_s"] = time.monotonic() - t0
            if r.returncode != 0:
                print(r.stdout[-500:], r.stderr[-500:], file=sys.stderr)
                raise SystemExit("failover did not succeed")
            vm2 = c.get(f"/vms/{args.vm}").raise_for_status().json()
            dr_uri = by_name_uri(c.get("/hosts").raise_for_status().json(), vm2["host_id"])
            guest2 = guestlib.GuestConsole(dr_uri, vm2["name"])
            try:
                out = guest2.run("sudo tail -1 /root/marks").split()
            finally:
                guest2.close()
            if not out:
                raise SystemExit("no marker found on the DR copy (/root/marks empty)")
            state["last_mark"] = float(out[-1])
            rows = c.get(f"/vms/{args.vm}/failovers").raise_for_status().json()
            state["api_loss"], state["api_ms"] = failover_numbers(rows, task_id_from_cli(r.stdout))

        run_site_loss(names, destroy, wait_dead, after, print)
        last_mark, failover_s = state["last_mark"], state["failover_s"]
        api_loss, api_ms = state["api_loss"], state["api_ms"]

    lost, ok = verdict(stop_at, last_mark, args.interval)
    print(f"data_lost_s={lost:.1f}")
    # control-plane clock, from the last heartbeat: a lower bound; data_lost_s mixes laptop and guest clocks
    print(f"api_data_loss_s={api_value(api_loss)} (lower bound)")
    print(f"api_failover_ms={api_value(api_ms)}")
    print(f"failover_s={failover_s:.1f}")
    print(f"{'PASS' if ok else 'FAIL'}: data_lost_s <= interval + {SLACK_S} ({args.interval + SLACK_S})")
    return 0 if ok else 1


def by_name_uri(hosts: list[dict], host_id: int) -> str:
    return next(h["libvirt_uri"] for h in hosts if h["id"] == host_id)


if __name__ == "__main__":
    sys.exit(main())
