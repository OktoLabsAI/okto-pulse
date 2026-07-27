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


def _registered_not_removed_evidence() -> AdapterEvidence:
    """Build a FRESH register-before-remove evidence object.

    ``dependency_audit_passed=False`` (the core concrete/dep is still present —
    removed only in R05-E) keeps the adapter ``blocked`` under
    ``evaluate_removal`` (fail-closed): registered + audited, but NOT removed.
    """
    return AdapterEvidence(
        port_closed=True,
        community_registered=True,
        oracle_passed=True,
        import_audit_passed=True,
        dependency_audit_passed=False,  # core concrete/dep NOT removed (R05-E)
        register_before_remove_passed=True,
    )


def community_onda_a_evidence() -> dict[str, AdapterEvidence]:
    """The Community's register-before-remove evidence per Onda A adapter_key.

    R03 IMP1 (FR1/TR2/AC1, ts_e4c2a991): every key — and the three in-memory
    slots ``inmemory_cache_backend`` / ``inmemory_token_bucket_rate_limiter`` /
    ``inmemory_session_store`` in particular — owns a DISTINCT, immutable
    ``AdapterEvidence`` instance. Previously a single shared instance was aliased
    across all keys; although ``AdapterEvidence`` is frozen (so value isolation
    already held), the keys were IDENTITY-coupled (``ev[a] is ev[b]``). Per-key
    construction guarantees that mutating/replacing one slot's evidence can never
    alias another's — cache_backend, rate_limiter and session_store stay
    independent.
    """
    return {key: _registered_not_removed_evidence() for key in ONDA_A_ADAPTER_KEYS}


__all__ = ["ONDA_A_ADAPTER_KEYS", "community_onda_a_evidence"]
