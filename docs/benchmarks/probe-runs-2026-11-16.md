# Lab probe runs, 2026-11-16 (preliminary)

Raw numbers behind the README Benchmarks section. Three-host lab, idle guest with 256 MiB of RAM,
1 vCPU, shared NFS disk. Single runs, no statistical weight.

## Live migration

Manual, managed (client-driven) migration, measured with `virsh domjobinfo --completed`:
host1 to host2 1660 ms total / 229 ms downtime; host2 to host1 835 ms total / 258 ms downtime
(n=2, best case).

Probe script via the Python API (managed migration, n=3):

| direction | wall ms | time_elapsed ms | downtime ms |
|---|---|---|---|
| host1 to host2 | 1050 | 696 | 88 |
| host2 to host1 | 620 | 395 | 115 |
| host1 to host2 | 600 | 363 | 78 |

The two methods differ, so compare only within a method.

## HA dry run

`scripts/probe_ha.py`: VM on host2, `virsh destroy host2`, restart the VM on host1 from the same
disk. NFS 4.2, `hard` mounts. With the default 90 s NFS lease, `dom.create()` blocked and succeeded
91 s after the power cut. With the lease lowered to 15 s (`lease-time=15`, `grace-time=15`) it
succeeded 27.5 s after the cut, about 1.8 times the lease (probably NFS client lock retry, not
verified).
