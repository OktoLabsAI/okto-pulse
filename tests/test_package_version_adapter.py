"""Artifact identity stays edition-owned and honors the composition port."""

from importlib.metadata import version

from okto_pulse.core import (
    register_package_version_provider,
    reset_package_version_provider_for_tests,
)
from okto_pulse.core.ports.package_version import MappingPackageVersionProvider
from okto_pulse.community.adapters.package_version import (
    ImportlibMetadataVersionProvider,
    default_core_version,
)
from okto_pulse.community.config import CommunitySettings


def test_installed_identity_and_unknown_distribution():
    provider = ImportlibMetadataVersionProvider()
    assert provider.version("okto-pulse-core") == version("okto-pulse-core")
    assert provider.version("pulse-nonexistent-fixture-distribution") is None
    settings = CommunitySettings(_env_file=None)
    assert settings.app_version == settings.mcp_server_version == version("okto-pulse-core")


def test_composition_version_override_is_preserved():
    register_package_version_provider(MappingPackageVersionProvider({"okto-pulse-core": "test"}))
    try:
        assert default_core_version() == "test"
    finally:
        reset_package_version_provider_for_tests()
