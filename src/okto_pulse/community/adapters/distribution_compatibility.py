"""Refuse an incompatible installed pair before loading runtime composition."""

from importlib import import_module, metadata

from okto_pulse.community import __version__ as community_code_version

EXPECTED_CORE_CONTRACT = "pulse-edition-api/4"


class IncompatiblePulseDistribution(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(
            f"incompatible_pulse_distribution: {reason}; install Core and Community "
            "from the same qualified release before starting Pulse."
        )


def require_compatible_core() -> None:
    try:
        core_version = metadata.version("okto-pulse-core")
        community_version = metadata.version("okto-pulse")
    except metadata.PackageNotFoundError as exc:
        raise IncompatiblePulseDistribution("paired_distribution_missing") from exc
    if core_version != community_version or community_version != community_code_version:
        raise IncompatiblePulseDistribution("paired_version_mismatch")
    try:
        contract = import_module("okto_pulse.core.ports.edition_compatibility")
        from okto_pulse.core import __version__ as core_code_version
    except ImportError as exc:
        raise IncompatiblePulseDistribution("core_contract_unavailable") from exc
    if core_code_version != core_version:
        raise IncompatiblePulseDistribution("core_code_version_mismatch")
    if getattr(contract, "EDITION_API_CONTRACT", None) != EXPECTED_CORE_CONTRACT:
        raise IncompatiblePulseDistribution("core_contract_mismatch")
