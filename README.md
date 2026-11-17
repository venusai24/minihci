# MiniHCI

## CLI

The `minihci` command talks to the REST API (default `http://localhost:8000`, override with
`--api` or `MINIHCI_API`). Add `--json` for machine-readable output. Mutating commands print a
`task_id`; add `--wait` to block until the task finishes, or run `minihci tasks wait ID`
(exit 0 succeeded, 2 failed, 4 timeout). `--idempotency-key KEY` makes retries safe.

```
minihci hosts add host1 qemu+ssh://host1/system --wait
minihci vms create web1 --vcpus 2 --mem-mb 1024 --image /srv/minihci/base/alpine.qcow2 --wait
minihci vms list
minihci vms migrate 1 --to 2
minihci scheduler set --cpu-headroom-pct 20
```
