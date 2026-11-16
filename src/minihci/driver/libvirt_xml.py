"""Pure XML builders for the libvirt driver (no libvirt import)."""

from xml.sax.saxutils import escape

from minihci.driver.base import DomainSpec


def _attr(value: str) -> str:
    return escape(value, {"'": "&apos;", '"': "&quot;"})


def domain_xml(spec: DomainSpec) -> str:
    return f"""<domain type='kvm'>
  <name>{escape(spec.name)}</name>
  <memory unit='MiB'>{int(spec.mem_mb)}</memory>
  <vcpu>{int(spec.vcpus)}</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{_attr(spec.disk_path)}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <serial type='pty'/>
    <console type='pty'/>
  </devices>
</domain>"""


def overlay_volume_xml(name: str, capacity: int, backing_path: str, backing_format: str) -> str:
    return f"""<volume>
  <name>{escape(name)}</name>
  <capacity unit='bytes'>{int(capacity)}</capacity>
  <target><format type='qcow2'/></target>
  <backingStore>
    <path>{escape(backing_path)}</path>
    <format type='{_attr(backing_format)}'/>
  </backingStore>
</volume>"""


def dir_pool_xml(name: str, path: str) -> str:
    return f"""<pool type='dir'>
  <name>{escape(name)}</name>
  <target><path>{escape(path)}</path></target>
</pool>"""
