#!/usr/bin/env bash
# MiniHCI reproducible host setup (NFR-7). OWNER SCRIPT: runs sudo/virsh/ssh on the real laptop.
# The owner must review this script before running it. It was written unattended and never executed.
# Refuses to run unless MINIHCI_REAL_HOSTS is set.
#
# Laptop side: NFS exports, NFS lease drop-in, default-network DHCP entries (fixed MACs), /etc/hosts,
#              host VMs from the Fedora cloud image.
# Host side (over ssh, not cloud-init): dnf packages, hostname, NFS mount, virt_use_nfs,
#              libvirtd, firewall ports for live migration.
# Every step checks the current state first, so re-running is safe.
#
# Required env: MINIHCI_REAL_HOSTS=1, FEDORA_IMAGE (path to Fedora cloud qcow2), SSH_PUBKEY (path)
# Optional: NUM_HOSTS (3), NFS_DIR (/srv/minihci), HOST_NFS_MOUNT (/var/lib/minihci),
#           HOST_DR_MOUNT (/srv/minihci/dr), HOST_MEM_MB (2560), HOST_VCPUS (2),
#           HOST_DISK_DIR (/var/lib/libvirt/images), SSH_USER (root), LAPTOP_IP (192.168.122.1)
set -euo pipefail

if [ -z "${MINIHCI_REAL_HOSTS:-}" ]; then
    echo "setup-hosts.sh: refusing to run: set MINIHCI_REAL_HOSTS=1 to confirm real hosts." >&2
    exit 2
fi

: "${FEDORA_IMAGE:?set FEDORA_IMAGE to the Fedora cloud qcow2}"
: "${SSH_PUBKEY:?set SSH_PUBKEY to the public key file the host VMs should trust}"
[ -r "$SSH_PUBKEY" ] || { echo "setup-hosts.sh: SSH_PUBKEY $SSH_PUBKEY is not readable" >&2; exit 2; }
[ -r "$FEDORA_IMAGE" ] || { echo "setup-hosts.sh: FEDORA_IMAGE $FEDORA_IMAGE is not readable" >&2; exit 2; }
FEDORA_IMAGE="$(realpath "$FEDORA_IMAGE")"
NUM_HOSTS="${NUM_HOSTS:-3}"
NFS_DIR="${NFS_DIR:-/srv/minihci}"
HOST_NFS_MOUNT="${HOST_NFS_MOUNT:-/var/lib/minihci}"
HOST_DR_MOUNT="${HOST_DR_MOUNT:-/srv/minihci/dr}"
HOST_MEM_MB="${HOST_MEM_MB:-2560}"
HOST_VCPUS="${HOST_VCPUS:-2}"
HOST_DISK_DIR="${HOST_DISK_DIR:-/var/lib/libvirt/images}"
SSH_USER="${SSH_USER:-root}"
LAPTOP_IP="${LAPTOP_IP:-192.168.122.1}"
SUBNET="${SUBNET:-192.168.122.0/24}"
[[ "$NUM_HOSTS" =~ ^[1-9]$ ]] || { echo "setup-hosts.sh: NUM_HOSTS must be a single digit 1-9" >&2; exit 2; }
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes)

log() { echo "setup-hosts: $*"; }
host_name() { echo "host$1"; }
host_ip() { echo "192.168.122.$((10 + $1))"; }
host_mac() { echo "52:54:00:4d:48:1$1"; }   # deterministic: 52:54:00:4d:48:1N

rssh() { local n="$1"; shift; ssh "${SSH_OPTS[@]}" "${SSH_USER}@$(host_ip "$n")" "$@"; }

