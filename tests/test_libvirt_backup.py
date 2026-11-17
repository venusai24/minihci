import xml.etree.ElementTree as ET

import pytest

from minihci.driver import libvirt as lv
from minihci.driver.base import BackupResult, CheckpointNotFound, DomainSpec, DriverError
from minihci.driver.libvirt_xml import backup_xml, blank_volume_xml, checkpoint_xml
from tests.stubs.fake_libvirt import FakePool, FakeVolume

U1 = "qemu:///system"
U2 = "qemu+ssh://root@host2/system"
IMG, VMS, DR = "/srv/minihci/base", "/srv/minihci/images", "/srv/minihci/dr"
SRC = f"{VMS}/vm1.qcow2"
T0 = f"{DR}/t0.qcow2"


@pytest.fixture
def drv(stub_libvirt):
    sleeps: list[float] = []
    d = lv.LibvirtDriver(
        sleep=sleeps.append,
        storage_uris=[U1, U2],
        image_root=IMG,
        storage_root=VMS,
        dr_root=DR,
        backup_timeout_s=0.2,
    )
    shared: dict = {}
    for u in (U1, U2):
        stub_libvirt.host(u).storage = shared
    pool = FakePool(stub_libvirt.host(U1), "seed", VMS)
    shared[SRC] = FakeVolume(pool, "vm1.qcow2", SRC, 10_000)
    pool2 = FakePool(stub_libvirt.host(U1), "seed2", DR)
    shared[T0] = FakeVolume(pool2, "t0.qcow2", T0, 10_000)
    d.sleeps = sleeps
    yield d
    lv.stop_event_loop()


@pytest.fixture
def dom(drv, stub_libvirt):
    drv.define_domain(U1, DomainSpec("vm1", 1, 512, SRC))
    drv.start_domain(U1, "vm1")
    return stub_libvirt.host(U1).domains["vm1"]


def test_backup_xml_full_and_incremental():
    full = ET.fromstring(backup_xml("/dr/t0.qcow2", None))
    assert full.get("mode") == "push" and full.find("incremental") is None
    disk = full.find("disks/disk")
    assert disk.get("name") == "vda" and disk.get("backup") == "yes" and disk.get("type") == "file"
    assert disk.find("driver").get("type") == "qcow2"
    inc = ET.fromstring(backup_xml('/dr/it\'s & "x".qcow2', "c0"))
    assert inc.findtext("incremental") == "c0"
    assert inc.find("disks/disk/target").get("file") == '/dr/it\'s & "x".qcow2'
    assert ET.fromstring(checkpoint_xml("c<1>")).findtext("name") == "c<1>"
    assert ET.fromstring(blank_volume_xml("a&b", 5)).findtext("capacity") == "5"
    assert ET.fromstring(blank_volume_xml("a&b", 5)).find("backingStore") is None


def test_backup_waits_for_job_and_returns_duration(drv, dom, stub_libvirt):
    dom.backup_polls = 3
    res = drv.backup_domain(U1, "vm1", T0, "c0", None)
    assert isinstance(res, BackupResult) and res.duration_ms >= 0
    assert drv.sleeps == [0.5] * 3
    b = stub_libvirt.host(U1).backups[0]
    assert b == {"target": T0, "since": None, "checkpoint": "c0", "flags": 1}
    assert drv.list_checkpoints(U1, "vm1") == ["c0"]
    assert drv.backup_active(U1, "vm1") is False
    drv.backup_domain(U1, "vm1", T0, "c1", "c0")
    assert stub_libvirt.host(U1).backups[1]["since"] == "c0"


def test_failed_job_deletes_new_checkpoint(drv, dom):
    drv.backup_domain(U1, "vm1", T0, "c0", None)
    dom.backup_outcome = "failed"
    with pytest.raises(DriverError):
        drv.backup_domain(U1, "vm1", T0, "c1", "c0")
    assert drv.list_checkpoints(U1, "vm1") == ["c0"]


def test_timeout_aborts_and_raises(drv, dom):
    dom.backup_outcome = "hang"
    with pytest.raises(DriverError, match="timed out"):
        drv.backup_domain(U1, "vm1", T0, "c0", None)
    assert dom.backup_job is None  # abortJob was called
    assert drv.list_checkpoints(U1, "vm1") == []


def test_since_missing_is_checkpoint_not_found(drv, dom):
    with pytest.raises(CheckpointNotFound):
        drv.backup_domain(U1, "vm1", T0, "c1", "nope")
    assert drv.list_checkpoints(U1, "vm1") == []
    with pytest.raises(CheckpointNotFound):
        drv.delete_checkpoint(U1, "vm1", "nope")


def test_backup_preconditions(drv, dom):
    drv.backup_domain(U1, "vm1", T0, "c0", None)
    with pytest.raises(DriverError, match="already exists"):
        drv.backup_domain(U1, "vm1", T0, "c0", None)
    with pytest.raises(DriverError, match="not found"):
        drv.backup_domain(U1, "vm1", f"{DR}/missing.qcow2", "c9", None)
    drv.destroy_domain(U1, "vm1")
    with pytest.raises(DriverError, match="not running"):
        drv.backup_domain(U1, "vm1", T0, "c9", None)


