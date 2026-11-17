import logging
import os

import pytest
from sqlalchemy import select

from minihci.config import Settings
from minihci.driver.base import DomainState
from minihci.driver.fake import FAKE_BLOCK_BYTES, FakeDriver
from minihci.models import Host, ProtectionPolicy, Replica, Vm
from minihci.tasks.engine import enqueue
from minihci.tasks.executor import StepContext
from minihci.tasks.types import vm_replicate as vr
from tests.helpers import add_ready_host
from tests.test_vm_migrate import URI_B, migrate
from tests.test_vm_power import URI, make_vm, run


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def vm(session_factory, driver, host_id):
    vm_id = make_vm(session_factory, driver, host_id)
    protect(session_factory, vm_id, retention=5)
    return vm_id


def protect(session_factory, vm_id, retention=5, enabled=True):
    with session_factory() as s, s.begin():
        s.merge(ProtectionPolicy(vm_id=vm_id, interval_s=60, retention=retention, enabled=enabled))


def replicate(session_factory, driver, vm_id):
    return run(session_factory, driver, "replicate", vm_id)


def rows(session_factory, vm_id=None):
    with session_factory() as s:
        q = select(Replica).order_by(Replica.id)
        return list(s.scalars(q))


def disk_path(session_factory, vm_id):
    with session_factory() as s:
        return s.get(Vm, vm_id).disk_path


def step_ctx(session_factory, driver, vm_id):
    with session_factory() as s, s.begin():
        tid = enqueue(s, "vm.replicate", vm_id, {}).id
    return StepContext(
        task_id=tid,
        target_id=vm_id,
        params={},
        driver=driver,
        session_factory=session_factory,
        settings=Settings(),
        log=logging.getLogger("minihci.tasks"),
    )


def test_first_replica_full_then_incremental(session_factory, driver, vm):
    driver.write_blocks(URI, "web1", [1, 2, 3])
    t1 = replicate(session_factory, driver, vm)
    assert t1.state == "succeeded"
    driver.write_blocks(URI, "web1", [2, 7])
    t2 = replicate(session_factory, driver, vm)
    assert t2.state == "succeeded"
    first, second = rows(session_factory)
    assert (first.mode, second.mode) == ("full", "incremental")
    assert first.status == second.status == "succeeded"
    assert first.chain_id == first.id and second.chain_id == first.id
    assert second.backing_path == first.dr_path and second.since_checkpoint == first.checkpoint
    assert first.dr_path == f"/srv/minihci/dr/vm{vm}-r{first.id}.qcow2"
    assert driver.read_disk(second.dr_path) == driver.read_disk(disk_path(session_factory, vm))
    assert second.size_bytes == 2 * FAKE_BLOCK_BYTES
    assert first.size_bytes == 3 * FAKE_BLOCK_BYTES
    assert driver.list_checkpoints(URI, "web1") == [f"minihci-r{second.id}"]


def test_checkpoint_missing_falls_back_to_full(session_factory, driver, host_id, vm, caplog):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    with session_factory() as s:
        dst = s.scalars(select(Host).where(Host.name == "b")).one().id
    driver.write_blocks(URI, "web1", [1])
    assert replicate(session_factory, driver, vm).state == "succeeded"
    assert migrate(session_factory, driver, vm, host_id, dst).state == "succeeded"
    assert driver.list_checkpoints(URI_B, "web1") == []
    with caplog.at_level(logging.INFO):
        assert replicate(session_factory, driver, vm).state == "succeeded"
    assert [r.mode for r in rows(session_factory)] == ["full", "full"]
    fallback = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "replica_full_fallback"]
    assert len(fallback) == 1 and fallback[0].fields["reason"] == "checkpoint_missing"
    assert "reason=checkpoint_missing" in fallback[0].getMessage()


def test_first_full_does_not_log_fallback(session_factory, driver, vm, caplog):
    with caplog.at_level(logging.INFO):
        replicate(session_factory, driver, vm)
    assert not [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "replica_full_fallback"]


def test_backup_step_rerun_after_partial_state(session_factory, driver, vm):
    driver.write_blocks(URI, "web1", [1, 2, 3])
    ctx = step_ctx(session_factory, driver, vm)
    vr.plan(ctx)
    (row,) = rows(session_factory)
    # leftovers: the target file, a stray checkpoint of this replica (via a scratch backup), a running job
    driver.create_blank_disk("/scratch.qcow2", like=disk_path(session_factory, vm))
    driver.backup_domain(URI, "web1", "/scratch.qcow2", row.checkpoint, None)
    driver.create_blank_disk(row.dr_path, like=disk_path(session_factory, vm))
    driver.set_backup_job(URI, "web1", True)
    vr.backup(ctx)
    assert driver.backup_active(URI, "web1") is False
    assert driver.read_disk(row.dr_path) == driver.read_disk(disk_path(session_factory, vm))
    assert driver.list_checkpoints(URI, "web1") == [row.checkpoint]


