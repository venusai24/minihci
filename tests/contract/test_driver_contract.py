"""One suite for every HostDriver implementation (FR-1, FR-2, FR-2.1, FR-3.2)."""

from contextlib import contextmanager

import pytest

from minihci.driver.base import (
    DomainExists,
    DomainNotFound,
    DomainSpec,
    DomainState,
    DriverError,
    HostInfo,
    HostStats,
    HostUnreachable,
    MigrationResult,
)

from .conftest import Env


@contextmanager
def vm(env: Env, uri: str | None = None, *, start: bool = False):
    """A defined VM with an overlay disk; always cleaned up."""
    uri = uri or env.uri1
    name = env.name()
    disk = env.disk(name)
    d = env.driver
    host_of = [uri]
    try:
        d.create_disk_overlay(env.base_image, disk)
        d.define_domain(uri, DomainSpec(name=name, vcpus=1, mem_mb=128, disk_path=disk))
        if start:
            d.start_domain(uri, name)
        yield name, disk, host_of
    finally:
        for u in {env.uri1, env.uri2}:
            try:
                if d.domain_state(u, name) == DomainState.RUNNING:
                    d.destroy_domain(u, name)
                if d.domain_state(u, name) is not None:
                    d.undefine_domain(u, name)
            except DriverError:
                pass
        if d.disk_exists(disk):
            d.delete_disk(disk)


def test_lifecycle(env: Env):
    d = env.driver
    with vm(env) as (name, _disk, _):
        assert d.domain_state(env.uri1, name) == DomainState.SHUT_OFF
        assert d.list_domains(env.uri1)[name] == DomainState.SHUT_OFF
        d.start_domain(env.uri1, name)
        assert d.domain_state(env.uri1, name) == DomainState.RUNNING
        assert d.list_domains(env.uri1)[name] == DomainState.RUNNING
        d.destroy_domain(env.uri1, name)
        assert d.domain_state(env.uri1, name) == DomainState.SHUT_OFF
        d.undefine_domain(env.uri1, name)
        assert d.domain_state(env.uri1, name) is None
        assert name not in d.list_domains(env.uri1)


def test_state_errors_on_wrong_state(env: Env):
    d = env.driver
    with vm(env) as (name, disk, _):
        with pytest.raises(DomainExists):
            d.define_domain(env.uri1, DomainSpec(name=name, vcpus=1, mem_mb=128, disk_path=disk))
        with pytest.raises(DriverError):
            d.destroy_domain(env.uri1, name)  # not running
        with pytest.raises(DriverError):
            d.shutdown_domain(env.uri1, name)  # not running
        d.start_domain(env.uri1, name)
        with pytest.raises(DriverError):
            d.start_domain(env.uri1, name)  # already running
        with pytest.raises(DriverError):
            d.undefine_domain(env.uri1, name)  # running


def test_missing_domain_errors(env: Env):
    d = env.driver
    name = env.name()
    assert d.domain_state(env.uri1, name) is None
    with pytest.raises(DomainNotFound):
        d.undefine_domain(env.uri1, name)
    with pytest.raises(DomainNotFound):
        d.start_domain(env.uri1, name)
    with pytest.raises(DomainNotFound):
        d.destroy_domain(env.uri1, name)
    with pytest.raises(DomainNotFound):
        d.shutdown_domain(env.uri1, name)
    with pytest.raises(DomainNotFound):
        d.reboot_domain(env.uri1, name)
    with pytest.raises(DomainNotFound):
        d.migrate_live(env.uri1, env.uri2, name)


def test_overlay_create_delete(env: Env):
    d = env.driver
    disk = env.disk(env.name())
    try:
        assert not d.disk_exists(disk)
        d.create_disk_overlay(env.base_image, disk)
        assert d.disk_exists(disk)
        with pytest.raises(DriverError):
            d.create_disk_overlay(env.base_image, disk)
        d.delete_disk(disk)
        assert not d.disk_exists(disk)
        with pytest.raises(DriverError):
            d.delete_disk(disk)
    finally:
        if d.disk_exists(disk):
            d.delete_disk(disk)


