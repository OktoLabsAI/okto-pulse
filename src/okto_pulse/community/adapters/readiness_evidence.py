"""Register-before-remove readiness evidence for the Onda A adapters (R05-B IMP4).

Feeds the R05-A ledger's ``evaluate_removal`` with the Community's bounded
evidence per Onda A ``adapter_key``. The Community adapters are REGISTERED and
consumed, but the core concretes/deps are NOT removed (removal = R05-E):
``dependency_audit_passed`` is therefore ``False``, so ``evaluate_removal`` stays
``blocked`` (fail-closed) — proving register-before-remove WITHOUT removing
anything. Imports only the PURE R05-A ledger DTOs, no concrete adapter.
"""

from __future__ import annotations

from okto_pulse.core.application.boundary.adapter_readiness_inventory import (
    AdapterEvidence,
)

#: The Onda A adapter_keys (R05-A ledger) the Community edition extracted.
ONDA_A_ADAPTER_KEYS: tuple[str, ...] = (
    "filesystem_storage_provider",
    "sentence_transformer_embedding_provider",
    "stub_embedding_provider",
    "cross_encoder_reranker",
    "inmemory_cache_backend",
    "inmemory_token_bucket_rate_limiter",
    "inmemory_session_store",
)


def community_onda_a_evidence() -> dict[str, AdapterEvidence]:
    """The Community's register-before-remove evidence per Onda A adapter_key.

    ``dependency_audit_passed=False`` (the core concrete/dep is still present —
    removed only in R05-E) keeps every adapter ``blocked`` under
    ``evaluate_removal`` (fail-closed): registered + audited, but NOT removed.
    """
    registered_not_removed = AdapterEvidence(
        port_closed=True,
        community_registered=True,
        oracle_passed=True,
        import_audit_passed=True,
        dependency_audit_passed=False,  # core concrete/dep NOT removed (R05-E)
        register_before_remove_passed=True,
    )
    return {key: registered_not_removed for key in ONDA_A_ADAPTER_KEYS}


__all__ = ["ONDA_A_ADAPTER_KEYS", "community_onda_a_evidence"]
