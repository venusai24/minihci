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
  `on_rolled_back` checks where the domain really is and sets `vms.host_id` to match. If it is still on src,
  the row becomes `failed`. The API checks up front and returns 409 for: VM not running, an open
  `vm.delete`/`vm.migrate` task, dst equal to the current host, dst not placeable, or no capacity
  (same lock (3,0)).
- **Consequences:** A failed migration leaves the VM on src and the DB consistent. A crash right after
  libvirt finishes still succeeds on retry, but that one migration has no timing, and metrics skip NULLs.
  Capacity is checked, not reserved: a create that lands on dst between the check and `switch_host` can
  overcommit it. The rebalancer (Q09) will reuse `placement.fits` and enqueue `vm.migrate`.
- **How to undo:** Drop the route, `tasks/types/vm_migrate.py`, revision 0007 and `migrate_live`. To
  reserve capacity, count the VMs of open `vm.migrate` tasks against dst in `usage`.
