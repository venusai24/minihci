# MiniHCI

MiniHCI is a small hyperconverged-infrastructure control plane. It manages KVM virtual machines
on a handful of hosts that share one NFS datastore: placement, power operations, live migration,
automatic restart of VMs from a dead host, and a CPU-based rebalancer. Everything is driven
through a REST API and a thin CLI.

Status: the API, the task engine, the worker, the libvirt driver, HA and the CLI are built. The
web dashboard (`ui/`) is not built yet. The disaster-recovery milestone is not started.

Spec: [PRD](docs/superpowers/specs/MiniHCI-PRD.md). Decisions: [docs/decisions.md](docs/decisions.md).

## Architecture

```
CLI / REST client
      |
   FastAPI  ----  writes a task row, returns 202 + task_id
      |
 PostgreSQL  (tasks, step log, vms, hosts, host_metrics, ...)
      |
   worker    (claims tasks with FOR UPDATE SKIP LOCKED, per-target pg_advisory_xact_lock,
      |       heartbeats, reconciler, metrics collector, rebalancer)
 HostDriver  (FakeDriver for tests, LibvirtDriver for real hosts)
      |
   hosts  --  shared NFS datastore (disk images)
```

- The API never calls libvirt. Every mutating endpoint writes a task and returns 202.
- All host access goes through the `HostDriver` interface. Tests use `FakeDriver`.
- Every task step checks actual state before acting, and steps are recorded in the database
  before and after they run, so a restarted worker resumes or rolls back.
- Hosts are libvirt hosts reached over `qemu+ssh://`. VM disks are qcow2 overlays on NFS, so a
  VM can be started or migrated on any host that mounts the share.

## Quick start (FakeDriver, no real hosts)

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and podman (for PostgreSQL).

```
uv sync
bash scripts/dev-db.sh                       # PostgreSQL 17 on :5433 (podman)
uv run alembic upgrade head                  # create the schema
MINIHCI_FAKE_STATE_FILE=/tmp/minihci-fake.json uv run python -m minihci.worker_main &
MINIHCI_FAKE_STATE_FILE=/tmp/minihci-fake.json uv run python -c \
  "import uvicorn; from minihci.app import create_app; from minihci.config import Settings; \
   uvicorn.run(create_app(Settings()), port=8000)" &
uv run minihci hosts add host1 qemu+ssh://host1/system --wait
```

Settings are read from `MINIHCI_*` environment variables (see `src/minihci/config.py`). The
default driver is `fake`: an in-memory fake host, nothing real is touched. The state file lets
separate processes share one fake world.

## Real-host setup

[scripts/setup-hosts.sh](scripts/setup-hosts.sh) builds the reference lab: NFS exports on the
control-plane machine, three KVM host VMs, and on each host the packages, NFS mount, SELinux
boolean, libvirtd and firewall ports for live migration. It refuses to run unless
`MINIHCI_REAL_HOSTS` is set, checks state before every step, and is meant to be read by the
owner before it is run.

Then install the libvirt extra and select the driver:

```
uv sync --extra libvirt
export MINIHCI_DRIVER=libvirt
```

Disk operations (create, check, delete an overlay) try `MINIHCI_LIBVIRT_STORAGE_URIS` in order and fall
back to the next URI after any storage error or timeout (`MINIHCI_LIBVIRT_STORAGE_TIMEOUT_S`, default 30).
The default is `["qemu:///system"]`, the NFS server machine itself; add the hosts as fallbacks (D-026):

```
export MINIHCI_LIBVIRT_STORAGE_URIS='["qemu:///system","qemu+ssh://root@host1/system","qemu+ssh://root@host2/system"]'
```

### Set the NFS lease to 15 seconds

On the NFS server, set `lease-time=15` and `grace-time=15` (for example in
`/etc/nfs.conf.d/minihci.conf`). The default lease is 90 s.

This lease is the HA disk fence. MiniHCI has no power fencing. When a host dies, its QEMU
processes hold an image lock on the shared disk, and the NFS server only releases that lock when
the dead host's lease expires. Until then a second copy of the VM cannot start on another host.
So the restart time is bounded below by the lease, and a shorter lease means faster recovery. See
[decision D-028](docs/decisions.md) for the limits of this approach.

## API

All mutating calls return `202` with a `task_id`; poll `GET /tasks/{id}`. Send an
`Idempotency-Key` header to make a retry safe.

