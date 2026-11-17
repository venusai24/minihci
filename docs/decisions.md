# Decisions log (NFR-6)

Entries D-001 to D-011 were backfilled by Q00 from code that already existed at commit cb8a062.
New entries are appended as `## D-NNN <title>` with Context / Decision / Consequences / How to undo.

## D-001 SQLAlchemy 2 ORM models, one Alembic revision per schema change
- **Context:** The autopilot notes asked for SQLAlchemy Core. The existing code already uses ORM models.
- **Decision:** Keep the declarative ORM models in `src/minihci/models.py` (Host, Vm, Task) and
  short sessions from `db.make_session_factory`. Schema changes go in new Alembic revisions after
  `migrations/versions/0001_core.py`. States are enforced by CHECK constraints in the database.
- **Consequences:** There is one way to access data. Explicit `select()` still reads like SQL.
  Every new state value needs a migration.
- **How to undo:** Convert the models to `Table` objects. Callers use `s.get()`/`select(Model)`.

## D-002 Task claiming: FIFO with FOR UPDATE SKIP LOCKED
- **Context:** Several worker threads must never claim the same task (FR-2.1, CLAUDE.md).
- **Decision:** `tasks.engine.claim_next` selects the lowest-id `pending` row with
  `FOR UPDATE SKIP LOCKED LIMIT 1`. In the same transaction it sets `running`, `attempts += 1` and `started_at`.
- **Consequences:** No double claims (tested concurrently). Tasks have no priority. `attempts`
  counts claims, and recovery uses it.
- **How to undo:** Change the ORDER BY or add a priority column. The locking clause stays.

## D-003 Per-target advisory lock held for the whole task run
- **Context:** Two tasks on the same VM must not interleave (FR-2.5).
- **Decision:** `tasks.locks.target_lock` runs `pg_advisory_xact_lock(ns, id)` on its own
  connection and holds it for the whole `run_task`. Namespaces are vm=1 and host=2, so VM 5 and
  host 5 do not collide. The lock is released by rolling back that connection's transaction, or
  when the connection dies.
- **Consequences:** Tasks on the same target run one after another. Tasks on different targets
  run in parallel. A crashed worker never leaves a lock behind. Each running task uses one extra
  DB connection.
- **How to undo:** Use a session-level lock with an explicit unlock in `finally`.

## D-004 One worker process with N threads, enforced by a singleton lock
- **Context:** Recovery treats every `running` task as orphaned. That is only safe if no other worker is alive.
- **Decision:** `worker_main` takes `pg_try_advisory_lock(0, 0)` on a dedicated NullPool
  connection and exits with code 1 if the lock is held. It then runs `recover_orphans` and starts
  `worker_threads` threads.
- **Consequences:** Recovery is simple and correct. Throughput is limited to one process, which
  is enough for 2–3 hosts.
- **How to undo:** For several processes, add a worker_id and lease expiry to tasks, and only
  recover tasks whose lease has expired.

## D-005 Idempotency-Key semantics
- **Context:** FR-2.3 asks for safe client retries.
- **Decision:** The optional `Idempotency-Key` header is stored in `tasks.idempotency_key`
  (UNIQUE) together with `request_hash`, the sha256 of the canonical sorted-key JSON body.
  - Same key, same body: return the original task as a 202 with `Idempotent-Replayed: true`.
    No new rows are written.
  - Same key, different body: 409 `{detail, task_id}` pointing at the original task.
  - Key that is empty or longer than 255 characters: 400.
  - Two concurrent requests with the same key: the loser's transaction (target row + task) is
    rolled back, and it returns the winner's task, whatever error it hit.
- **Consequences:** The key belongs to the task row and keys never expire. A replay returns the
  task, not a re-run of the validation.
- **How to undo:** Move keys to their own table with a TTL.

## D-006 Step log as JSONB on the task, one commit per transition
- **Context:** A restarted worker must know how far a task got (FR-2.4, CLAUDE.md).
- **Decision:** `tasks.steps` is a list of `{name, status, started_at, finished_at, error}`. The
  executor fills it in from the TaskType the first time the task runs. Each transition
  (`pending→running→done`, `failed`, `undone`, `undo_failed`) is its own committed transaction,
  written before and after the side effect. The field is called `error`, not `detail`.
- **Consequences:** Resume skips `done` steps and re-runs the `running` step, so every
  `do`/`undo` must check real driver state first. The steps for a type must not be reordered
  while tasks of that type are in flight.
- **How to undo:** Add a `task_steps` table. The executor API stays the same.

## D-007 Rollback semantics (as built; stop-at-first-failure rule superseded by D-013)
- **Decision:** `rollback_task` walks the steps in reverse and undoes any step marked
  `done|failed|running|undo_failed`. The failed or interrupted step is included, so its partial
  effect is undone too. If every undo succeeds, the type's `on_rolled_back` hook runs and the task
  ends `rolled_back`. The **first** undo that fails stops the rollback, and the task ends `failed`
  with both errors.
- **Consequences:** It is simple, but one bad undo leaves the later undos unrun (known gap, Q01).
- **How to undo:** Superseded by D-013, which continues past a failed undo.

## D-008 Orphan recovery rules
- **Decision:** At worker start, `recover_orphans` looks at every `running` task:
  - unknown type: `failed`
  - `attempts >= max_attempts` (default 3), or any step already `failed`, `undone` or
    `undo_failed`: finish the rollback and never run forwards again
  - otherwise: back to `pending`, so it resumes
  While running, an infrastructure (DB or lock) error requeues the task, or fails it once
  attempts run out.
- **Consequences:** A crash loop on one task ends after `max_attempts` claims. Proven by the SIGKILL
  test at 6 kill points (3 steps × before/after the driver call) plus the rollback and max-attempts cases.

## D-009 FakeDriver: not idempotent, state optionally in a file
- **Decision:** `driver/fake.py` raises on the same conditions libvirt does (DomainExists,
  DomainNotFound, already running, and so on). Idempotency therefore has to live in the task
  steps. State can be kept in a JSON file (`MINIHCI_FAKE_STATE_FILE`) under `fcntl.flock` with an
  atomic replace, so subprocess workers and tests share it and it survives SIGKILL. Delays
  before and after the effect, and counted failures, can be injected for each method.