# ---------------------------------------------------------------- laptop side
laptop_nfs_export() {
    sudo mkdir -p "$NFS_DIR/primary/images" "$NFS_DIR/primary/vms" "$NFS_DIR/dr" /etc/exports.d
    local opts="${SUBNET}(rw,sync,no_root_squash,no_subtree_check)"
    local want
    want="$NFS_DIR/primary $opts
$NFS_DIR/dr $opts"
    if [ "$(sudo cat /etc/exports.d/minihci.exports 2>/dev/null || true)" != "$want" ]; then
        log "writing /etc/exports.d/minihci.exports"
        printf '%s\n' "$want" | sudo tee /etc/exports.d/minihci.exports >/dev/null
    fi
    sudo systemctl enable --now nfs-server
    sudo exportfs -ra
}

# HA disk fence: a dead host's NFSv4 locks/leases must expire quickly (15 s) so a surviving host
# can take over its disks inside the HA window. The default (90 s) would blow the 60 s HA target.
laptop_nfs_lease() {
    local f=/etc/nfs.conf.d/minihci.conf
    local want
    want="# MiniHCI: HA disk fence. Short NFSv4 lease/grace so a dead host loses its locks fast.
[nfsd]
lease-time=15
grace-time=15"
    if [ "$(sudo cat "$f" 2>/dev/null || true)" != "$want" ]; then
        log "writing $f and restarting nfs-server"
        sudo mkdir -p /etc/nfs.conf.d
        printf '%s\n' "$want" | sudo tee "$f" >/dev/null
        sudo systemctl restart nfs-server
    fi
}

# Let libvirt-zone clients reach NFS; does nothing when firewalld is not running.
laptop_firewall() {
    systemctl is-active --quiet firewalld || return 0
    sudo firewall-cmd --permanent --zone=libvirt --query-service=nfs >/dev/null \
        || sudo firewall-cmd --permanent --zone=libvirt --add-service=nfs
    sudo firewall-cmd --zone=libvirt --query-service=nfs >/dev/null \
        || sudo firewall-cmd --zone=libvirt --add-service=nfs
}

laptop_dhcp_entries() {
    local n entry
    for n in $(seq 1 "$NUM_HOSTS"); do
        entry="<host mac='$(host_mac "$n")' name='$(host_name "$n")' ip='$(host_ip "$n")'/>"
        if sudo virsh net-dumpxml default | grep -qF "mac='$(host_mac "$n")'"; then
            continue
        fi
        log "adding DHCP entry for $(host_name "$n")"
        sudo virsh net-update default add ip-dhcp-host "$entry" --live --config
    done
}

laptop_etc_hosts() {
    local n
    for n in $(seq 1 "$NUM_HOSTS"); do
        if ! grep -qE "^$(host_ip "$n")[[:space:]]+$(host_name "$n")( |\$)" /etc/hosts; then
            log "adding $(host_name "$n") to /etc/hosts"
            echo "$(host_ip "$n") $(host_name "$n")" | sudo tee -a /etc/hosts >/dev/null
        fi
    done
}

laptop_host_vms() {
    local n name disk
    for n in $(seq 1 "$NUM_HOSTS"); do
        name="$(host_name "$n")"
        if sudo virsh dominfo "$name" >/dev/null 2>&1; then
            continue
        fi
        disk="$HOST_DISK_DIR/$name.qcow2"
        if [ ! -e "$disk" ]; then
            sudo rm -f "$disk.tmp"
            log "creating disk $disk from $FEDORA_IMAGE"
            sudo qemu-img create -f qcow2 -F qcow2 -b "$FEDORA_IMAGE" "$disk.tmp" 40G
            sudo virt-customize -a "$disk.tmp" --ssh-inject "root:file:$SSH_PUBKEY" --selinux-relabel
            sudo mv "$disk.tmp" "$disk"
        fi
        log "defining and starting host VM $name"
        # No cloud-init: host-side configuration is done over ssh below.
        sudo virt-install --name "$name" --memory "$HOST_MEM_MB" --vcpus "$HOST_VCPUS" \
            --cpu host-passthrough --disk "path=$disk" --import --os-variant fedora-unknown \
            --network "network=default,mac=$(host_mac "$n")" --graphics none --noautoconsole
    done
}

