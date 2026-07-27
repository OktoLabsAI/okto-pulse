"""R03 REPLAN-IMP1 — per-key readiness evidence independence (FR1/TR2/AC1).

ts_e4c2a991: the Onda A readiness evidence is per-key and immutable — mutating
(replacing) one slot's evidence cannot alias another's. Discriminating teeth: the
three in-memory slots are DISTINCT instances; the previous shared-instance
aliasing made ``ev[a] is ev[b]`` True (value isolation already held because
``AdapterEvidence`` is frozen, so identity is what the fix actually changes).
"""

from __future__ import annotations

from okto_pulse.community.adapters.readiness_evidence import (
    ONDA_A_ADAPTER_KEYS,
    community_onda_a_evidence,
)
from okto_pulse.core.application.boundary.adapter_readiness_inventory import (
    AdapterEvidence,
    build_adapter_inventory,
    evaluate_removal,
)

CACHE = "inmemory_cache_backend"
RATE = "inmemory_token_bucket_rate_limiter"
SESSION = "inmemory_session_store"


def _entry(adapter_key: str):
    return next(e for e in build_adapter_inventory() if e.adapter_key == adapter_key)


def test_every_key_owns_a_distinct_evidence_instance():
    ev = community_onda_a_evidence()
    ids = [id(ev[k]) for k in ONDA_A_ADAPTER_KEYS]
    assert len(set(ids)) == len(ONDA_A_ADAPTER_KEYS), "evidence instances are aliased"
    # the three in-memory slots in particular are pairwise DISTINCT objects
    assert ev[CACHE] is not ev[RATE]
    assert ev[CACHE] is not ev[SESSION]
    assert ev[RATE] is not ev[SESSION]


def test_mutating_one_slot_evidence_leaves_others_independent():
    ev = community_onda_a_evidence()
    rate_before, session_before = ev[RATE], ev[SESSION]
    rate_readiness_before = evaluate_removal(_entry(RATE), ev[RATE]).status
    session_readiness_before = evaluate_removal(_entry(SESSION), ev[SESSION]).status

    # "Mutate" cache_backend's evidence == replace it with a fully-different state.
    ev[CACHE] = AdapterEvidence(
        port_closed=False,
        community_registered=False,
        oracle_passed=False,
        import_audit_passed=False,
        dependency_audit_passed=False,
        register_before_remove_passed=False,
    )

    # rate_limiter / session_store evidence are untouched (identity + value), and
    # so is their readiness verdict — only cache_backend changed.
    assert ev[RATE] is rate_before
    assert ev[SESSION] is session_before
    assert ev[RATE].as_map() == rate_before.as_map()
    assert ev[SESSION].as_map() == session_before.as_map()
    assert evaluate_removal(_entry(RATE), ev[RATE]).status == rate_readiness_before
    assert evaluate_removal(_entry(SESSION), ev[SESSION]).status == session_readiness_before


def test_independent_objects_across_calls():
    """Each call yields fresh per-key objects — no cross-call shared mutable state
    that one caller could observe another mutating."""
    a = community_onda_a_evidence()
    b = community_onda_a_evidence()
    for k in (CACHE, RATE, SESSION):
        assert a[k] is not b[k]
        assert a[k].as_map() == b[k].as_map()
