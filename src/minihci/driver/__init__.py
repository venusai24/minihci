from minihci.config import Settings
from minihci.driver.base import HostDriver


def build_driver(settings: Settings) -> HostDriver:
    if settings.driver == "fake":
        from minihci.driver.fake import FakeDriver

        return FakeDriver(
            state_file=settings.fake_state_file,
            delays=settings.fake_delays,
            delays_after=settings.fake_delays_after,
            fail=settings.fake_fail,
        )
    if settings.driver == "libvirt":
        from minihci.driver.libvirt import LibvirtDriver

        return LibvirtDriver(
            storage_uris=settings.libvirt_storage_uris,
            image_root=settings.image_root,
            storage_root=settings.storage_root,
        )
    raise NotImplementedError(f"driver not implemented: {settings.driver}")