def test_backup_active_and_abort(drv, dom):
    with pytest.raises(DriverError):
        drv.abort_backup(U1, "vm1")
    dom.backup_outcome = "hang"
    dom.backupBegin(
        "<domainbackup><disks><disk><target file='x'/></disk></disks></domainbackup>", checkpoint_xml("cx"), 0
    )
    assert drv.backup_active(U1, "vm1") is True
    drv.abort_backup(U1, "vm1")
    assert drv.backup_active(U1, "vm1") is False


def test_checkpoints_sorted_by_creation_time(drv, dom):
    for name in ("c-b", "c-a", "c-c"):
        drv.backup_domain(U1, "vm1", T0, name, None)
    assert drv.list_checkpoints(U1, "vm1") == ["c-b", "c-a", "c-c"]
    dom.checkpoints["c-c"].ctime = dom.checkpoints["c-a"].ctime  # tie: by name
    assert drv.list_checkpoints(U1, "vm1") == ["c-b", "c-a", "c-c"]
    dom.checkpoints["c-b"].ctime = 99
    assert drv.list_checkpoints(U1, "vm1") == ["c-a", "c-c", "c-b"]
    drv.delete_checkpoint(U1, "vm1", "c-a")
    assert drv.list_checkpoints(U1, "vm1") == ["c-c", "c-b"]


def test_undefine_passes_checkpoint_metadata_flag(drv, stub_libvirt):
    drv.define_domain(U1, DomainSpec("vm1", 1, 512, SRC))
    drv.undefine_domain(U1, "vm1")
    assert stub_libvirt.host(U1).undefine_flags == [stub_libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA]
    drv.define_domain(U1, DomainSpec("vm2", 1, 512, SRC))
    del stub_libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA
    drv.undefine_domain(U1, "vm2")  # falls back to undefine()
    assert len(stub_libvirt.host(U1).undefine_flags) == 1
    assert "vm2" not in stub_libvirt.host(U1).domains


def test_dr_paths_allowed_other_paths_rejected(drv, stub_libvirt):
    assert drv.disk_exists(T0) and drv.disk_allocation(T0) == 0
    t1 = f"{DR}/t1.qcow2"
    drv.create_disk_overlay(T0, t1)  # base under dr_root
    assert stub_libvirt.host(U1).storage[t1].backing == T0
    assert stub_libvirt.host(U1).pools["minihci-dr"].path == DR
    drv.delete_disk(t1)
    for bad in ("/etc/x.qcow2", f"{DR}/sub/x.qcow2", f"{IMG}/x.qcow2"):
        with pytest.raises(DriverError):
            drv.delete_disk(bad)
        with pytest.raises(DriverError):
            drv.create_blank_disk(bad, SRC)
        with pytest.raises(DriverError):
            drv.disk_allocation(bad)
    with pytest.raises(DriverError):
        drv.create_disk_overlay("/etc/passwd", f"{DR}/t2.qcow2")


def test_dr_pool_mismatch_falls_through_to_next_host(drv, stub_libvirt):
    h1 = stub_libvirt.host(U1)
    drv.disk_exists(T0)
    h1.pools["minihci-dr"].path = "/elsewhere"  # the first host sees the share elsewhere
    assert drv.disk_exists(T0) is True  # U2 answers; the pool is not redefined
    assert h1.pools["minihci-dr"].path == "/elsewhere"


def test_blank_disk_uses_like_capacity(drv, stub_libvirt):
    t = f"{DR}/blank.qcow2"
    drv.create_blank_disk(t, SRC)
    vol = stub_libvirt.host(U1).storage[t]
    assert vol.capacity == 10_000 and vol.backing is None
    with pytest.raises(DriverError, match="exists"):
        drv.create_blank_disk(t, SRC)
    with pytest.raises(DriverError, match="not found"):
        drv.create_blank_disk(f"{DR}/b2.qcow2", f"{VMS}/nope.qcow2")
    vol.allocation = 4096
    assert drv.disk_allocation(t) == 4096
    with pytest.raises(DriverError):
        drv.disk_allocation(f"{DR}/nope.qcow2")


def test_timeout_waits_for_slow_abort_and_retries_checkpoint_delete(drv, dom):
    dom.backup_outcome = "hang"
    dom.abort_polls = 3
    dom.checkpoint_delete_failures = 1
    with pytest.raises(DriverError, match="timed out"):
        drv.backup_domain(U1, "vm1", T0, "c0", None)
    assert dom.backup_job is None
    assert drv.list_checkpoints(U1, "vm1") == []


def test_checkpoint_still_present_after_failed_backup_is_an_error(drv, dom):
    dom.backup_outcome = "failed"
    dom.checkpoint_delete_failures = 5
    with pytest.raises(DriverError, match="still present"):
        drv.backup_domain(U1, "vm1", T0, "c0", None)
    assert drv.list_checkpoints(U1, "vm1") == ["c0"]
