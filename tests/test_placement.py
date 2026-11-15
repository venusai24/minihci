from types import SimpleNamespace as NS

import pytest

from minihci.scheduler.placement import PLACEABLE_STATUSES, NoCapacity, place

CFG = NS(cpu_headroom_pct=80)


def h(id, cpu=10, mem=1000, status="ready"):
    return NS(id=id, status=status, cpu_total=cpu, mem_total_mb=mem)


def req(vcpus=1, mem_mb=100):
    return NS(vcpus=vcpus, mem_mb=mem_mb)


@pytest.mark.parametrize(
    "hosts,usage,vm,cfg,expected",
    [
        # best-fit: least free memory left after placement
        ([h(1, mem=1000), h(2, mem=500)], {}, req(), CFG, 2),
        ([h(1, mem=1000), h(2, mem=1000)], {2: (0, 600)}, req(), CFG, 2),
        # tie -> lowest id
        ([h(3), h(2), h(5)], {}, req(), CFG, 2),
        # memory exactly fits
        ([h(1, mem=1000)], {1: (0, 900)}, req(mem_mb=100), CFG, 1),
        # vcpus: 10 cpus * 80% = 8
        ([h(1)], {1: (7, 0)}, req(vcpus=1), CFG, 1),
        ([h(1), h(2, cpu=20)], {1: (8, 0)}, req(vcpus=1), CFG, 2),
        # headroom floor: 5 * 80% = 4
        ([h(1, cpu=5)], {}, req(vcpus=4), CFG, 1),
        # config changes limit: 50% of 10 = 5
        ([h(1), h(2, cpu=20)], {}, req(vcpus=6), NS(cpu_headroom_pct=50), 2),
        # skipped hosts
        ([h(1, status="failed", mem=200), h(2)], {}, req(), CFG, 2),
        ([h(1, cpu=None), h(2, mem=None), h(3)], {}, req(), CFG, 3),
    ],
)
def test_place_picks(hosts, usage, vm, cfg, expected):
    assert place(vm, hosts, usage, cfg).id == expected


@pytest.mark.parametrize(
    "hosts,usage,vm",
    [
        ([], {}, req()),
        ([h(1, mem=1000)], {1: (0, 901)}, req(mem_mb=100)),
        ([h(1)], {1: (8, 0)}, req(vcpus=1)),
        ([h(1, cpu=5)], {}, req(vcpus=5)),
        ([h(1, status="registering"), h(2, status="removed")], {}, req()),
        ([h(1, cpu=None)], {}, req()),
    ],
)
def test_place_no_capacity(hosts, usage, vm):
    with pytest.raises(NoCapacity):
        place(vm, hosts, usage, CFG)


def test_placeable_statuses():
    assert PLACEABLE_STATUSES == ("ready",)