- **Consequences:** Crash tests can kill a worker either before or after a side effect.

## D-010 Rolled-back VM rows are kept and marked absent
- **Decision:** `vm.create`'s `on_rolled_back` sets `desired_state = actual_state = 'absent'`
  and keeps the row for audit.
- **Consequences:** Because `vms.name` is UNIQUE, the name stays taken and a re-create returns 409. Q01 fixes this.

## D-011 The API never touches the driver; disk path is derived
- **Decision:** `minihci.api` never imports `minihci.driver` (a test checks this). Even host
  registration probes the host inside the `host.register` task, and the host is `registering`
  until that task finishes. The VM disk path is `{storage_root}/{name}.qcow2`, and the image is
  stored as given (validation comes in Q01).
- **Consequences:** Every mutating request is 202 + task_id. `GET /tasks/{id}` is how a client
  sees the result.

## D-012 VM names are unique only among live VMs (partial unique index)
- **Context:** D-010 keeps rolled-back VM rows with `desired_state='absent'`, but `vms.name` is a
  plain UNIQUE, so a failed create blocks the name forever (Q01). Options: free the name on
  rollback (rename the row, e.g. `web1~42`) or a partial unique index.
- **Decision:** Alembic revision `0002` drops the plain unique constraint on `vms.name` and creates
  `uq_vms_name_live`: `UNIQUE (name) WHERE desired_state <> 'absent'`. `models.Vm` declares the same
  index (`Index(..., unique=True, postgresql_where=...)`). The API name check in `api/vms.py`
  filters `desired_state != 'absent'`; the IntegrityError fallback stays for races.
- **Consequences:** Audit rows keep their real name; several `absent` rows may share a name.
  A VM whose rollback *failed* keeps `desired_state` non-absent (on_rolled_back does not run), so
  its name, and the derived disk path `{storage_root}/{name}.qcow2`, stay reserved until it is
  cleaned up. Q03's delete frees a name the same way (sets `absent`). Lookups by name must filter live rows.
- **How to undo:** Downgrade 0002. It does not modify data: if two rows share a name, restoring
  `vms_name_key` fails with an IntegrityError and the transaction rolls back. Delete the duplicate
  `absent` rows first (and their `task_steps`/`tasks` if FKs require it), then re-run the downgrade.

## D-013 Rollback runs every undo, even after one fails
- **Context:** D-007 stops at the first failing undo, leaving earlier steps' effects in place.
- **Decision:** `rollback_task` walks all eligible steps in reverse; a failing undo is marked
  `undo_failed` with its error, and the loop continues. If any undo failed, `on_rolled_back` is
  skipped and the task ends `failed` with every undo error joined into `tasks.error`; otherwise
  as before. A worker dying mid-rollback leaves the task `running` with `undone`/`undo_failed`
  steps, so `recovery._was_rolling_back` resumes the rollback; `undo_failed` steps are retried
  (undos check state first, so this is safe). Supersedes the stop rule in D-007.
- **Consequences:** Maximum cleanup per attempt. A `failed` task with `undo_failed` steps needs
  an operator (or a later reconciler) to clean up; it is not retried automatically once terminal.
- **How to undo:** Restore the early `return` in the except branch of `rollback_task`.

## D-014 Image paths must live under `Settings.image_root`
- **Context:** `VmIn.image` was stored as given (D-011), so a client could point a VM at any host file.
- **Decision:** New `Settings.image_root` (env `MINIHCI_IMAGE_ROOT`, default `/srv/minihci/base`).
  The API (422) requires an absolute path with no `..` component whose `os.path.realpath` lies
  under `realpath(image_root)`; this is a string/filesystem check only, never a driver call
  (D-011 holds). `vm_create.create_disk` re-runs the same helper before calling the driver and
  fails the step (task rolls back) if it no longer passes. One shared helper, e.g.
  `minihci.paths.resolve_image(path, root) -> str` raising `ValueError`.
- **Consequences:** Symlinks pointing out of the root are rejected wherever the path exists
  locally; on hosts where the API cannot see NFS, the check is lexical and the worker re-check is
  the backstop. A real-host driver should resolve on the host too (Q13 note).
- **How to undo:** Drop the validator; images are trusted again.

## D-015 Removed hosts are soft-deleted (status `removed`), not row-deleted
- **Context:** FR-1.1 needs host removal. `vms.host_id` is a FK to `hosts`, and D-010/D-012 keep
  `absent` VM rows for audit, so deleting the host row would break the FK or the audit trail.
  `hosts.name`/`libvirt_uri` are plain UNIQUE, so a soft-deleted host would block re-registering
  the same machine (the same trap D-012 fixed for VMs).
- **Decision:** Revision `0003` adds `removing` and `removed` to `ck_hosts_status` and replaces the
  two plain unique constraints with partial unique indexes `WHERE status <> 'removed'`. `DELETE
  /hosts/{id}` sets `removing` and enqueues `host.remove`, whose last step sets `removed`. Removal
  makes no driver calls: it only stops MiniHCI managing the host, so an unreachable host can be
  removed. `GET /hosts` and the register conflict check skip `removed` rows; `POST /vms` already
  requires `ready`.
- **Consequences:** The row stays for audit and old VM rows keep a valid FK. Q10 will rewrite
  `ck_hosts_status` again and must keep `removing`/`removed`. Heartbeat/placement must skip them.
- **How to undo:** Downgrade 0003. It fails if any `removing`/`removed` rows exist; delete those
  (and their `absent` VMs) first.

## D-016 Removing a host with VMs: 409 up front, re-checked in the task
- **Context:** FR-1.1 / Q02 allow either a 409 or a task failure when VMs are placed on the host.
- **Decision:** Both, layered. The API counts live VMs (`desired_state <> 'absent'`) inside the
  idempotent `create` transaction while holding `SELECT ... FOR UPDATE` on the host row, and
  returns 409 listing the VM names (no task row). `POST /vms` takes `FOR SHARE` on the host row,
  so a create and a remove on one host serialize; whichever commits second sees the other's effect
  (status `removing`, or the new VM). The `host.remove` step `check_empty` re-counts as a
  backstop; if it finds VMs the task rolls back (`rolled_back`, error names the VMs) and
  `on_rolled_back` restores the previous status from `params.prev_status`. DELETE on a
  `registering` or `removing` host → 409; on a missing or `removed` host → 404. An idempotent
  replay returns the original task (D-005), even after the host is removed.
