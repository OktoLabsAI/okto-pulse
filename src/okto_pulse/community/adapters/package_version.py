"""Edition-owned access to installed distribution metadata."""

from importlib.metadata import PackageNotFoundError, version

from okto_pulse.core import __version__ as core_version, resolve_package_version


class ImportlibMetadataVersionProvider:
    """Implement the public PackageVersionProvider port using installed wheels."""

    def version(self, package_name: str) -> str | None:
        try:
            return version(package_name)
        except PackageNotFoundError:
            return None


def default_core_version() -> str:
    """Honor composition overrides before the edition's installed metadata."""
    return (
        resolve_package_version("okto-pulse-core")
        or ImportlibMetadataVersionProvider().version("okto-pulse-core")
        or core_version
    )
