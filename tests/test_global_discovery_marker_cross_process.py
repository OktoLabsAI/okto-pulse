"""Real cross-process INV-E2 floor (blocker 16): R1 and R3 with forced exits.

These regressions must use genuine separate OS processes and persisted evidence
- not monkeypatch + a second in-interpreter object.  A writer process force-exits
(`os._exit`) at a precise point, then a *fresh* process observes the durable
truth on disk.

* R1: force-exit mid-DDL -> marker + partial primary; a fresh process classifies
  it PRESENT_UNREADABLE_OR_ERROR / bootstrap_incomplete and refuses bootstrap.
* R3: force-exit after primary+readback complete but before marker clear -> a
  fresh process runs the real fenced recovery ceremony, which validates/preserves
  the data, clears the marker, and Global Discovery becomes readable.

R2/R4 live in the in-process suites; these two require real persisted evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("ladybug")

REPO_SRC = Path(__file__).parent.parent / "src"
WORKSPACE_ROOT = Path(__file__).parent.parent.parent
CORE_SRC_CANDIDATES = (
    WORKSPACE_ROOT / "okto_labs_pulse_core" / "src",
    WORKSPACE_ROOT / "okto-pulse-core" / "src",
)
SRC_PATHS = [REPO_SRC, *(p for p in CORE_SRC_CANDIDATES if p.exists())]

_PREAMBLE = """
import os, sys
SRC = {src!r}
for p in reversed(SRC):
    if p not in sys.path:
        sys.path.insert(0, p)
from pathlib import Path
from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterLease


class _Lock:
    def is_owner(self, *a):
        return True

    def release(self, **k):
        return True


def _lease():
    return GlobalDiscoveryWriterLease(lock=_Lock(), owner_token="w", operation="op")


def _configure(legacy):
    # A fresh process is its own composition root: configure Core settings with
    # an isolated home so the real graph runtime can open Ladybug.
    os.environ["OKTO_PULSE_HOME"] = str(legacy.parent.parent)
    os.environ["OKTO_PULSE_SKIP_DEMO_SEED"] = "1"
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings

    configure_settings(CommunitySettings())
"""


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    src = [str(p) for p in SRC_PATHS]
    full = _PREAMBLE.format(src=src) + textwrap.dedent(script)
    return subprocess.run(
        [sys.executable, "-c", full, *args],
        capture_output=True,
        text=True,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# R1 - force-exit mid-DDL, fresh process observes marker+partial as unreadable
# ---------------------------------------------------------------------------

_R1_WRITER = """
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters import global_discovery_schema

legacy = Path(sys.argv[1])
_configure(legacy)

def _boom(_conn):
    # Hard exit mid-DDL: NODE_DDL/REL_DDL already created a partial primary and
    # the durable marker was written first.
    os._exit(42)

global_discovery_schema.ensure_decision_digest_layer_column = _boom
rt = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: legacy)
with _lease().guard():
    rt.bootstrap()
os._exit(0)
"""

_R1_OBSERVER = """
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_present,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
)

legacy = Path(sys.argv[1])
_configure(legacy)
rt = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: legacy)
st = rt.state()
assert st.state == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR, st.state
assert st.reason_code == "global_discovery_bootstrap_incomplete", st.reason_code
assert st.details.get("primary_confirmed_absent") is False, st.details
assert bootstrap_marker_present(legacy) is True

refused = False
try:
    with _lease().guard():
        rt.bootstrap()
except RuntimeError as exc:
    refused = "global_discovery_bootstrap_refused_marker_present" in str(exc)
assert refused, "fresh process bootstrap did not refuse over marker+partial"
print("R1_OK")
"""


def test_r1_forced_exit_mid_ddl_then_fresh_process_unreadable(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"

    writer = _run(_R1_WRITER, str(legacy))
    assert writer.returncode == 42, (writer.returncode, writer.stdout, writer.stderr)
    # Persisted evidence: marker + a partial primary artifact on disk.
    from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
        bootstrap_marker_path,
    )

    assert bootstrap_marker_path(legacy).exists()
    assert legacy.exists()

    observer = _run(_R1_OBSERVER, str(legacy))
    assert observer.returncode == 0, (observer.stdout, observer.stderr)
    assert "R1_OK" in observer.stdout


# ---------------------------------------------------------------------------
# R3 - force-exit after complete bootstrap before marker clear; fresh-process
# recovery ceremony validates/preserves, clears marker, becomes readable
# ---------------------------------------------------------------------------

_R3_WRITER = """
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
import okto_pulse.community.adapters.global_discovery_bootstrap_marker as mm

