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

## D-007 Rollback semantics (as built; Q01 changes the stop-at-first-failure rule)
- **Decision:** `rollback_task` walks the steps in reverse and undoes any step marked
  `done|failed|running|undo_failed`. The failed or interrupted step is included, so its partial
  effect is undone too. If every undo succeeds, the type's `on_rolled_back` hook runs and the task
  ends `rolled_back`. The **first** undo that fails stops the rollback, and the task ends `failed`
  with both errors.
- **Consequences:** It is simple, but one bad undo leaves the later undos unrun (known gap, Q01).
- **How to undo:** Q01 continues past a failed undo.

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
