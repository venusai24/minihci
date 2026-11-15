# MiniHCI – Product Requirements

Nov 15, 2026 · @Advaith

## Overview

MiniHCI is a control plane that manages KVM virtual machines across 2–3 hosts as one cluster: it places VMs, moves them live, restarts them when a host dies, and replicates them to a second site. It is a small, from-scratch version of what Nutanix Prism, AHV's scheduler, and Cerebro do.

**Goals**

1. Demonstrate distributed-systems skills on a resume: scheduling, live migration, failure detection, crash-safe task execution, replication.
2. Cover the Nutanix MTS Intern JD: Python, REST APIs, distributed systems, SQL, and a React UI.
3. Produce measured results (downtime, recovery time, RPO) that can be quoted on a resume.

**Non-goals**

- Building a hypervisor or storage engine. KVM, QEMU, libvirt, and NFS are used as-is.
- Multi-tenancy, user accounts, or role-based access control.
- Production-grade security or networking (SDN, VLANs, microsegmentation).
- A highly available control plane. The control plane runs as a single instance; it must survive its own restart, not run as a cluster.
- Integration with LogBlock. It is an optional later demo, not part of this scope.

## Users and environment

**Primary user:** a cluster administrator who manages VMs through the REST API, a CLI, or the web dashboard. There is one admin and no login.

**Secondary user:** a resume reviewer or interviewer reading the README, benchmarks, and demo video.

**Environment assumptions**

- 2–3 Linux hosts running KVM, QEMU, and libvirt. These can be nested VMs on one machine or small cloud VMs that support nested virtualization.
- Shared storage over NFS, mounted at the same path on every host, so live migration doesn't need to copy disks.
- A second "site" for DR: a separate NFS export or directory, ideally on a different host.
- The control plane runs on one machine (or one of the hosts) with network access to every host's libvirt.
- VMs use small images (for example, Alpine or a cloud image under 1 GiB) to keep tests fast.

**Stack (decided)**

| Layer | Choice |
| --- | --- |
| API | Python 3.12, FastAPI |
| Database | PostgreSQL (SQLite acceptable for M1 only) |
| Host access | libvirt Python bindings over SSH or TCP |
| Workers | Python background workers reading tasks from the database |
| UI | React with TypeScript |
| Tests | pytest, plus scripted failure tests |

## Architecture

The API never touches a host directly. It writes a task row, and background workers carry the task out through libvirt. That single rule is what makes the system crash-safe, idempotent, and recoverable.

&#91;embedded content: MiniHCI architecture · API, database, workers, 3 hosts, 2 storage sites\]

PostgreSQL (highlighted) is the source of truth for desired state; workers reconcile the hosts toward it.

## Functional requirements

Six milestones, built in order. Each ends in something demoable, and each requirement has an acceptance test.

### M1: Hosts and VM lifecycle

- **FR-1.1** Register, list, and remove hosts. Store each host's libvirt URI, total CPU and memory,`docs/superpowers/specs/minihci-prd.md`. Superpowers’ `writing-plans` skill norm and status.
- **FR-1.2** Create a VM with name, vCPUs, memory, and disk image on a chosen host. The disk is a qcow2 overlay on shared NFS.
- **FR-1.3** Start, stop (graceful and forced), reboot, and delete a VM.
- **FR-1.4** List VMs with their host, state, and resources. Reconcile the database with libvirt's actual state on startup and every 30 seconds.
- **FR-1.5** A small CLI that wraps the API.

*Acceptance:* a VM created through the API boots on the chosen host, and a VM deleted outside MiniHCI is detected by reconciliation.

### M2: Crash-safe task engine

- **FR-2.1** Every mutating API call creates a task row and returns `202 Accepted` with a task ID. Workers execute tasks.
- **FR-2.2** Task states: `pending`, `running`, `succeeded`, `failed`, `rolled_back`. Each task records its steps and progress.
- **FR-2.3** Clients may send an idempotency key. A repeated request with the same key returns the original task instead of creating a new one.
- **FR-2.4** If the control plane restarts mid-task, the task resumes or rolls back on startup. No VM is left half-created and untracked.
- **FR-2.5** Two tasks on the same VM never run at the same time (per-VM locking).

*Acceptance:* kill the API and workers during a VM create; after restart the task completes or rolls back, and the database matches libvirt.

### M3: Live migration and scheduler

- **FR-3.1** Live-migrate a running VM to a named host through the API.
- **FR-3.2** Placement on create: if no host is named, pick one by a documented policy (for example, best-fit on memory with a CPU headroom limit). Reject the request if no host fits.
- **FR-3.3** Collect per-host CPU and memory usage at a fixed interval and store it.
- **FR-3.4** Rebalancer: when a host stays above a configurable load threshold, choose a VM and migrate it to the least-loaded host. Avoid ping-pong with a cooldown.
- **FR-3.5** Record migration duration and VM downtime for every migration.

*Acceptance:* load one host artificially; the rebalancer migrates a VM off it, and the VM keeps answering pings with downtime recorded.

### M4: High availability

- **FR-4.1** Heartbeat each host at a fixed interval. Mark a host `suspect` after missed heartbeats and `dead` after a longer timeout.
- **FR-4.2** When a host is declared dead, restart its VMs on surviving hosts using the placement policy.
- **FR-4.3** Fencing rule: never start a VM elsewhere until its old host is confirmed dead, to avoid two copies writing the same disk. Document the rule and its limits.
- **FR-4.4** VMs that cannot be placed stay in a visible `pending_restart` state.

*Acceptance:* power off a host; its VMs boot on other hosts, and the time from failure to running is recorded.

### M5: DR replication