def test_injected_backup_failure_rolls_back(session_factory, driver, vm):
    driver.fail = {"backup_domain": 1}
    t = replicate(session_factory, driver, vm)
    assert t.state == "rolled_back"
    (row,) = rows(session_factory)
    assert row.status == "failed"
    assert not driver.disk_exists(row.dr_path)
    assert driver.list_checkpoints(URI, "web1") == []
    # and the next attempt starts clean with a full replica
    assert replicate(session_factory, driver, vm).state == "succeeded"
    assert [r.status for r in rows(session_factory)] == ["failed", "succeeded"]


def test_later_step_failure_keeps_succeeded_replica(session_factory, driver, vm):
    """Review focus: undo of `backup` must never destroy a replica that `record` already marked succeeded."""
    ctx = step_ctx(session_factory, driver, vm)
    vr.plan(ctx)
    vr.backup(ctx)
    vr.record(ctx)
    vr.undo_backup(ctx)
    vr.mark_failed(ctx)
    (row,) = rows(session_factory)
    assert row.status == "succeeded" and driver.disk_exists(row.dr_path)
    assert driver.list_checkpoints(URI, "web1") == [row.checkpoint]


def test_each_step_safe_to_rerun(session_factory, driver, vm):
    driver.write_blocks(URI, "web1", [1, 2])
    for _ in range(2):  # second round is an incremental so prune/trim have something to look at
        ctx = step_ctx(session_factory, driver, vm)
        for step in (vr.plan, vr.backup, vr.record, vr.trim_checkpoints, vr.prune):
            step(ctx)
            before = (
                [(r.id, r.status, r.size_bytes, r.dr_path) for r in rows(session_factory)],
                driver.list_checkpoints(URI, "web1"),
                {r.dr_path: driver.read_disk(r.dr_path) for r in rows(session_factory)},
            )
            step(ctx)
            after = (
                [(r.id, r.status, r.size_bytes, r.dr_path) for r in rows(session_factory)],
                driver.list_checkpoints(URI, "web1"),
                {r.dr_path: driver.read_disk(r.dr_path) for r in rows(session_factory)},
            )
            assert before == after, step.__name__
        driver.write_blocks(URI, "web1", [5])
    assert [r.mode for r in rows(session_factory)] == ["full", "incremental"]


def test_retention_prunes_whole_old_chain(session_factory, driver, host_id):
    vm_id = make_vm(session_factory, driver, host_id)
    protect(session_factory, vm_id, retention=2)
    for i in range(5):
        driver.write_blocks(URI, "web1", [i])
        assert replicate(session_factory, driver, vm_id).state == "succeeded"
    allrows = rows(session_factory)
    pruned = [r for r in allrows if r.status == "pruned"]
    live = [r for r in allrows if r.status == "succeeded"]
    assert pruned and live
    for r in pruned:
        assert not driver.disk_exists(r.dr_path)
    for r in live:
        assert driver.disk_exists(r.dr_path)
        if r.backing_path:
            assert driver.disk_exists(r.backing_path)
    assert driver.read_disk(live[-1].dr_path) == driver.read_disk(disk_path(session_factory, vm_id))


def test_foreign_checkpoint_survives_trim_and_undo(session_factory, driver, vm):
    driver.create_blank_disk("/scratch.qcow2", like=disk_path(session_factory, vm))
    driver.backup_domain(URI, "web1", "/scratch.qcow2", "user-ckpt", None)
    assert replicate(session_factory, driver, vm).state == "succeeded"
    (row,) = rows(session_factory)
    assert sorted(driver.list_checkpoints(URI, "web1")) == sorted(["user-ckpt", row.checkpoint])
    # a stale minihci checkpoint is trimmed, the foreign one stays
    driver.create_blank_disk("/s2.qcow2", like=disk_path(session_factory, vm))
    driver.backup_domain(URI, "web1", "/s2.qcow2", "minihci-r12345", None)
    vr.trim_checkpoints(step_ctx_for(session_factory, driver, vm, row))
    assert sorted(driver.list_checkpoints(URI, "web1")) == sorted(["user-ckpt", row.checkpoint])


def step_ctx_for(session_factory, driver, vm_id, row):
    ctx = step_ctx(session_factory, driver, vm_id)
    return StepContext(**{**ctx.__dict__, "task_id": row.task_id})


def test_refuses_vm_not_running(session_factory, driver, host_id):
    vm_id = make_vm(session_factory, driver, host_id, running=False)
    protect(session_factory, vm_id)
    t = replicate(session_factory, driver, vm_id)
    assert t.state == "rolled_back" and "not running" in t.error
    assert rows(session_factory) == []


def test_refuses_dr_site_vm(session_factory, driver, host_id, vm):
    with session_factory() as s, s.begin():
        s.get(Host, host_id).site = "dr"
    t = replicate(session_factory, driver, vm)
    assert t.state == "rolled_back" and "primary" in t.error
    assert rows(session_factory) == []


def test_refuses_without_enabled_policy(session_factory, driver, vm):
    protect(session_factory, vm, enabled=False)
    t = replicate(session_factory, driver, vm)
    assert t.state == "rolled_back" and "policy" in t.error


def test_dr_files_flat_under_dr_root(session_factory, driver, vm):
    replicate(session_factory, driver, vm)
    (row,) = rows(session_factory)
    assert os.path.dirname(row.dr_path) == Settings().dr_root
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
