"""Pure placement policy (FR-3.2, D-022). No DB or driver access."""

PLACEABLE_STATUSES = ("ready",)


class NoCapacity(Exception):
    """No host can take the requested VM."""


def place(vm_req, hosts, usage, config):
    """Return the best-fit host for vm_req or raise NoCapacity.

    usage maps host_id -> (vcpus_allocated, mem_mb_allocated); missing means (0, 0).
    Best fit = fitting host with the least free memory left; ties go to the lowest id.
    """
    best = None
    best_key = None
    for host in hosts:
        if host.status not in PLACEABLE_STATUSES:
            continue
        if host.cpu_total is None or host.mem_total_mb is None:
            continue
        vcpus_used, mem_used = usage.get(host.id, (0, 0))
        if mem_used + vm_req.mem_mb > host.mem_total_mb:
            continue
        if vcpus_used + vm_req.vcpus > host.cpu_total * config.cpu_headroom_pct // 100:
            continue
        key = (host.mem_total_mb - mem_used - vm_req.mem_mb, host.id)
        if best_key is None or key < best_key:
            best, best_key = host, key
    if best is None:
        raise NoCapacity("no host has capacity")
    return best