legacy = Path(sys.argv[1])
_configure(legacy)
emb = [0.02 * (i % 5) for i in range(384)]

rt = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: legacy)
# Bootstrap #1 completes (clearing its own marker), then persist REAL
# board/digest/link content durably into the complete primary.
with _lease().guard():
    rt.bootstrap()
    rt.upsert_board_summary(
        board_id="board-preserve",
        name="Preserve",
        summary="preserved summary",
        summary_embedding=emb,
        decision_count=1,
        synced_at="2026-07-18T00:00:00",
    )
    rt.upsert_decision_digest(
        digest_id="digest-preserve",
        board_id="board-preserve",
        original_node_id="node-preserve",
        title="Preserved decision",
        summary="preserved decision summary",
        node_type="Decision",
        graph_layer="canonical",
        embedding=emb,
        created_at="2026-07-18T00:00:00",
    )
    rt.link_board_digest(board_id="board-preserve", digest_id="digest-preserve")
    # Blocker 8: durably flush the content, then CLOSE and REOPEN before
    # capturing the canonical projection, so the evidence provably represents
    # durable pre-crash truth (not just page-cache state).
    rt.flush_after_write_batch()
    rt.close()

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery as _Rec,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    canonical_sha256 as _csha,
    write_json_atomic as _wja,
)
rt_reopen = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: legacy)
with _lease().guard():
    _proj = _Rec._actual_semantic_projection(rt_reopen)
rt_reopen.close()
# Atomically write the evidence to an INDEPENDENT path (temp+rename) and fsync
# its parent directory, BEFORE bootstrap #2 creates the marker.  R7: CAPTURE the
# parent-directory-fsync support the writer actually returns (it is NOT assumed)
# and record it to a sidecar so the parent asserts the OBSERVED boolean rather
# than implying guaranteed durability (this is legitimately False on Windows).
_parent_fsync_supported = _wja(
    Path(sys.argv[2]), {"projection": _proj, "fingerprint": _csha(_proj)}
)
Path(sys.argv[2] + ".parent_fsync").write_text(
    "true" if _parent_fsync_supported else "false", encoding="ascii"
)

# Bootstrap #2 re-bootstraps (content-preserving idempotent DDL) and crashes
# after the readback is complete but BEFORE the marker clear.
def _exit_before_clear(legacy_path, *, fence_check):
    os._exit(43)

mm.clear_bootstrap_marker = _exit_before_clear
with _lease().guard():
    rt.bootstrap()
os._exit(0)
"""

_R3_RECOVERY = """
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_present,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    resolve_active_graph_path,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
)
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryDigestSeed,
)

legacy = Path(sys.argv[1])
_configure(legacy)
emb = tuple(0.01 * (i % 7) for i in range(384))
# Seeds are deliberately DIFFERENT from the pre-crash content, so proving the
# original content survived is a preservation proof, not seed recreation.
seeds = (
    GlobalDiscoveryBoardSeed(
        board_id="board-from-seed",
        board_name="Board From Seed",
        summary="Board summary",
        summary_embedding=emb,
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id="node-seed",
                title="Decision",
                summary="Decision summary",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref="artifact-seed",
                embedding=emb,
            ),
        ),
        source_inventory_hash="source-hash",
    ),
)

# Fresh-process fenced recovery ceremony (real runtime + real candidate builds).
global_runtime = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: legacy)
# Pre-condition: a fresh process sees the complete-but-marked graph as unreadable.
pre = global_runtime.state()
assert pre.state == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR, pre.state
assert pre.reason_code == "global_discovery_bootstrap_incomplete", pre.reason_code
assert bootstrap_marker_present(legacy) is True

