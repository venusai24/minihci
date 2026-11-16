"""Lazy libvirt import and libvirt error -> HostDriver exception mapping (D-026)."""

from __future__ import annotations

import importlib
from types import ModuleType

from minihci.driver.base import DomainExists, DomainNotFound, DriverError, HostUnreachable

INSTALL_HINT = "libvirt-python is not installed; install the extra with `uv sync --extra libvirt`"


def load_libvirt() -> ModuleType:
    try:
        mod = importlib.import_module("libvirt")
    except ImportError as e:  # also covers sys.modules['libvirt'] = None
        raise DriverError(INSTALL_HINT) from e
    if mod is None:
        raise DriverError(INSTALL_HINT)
    return mod


def map_error(e: BaseException, lib: ModuleType | None = None) -> DriverError:
    """Translate a libvirt.libvirtError into the driver's exception types."""
    if isinstance(e, DriverError):
        return e
    lib = lib or load_libvirt()
    get_code = getattr(e, "get_error_code", None)
    code = get_code() if callable(get_code) else None
    msg = str(e)

    def is_(name: str) -> bool:
        return code is not None and code == getattr(lib, name, object())

    if is_("VIR_ERR_NO_DOMAIN"):
        return DomainNotFound(msg)
    if is_("VIR_ERR_DOM_EXIST"):
        return DomainExists(msg)
    if any(is_(n) for n in ("VIR_ERR_SYSTEM_ERROR", "VIR_ERR_NO_CONNECT", "VIR_ERR_RPC", "VIR_ERR_INVALID_CONN")):
        return HostUnreachable(msg)
    return DriverError(msg)
