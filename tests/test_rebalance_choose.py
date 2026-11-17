"""Table tests for the pure rebalance policy (FR-3.4, D-025)."""

from types import SimpleNamespace as NS

from minihci.scheduler.config import Config
from minihci.scheduler.rebalance import Move, choose_move

CFG = Config(rebalance_cpu_threshold_pct=80, cpu_headroom_pct=100)


def host(i, cpu=8, mem=16000, status="ready"):
    return NS(id=i, status=status, cpu_total=cpu, mem_total_mb=mem)


def vm(i, h, vcpus=2, mem=1024):
    return NS(id=i, host_id=h, vcpus=vcpus, mem_mb=mem)


def usage_of(vms):
    u = {}
    for v in vms:
        a, b = u.get(v.host_id, (0, 0))
        u[v.host_id] = (a + v.vcpus, b + v.mem_mb)
    return u


def run(hosts, avg, vms, cfg=CFG, cv=(), ch=()):
    return choose_move(hosts, avg, vms, usage_of(vms), cfg, set(cv), set(ch))


def test_no_hot_host():
    vms = [vm(1, 1), vm(2, 1)]
    assert run([host(1), host(2)], {1: 70, 2: 10}, vms) is None


def test_hot_host_moves_to_other():
    vms = [vm(1, 1), vm(2, 1)]
    assert run([host(1), host(2)], {1: 90, 2: 10}, vms) == Move(1, 1, 2)


def test_picks_vm_minimising_projected_max():
    vms = [vm(1, 1, vcpus=1), vm(2, 1, vcpus=6), vm(3, 1, vcpus=1)]
    # share of vm2 = 90*6/8 = 67.5 -> src 22.5, dst 77.5 (max 77.5); vm1 -> max 78.75
    assert run([host(1), host(2)], {1: 90, 2: 10}, vms) == Move(2, 1, 2)


def test_tie_smaller_mem_then_lower_id():
    vms = [vm(1, 1, mem=4096), vm(2, 1, mem=1024), vm(3, 1, mem=1024)]
    assert run([host(1), host(2)], {1: 90, 2: 10}, vms) == Move(2, 1, 2)


def test_dst_is_least_loaded_fitting():
    vms = [vm(1, 1), vm(2, 1)]
    hosts = [host(1), host(2), host(3)]
    assert run(hosts, {1: 90, 2: 40, 3: 20}, vms).dst_host_id == 3


def test_dst_skips_host_that_does_not_fit():
    vms = [vm(1, 1), vm(2, 1)]
    hosts = [host(1), host(2, mem=512), host(3)]
    assert run(hosts, {1: 90, 2: 5, 3: 30}, vms).dst_host_id == 3


def test_cooling_vm_skipped():
    vms = [vm(1, 1), vm(2, 1, mem=2048)]
    assert run([host(1), host(2)], {1: 90, 2: 10}, vms, cv={1}) == Move(2, 1, 2)


def test_all_vms_cooling():
    vms = [vm(1, 1)]
    assert run([host(1), host(2)], {1: 90, 2: 10}, vms, cv={1}) is None


def test_cooling_dst_host_skipped():
    vms = [vm(1, 1), vm(2, 1)]
    hosts = [host(1), host(2), host(3)]
    assert run(hosts, {1: 90, 2: 5, 3: 30}, vms, ch={2}).dst_host_id == 3
    assert run([host(1), host(2)], {1: 90, 2: 5}, vms, ch={2}) is None


def test_dst_without_samples_skipped():
    vms = [vm(1, 1), vm(2, 1)]
    assert run([host(1), host(2)], {1: 90}, vms) is None


def test_move_that_does_not_lower_max_is_none():
    vms = [vm(1, 1, vcpus=4)]  # single VM carries all load; dst would reach 90+ -> not lower
    assert run([host(1), host(2)], {1: 90, 2: 79}, vms) is None


def test_nothing_fits():
    vms = [vm(1, 1), vm(2, 1)]
    hosts = [host(1), host(2, status="down")]
    assert run(hosts, {1: 90, 2: 10}, vms) is None
    cfg = Config(rebalance_cpu_threshold_pct=80, cpu_headroom_pct=10)
    assert run([host(1), host(2)], {1: 90, 2: 10}, vms, cfg=cfg) is None


def test_hottest_host_chosen():
    vms = [vm(1, 1), vm(2, 2), vm(3, 2)]
    hosts = [host(1), host(2), host(3)]
    assert run(hosts, {1: 85, 2: 95, 3: 5}, vms) == Move(2, 2, 3)


def dr_host(i, cpu=8, mem=16000):
    return NS(id=i, status="ready", cpu_total=cpu, mem_total_mb=mem, site="dr")


def test_hot_dr_host_is_never_a_source():
    """F3: moving a failed-over VM off a hot DR host to a primary host would be an accidental failback (D-037)."""
    vms = [vm(1, 1), vm(2, 1)]
    assert run([dr_host(1), host(2)], {1: 95, 2: 10}, vms) is None


def test_hot_dr_host_does_not_hide_a_hot_primary():
    vms = [vm(1, 1), vm(2, 1), vm(3, 2), vm(4, 2)]
    assert run([dr_host(1), host(2), host(3)], {1: 99, 2: 90, 3: 10}, vms) == Move(3, 2, 3)