- **Consequences:** The common case gets an immediate, clear error; the rare race ends as a
  rolled-back task, never a host removed with VMs still on it. VMs whose rollback failed still
  count as live, so they block removal until cleaned up.
- **How to undo:** Drop the API check; the task check alone still keeps removal correct.

## D-017 VM power operations: desired state set by the API, no-op when already there
- **Context:** FR-1.3 asks for start, stop (graceful and forced) and reboot. The PRD does not say
  what happens on a wrong-state request, how long a graceful stop waits, or what undo means here.
- **Decision:** The API sets `desired_state` (start/reboot → `running`, stop → `stopped`) in the same
  transaction as the task row, holding `SELECT vm FOR UPDATE`. Workers set `actual_state`. Each step
  checks `domain_state` first. Starting a running VM, or stopping a stopped one, succeeds without
  calling the driver. Rebooting a stopped VM starts it. Graceful stop calls the new
  `HostDriver.shutdown_domain`, then polls until `shut_off` or `Settings.vm_shutdown_timeout_s`
  (default 60). On timeout the step fails. It never falls back to force on its own: the client
  sends `{force: true}`, which uses `destroy_domain`. There are no undos, because "un-starting" a VM
  is not a useful rollback. Instead `on_rolled_back` copies the driver's real state into
  `actual_state` (`error` if the domain is gone).
- **Consequences:** Desired and actual state can differ after a failure. That is visible, and Q04's
  reconciler closes the gap. A reboot step re-run after a crash may reboot twice, which does no harm.
  The FakeDriver's graceful shutdown finishes at once. Real libvirt returns before the guest is off,
  so the polling loop matters there.
- **How to undo:** To auto-escalate, call `destroy_domain` after the timeout instead of failing.

## D-018 VM delete: forward-only steps; the name stays reserved until the delete finishes
- **Context:** FR-1.3 delete means destroy, undefine and delete the disk. The disk path comes from
  the name (D-011), and names are unique only among non-`absent` rows (D-012).
- **Decision:** The `vm.delete` steps are `destroy_domain`, `undefine_domain`, `delete_disk` and
  `mark_absent`, each skipped if already done. None has an undo: a deleted disk cannot be restored.
  The API does **not** set `desired_state='absent'`. Only `mark_absent`, the last step, does. So
  the name and the `{storage_root}/{name}.qcow2` path stay reserved until the disk is really gone.
  A new VM with the same name can therefore never have its fresh disk deleted. While a `vm.delete`
  is pending or running, other VM operations get 409. If the delete fails, the task ends
  `rolled_back` with `actual_state` taken from the driver (`error` if partly deleted). Sending
  DELETE again finishes the job.
- **Consequences:** Delete is retryable, never half-undone, and crash-safe at every kill point
  (Q03.5). Until it finishes, `desired_state` still says `running`/`stopped`, so the Q04
  reconciler must skip VMs that have a pending or running task.
- **How to undo:** Set `absent` in the API instead, and give each VM its own disk path (for
  example `{name}-{id}.qcow2`) so names and paths stop being linked.

## D-019 Reconciler: per-VM try-lock, fresh driver read, corrective tasks only
- **Context:** FR-1.4 and NFR-1 want the DB and libvirt reconciled on startup and every 30 s, with
  every drift logged and fixed. The reconciler must not race a task that is changing the same VM
  (D-003, D-018), and the constitution says only tasks change hosts.
