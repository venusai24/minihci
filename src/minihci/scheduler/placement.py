"""Pure placement policy (FR-3.2, D-022). No DB or driver access."""

PLACEABLE_STATUSES = ("ready",)


class NoCapacity(Exception):
    """No host can take the requested VM."""


def fits(vm_req, host, usage, config) -> bool:
    """True if host is placeable and has memory and CPU headroom left for vm_req."""
    if host.status not in PLACEABLE_STATUSES:
        return False
    if host.cpu_total is None or host.mem_total_mb is None:
        return False
    vcpus_used, mem_used = usage.get(host.id, (0, 0))
    if mem_used + vm_req.mem_mb > host.mem_total_mb:
        return False
    return vcpus_used + vm_req.vcpus <= host.cpu_total * config.cpu_headroom_pct // 100


def place(vm_req, hosts, usage, config):
    """Return the best-fit host for vm_req or raise NoCapacity.

    usage maps host_id -> (vcpus_allocated, mem_mb_allocated); missing means (0, 0).
    Best fit = fitting host with the least free memory left; ties go to the lowest id.
    """
    best = None
    best_key = None
    for host in hosts:
        if not fits(vm_req, host, usage, config):
            continue
        mem_used = usage.get(host.id, (0, 0))[1]
        key = (host.mem_total_mb - mem_used - vm_req.mem_mb, host.id)
        if best_key is None or key < best_key:
            best, best_key = host, key
    if best is None:
        raise NoCapacity("no host has capacity")
    return best
