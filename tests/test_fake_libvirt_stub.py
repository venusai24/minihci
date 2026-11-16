import threading
import time

import pytest

from minihci.driver.base import DomainNotFound, HostUnreachable
from minihci.driver.libvirt_errors import load_libvirt, map_error

U1, U2 = "qemu+ssh://root@host1/system", "qemu+ssh://root@host2/system"
XML = "<domain type='kvm'><name>vm1</name></domain>"
POOL = "<pool type='dir'><name>p</name><target><path>/img</path></target></pool>"
VOL = "<volume><name>{n}</name><capacity unit='bytes'>100</capacity>{b}</volume>"


def test_injected_and_error_mapping(stub_libvirt):
    lib = load_libvirt()
    assert lib is stub_libvirt
    c = lib.open(U1)
    with pytest.raises(lib.libvirtError) as ei:
        c.lookupByName("nope")
    assert isinstance(map_error(ei.value), DomainNotFound)
    stub_libvirt.host(U2).down = True
    with pytest.raises(lib.libvirtError) as ei:
        lib.open(U2)
    assert isinstance(map_error(ei.value), HostUnreachable)


def test_keepalive_requires_event_loop(stub_libvirt):
    c = stub_libvirt.open(U1)
    with pytest.raises(stub_libvirt.libvirtError):
        c.setKeepAlive(5, 3)
    stub_libvirt.virEventRegisterDefaultImpl()
    c.setKeepAlive(5, 3)
    assert c.keepalive == (5, 3)


def test_close_callback_and_alive(stub_libvirt):
    c = stub_libvirt.open(U1)
    seen = []
    c.registerCloseCallback(lambda conn, reason, op: seen.append((conn, reason, op)), "x")
    assert c.isAlive()
    c.simulate_close()
    assert seen == [(c, 2, "x")] and not c.isAlive()


def test_domain_lifecycle(stub_libvirt):
    c = stub_libvirt.open(U1)
    d = c.defineXML(XML)
    with pytest.raises(stub_libvirt.libvirtError):
        c.defineXML(XML)
    d.create()
    assert d.state()[0] == stub_libvirt.VIR_DOMAIN_RUNNING
    d.destroy()
    assert [x.name() for x in c.listAllDomains()] == ["vm1"]
    d.undefine()
    with pytest.raises(stub_libvirt.libvirtError):
        c.lookupByName("vm1")


def test_pool_cache_needs_refresh_across_hosts(stub_libvirt):
    c1, c2 = stub_libvirt.open(U1), stub_libvirt.open(U2)
    p1, p2 = c1.storagePoolDefineXML(POOL), c2.storagePoolDefineXML(POOL)
    p1.createXML(VOL.format(n="a.qcow2", b="<backingStore><path>/b</path><format type='qcow2'/></backingStore>"))
    stub_libvirt.host(U2).storage = stub_libvirt.host(U1).storage  # shared NFS
    with pytest.raises(stub_libvirt.libvirtError):
        c2.storageVolLookupByPath("/img/a.qcow2")
    p2.refresh(0)
    v = c2.storageVolLookupByPath("/img/a.qcow2")
    assert v.info()[1] == 100 and "<path>/b</path>" in v.XMLDesc()
    v.delete(0)
    assert "/img/a.qcow2" not in stub_libvirt.host(U1).storage


def test_migrate3_and_job_stats(stub_libvirt):
    s, d = stub_libvirt.open(U1), stub_libvirt.open(U2)
    dom = s.defineXML(XML)
    dom.create()
    lib = stub_libvirt
    flags = lib.VIR_MIGRATE_LIVE | lib.VIR_MIGRATE_PERSIST_DEST | lib.VIR_MIGRATE_UNDEFINE_SOURCE
    new = dom.migrate3(d, {}, flags)
    assert new.jobStats(lib.VIR_DOMAIN_JOB_STATS_COMPLETED)["downtime"] == 90
    with pytest.raises(lib.libvirtError):
        dom.jobStats(lib.VIR_DOMAIN_JOB_STATS_COMPLETED)
    assert d.lookupByName("vm1").state()[0] == lib.VIR_DOMAIN_RUNNING
    assert not s.listAllDomains()


def test_stats(stub_libvirt):
    c = stub_libvirt.open(U1)
    assert c.getInfo()[1:3] == [2456, 2]
    assert set(c.getCPUStats(-1)) == {"user", "kernel", "idle", "iowait", "guest"}
    assert set(c.getMemoryStats(-1)) == {"total", "free", "buffers", "cached"}


def test_hang_hook(stub_libvirt):
    ev = stub_libvirt.hang(U1, "open")
    out = []
    t = threading.Thread(target=lambda: out.append(stub_libvirt.open(U1)), daemon=True)
    t.start()
    time.sleep(0.1)
    assert t.is_alive() and not out
    ev.set()
    t.join(2)
    assert len(out) == 1
