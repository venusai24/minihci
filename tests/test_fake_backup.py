from pathlib import Path

import pytest

from minihci.driver.base import CheckpointNotFound, DomainNotFound, DomainSpec, DriverError
from minihci.driver.fake import FAKE_BLOCK_BYTES, FakeDriver

U = "fake://h1"


def make(state_file: Path | None = None):
    d = FakeDriver(state_file=state_file)
    d.add_host(U, 8, 16384)
    d.create_disk_overlay("/base/b.qcow2", "/base/b.qcow2")
    d.create_disk_overlay("/base/b.qcow2", "/vms/a.qcow2")
    d.define_domain(U, DomainSpec(name="a", vcpus=1, mem_mb=128, disk_path="/vms/a.qcow2"))
    d.start_domain(U, "a")
    return d


def chain(d: FakeDriver) -> None:
    d.write_blocks(U, "a", [1, 2, 3, 4])
    d.create_blank_disk("/dr/t0", like="/vms/a.qcow2")
    d.backup_domain(U, "a", "/dr/t0", "c0", None)
    d.write_blocks(U, "a", [2, 9])
    d.create_disk_overlay("/dr/t0", "/dr/t1")
    d.backup_domain(U, "a", "/dr/t1", "c1", "c0")
    assert d.read_disk("/dr/t1") == d.read_disk("/vms/a.qcow2")
    assert d.disk_allocation("/dr/t1") == 2 * FAKE_BLOCK_BYTES
    assert d.list_checkpoints(U, "a") == ["c0", "c1"]


def test_full_then_incremental_chain_matches_source():
    chain(make())


def test_state_file_roundtrip(tmp_path):
    chain(make(tmp_path / "s.json"))


def test_backup_errors():
    d = make()
    d.write_blocks(U, "a", [1])
    d.create_blank_disk("/dr/t0", like="/vms/a.qcow2")
    d.backup_domain(U, "a", "/dr/t0", "c0", None)
    before = d.list_checkpoints(U, "a")
    with pytest.raises(DriverError):
        d.backup_domain(U, "a", "/dr/t0", "c0", None)
    assert d.list_checkpoints(U, "a") == before
    with pytest.raises(DriverError):
        d.backup_domain(U, "a", "/dr/missing", "c1", None)
    assert d.list_checkpoints(U, "a") == before
    with pytest.raises(CheckpointNotFound):
        d.backup_domain(U, "a", "/dr/t0", "c1", "nope")
    assert d.list_checkpoints(U, "a") == before
    d.destroy_domain(U, "a")
    with pytest.raises(DriverError):
        d.backup_domain(U, "a", "/dr/t0", "c1", None)
    assert d.list_checkpoints(U, "a") == before


def test_delete_checkpoint_and_missing():
    d = make()
    d.create_blank_disk("/dr/t0", like="/vms/a.qcow2")
    d.backup_domain(U, "a", "/dr/t0", "c0", None)
    d.delete_checkpoint(U, "a", "c0")
    assert d.list_checkpoints(U, "a") == []
    with pytest.raises(CheckpointNotFound):
        d.delete_checkpoint(U, "a", "c0")
    with pytest.raises(DomainNotFound):
        d.delete_checkpoint(U, "nope", "c0")


def test_abort_backup_without_job_raises():
    d = make()
    assert d.backup_active(U, "a") is False
    with pytest.raises(DriverError):
        d.abort_backup(U, "a")
    d.set_backup_job(U, "a", True)
    assert d.backup_active(U, "a") is True
    d.abort_backup(U, "a")
    assert d.backup_active(U, "a") is False
