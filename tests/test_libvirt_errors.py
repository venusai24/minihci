import subprocess
import sys
import types

import pytest

from minihci.driver.base import DomainExists, DomainNotFound, DriverError, HostUnreachable
from minihci.driver.libvirt_errors import load_libvirt, map_error


class FakeErr(Exception):
    def __init__(self, code, msg="boom"):
        super().__init__(msg)
        self.code = code

    def get_error_code(self):
        return self.code


@pytest.fixture
def lib():
    m = types.ModuleType("libvirt")
    for i, n in enumerate(
        ["NO_DOMAIN", "DOM_EXIST", "OPERATION_INVALID", "SYSTEM_ERROR", "NO_CONNECT", "RPC", "INVALID_CONN", "OTHER"], 1
    ):
        setattr(m, f"VIR_ERR_{n}", i)
    return m


@pytest.mark.parametrize(
    "name,exc",
    [
        ("NO_DOMAIN", DomainNotFound),
        ("DOM_EXIST", DomainExists),
        ("OPERATION_INVALID", DriverError),
        ("SYSTEM_ERROR", HostUnreachable),
        ("NO_CONNECT", HostUnreachable),
        ("RPC", HostUnreachable),
        ("OTHER", DriverError),
    ],
)
def test_map_error(lib, name, exc):
    out = map_error(FakeErr(getattr(lib, f"VIR_ERR_{name}")), lib)
    assert type(out) is exc
    assert "boom" in str(out)


def test_map_error_without_code(lib):
    assert type(map_error(ValueError("x"), lib)) is DriverError


def test_load_libvirt_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "libvirt", None)
    with pytest.raises(DriverError, match="--extra libvirt"):
        load_libvirt()


def test_load_libvirt_stub(monkeypatch, lib):
    monkeypatch.setitem(sys.modules, "libvirt", lib)
    assert load_libvirt() is lib


def test_package_imports_without_libvirt():
    code = "import sys; sys.modules['libvirt']=None; import minihci, minihci.driver, minihci.driver.libvirt_errors"
    r = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
