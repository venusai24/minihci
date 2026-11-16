import xml.etree.ElementTree as ET
from pathlib import Path

from minihci.driver.base import DomainSpec
from minihci.driver.libvirt_xml import dir_pool_xml, domain_xml, overlay_volume_xml

GOLDEN = Path(__file__).parent / "golden" / "domain_test1.xml"


def test_domain_xml_matches_golden():
    spec = DomainSpec(name="test1", vcpus=1, mem_mb=256, disk_path="/var/lib/minihci/vms/test1.qcow2")
    assert domain_xml(spec).strip() == GOLDEN.read_text().strip()


def test_domain_xml_parameters_and_escaping():
    root = ET.fromstring(domain_xml(DomainSpec("a&b", 4, 2048, "/x/<y>.qcow2")))
    assert root.findtext("name") == "a&b"
    assert root.findtext("memory") == "2048"
    assert root.findtext("vcpu") == "4"
    assert root.find("devices/disk/source").get("file") == "/x/<y>.qcow2"


def test_overlay_volume_xml():
    root = ET.fromstring(overlay_volume_xml("vm1.qcow2", 117440512, "/img/cirros.img", "qcow2"))
    assert root.tag == "volume"
    assert root.findtext("name") == "vm1.qcow2"
    cap = root.find("capacity")
    assert cap.get("unit") == "bytes" and cap.text == "117440512"
    assert root.find("target/format").get("type") == "qcow2"
    assert root.findtext("backingStore/path") == "/img/cirros.img"
    assert root.find("backingStore/format").get("type") == "qcow2"


def test_dir_pool_xml():
    root = ET.fromstring(dir_pool_xml("minihci-vms", "/var/lib/minihci/vms"))
    assert root.get("type") == "dir"
    assert root.findtext("name") == "minihci-vms"
    assert root.findtext("target/path") == "/var/lib/minihci/vms"
