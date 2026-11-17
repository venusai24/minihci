#!/usr/bin/env bash
# Milestone 1 acceptance (FR-1.5, NFR-1). OWNER SCRIPT: runs against real hosts.
# Refuses to run unless MINIHCI_REAL_HOSTS is set. Uses only the minihci CLI (REST API).
#
# Required env:
#   MINIHCI_REAL_HOSTS=1
#   MINIHCI_HOST1_URI, MINIHCI_HOST2_URI   libvirt URIs, e.g. qemu+ssh://root@host1/system
#   MINIHCI_CIRROS_IMAGE                   image name/path as the API expects it
# Optional: MINIHCI_API (default http://127.0.0.1:8000), VM_NAME (default m1-accept),
#   RECONCILE_TIMEOUT seconds (default 120).
#
# It prints the one undefine command for you to run on host1 by hand; the script
# never runs that command itself.
set -euo pipefail

if [ -z "${MINIHCI_REAL_HOSTS:-}" ]; then
    echo "acceptance_m1.sh: refusing to run: set MINIHCI_REAL_HOSTS=1 to confirm real hosts." >&2
    exit 2
fi

: "${MINIHCI_HOST1_URI:?set MINIHCI_HOST1_URI}"
: "${MINIHCI_HOST2_URI:?set MINIHCI_HOST2_URI}"
: "${MINIHCI_CIRROS_IMAGE:?set MINIHCI_CIRROS_IMAGE}"
VM_NAME="${VM_NAME:-m1-accept}"
RECONCILE_TIMEOUT="${RECONCILE_TIMEOUT:-120}"
CLI=(uv run minihci --json)

# json_field <field>: read JSON on stdin, print the field of the object.
json_field() {
    python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

# host_id_of <name>: id of the host with that name from `hosts list`.
host_id_of() {
    "${CLI[@]}" hosts list | python3 -c '
import json, sys
for h in json.load(sys.stdin):
    if h["name"] == sys.argv[1]:
        print(h["id"]); break
' "$1"
}

vm_state() {
    "${CLI[@]}" vms show "$1" | json_field actual_state
}

echo "== register hosts"
"${CLI[@]}" hosts add host1 "$MINIHCI_HOST1_URI" --wait
"${CLI[@]}" hosts add host2 "$MINIHCI_HOST2_URI" --wait
HOST1_ID="$(host_id_of host1)"
[ -n "$HOST1_ID" ] || { echo "FAIL: host1 not listed" >&2; exit 1; }

echo "== create VM $VM_NAME on host1 (id $HOST1_ID)"
"${CLI[@]}" vms create "$VM_NAME" --vcpus 1 --mem-mb 256 --image "$MINIHCI_CIRROS_IMAGE" \
    --host-id "$HOST1_ID" --wait
VM_ID="$("${CLI[@]}" vms list | python3 -c '
import json, sys
for v in json.load(sys.stdin):
    if v["name"] == sys.argv[1]:
        print(v["id"]); break
' "$VM_NAME")"
[ -n "$VM_ID" ] || { echo "FAIL: VM not listed" >&2; exit 1; }

STATE="$(vm_state "$VM_ID")"
if [ "$STATE" != "running" ]; then
    echo "FAIL: expected running, got $STATE" >&2
    exit 1
fi
echo "OK: VM $VM_ID is running"

echo
echo "== now go behind MiniHCI's back. On host1 run exactly:"
echo "    virsh -c qemu:///system undefine --nvram $VM_NAME"
echo "(If the domain is still running, also: virsh -c qemu:///system destroy $VM_NAME)"
read -r -p "Press Enter once you have done that: " _

echo "== waiting up to ${RECONCILE_TIMEOUT}s for the reconciler to notice"
deadline=$((SECONDS + RECONCILE_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
    STATE="$(vm_state "$VM_ID")"
    if [ "$STATE" != "running" ]; then
        echo "OK: drift detected, actual_state is now '$STATE'"
        exit 0
    fi
    sleep 2
done
echo "FAIL: reconciler did not detect drift within ${RECONCILE_TIMEOUT}s (still running)" >&2
exit 1