- **FR-5.1** A protection policy per VM: replication interval (the RPO target) and retention count.
- **FR-5.2** Take crash-consistent snapshots on schedule and copy them to the DR site, sending only changes where possible.
- **FR-5.3** Failover command: start the latest replicated copy of a VM at the DR site.
- **FR-5.4** Show each VM's last replication time and achieved RPO.

*Acceptance:* stop the primary site; failover boots the VM at the DR site, and the data lost is within the RPO.

### M6: Dashboard

- **FR-6.1** Pages for hosts (with load), VMs (with actions), tasks (with live status), and replication status.
- **FR-6.2** VM actions from the UI: create, start, stop, migrate, delete.
- **FR-6.3** The UI uses only the public REST API.

## Non-functional requirements

- **NFR-1 Correctness over speed.** The database and libvirt must never silently disagree. Reconciliation logs and fixes every drift.
- **NFR-2 Crash safety.** Killing any MiniHCI process at any time must leave the system recoverable on restart. A scripted kill test proves it.
- **NFR-3 Idempotency.** Every mutating operation is safe to retry.
- **NFR-4 Observability.** Structured JSON logs with task IDs, plus a `/metrics` endpoint (Prometheus format) for task counts, migration times, and heartbeat status.
- **NFR-5 Testability.** Unit tests for the scheduler and task state machine using a fake libvirt layer, plus integration tests against real hosts. Failure tests are scripted and repeatable.
- **NFR-6 Documented decisions.** A decisions log in the repo records each trade-off (placement policy, heartbeat timeouts, fencing rule, replication method).
- **NFR-7 Reproducible setup.** One script or Ansible playbook brings up the hosts, NFS, and libvirt from scratch.
- **NFR-8 Starting performance targets** (to validate, then replace with measured numbers):
  - Live-migration downtime under 1 second for a 1 GiB VM on the local network.
  - HA restart within 60 seconds of a host failure.
  - Achieved RPO within the configured interval plus 30 seconds.

## API surface and data model

All mutating endpoints return `202 Accepted` with a task ID and accept an optional `Idempotency-Key` header.

| Method | Path | Purpose | Milestone |
| --- | --- | --- | --- |
| POST / GET / DELETE | `/hosts`, `/hosts/{id}` | Register, list, remove hosts | M1 |
| POST / GET / DELETE | `/vms`, `/vms/{id}` | Create, list, delete VMs | M1 |
| POST | `/vms/{id}/start`, `/stop`, `/reboot` | Power operations | M1 |
| GET | `/tasks`, `/tasks/{id}` | Task status and steps | M2 |
| POST | `/vms/{id}/migrate` | Live migration to a host | M3 |
| GET / PUT | `/scheduler/config` | Placement and rebalance settings | M3 |
| GET | `/hosts/{id}/health` | Heartbeat and HA state | M4 |
| PUT | `/vms/{id}/protection` | Replication policy | M5 |
| POST | `/vms/{id}/failover` | Fail over to the DR site | M5 |
| GET | `/metrics` | Prometheus metrics | M2 onward |

**Core tables**

| Table | Key fields |
| --- | --- |
| `hosts` | id, name, libvirt\_uri, cpu\_total, mem\_total, status, last\_heartbeat |
| `vms` | id, name, host\_id, vcpus, mem\_mb, disk\_path, desired\_state, actual\_state |
| `tasks` | id, type, target\_id, state, steps, idempotency\_key, created\_at, finished\_at, error |
| `host_metrics` | host\_id, ts, cpu\_pct, mem\_used |
| `migrations` | id, vm\_id, src\_host, dst\_host, duration\_ms, downtime\_ms |
| `protection_policies` | vm\_id, interval\_s, retention |
| `replicas` | id, vm\_id, snapshot\_time, dr\_path, size\_bytes |

Keeping both `desired_state` and `actual_state` on each VM is what lets reconciliation and HA work: the system always knows what should be running and can act to make it so.

## Success metrics, deliverables, and priorities

**Numbers to measure and publish in the README**

- Live-migration duration and downtime (median and worst, over at least 10 runs)
- Host balance before and after rebalancing
- HA recovery time from host failure to VM running
- Achieved RPO and failover time
- Task-engine recovery: crash-test iterations passed

**Deliverables**

- [ ] Public repo with README: architecture diagram, setup guide, benchmarks with exact setup, and a "design trade-offs and known limitations" section
- [ ] Decisions log
- [ ] Scripted failure tests (control-plane kill, host power-off, site loss)
- [ ] 2–3 minute demo video: create, migrate, kill a host, fail over
- [ ] Resume bullet with measured numbers

**Priority if time runs short**

| Milestone | Priority | Why |
| --- | --- | --- |
| M1 Hosts and VMs | Must | Foundation |
| M2 Task engine | Must | Strongest correctness signal |
| M3 Migration and scheduler | Must | The headline feature |
| M4 HA | Should | Most "distributed systems" feature per hour of work |
| M5 DR | Could | Cut before M4 |
| M6 Dashboard | Could | A basic UI is enough |

**Decisions (resolved open questions)**

- **Hardware:** one Fedora Linux machine with nested virtualization enabled. Three Fedora or Rocky VMs act as Hosts A–C. The physical machine runs the control plane, PostgreSQL, and two NFS exports (primary storage and the DR site). Test guests are tiny images such as CirrOS or Alpine. A host "power cut" for HA tests is `virsh destroy` on that host's VM.
- **DR replication:** incremental, using libvirt checkpoints and incremental backups (QEMU persistent dirty bitmaps). The first sync of each VM is a full copy as the base; every later sync sends only changed blocks. Publish full vs. incremental bytes transferred and sync time as a benchmark.