def test_host_info_and_stats_shapes(env: Env):
    d = env.driver
    info = d.probe_host(env.uri1)
    assert isinstance(info, HostInfo)
    assert isinstance(info.cpu_total, int) and info.cpu_total > 0
    assert isinstance(info.mem_total_mb, int) and info.mem_total_mb > 0
    stats = d.host_stats(env.uri1)
    assert isinstance(stats, HostStats)
    assert isinstance(stats.cpu_pct, float) and 0 <= stats.cpu_pct <= 100
    assert isinstance(stats.mem_used_mb, int) and 0 <= stats.mem_used_mb <= info.mem_total_mb


def test_migrate_live_result_shape(env: Env):
    if env.uri1 == env.uri2:
        pytest.skip("needs two hosts")
    d = env.driver
    with vm(env, start=True) as (name, _disk, _):
        res = d.migrate_live(env.uri1, env.uri2, name)
        assert isinstance(res, MigrationResult)
        assert isinstance(res.duration_ms, int) and res.duration_ms >= 0
        assert res.downtime_ms is None or (isinstance(res.downtime_ms, int) and res.downtime_ms >= 0)
        assert d.domain_state(env.uri1, name) is None
        assert d.domain_state(env.uri2, name) == DomainState.RUNNING


def test_migrate_live_errors_leave_domain_on_source(env: Env):
    if env.uri1 == env.uri2:
        pytest.skip("needs two hosts")
    d = env.driver
    with vm(env) as (name, _disk, _):
        with pytest.raises(DriverError):  # defined but not running
            d.migrate_live(env.uri1, env.uri2, name)
        assert d.domain_state(env.uri1, name) == DomainState.SHUT_OFF
        assert d.domain_state(env.uri2, name) is None


def test_unknown_host_is_unreachable(env: Env):
    if env.uri1.startswith("qemu"):
        pytest.skip("real hosts: no known-bad host to use safely")
    with pytest.raises(HostUnreachable):
        env.driver.host_stats("fake://nope")


def test_ping_known_host_true(env: Env):
    assert env.driver.ping(env.uri1, 5) is True


def test_backup_full_incremental_shapes(env: Env):
    from minihci.driver.base import BackupResult

    d = env.driver
    with vm(env, start=True) as (name, disk, _):
        t0, t1 = env.disk(name + "-t0"), env.disk(name + "-t1")
        try:
            if hasattr(d, "write_blocks"):  # FakeDriver: give the guest something to back up
                d.write_blocks(env.uri1, name, [1, 2])
            d.create_blank_disk(t0, like=disk)
            res = d.backup_domain(env.uri1, name, t0, "c0", None)
            assert isinstance(res, BackupResult) and res.duration_ms >= 0
            assert d.disk_allocation(t0) > 0
            d.create_disk_overlay(t0, t1)
            res = d.backup_domain(env.uri1, name, t1, "c1", "c0")
            assert res.duration_ms >= 0
            assert d.list_checkpoints(env.uri1, name) == ["c0", "c1"]
        finally:
            for t in (t1, t0):
                if d.disk_exists(t):
                    d.delete_disk(t)


def test_backup_since_missing_raises_checkpoint_not_found(env: Env):
    from minihci.driver.base import CheckpointNotFound

    d = env.driver
    with vm(env, start=True) as (name, disk, _):
        t0 = env.disk(name + "-t0")
        try:
            d.create_blank_disk(t0, like=disk)
            with pytest.raises(CheckpointNotFound):
                d.backup_domain(env.uri1, name, t0, "c1", "nope")
            assert d.list_checkpoints(env.uri1, name) == []
        finally:
            if d.disk_exists(t0):
                d.delete_disk(t0)


def test_blank_disk_exists_and_deletes(env: Env):
    d = env.driver
    with vm(env) as (name, disk, _):
        t0 = env.disk(name + "-blank")
        try:
            d.create_blank_disk(t0, like=disk)
            assert d.disk_exists(t0)
            with pytest.raises(DriverError):
                d.create_blank_disk(t0, like=disk)
        finally:
            if d.disk_exists(t0):
                d.delete_disk(t0)
        assert not d.disk_exists(t0)
        with pytest.raises(DriverError):
            d.disk_allocation(t0)
