"""Pure rebalance policy (FR-3.4, D-025). No DB or driver access."""

from dataclasses import dataclass

from minihci.scheduler.placement import fits


@dataclass(frozen=True)
class Move:
    vm_id: int
    src_host_id: int
    dst_host_id: int


def choose_move(hosts, avg_cpu, vms, usage, config, cooling_vms, cooling_hosts) -> Move | None:
    """Pick one VM to move off the hottest host, or None.

    hosts: host rows (id, status, cpu_total, mem_total_mb). avg_cpu: host_id -> window average
    (only hosts with enough samples). vms: running VMs (id, host_id, vcpus, mem_mb).
    usage: host_id -> (vcpus, mem_mb) allocated. cooling_vms / cooling_hosts: sets of ids.
    """
    threshold = config.rebalance_cpu_threshold_pct
    by_id = {h.id: h for h in hosts}
    # Sources are primary-site hosts only (D-033): moving a VM off a hot DR host would be an accidental failback
    # (D-037). on_src below only ever holds VMs of the chosen source, so it inherits the filter.
    hot = [
        (-load, hid)
        for hid, load in avg_cpu.items()
        if hid in by_id
        and load > threshold
        and by_id[hid].status == "ready"
        and getattr(by_id[hid], "site", "primary") == "primary"
    ]
    if not hot:
        return None
    src_id = min(hot)[1]
    src = by_id[src_id]
    src_load = avg_cpu[src_id]
    on_src = [v for v in vms if v.host_id == src_id]
    total_vcpus = sum(v.vcpus for v in on_src)
    if total_vcpus <= 0 or not src.cpu_total:
        return None

    dsts = sorted(
        (avg_cpu[h.id], h.id, h)
        for h in hosts
        if h.id != src_id
        and h.id in avg_cpu
        and h.id not in cooling_hosts
        and avg_cpu[h.id] < threshold
        and h.cpu_total
    )

    best = None
    best_key = None
    for vm in on_src:
        if vm.id in cooling_vms:
            continue
        share = src_load * vm.vcpus / total_vcpus
        usage_wo = dict(usage)
        s_v, s_m = usage_wo.get(src_id, (0, 0))
        usage_wo[src_id] = (s_v - vm.vcpus, s_m - vm.mem_mb)
        for dst_load, dst_id, dst in dsts:
            if not fits(vm, dst, usage_wo, config):
                continue
            src_after = src_load - share
            dst_after = dst_load + share * src.cpu_total / dst.cpu_total
            projected = max(src_after, dst_after)
            if projected >= src_load:
                break  # least-loaded fitting host is the only one considered
            key = (projected, vm.mem_mb, vm.id)
            if best_key is None or key < best_key:
                best, best_key = Move(vm.id, src_id, dst_id), key
            break
    return best