- **Decision:** `reconcile.reconcile_once(session_factory, driver, settings)` runs in the worker
  process: one pass after `recover_orphans`, then a timer thread every `Settings.reconcile_interval_s`
  (default 30). For each host with status `ready` it calls `list_domains(uri)`. On HostUnreachable it
  logs and skips the host (host health is Q10's job). A domain with no live VM row on that host is
  logged as `unknown_domain` and never touched. Each VM (desired not `absent`, on that host) is handled
  in one short transaction: `pg_try_advisory_xact_lock(1, vm_id)` (same key as `target_lock`; busy →
  skip), `SELECT vm FOR UPDATE` (serializes with the API), skip if any task for the VM is
  `pending|running`, then re-read `domain_state` **while holding the lock** and decide. Domain gone →
  `actual_state='missing'` (D-020). Driver state differs from `actual_state` → copy the driver state
  in and log `actual_drift`. Driver state differs from `desired_state` → enqueue `vm.start` or
  `vm.stop {force: false}` and log `corrective_task`. The reconciler never calls a mutating driver
  method. Drift log lines are JSON with `event`, `vm_id`, `host_id`, `expected`, `observed`
  (JsonFormatter gains an optional `fields` extra).
- **Consequences:** No double work: a VM with a queued or running task is left to that task, and the
  next pass checks it again. A corrective task that keeps failing (for example a graceful-stop
  timeout) is re-enqueued every interval. That shows in the task list and does no harm. VMs on hosts
  that are not `ready` are not checked.
- **How to undo:** Drop the try-lock and rely only on the pending/running task check. That is
  smaller, but it races a task that finishes between the driver read and the DB write.

## D-020 A VM deleted outside MiniHCI becomes `missing` and is not re-created automatically
- **Context:** The PRD's M1 acceptance is that a VM deleted outside MiniHCI is "detected by
  reconciliation". NFR-1 says drift is fixed, but re-creating a domain someone removed on purpose
  (maybe with its disk) could bring back a VM the operator wanted gone.
- **Decision:** New `actual_state` value `missing` (revision 0004, added to `ck_vms_actual_state`).
  The reconciler sets it, logs `vm_missing` and enqueues no task. The operator decides: `DELETE
  /vms/{id}` (vm.delete skips steps already done, D-018) or a later re-create. A `missing` row keeps
  its name reserved (desired is not `absent`, D-012).
- **Consequences:** Detection is automatic; repair is a human decision. `GET /vms` shows `missing`.
  If the domain comes back, the next pass sees it and copies its real state in.
- **How to undo:** Enqueue `vm.create` for missing VMs whose disk still exists. Its steps check state
  first, so it would only define and start the domain.

## D-021 `/metrics` is computed from the DB at scrape time
- **Context:** NFR-4 wants Prometheus metrics for task counts, task/migration times and heartbeat
  status. Tasks run in separate worker processes, so in-process counters in the API would miss them,
  and the API must never import `minihci.driver`. The `migrations` table does not exist until Q08.
- **Decision:** `api/metrics.py` builds a fresh `CollectorRegistry` per request with one custom
  collector that queries Postgres: `minihci_tasks{type,state}` (gauge), `minihci_task_duration_seconds{type}`
  (histogram from `finished_at - started_at`, fixed buckets 0.1/0.5/1/5/15/60/300s),
  `minihci_host_status{host,status}` (one-hot, removed hosts excluded) and
  `minihci_host_last_heartbeat_timestamp_seconds{host}`. `minihci_migration_duration_seconds` and
  `minihci_migration_downtime_seconds` are emitted now with no samples; Q08 fills them from `migrations`.
- **Consequences:** Numbers are correct across processes and restarts, no multiprocess setup. Each
  scrape runs a few GROUP BY queries over the whole tasks table, fine at this scale. Histograms are
  recomputed, not true counters, so they shrink if task rows are ever deleted.
- **How to undo:** Switch to `prometheus_client` multiprocess mode with counters incremented in the
  executor, or add an index/rollup table if scrapes get slow.

## D-022 Placement: best-fit on allocated memory, 80% vCPU headroom, config row changed by a task
- **Context:** FR-3.2 asks for a documented policy when `POST /vms` names no host, and a 409 when
  nothing fits. Measured host load (Q07) does not exist yet, and the PRD does not say how the
  scheduler config is stored or changed. CLAUDE.md requires every mutating endpoint to write a task row.
- **Decision:** `scheduler/placement.py` `place(vm_req, hosts, usage, config)` is pure. Candidates
  are hosts whose status is in `PLACEABLE_STATUSES = ("ready",)` (Q10 changes it to `up`) and whose
  `cpu_total`/`mem_total_mb` are known. Usage is *allocated*, not measured: sums of `vcpus`/`mem_mb`
  of VMs on the host with `desired_state <> 'absent'` (pending creates count). A host fits if
  `mem_used + mem_mb <= mem_total_mb` and `vcpus_used + vcpus <= floor(cpu_total * cpu_headroom_pct / 100)`.
  Best-fit: pick the fitting host with the least free memory left after placement, ties → lowest id.
  None fits → `NoCapacity` → API 409 with no VM or task row. To stop two creates from both taking
  the last slot, the auto-placement path takes `pg_advisory_xact_lock(3, 0)` and `FOR SHARE` on
  the candidate hosts (keeps D-016's create/remove ordering). An explicit `host_id` keeps today's
  behaviour (no capacity check). Config lives in table `scheduler_config` (revision 0005): one row,
  `id = 1` (CHECK), `cpu_headroom_pct int NOT NULL DEFAULT 80 CHECK 1..100`, `updated_at`; seeded
  by the migration, and `load_config` returns defaults if the row is missing. `GET /scheduler/config`
  reads it. `PUT` goes through `run_idempotent` and enqueues `scheduler.config_set` (target_kind
  `scheduler`, lock namespace 3, target_id 1) whose single step overwrites the row → 202 + task_id.
- **Consequences:** Placement is deterministic and easy to table-test, and it ignores real load, so
  a busy host with little allocated memory still gets VMs until Q09 rebalances. The config change
  is async (visible after the worker runs it), the price of keeping the 202 rule. Q09 adds its
  threshold/cooldown columns to the same row.
- **How to undo:** Make `host_id` required again and drop `api/scheduler.py`, the task type and
  revision 0005. To switch to measured load, feed `place` the latest `host_metrics` instead of allocations.

## D-023 Host metrics: `host_stats` returns CPU % + used memory; collector thread appends rows, prunes after 24 h
- **Context:** FR-3.3 says to collect per-host CPU and memory usage at a fixed interval and store it.
  The PRD does not say what one sample holds, how it is stored, how long it is kept, or where the
  loop runs. Q09 (rebalancer) needs "average CPU over the last W minutes" per host, and it must be
  testable with FakeDriver.
- **Decision:** New `HostStats(cpu_pct: float, mem_used_mb: int)` dataclass and HostDriver method
  `host_stats(uri) -> HostStats` (HostUnreachable for an unknown/down host). `cpu_pct` is host-wide
  busy CPU, 0..100. The libvirt driver (Q13) gets it from two `getCPUStats` samples about 0.5 s apart
  and `getMemoryStats`. FakeDriver keeps `load: {cpu_pct, mem_used_mb}` in the host dict, set by the
  test helper `set_load(uri, cpu_pct, mem_used_mb=None)` (ValueError outside 0..100). Without a set
  load it returns `cpu_pct=0.0` and `mem_used_mb` = sum of `mem_mb` of running domains. It goes through
  `_call`, so delay/fail injection works. Table `host_metrics` (revision 0006): `id bigserial PK`,
  `host_id int NOT NULL FK hosts(id) ON DELETE CASCADE`, `collected_at timestamptz NOT NULL DEFAULT now()`,
  `cpu_pct real NOT NULL CHECK 0..100`, `mem_used_mb int NOT NULL CHECK >= 0`, index
  `(host_id, collected_at)`. `scheduler/collector.py` `collect_once(session_factory, driver, settings)`
  reads hosts with status in `placement.PLACEABLE_STATUSES` (short session, no locks), calls the driver
  outside any transaction, then inserts one row per answering host and runs
  `DELETE ... WHERE collected_at < now() - retention`, all in one transaction. Timestamps come from the
  DB clock. A host that fails is logged (`host_stats_failed`, host_id) and gets no row. New settings
  `collector_interval_s = 10`, `metrics_retention_h = 24`. `collector.run_forever` copies the reconciler
  loop pattern and runs as a `collector` thread in `worker_main`. It writes no tasks: a metric sample is an
  observation, not a change to desired state (the reconciler already writes `actual_state` directly).
- **Consequences:** Simple append-only table. At 3 hosts and 10 s it holds about 26k rows, and pruning is
  one indexed DELETE per pass. Samples stop when the worker is down. That is acceptable because the
  rebalancer also runs in the worker. Because the Fake's default load is 0 % CPU, nothing gets rebalanced
  unless a test calls `set_load`. The libvirt `host_stats` call takes about 0.5 s per host, which is fine
  at a 10 s interval.
- **How to undo:** Drop the `collector` thread and revision 0006, then remove `host_stats` from the
  protocol and FakeDriver. For longer history, add a downsampled rollup table instead of raising retention.

## D-024 Live migration: `vm.migrate` task with a `migrations` row, no undo, rollback syncs to where the domain is
- **Context:** FR-3.1 wants a running VM live-migrated to a named host through the API. FR-3.5 wants
  duration and downtime recorded for every migration, and Q05's `minihci_migration_*` histograms are
  waiting for that data (D-021). The PRD lists table `migrations(id, vm_id, src_host, dst_host,
  duration_ms, downtime_ms)` and says nothing about crash handling, capacity checks or what the fake does.
  A crash can land between libvirt finishing the migration and us writing the numbers down.
- **Decision:** New HostDriver method `migrate_live(src_uri, dst_uri, name) -> MigrationResult(duration_ms,
  downtime_ms)`. It raises DomainNotFound if the domain is not on src, DriverError if it is not running or
  dst already has that name, and HostUnreachable for an unknown host. On any error the domain stays on src.
  FakeDriver moves the domain dict (state and boot_count kept) from src to dst inside `_call`, so
  delay/fail injection works and failures happen before the effect. It returns the measured wall time as
  `duration_ms` and a fixed `FAKE_DOWNTIME_MS = 30`. Q13's libvirt driver measures downtime by polling.
  Table `migrations` (revision 0007): `id bigserial`, `task_id` FK tasks UNIQUE, `vm_id` FK vms,
  `src_host_id`/`dst_host_id` FK hosts, `status` CHECK `running|succeeded|failed`, `started_at` default
  now(), `finished_at`, `duration_ms`/`downtime_ms` int NULL CHECK >= 0.
  Task `vm.migrate` (target_kind vm; params `src_host_id`, `dst_host_id`) has four steps and no undos:
  1. `check_destination`: dst is in `PLACEABLE_STATUSES` and `placement.fits(vm, dst, usage excluding
     this VM, config)` under `pg_advisory_xact_lock(3,0)`. It skips the check if the domain is already on
     dst. It inserts the migrations row with `ON CONFLICT (task_id) DO NOTHING`.
  2. `migrate`: read the domain state on both hosts. src running and dst None → `migrate_live`, then write
     the numbers to the row. src None and dst running → already moved by a crashed attempt; leave the
     numbers NULL and log `migration_unmeasured`. Any other combination → raise.
  3. `switch_host`: `vms.host_id = dst` (idempotent).
  4. `finish`: row `succeeded`, `finished_at`.
  `on_rolled_back` (`sync_host`) checks where the domain really is and closes a still-`running` row
  (amended by Q08.7): dst only → `vms.host_id = dst`, row `succeeded`; src only → `vms.host_id = src`, row
  `failed`; on both or neither → `vms.host_id` unchanged, row `failed`, warning event
  `migrate_placement_ambiguous` (on_src, on_dst). Any change to `vms.host_id` logs `migrate_host_drift`
  (task_id, vm_id, expected, observed). So a task can end `rolled_back` while its migrations row is
  `succeeded`: the migration really happened, a later step failed. The API checks up front and returns 409 for: VM not running, an open
  `vm.delete`/`vm.migrate` task, dst equal to the current host, dst not placeable, or no capacity
  (same lock (3,0)).
- **Consequences:** A failed migration leaves the VM on src and the DB consistent. A crash right after
  libvirt finishes still succeeds on retry, but that one migration has no timing, and metrics skip NULLs.
  Capacity is checked, not reserved: a create that lands on dst between the check and `switch_host` can
  overcommit it. The rebalancer (Q09) will reuse `placement.fits` and enqueue `vm.migrate`.
- **How to undo:** Drop the route, `tasks/types/vm_migrate.py`, revision 0007 and `migrate_live`. To
  reserve capacity, count the VMs of open `vm.migrate` tasks against dst in `usage`.

## D-025 Rebalancer: DB-only pass, VM chosen by projected max load, cooldowns read from `tasks`
- **Context:** FR-3.4 says: when a host stays above a configurable load threshold, choose a VM, migrate
  it to the least-loaded host, and use a cooldown to avoid ping-pong. The PRD does not define "stays
  above", how to choose the VM, the cooldown values, or where cooldown state lives. There are no per-VM
  CPU stats, only host-wide `host_metrics.cpu_pct` (D-023).
- **Decision:** Settings live in the `scheduler_config` row (revision 0008): `rebalance_enabled` (default
  **false**), `rebalance_cpu_threshold_pct` 80, `rebalance_window_s` 300, `rebalance_vm_cooldown_s` 1800,
  `rebalance_host_cooldown_s` 600. They change through `PUT /scheduler/config` (partial update, same task).
  `scheduler/rebalance.py` runs as a `rebalancer` thread in the worker every `rebalance_interval_s` = 30 s.
  It reads only the DB and never calls the driver. One pass:
  1. Disabled, or any `vm.migrate` task pending/running → do nothing (at most one migration in flight).
  2. Host load = the average `cpu_pct` over the last `rebalance_window_s` for hosts in `PLACEABLE_STATUSES`
     that have **>= 2 samples** ("stays above", so one spike does not count). A host is hot if load > threshold.
  3. VM choice: candidates are VMs on the hottest host with desired = actual = running and no VM cooldown.
     A VM's load share is estimated from allocations: `share = src_load * vm.vcpus / sum(vcpus on src)`.
     On dst it adds `share * src.cpu_total / dst.cpu_total`. For each candidate, dst = the least-loaded
     host (with samples, not in host cooldown, below threshold) that passes `placement.fits` (usage
     without this VM). Pick the move with the lowest projected max(src_after, dst_after). Ties go to the
     smaller `mem_mb` (cheaper to migrate), then the lower VM id. If no move lowers the max below the
     current src load → nothing.
  4. Enqueue `vm.migrate` (params `src_host_id`, `dst_host_id`, `reason: "rebalance"`) in one transaction
     under `pg_advisory_xact_lock(3,0)`, after re-checking the VM and `fits`, the same checks as the API.
  Cooldowns need no new table. A VM cools down if it has a `vm.migrate` task created within the VM
  cooldown. A host cools down if a `vm.migrate` task with that host as `src_host_id` or `dst_host_id` was
  created within the host cooldown. Manual migrations count too.
- **Consequences:** The rule is easy to explain and to table-test, and only one VM moves per pass. The
  per-VM share is a guess based on vCPUs, so a busy 1-vCPU VM may be underestimated. Host cooldown
  (10 min) is twice the window (5 min), so the metrics after a move settle before that host is judged
  again. VM cooldown (30 min) stops a VM from bouncing between hosts. Default-off means a fresh install
  never migrates on its own.
- **How to undo:** Stop the `rebalancer` thread and drop revision 0008 (or keep
  `rebalance_enabled=false`). For a better VM choice, collect per-VM CPU (`domain.getCPUStats`) and
  replace the share estimate.
- **Clarification (Q09.1 review):** "Partial update" covers every field, `cpu_headroom_pct` included.
  All `PUT /scheduler/config` body fields are optional. A body that sets no field (`{}` or all null)
  returns 422. The task params hold only the keys that were sent, and `scheduler.config_set` updates
  only those columns.
- **Lock order (Q09.3 review):** Any transaction that takes more than one of these locks takes them in
  this order: (1) the `vms` row `FOR UPDATE`, (2) `pg_advisory_xact_lock(3,0)`, (3) `hosts` rows
  `FOR SHARE`. `POST /vms/{id}/migrate` already does this. The rebalancer's enqueue transaction does the
  same: lock the VM row, re-check the VM and its open delete/migrate tasks, then take the advisory lock,
  then re-check "no `vm.migrate` pending/running", then lock dst and run `fits`. Paths that take only
  (2)+(3), such as `_auto_place` and `vm.migrate` `check_destination`, are compatible. `check_destination`
  takes a FK key-share on the VM row after (2). That is safe because a caller that holds (1) on that VM
  sees its open migrate task and exits before it asks for (2).

## D-026 Libvirt driver: optional extra, disk host, first `host_stats` sample, state mapping
- **Context:** Q10 implements HostDriver with libvirt-python while the hosts stay off. The PRD does not say
  whether libvirt-python is a hard dependency, which host serves the URI-less disk methods
  (`disk_exists`, `create_disk_overlay`, `delete_disk`), what the first `host_stats` call returns, how
  libvirt domain states map to `DomainState`, or what to record when migration job stats lack a key.
- **Decision:** (1) `libvirt-python` is an optional extra `[libvirt]`; `minihci.driver.libvirt` imports it
  lazily, so `import minihci` and the default FakeDriver install never need it. A missing module raises
  `DriverError` naming the extra. (2) Disk methods run on the first host that answers `ping` from the new
  setting `libvirt_storage_uris: list[str]` (in order); none answering raises `HostUnreachable`. The NFS
  share is common to all hosts, so any host works, and `pool.refresh(0)` precedes every path lookup.
  (3) `host_stats` keeps the previous `getCPUStats` sample per URI; on the first call (no previous
  sample) it takes two samples 0.5 s apart (sleep injectable) and returns a real float, as D-023 says.
  It never returns `None`, because `HostStats.cpu_pct` and `host_metrics.cpu_pct` are NOT NULL.
  (4) RUNNING = running, blocked, paused, shutting down, pmsuspended; SHUT_OFF = shutoff, crashed, nostate.
  (5) `MigrationResult.downtime_ms` becomes `int | None` (the column is already nullable). `duration_ms`
  stays `int`: `time_elapsed`, else the wall clock around `migrate3`. (6) `ping(uri, timeout=3)` is a
  public LibvirtDriver method, not yet in the Protocol; Q12 adds it to the Protocol and FakeDriver.
  (7) Connection teardown: every close first calls `unregisterCloseCallback()` (errors ignored), because a
  registered callback holds a ref on the conn. The close callback never closes or unregisters (that can
  deadlock inside libvirt); it evicts and queues the conn, and the next `get()`/`drop()` closes queued conns
  outside the lock. `ping` and failed driver ops evict only the conn they used, never a newer one. Only the party that removes a conn from the cache closes it. Every
  successful eviction (close callback, ping, failed op) runs the `on_close` hook once, which drops that URI's `host_stats` sample.
- **Consequences:** Default installs stay light. The real driver needs `uv sync --extra libvirt`. Disk
  operations depend on at least one storage host being reachable. During HA they fail over through the list.
  The first stats call per host costs 0.5 s once.
- **How to undo:** Move libvirt-python into the main dependencies. Pass a URI to the disk methods (a
  Protocol change). Return `None` on the first sample after making the column nullable.
- **Amendment (Q10.7 ruling):** (8) A pre-existing `minihci-images`/`minihci-vms` pool whose `target/path` (normalized) differs from `image_root`/`storage_root` raises `DriverError` naming both paths; the driver never redefines or moves a pool. Settings defaults stay as they are (FakeDriver and tests use them); a libvirt deployment must set `MINIHCI_IMAGE_ROOT=/var/lib/minihci/images` and `MINIHCI_STORAGE_ROOT=/var/lib/minihci/vms` to match the hosts' pools. Undo: redefine mismatched pools instead of raising.

## D-027 Heartbeats: timings, `ready` is "up", dead measured from the last good heartbeat
- **Context:** FR-4.1 wants `up -> suspect -> dead` from missed heartbeats. The PRD gives no exact numbers, and the existing host status for a healthy host is `ready`. The Q10 probes measured: a long-off host fails a connect in ~3 s, a just-died host hangs, an open connection's keepalive breaks in ~6 s.
- **Decision:** (1) Settings: `heartbeat_interval_s=5`, `heartbeat_ping_timeout_s=3` (shorter than the interval, so pings never stack), `heartbeat_suspect_misses=3`, `heartbeat_dead_after_s=30`. All configurable; tests use small values. (2) `ready` stays the name for the PRD's "up" (no rename): placement, the reconciler and the collector already filter on `ready`, so suspect and dead hosts drop out of them with no extra code. (3) The pure function `ha.health.next_health` does the transitions: a good ping gives `ready`, missed 0, last_heartbeat=now; a miss adds 1 and gives `suspect` at >= 3 misses, and `dead` when `now - last_heartbeat >= 30 s` AND at least as many misses as the suspect threshold (3). Dead is measured from the last good heartbeat, not from the first miss, so it does not depend on how many pings ran. A dead host stays dead until a ping succeeds. (4) A host that never had a heartbeat starts its dead clock at its first miss, and that time is stored in `last_heartbeat` as the baseline. (5) A `dead` host that answers a ping again stays `dead` until `host.reclaim` has removed its stale VM copies and then sets it `ready` (see D-028 point 5). Without a reclaim path it would go straight to `ready`.
- **Consequences:** Suspect after about 15 s, dead after 30 s (about 6 missed pings), about 5 s of margin over the 15 s NFS lease so HA does not start while the old host may still hold the disk lock. A host with a long-ago heartbeat (control plane was down) needs 3 consecutive misses before it goes dead, so one slow ping cannot start failover (amended after review).
- **How to undo:** Change the four settings, or measure dead from the first miss.

## D-028 HA restart: fence on `dead` only, level-triggered failover pass, returning host stays `dead` until reclaimed
- **Context:** FR-4.2/4.3/4.4 want a dead host's VMs restarted elsewhere, never before the old host is confirmed dead, and VMs that cannot be placed shown as `pending_restart`. The HA dry run showed that `create()` on the survivor blocks until the dead host's NFS lease (15 s) frees QEMU's image lock (~27.5 s after the cut). A returning host may still have the VM defined and shut off, or may not have it defined at all.
- **Decision:** (1) Fencing rule: a VM is restarted elsewhere only while its host is `dead` (D-027: 30 s without a good heartbeat). `choose_host` checks this under the scheduler lock (3, 0) in the same transaction as placement and refuses if the host is not `dead`. The full fence runs again in `start_domain`, the only step that can create a second running copy, every time it is about to call start (so a resumed task is fenced too): the old host must still be `dead` (read under lock (3, 0)), and the old host is asked for the domain; if it answers RUNNING the step fails rather than start a second copy (HostUnreachable means go ahead). The probe runs once per run, in `start_domain` only, so a hanging connect to a just-died host (up to ~40 s) is paid at most once. (2) Trigger: `ha/failover.py` `failover_once(session_factory, settings)` runs after each heartbeat pass. It only touches the DB and is level-triggered. Candidates are VMs with `desired_state='running'`, `actual_state` in (`running`, `pending_restart`), on a host whose status is `dead` (joined in the query; VMs on other hosts are never row-locked by this pass). Each candidate's row is taken with `FOR NO KEY UPDATE SKIP LOCKED` (a locked VM is skipped until the next pass, so the heartbeat thread never waits on a reconciler or API lock, and the lock does not conflict with the `FOR KEY SHARE` a `migrations` FK insert needs). A VM with any open `vm.%` task (pending/running) is skipped *before* the scheduler lock (3, 0) is taken, which also prevents the deadlock with `vm.migrate.check_destination` (which takes (3, 0) and then references the VM row). A `pending_restart` VM whose host is no longer `dead` is not touched by this pass; the reconciler overwrites `actual_state` from the observed domain once the host is `ready`. For each remaining VM it runs `place()`. If a host fits, it enqueues `vm.ha_restart` with params `dead_host_id` and `failed_at`. If none fits, it sets `actual_state='pending_restart'` and logs `ha_pending_restart`. The same pass retries `pending_restart` VMs, so they restart once capacity appears, and it creates no task until something fits. Because the pass is level-triggered, an enqueue lost in a crash happens again on the next pass. (3) Steps: `choose_host` (scheduler lock, fence check, place, insert an `ha_restarts` row; a resumed task reuses the row's host); `define_domain` on the target using the same `disk_path` (no new disk); `start_domain` (no short timeout, because it may block until the lease expires); `mark_running` (sets `vms.host_id`, `actual_state='running'`, and `ha_restarts.running_at` and `recovery_ms`). The undo for define and start is destroy + undefine on the target. `on_rolled_back` (DB only, no driver call) sets `pending_restart` only if the old host is still `dead` and `vms.host_id` still points at it, so the next pass retries; otherwise (fence refused because the host is `suspect`/`ready`) it leaves `actual_state` unchanged and logs `ha_restart_abandoned`. Known edge: a `dead` host that answers RUNNING is labelled `pending_restart`, and the level-triggered pass re-enqueues a fast-failing task every 5 s until that host is reclaimed; accepted. Restarts of different VMs run in parallel on the worker threads (`worker_threads`, default 2). (4) Recovery time = `running_at - failed_at`, where `failed_at` is the host's `last_heartbeat` (its last good heartbeat) at the moment the pass enqueued the restart. That overstates the real time by up to one interval (5 s), which is the conservative side. `next_health` always sets `last_heartbeat` before a host can become `dead`, so NULL only happens if a host is set `dead` by hand; then `failed_at = now - heartbeat_dead_after_s` (the latest time the host can have failed; hosts have no `created_at` column, and none is added for this). It is stored in `ha_restarts` and exported in `/metrics`. (5) Returning host: when a `dead` host answers a ping again, the heartbeat leaves it `dead` (missed reset to 0, last_heartbeat updated) and enqueues `host.reclaim` unless one is already open. For each domain on that host, `host.reclaim` first takes the advisory lock of every VM row with that name (ascending id; no row means no lock), and only then decides, under the lock, whether the copy is stale. Stale means the VM row points at another host, or is `absent`, or there is no row. A stale copy is destroyed if running, undefined if defined, and logged as `stale_copy_removed`. It keeps domains whose VM row still points at this host (for example `pending_restart` VMs). Because the worker holds a VM's lock for a whole task, a `vm.ha_restart` that is still running (for example blocked in `start_domain` for ~27 s) makes reclaim wait until `mark_running` has moved `vms.host_id`, so the copy is then seen as stale. A `vm.ha_restart` that is still pending when reclaim keeps a copy is stopped by the fence: `start_domain` also refuses while the old host has an open (pending/running) `host.reclaim` task, so a kept copy never gets a second copy elsewhere. Lock order is host lock (held by the worker) then VM locks. No VM-target task takes a host lock, so this cannot deadlock. Its last step sets the host to `ready` only if it is still `dead` with `missed_heartbeats = 0`. Otherwise it leaves the host `dead`, and the next ping that answers enqueues a new reclaim.
- **Limits (no STONITH):** `dead` only means "the control plane cannot reach it". A partitioned host that is still alive keeps its VM running and keeps renewing its NFS lease. QEMU's image lock then holds, and our start blocks or fails. So the disk is protected by NFS locking plus QEMU image locking, not by MiniHCI. If that lock is lost (NFS mounted `nolock`, local storage, or a lease that expires while the old guest is still writing), two copies could write the same disk. Real fixes: power fencing (IPMI/Redfish STONITH before the restart), storage leases (sanlock or a virtlockd lockspace on the shared disk), or a host watchdog that kills its own VMs when it loses contact. Expected recovery is max(dead timeout 30 s, lock release ~27.5 s) plus the start, so about 30–35 s (PRD target: 60 s). A `dead` timeout below ~30 s gains nothing, because the start would only wait for the lock.
- **Consequences:** One new revision: `actual_state` gains `pending_restart`, plus an `ha_restarts` table. A returning host shows `dead` until its reclaim finishes (a few seconds). The failover pass adds a few DB queries every 5 s.
- **How to undo:** Remove the failover call from the heartbeat thread and drop the two task types. For real fencing, add a `fence_host` driver call and a step before `choose_host`.

## D-029 CLI: Typer over httpx, exit codes, output format
- **Context:** FR-1.5 asks for "a small CLI that wraps the API" and fixes nothing else. Scripts (Q15) need to tell a failed task from a bad request from an unreachable API.
- **Decision:** `src/minihci/cli.py`, a Typer app installed as the `minihci` console script, that only talks HTTP (httpx). It never imports the DB or drivers. Base URL comes from `--api`, then `MINIHCI_API`, then `http://localhost:8000`. Output is a plain aligned table (lists) or `key: value` lines (single objects); `--json` prints the API JSON body unchanged. Mutating commands print the `task_id`, and with `--wait` they then wait. Exit codes: 0 ok; 1 the API rejected the request (4xx, `detail` printed on stderr); 2 the task ended `failed` or `rolled_back` (its `error` printed); 3 the API cannot be reached or returned 5xx; 4 `tasks wait` timed out. `tasks wait` polls `GET /tasks/{id}` every `--interval` s (default 1), `--timeout` default 600 s. Tests inject a transport through a `make_client(api)` function.
- **Consequences:** `typer` and `httpx` become runtime dependencies. No auth, no config file, no client-side retries (`--idempotency-key` makes a retry by the user safe).
- **How to undo:** Delete `cli.py`, the `[project.scripts]` entry and the two dependencies; the API is unchanged.

## D-030 Migration to an unknown destination host returns 409, not 404
- **Context:** FR-3.1 (live-migrate to a named host): `POST /vms/{id}/migrate` takes the destination as `host_id` in the body. Q08.4 (LOG iter 26, commit cb8438b) chose 409 for a destination id that does not exist, but no decision entry recorded it.
- **Decision:** 404 is kept for the resource in the URL path (the VM). A body `host_id` that does not exist, or names a removed or non-`ready` host, is treated the same way: "destination host is not placeable", 409. The check is `dst is None or dst.status not in PLACEABLE_STATUSES` in `api/vms.py`, so one branch covers missing, soft-deleted (D-015) and unhealthy hosts.
- **Consequences:** A client cannot tell "no such host" from "host not usable" by status code alone; the `detail` text is the same. Callers (the CLI exits 1 on any 4xx, D-029) treat both as a rejected request, which is what they would do anyway. A removed host's id behaves the same as an id that never existed.
- **How to undo:** In the migrate handler, split the branch: `if dst is None: raise HTTPException(404, ...)` before the status check, and change the test that expects 409 for an unknown id.

## D-031 Scheduler-config task lock (3, 1) does not serialise with the placement lock (3, 0)
- **Context:** `scheduler.config_set` runs as a task with `target_kind='scheduler'`, so the worker holds advisory lock (3, 1) (namespace `scheduler`=3, target id 1) for it. Placement (`POST /vms` auto-place, migrate destination check, `vm.migrate` re-check, HA `choose_host`, rebalancer) takes (3, 0). The Q06.5 review (LOG iter 19) noted the two never wait on each other; no decision entry recorded that this is intended.
- **Decision:** Leave them separate. (3, 0) protects allocated *usage* (two placements must not both fill the last slot); it is not meant to protect the config. The config change is one atomic upsert of row `id=1`, and each placement reads the config once with `load_config` inside its own transaction, so it sees either the whole old row or the whole new row, never a mix. A placement that overlaps a config commit behaves as if it ran just before the change.
- **Consequences:** Right after a config change (for example a lower `cpu_headroom_pct`), one placement already in flight may still be judged by the old limit. This is the same state as a VM placed a moment before the change: existing VMs are never evicted when the limit drops, so "over the new headroom" is already a possible state. Placements do not queue behind config tasks.
- **How to undo:** Make `scheduler.config_set`'s `apply` step take `pg_advisory_xact_lock(3, 0)` in its transaction before the upsert (or give the task target id 0), so config changes and placements serialise.