# Blocker 19/28: bind ONE lease instance, use fence_check=lease.assert_fenced,
# and execute under that SAME lease.guard() — exact-token continuity, not an
# unrelated guard with a no-op fence.
lease = _lease()
adapter = CommunityGlobalDiscoveryRecovery(
    global_runtime=global_runtime,
    graph_path_provider=lambda: legacy,
    fence_check=lease.assert_fenced,
)
before = adapter.inspect_live_artifact()
# Unified recovery: a genuinely complete live primary is ADOPTED (its exact
# bytes published as the active generation), not rebuilt from seeds.
with lease.guard():
    result = adapter.recover_and_cutover(
        run_id="gdr_r3recover",
        expected_live_sha256=before.sha256,
        boards=seeds,
    )
assert result.outcome == "completed", result.outcome
assert bootstrap_marker_present(legacy) is False
assert resolve_active_graph_path(legacy) != legacy
post = global_runtime.state()
assert post.state == GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE, post.state

# Blocker 1/2: EXACT projection equality against INDEPENDENT pre-crash evidence.
# Build the full canonical semantic projection of the RECOVERED ACTIVE graph and
# assert it equals the pre-crash evidence exactly (every board/digest/link
# property, embedding hash, endpoint, ownership, counts) AND the canonical
# fingerprint matches.  Quarantine is additional evidence only.
import json as _json
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery as _Rec,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    canonical_sha256 as _csha,
)
evidence = _json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
# The projection RETURNs summary_embedding, which the runtime classifies as a
# vector op requiring the writer fence, so read it under a fresh lease guard.
with _lease().guard():
    active_projection = _Rec._actual_semantic_projection(global_runtime)
assert active_projection == evidence["projection"], (
    active_projection, evidence["projection"]
)
assert _csha(active_projection) == evidence["fingerprint"]

# Negative: the deliberately different seed board/digest are ABSENT from the
# recovered active graph (seeds cannot recreate the adopted content).
active_boards = {str(b["board_id"]) for b in active_projection["boards"]}
assert active_boards == {"board-preserve"}, active_boards
assert "board-from-seed" not in active_boards, active_boards
assert {str(d["id"]) for d in active_projection["digests"]} == {"digest-preserve"}
assert len(active_projection["links"]) == 1

# The quarantined original is additional (not substitute) preservation evidence.
quarantine_original = (
    legacy.parent
    / "quarantine"
    / "global-discovery"
    / "gdr_r3recover"
    / "original"
    / legacy.name
)
assert quarantine_original.exists(), quarantine_original
print("R3_OK")
"""


def test_r3_forced_exit_before_clear_then_fresh_process_ceremony_recovers(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    # Independent pre-crash evidence path (outside the graph + quarantine dirs).
    evidence = tmp_path / "pre_crash_evidence.json"

    writer = _run(_R3_WRITER, str(legacy), str(evidence))
    assert writer.returncode == 43, (writer.returncode, writer.stdout, writer.stderr)
    # Persisted evidence: a COMPLETE primary plus the still-present marker.
    from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
        bootstrap_marker_path,
    )

    assert legacy.exists()
    assert bootstrap_marker_path(legacy).exists()
    assert evidence.exists()  # canonical projection captured before the marker

    # R7: the writer captured and recorded the ACTUAL parent-directory-fsync
    # support of the evidence write.  Assert the recorded value is a real,
    # honestly-observed boolean — NOT that durability was guaranteed.  On Windows
    # this is legitimately "false"; the test states the observed value rather than
    # implying support.
    parent_fsync_sidecar = Path(str(evidence) + ".parent_fsync")
    assert parent_fsync_sidecar.exists()
    observed_parent_fsync = parent_fsync_sidecar.read_text(encoding="ascii").strip()
    assert observed_parent_fsync in ("true", "false"), observed_parent_fsync
    if os.name == "nt":
        # Directory fsync is not supported on Windows; the evidence must report it
        # honestly as false, never optimistically true.
        assert observed_parent_fsync == "false"

    recovery = _run(_R3_RECOVERY, str(legacy), str(evidence))
    assert recovery.returncode == 0, (recovery.stdout, recovery.stderr)
    assert "R3_OK" in recovery.stdout