# ---------------------------------------------------------------- host side (ssh)
wait_ssh() {
    local n="$1" i
    for i in $(seq 1 60); do
        rssh "$n" true 2>/dev/null && return 0
        sleep 5
    done
    echo "setup-hosts.sh: $(host_name "$n") not reachable over ssh" >&2
    return 1
}

host_dnf() {
    local n="$1" i
    if rssh "$n" 'rpm -q qemu-kvm libvirt libvirt-daemon-kvm nfs-utils firewalld >/dev/null 2>&1'; then
        return 0
    fi
    for i in 1 2 3 4 5; do
        if rssh "$n" 'dnf install -y qemu-kvm libvirt libvirt-daemon-kvm nfs-utils firewalld policycoreutils'; then
            return 0
        fi
        log "dnf failed on $(host_name "$n") (attempt $i), retrying"
        sleep $((i * 10))
    done
    echo "setup-hosts.sh: dnf install failed on $(host_name "$n")" >&2
    return 1
}

host_hostname() {
    local n="$1" want
    want="$(host_name "$n")"
    if [ "$(rssh "$n" hostname)" != "$want" ]; then
        rssh "$n" "hostnamectl set-hostname $want"
    fi
}

host_nfs_mount() {
    local n="$1" m="$HOST_NFS_MOUNT"
    local fstab="$LAPTOP_IP:$NFS_DIR/primary $m nfs4 vers=4.2,hard,_netdev 0 0"
    rssh "$n" "mkdir -p '$m'"
    # Append only if no fstab entry already uses this mount point.
    rssh "$n" "awk -v m='$m' '\$2==m{f=1} END{exit !f}' /etc/fstab || echo '$fstab' >> /etc/fstab"
    rssh "$n" "mountpoint -q '$m' || timeout 60 mount '$m'"
}

# DR share (D-033): mounted at the SAME path as on the laptop, so MINIHCI_DR_ROOT is identical everywhere.
# Appends only when no fstab entry uses the mount point, so a hand-made line is left alone.
host_dr_mount() {
    local n="$1" m="$HOST_DR_MOUNT"
    local fstab="$LAPTOP_IP:$NFS_DIR/dr $m nfs4 vers=4.2,hard,_netdev 0 0"
    rssh "$n" "mkdir -p '$m'"
    rssh "$n" "awk -v m='$m' '\$2==m{f=1} END{exit !f}' /etc/fstab || echo '$fstab' >> /etc/fstab"
    rssh "$n" "mountpoint -q '$m' || timeout 60 mount '$m'"
}

host_selinux() {
    local n="$1"
    if [ "$(rssh "$n" 'getsebool virt_use_nfs' | awk '{print $NF}')" != "on" ]; then
        rssh "$n" 'setsebool -P virt_use_nfs on'
    fi
}

host_libvirtd() {
    local n="$1"
    rssh "$n" 'systemctl is-active --quiet libvirtd || systemctl enable --now libvirtd'
    rssh "$n" 'systemctl is-enabled --quiet libvirtd || systemctl enable libvirtd'
}

host_firewall() {
    local n="$1"
    rssh "$n" 'systemctl is-active --quiet firewalld || systemctl enable --now firewalld'
    rssh "$n" 'firewall-cmd --permanent --query-port=49152-49215/tcp >/dev/null \
        || { firewall-cmd --permanent --add-port=49152-49215/tcp && firewall-cmd --reload; }'
}

main() {
    laptop_nfs_export
    laptop_nfs_lease
    laptop_firewall
    laptop_dhcp_entries
    laptop_etc_hosts
    laptop_host_vms
    local n
    for n in $(seq 1 "$NUM_HOSTS"); do
        wait_ssh "$n"
        host_dnf "$n"
        host_hostname "$n"
        host_nfs_mount "$n"
        host_dr_mount "$n"
        host_selinux "$n"
        host_libvirtd "$n"
        host_firewall "$n"
        log "$(host_name "$n") ready"
    done
    log "done"
    log "DR site: register the laptop as the DR host with: minihci hosts add laptop qemu:///system --site dr"
}

main "$@"