| Method | Path | What it does |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/metrics` | Prometheus metrics, computed from the database |
| POST | `/hosts` | register a host (task) |
| GET | `/hosts` | list hosts |
| GET | `/hosts/{host_id}/health` | heartbeat state of one host |
| DELETE | `/hosts/{host_id}` | remove a host (task; 409 if it still has VMs) |
| POST | `/vms` | create a VM, placed automatically or on a given host (task) |
| GET | `/vms` | list VMs |
| GET | `/vms/{vm_id}` | one VM |
| GET | `/vms/{vm_id}/migrations` | migration history of a VM |
| POST | `/vms/{vm_id}/start` | start (task) |
| POST | `/vms/{vm_id}/stop` | stop (task) |
| POST | `/vms/{vm_id}/reboot` | reboot (task) |
| POST | `/vms/{vm_id}/migrate` | live-migrate to another host (task) |
| DELETE | `/vms/{vm_id}` | delete the VM and its disk (task) |
| GET | `/tasks` | list tasks, optional `?state=` filter |
| GET | `/tasks/{task_id}` | one task with its step log |
| GET | `/scheduler/config` | placement and rebalancer settings |
| PUT | `/scheduler/config` | change them (task) |

## CLI

The `minihci` command talks to the REST API (default `http://localhost:8000`, override with
`--api` or `MINIHCI_API`). Add `--json` for machine-readable output. Mutating commands print a
`task_id`; add `--wait` to block until the task finishes, or run `minihci tasks wait ID`
(exit 0 succeeded, 2 failed, 4 timeout). `--idempotency-key KEY` makes retries safe.

```
minihci hosts add host1 qemu+ssh://host1/system --wait
minihci vms create web1 --vcpus 2 --mem-mb 1024 --image /srv/minihci/base/alpine.qcow2 --wait
minihci vms list
minihci vms migrate 1 --to 2
minihci scheduler set --cpu-headroom-pct 20
```

## Tests

```
bash scripts/dev-db.sh
uv run pytest -q                              # everything against FakeDriver
CHECK_FAST=1 bash scripts/check.sh            # lint + fast tests (skips tests marked slow)
bash scripts/check.sh                         # full gate
```

Crash tests ([tests/test_crash.py](tests/test_crash.py)) start a real worker process, kill it at
a chosen step with SIGKILL, restart it, and assert that the task completes or rolls back with
no orphaned resources. They are marked `slow`. `CRASH_ITERS` sets the number of random-kill
iterations (the full gate uses 20; the fast gate uses 5).

```
CRASH_ITERS=50 uv run pytest -q tests/test_crash.py
```

The driver contract suite ([tests/contract/](tests/contract/)) runs one set of tests against
every `HostDriver`. It is skipped unless real hosts are named, so it never runs by accident:

```
MINIHCI_REAL_HOSTS=host1,host2 uv run pytest -q tests/contract -m real_hosts
```

Only run that against hosts you own, with the libvirt extra installed.

## Design trade-offs and known limitations

The full reasoning is in [docs/decisions.md](docs/decisions.md). The ones that matter most:

- No power fencing. Split-brain protection for HA relies on NFS locking plus QEMU image
  locking, and on the 15 s lease above. A partitioned but alive host, or a mount with locking
  disabled, can defeat it (D-028).
- One worker process (with threads) is enforced by a singleton lock (D-004). The API can scale;
  the worker cannot yet.
- Live migration is client-driven (managed), not peer-to-peer, and the disk is shared, so no
  storage is copied (D-024, D-026).
- A VM deleted outside MiniHCI is marked `missing`, not re-created (D-020).
- Removed hosts are soft-deleted (D-015).
- Authentication is one optional shared bearer token (`MINIHCI_API_TOKEN`, D-032); there is no TLS and no multi-tenancy. Without the token the API is open, so keep it on a trusted network.
- The dashboard (`ui/`) is not built yet.
- Disaster recovery is covered in the next section (M5), with its own limitations.

## M5 DR replication

**Topology.** The laptop is the control plane, the libvirtd at `qemu:///system`, and the NFS server
(`/srv/minihci/primary`, `/srv/minihci/dr`). Hosts host1-3 are the primary site and mount primary at
`/var/lib/minihci`. Everyone, including the hosts, mounts the DR share at the same path,
`/srv/minihci/dr` (D-033), which `scripts/setup-hosts.sh` does idempotently. The laptop is registered as
the DR host with `minihci hosts add laptop qemu:///system --site dr`; placement, migration, the
rebalancer and HA never use a DR host.

**Method.** Each protected VM gets a libvirt push backup (checkpoint plus `REUSE_EXTERNAL`) of its disk
into a qcow2 chain on the DR share every `interval_s`: a full copy first, then increments, a new chain
every `retention` replicas (D-034, D-035). Lag and achieved RPO are defined in D-036; the target is
`interval_s + 30 s`.

**Protect and fail over.**

```
minihci vms protect 1 --interval 60 --retention 3 --wait
minihci vms protection 1
minihci vms replicas 1
minihci replication
minihci vms failover 1 --wait          # needs the VM's host to be dead; --force overrides (D-037)
```

Owner scripts (they refuse to run without `MINIHCI_REAL_HOSTS`; read them first):
`scripts/bench_dr.py` (sync size and time per round, `--api --vm --rounds --write-mib`) and
`scripts/acceptance_m5.py` (destroys host1-3, fails over, measures lost writes; once it has started
destroying hosts, it always ends, on success and on every failure path, by printing the
`virsh -c qemu:///system start hostN` lines for the hosts it touched). Guests are CirrOS, reached over
`virsh console` through pexpect (`scripts/guestlib.py`).

**Known limitations** (D-033, D-037):

- One machine and one NFS server simulate a site loss; this does not survive a real one.
- The control plane is not replicated.
- No failback and no reverse replication.
- Writes after the last replica are lost by design.
- A partitioned but alive primary can keep running its copy beside the DR copy.
- Stopped VMs are not replicated; they keep their last replica and the lag grows.
- Retention keeps at most `2*retention - 1` files per VM; deleting a VM leaves its replicas.

## Benchmarks

All numbers below are preliminary: single manual or probe runs on the three-host lab, an idle
guest with 256 MiB of RAM, and live migration with the shared NFS disk. They are not the result
of the scripted benchmarks and carry no statistical weight.

### Live migration

| Method | Direction | Runs | Wall (ms) | Total / elapsed (ms) | Downtime (ms) | Status |
|---|---|---|---|---|---|---|
| manual `virsh migrate --live`, `domjobinfo --completed` | host1 to host2 | n=2 (best case) | not measured | 1660 | 229 | preliminary |
| manual `virsh migrate --live`, `domjobinfo --completed` | host2 to host1 | n=2 (best case) | not measured | 835 | 258 | preliminary |
| probe script via the Python API (`migrate3`, completed job stats) | host1 to host2 | n=3, run 1 | 1050 | 696 | 88 | preliminary |
| probe script via the Python API (`migrate3`, completed job stats) | host2 to host1 | n=3, run 2 | 620 | 395 | 115 | preliminary |
| probe script via the Python API (`migrate3`, completed job stats) | host1 to host2 | n=3, run 3 | 600 | 363 | 78 | preliminary |

The two methods measure slightly differently, so compare only within a method.

### HA restart (dry run)

Method: probe script, one VM on host2, power cut with `virsh destroy host2`, then restart the VM
on host1 from the same disk. There is no failure-detection step in this run: the clock starts at
the power cut and stops when `dom.create()` returns on host1. Preliminary, one run per setting.

| NFS lease | Running on host1 after the power cut | Status |
|---|---|---|
| 90 s (default) | 91 s | preliminary |
| 15 s | 27.5 s | preliminary |

The restart did not fail; `create()` blocked until the dead host's NFS lease expired and QEMU's
image lock was released. That wait is the disk fence described above, which is why the lease is
set to 15 s. The 27.5 s with a 15 s lease is about 1.8 times the lease; the extra time is
probably NFS client lock retry, but that is not verified.

### DR replication

| Measurement | Value | Method | Status |
|---|---|---|---|
| Full copy size | 50532352 bytes | `probe_dr.py Q3, CirrOS idle 60 s, n=1, preliminary` | preliminary |
| Incremental size after 60 s idle | 724992 bytes | `probe_dr.py Q3, CirrOS idle 60 s, n=1, preliminary` | preliminary |
| Sync time (full / incremental) | TBD | `bench_dr.py` (not run) | TBD |
| Achieved RPO | TBD | `acceptance_m5.py` (not run) | TBD |
| Failover time | TBD | `acceptance_m5.py` (not run) | TBD |

### Scripted benchmarks

Not yet run. [scripts/bench_migration.py](scripts/bench_migration.py) and
[scripts/bench_ha.py](scripts/bench_ha.py) write their results to `docs/benchmarks/`.

| Benchmark | Result | Status |
|---|---|---|
| Live migration, repeated rounds | TBD | TBD |
| HA failover through the control plane, with detection | TBD | TBD |
| VM creation time | TBD | TBD |
