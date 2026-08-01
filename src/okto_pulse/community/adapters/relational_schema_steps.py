"""Community-owned concrete relational schema migration steps."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)

StepCallable = Callable[[], "Awaitable[object] | object"]


def normalize_global_discovery_source_revision_trigger_sql(raw: object) -> str:
    """Canonicalize SQLite trigger DDL for bounded integrity comparison."""

    return re.sub(r'[\s"`;\[\]]+', "", str(raw or "").lower())


def global_discovery_source_revision_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return the exact owned trigger name -> (table, SQL) contract."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
        GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
        GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX,
        GlobalDiscoverySourceRevision,
    )

    revision_table_name = GlobalDiscoverySourceRevision.__tablename__
    operation_sql = {
        "insert": "INSERT",
        "update": "UPDATE",
        "delete": "DELETE",
    }
    expected: dict[str, tuple[str, str]] = {}
    for table_name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES:
        for operation, sql_operation in operation_sql.items():
            trigger_name = (
                f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
AFTER {sql_operation} ON "{table_name}"
BEGIN
    UPDATE "{revision_table_name}"
    SET revision = revision + 1,
        mutation_nonce = lower(hex(randomblob(32))),
        updated_at = CURRENT_TIMESTAMP
    WHERE scope_id = '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}';
    SELECT CASE WHEN changes() <> 1
        THEN RAISE(ABORT, 'global_discovery_source_revision_missing') END;
END'''
            expected[trigger_name] = (table_name, trigger_sql)

    delete_guard_name = (
        f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_singleton_delete_guard"
    )
    expected[delete_guard_name] = (
        revision_table_name,
        f'''CREATE TRIGGER "{delete_guard_name}"
BEFORE DELETE ON "{revision_table_name}"
WHEN OLD.scope_id = '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}'
BEGIN
    SELECT RAISE(ABORT, 'global_discovery_source_revision_delete_forbidden');
END''',
    )
    scope_guard_name = (
        f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}_scope_update_guard"
    )
    expected[scope_guard_name] = (
        revision_table_name,
        f'''CREATE TRIGGER "{scope_guard_name}"
BEFORE UPDATE OF scope_id ON "{revision_table_name}"
WHEN OLD.scope_id = '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}'
    AND NEW.scope_id <> '{GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID}'
BEGIN
    SELECT RAISE(ABORT, 'global_discovery_source_revision_scope_forbidden');
END''',
    )
    return expected


COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX = "trg_kg_cognitive_source_immutable"

KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX = "trg_knowledge_propagation_v2"
GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX = "trg_guideline_policy_immutable"
GUIDELINE_IMPORT_CANDIDATE_TRIGGER_PREFIX = (
    "trg_guideline_import_binding_candidate"
)
GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX = "trg_guideline_revision_noop"
GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX = "trg_guideline_impact_v2"
POLICY_COMPLIANCE_IMMUTABILITY_TRIGGER_PREFIX = "trg_policy_compliance_immutable"
POLICY_WAIVER_TRIGGER_PREFIX = "trg_policy_waiver_v2"
POLICY_WAIVER_PREDECESSOR_TRIGGER_PREFIX = "trg_policy_waiver_v1"
SEMANTIC_GUIDELINE_TRIGGER_PREFIX = "trg_semantic_guideline_v3"


def _guideline_binding_fence_payload_v2(
    *,
    board_id: str,
    guideline_id: str,
    binding_id: str,
    binding_revision: int,
    revision_id: str,
    semantic_version: str,
    revision_digest: str,
    priority: int,
    enforcement: str,
    minimum_confidence: int,
    metric_threshold_overrides: Mapping[str, int],
    configuration_digest: str,
    state: str,
    source_kind: str,
) -> dict[str, object]:
    """Rebuild the exact Core-owned semantic binding-fence payload."""

    return {
        "contract": "guideline-impact/v2",
        "kind": "binding_fence",
        "board_id": board_id,
        "guideline_id": guideline_id,
        "binding_id": binding_id,
        "binding_revision": binding_revision,
        "revision_id": revision_id,
        "semantic_version": semantic_version,
        "revision_digest": revision_digest,
        "priority": priority,
        "enforcement": enforcement,
        "minimum_confidence": minimum_confidence,
        "metric_threshold_overrides": dict(metric_threshold_overrides),
        "configuration_digest": configuration_digest,
        "state": state,
        "source_kind": source_kind,
    }


def _guideline_binding_fence_digest_v2(
    *,
    board_id: str,
    guideline_id: str,
    binding_id: str,
    binding_revision: int,
    revision_id: str,
    semantic_version: str,
    revision_digest: str,
    priority: int,
    enforcement: str,
    minimum_confidence: int,
    metric_threshold_overrides: Mapping[str, int],
    configuration_digest: str,
    state: str,
    source_kind: str,
) -> str:
    """Hash a complete v2 binding fence for relational integrity audits."""

    from okto_pulse.core.domain.quality_canonicalization import canonical_sha256

    return canonical_sha256(
        _guideline_binding_fence_payload_v2(
            board_id=board_id,
            guideline_id=guideline_id,
            binding_id=binding_id,
            binding_revision=binding_revision,
            revision_id=revision_id,
            semantic_version=semantic_version,
            revision_digest=revision_digest,
            priority=priority,
            enforcement=enforcement,
            minimum_confidence=minimum_confidence,
            metric_threshold_overrides=metric_threshold_overrides,
            configuration_digest=configuration_digest,
            state=state,
            source_kind=source_kind,
        )
    )


def guideline_revision_noop_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """SQLite CAS and append-only guards for the revision no-op ledger."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        Guideline,
        GuidelineHeadRow,
        GuidelineRevisionNoopReplayRow,
    )

    table_name = GuidelineRevisionNoopReplayRow.__tablename__
    head_table = GuidelineHeadRow.__tablename__
    guideline_table = Guideline.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    insert_name = f"{GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX}_insert"
    update_name = f"{GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX}_update"
    delete_name = f"{GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX}_delete"
    delete_when = ""
    if allow_board_erasure:
        delete_when = f'''
WHEN NOT EXISTS (
    SELECT 1
    FROM "{permit_table}" AS permit
    JOIN "{guideline_table}" AS guideline
      ON guideline."board_id" = permit."board_id"
    WHERE guideline."id" = OLD."guideline_id"
      AND guideline."scope" = 'inline'
)'''
    return {
        insert_name: (
            table_name,
            f'''CREATE TRIGGER "{insert_name}"
BEFORE INSERT ON "{table_name}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_revision_noop_head_conflict')
    WHERE NOT EXISTS (
        SELECT 1
        FROM "{head_table}" AS head
        WHERE head."guideline_id" = NEW."guideline_id"
          AND head."revision_id" = NEW."revision_id"
          AND head."revision_number" = NEW."revision_number"
          AND head."semantic_version" = NEW."semantic_version"
          AND head."head_revision" = NEW."original_head_revision"
          AND head."updated_at" = NEW."original_head_updated_at"
    );
END''',
        ),
        update_name: (
            table_name,
            f'''CREATE TRIGGER "{update_name}"
BEFORE UPDATE ON "{table_name}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_revision_noop_immutable');
END''',
        ),
        delete_name: (
            table_name,
            f'''CREATE TRIGGER "{delete_name}"
BEFORE DELETE ON "{table_name}"{delete_when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_revision_noop_immutable');
END''',
        ),
    }


def guideline_revision_noop_postgresql_ddl() -> tuple[str, str]:
    """PostgreSQL CAS/immutability function and trigger for the no-op ledger."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        Guideline,
        GuidelineHeadRow,
        GuidelineRevisionNoopReplayRow,
    )

    table_name = GuidelineRevisionNoopReplayRow.__tablename__
    head_table = GuidelineHeadRow.__tablename__
    guideline_table = Guideline.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    function_name = "pulse_guideline_revision_noop_guard"
    trigger_name = GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX
    function = f'''CREATE OR REPLACE FUNCTION "{function_name}"()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM "{head_table}" AS head
            WHERE head."guideline_id" = NEW."guideline_id"
              AND head."revision_id" = NEW."revision_id"
              AND head."revision_number" = NEW."revision_number"
              AND head."semantic_version" = NEW."semantic_version"
              AND head."head_revision" = NEW."original_head_revision"
              AND head."updated_at" = NEW."original_head_updated_at"
        ) THEN
            RAISE EXCEPTION 'guideline_revision_noop_head_conflict';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM "{permit_table}" AS permit
        JOIN "{guideline_table}" AS guideline
          ON guideline."board_id" = permit."board_id"
        WHERE guideline."id" = OLD."guideline_id"
          AND guideline."scope" = 'inline'
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guideline_revision_noop_immutable';
END;
$$ LANGUAGE plpgsql'''
    trigger = f'''CREATE TRIGGER "{trigger_name}"
BEFORE INSERT OR UPDATE OR DELETE ON "{table_name}"
FOR EACH ROW EXECUTE FUNCTION "{function_name}"()'''
    return function, trigger


def guideline_import_binding_candidate_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """SQLite append-only guards for inert imported binding evidence."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        GuidelineImportBindingCandidateRow,
    )

    table_name = GuidelineImportBindingCandidateRow.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    manifest: dict[str, tuple[str, str]] = {}
    for operation in ("update", "delete"):
        trigger_name = (
            f"{GUIDELINE_IMPORT_CANDIDATE_TRIGGER_PREFIX}_{operation}"
        )
        when = ""
        if allow_board_erasure and operation == "delete":
            when = f'''
WHEN NOT EXISTS (
    SELECT 1
    FROM "{permit_table}" AS permit
    WHERE permit."board_id" = OLD."target_board_id"
)'''
        manifest[trigger_name] = (
            table_name,
            f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_import_binding_candidate_immutable');
END''',
        )
    return manifest


def guideline_import_binding_candidate_postgresql_ddl() -> tuple[str, str]:
    """PostgreSQL function/trigger pair matching the SQLite guard."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        GuidelineImportBindingCandidateRow,
    )

    table_name = GuidelineImportBindingCandidateRow.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    function_name = "pulse_guideline_import_binding_candidate_guard"
    trigger_name = GUIDELINE_IMPORT_CANDIDATE_TRIGGER_PREFIX
    function = f'''CREATE OR REPLACE FUNCTION "{function_name}"()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM "{permit_table}" AS permit
        WHERE permit."board_id" = OLD."target_board_id"
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guideline_import_binding_candidate_immutable';
END;
$$ LANGUAGE plpgsql'''
    trigger = f'''CREATE TRIGGER "{trigger_name}"
BEFORE UPDATE OR DELETE ON "{table_name}"
FOR EACH ROW EXECUTE FUNCTION "{function_name}"()'''
    return function, trigger


def guideline_impact_immutability_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
    include_unlink: bool = True,
    include_retirement: bool = True,
    require_retirement_head_match: bool = False,
    verify_full_adoption_evidence: bool = True,
    verify_full_unlink_evidence: bool = True,
    verify_full_retirement_evidence: bool = True,
    verify_default_materialization: bool = True,
    protect_materialized_events: bool = True,
) -> dict[str, tuple[str, str]]:
    """Exact SQLite guards for sealed B08 preview/adoption evidence.

    The feature flags intentionally describe exact deployed predecessor
    manifests that may be upgraded in place.  They are not runtime policy
    switches: callers install the default manifest and use the variants only
    for exact predecessor recognition.
    """

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        DomainEventHandlerExecution,
        GuidelineImpactAdoptionRow,
        GuidelineImpactItemRow,
        GuidelineImpactReceiptRow,
        GuidelineImpactUnlinkRow,
        GuidelineRetirementImpactRow,
    )
    from okto_pulse.core.events.types import (
        PolicyAdoptionChanged,
        POLICY_BINDING_MATERIALIZED_EVENT_TYPE,
        PolicyRetirementChanged,
        SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE,
        SEMANTIC_GUIDELINE_PROJECTION_SCHEMA_VERSION,
    )

    permit_table = BoardErasurePermit.__tablename__
    receipt_table = GuidelineImpactReceiptRow.__tablename__
    item_table = GuidelineImpactItemRow.__tablename__
    adoption_table = GuidelineImpactAdoptionRow.__tablename__
    unlink_table = GuidelineImpactUnlinkRow.__tablename__
    retirement_impact_table = GuidelineRetirementImpactRow.__tablename__
    execution_table = DomainEventHandlerExecution.__tablename__
    manifest: dict[str, tuple[str, str]] = {}
    unlink_absent = (
        '\n          AND NEW."impact_unlink_id" IS NULL' if include_unlink else ""
    )
    retirement_head_match = (
        '''
      AND retirement."retired_revision_id" = NEW."revision_id"
      AND retirement."retired_revision_number" =
          NEW."revision_number"
      AND retirement."retired_semantic_version" =
          NEW."semantic_version"
      AND retirement."retired_revision_digest" =
          NEW."revision_digest"'''
        if require_retirement_head_match
        else ""
    )
    adoption_full_evidence = (
        """
      AND receipt."requires_explicit_adoption" = 1
      AND binding."impact_unlink_id" IS NULL
      AND binding."binding_revision" =
          COALESCE(receipt."expected_binding_revision", 0) + 1
      AND binding."idempotency_key" = NEW."idempotency_key"
      AND binding."request_digest" = NEW."request_digest"
      AND length(NEW."adoption_digest") = 64
      AND event."actor_type" = activity."actor_type"
      AND event."actor_type" IN ('agent', 'user', 'system')
      AND activity."actor_name" = NEW."adopted_by"
      AND json_type(event."payload_json") = 'object'
      AND (
          SELECT COUNT(*)
          FROM json_each(event."payload_json")
      ) = 28
      AND json_extract(
          event."payload_json", '$.event_schema_version'
      ) = 'guideline-impact/v2'
      AND json_extract(event."payload_json", '$.event_id') =
          NEW."event_id"
      AND (
          (
              receipt."expected_binding_revision" IS NULL
              AND json_type(
                  event."payload_json", '$.previous_binding_revision'
              ) = 'null'
          )
          OR json_extract(
              event."payload_json", '$.previous_binding_revision'
          ) = receipt."expected_binding_revision"
      )
      AND (
          (
              receipt."from_revision_id" IS NULL
              AND json_type(
                  event."payload_json", '$.from_revision_id'
              ) = 'null'
          )
          OR json_extract(
              event."payload_json", '$.from_revision_id'
          ) = receipt."from_revision_id"
      )
      AND (
          (
              receipt."from_semantic_version" IS NULL
              AND json_type(
                  event."payload_json", '$.from_semantic_version'
              ) = 'null'
          )
          OR json_extract(
              event."payload_json", '$.from_semantic_version'
          ) = receipt."from_semantic_version"
      )
      AND (
          (
              receipt."from_revision_digest" IS NULL
              AND json_type(
                  event."payload_json", '$.from_revision_digest'
              ) = 'null'
          )
          OR json_extract(
              event."payload_json", '$.from_revision_digest'
          ) = receipt."from_revision_digest"
      )
      AND json_extract(event."payload_json", '$.to_revision_id') =
          receipt."to_revision_id"
      AND json_extract(
          event."payload_json", '$.to_semantic_version'
      ) = receipt."to_semantic_version"
      AND json_extract(
          event."payload_json", '$.to_revision_digest'
      ) = receipt."to_revision_digest"
      AND json_extract(
          event."payload_json", '$.binding_digest_before'
      ) = receipt."binding_digest"
      AND json_extract(
          event."payload_json", '$.binding_head_digest_before'
      ) = receipt."binding_head_digest_before"
      AND json_extract(
          event."payload_json", '$.binding_head_digest_after'
      ) = receipt."binding_head_digest_after"
      AND json_extract(
          event."payload_json", '$.policy_set_digest'
      ) = receipt."policy_set_digest_after"
      AND json(json_extract(
          event."payload_json", '$.added_metric_ids'
      )) = json(receipt."added_metric_ids")
      AND json(json_extract(
          event."payload_json", '$.changed_metric_ids'
      )) = json(receipt."changed_metric_ids")
      AND json(json_extract(
          event."payload_json", '$.removed_metric_ids'
      )) = json(receipt."removed_metric_ids")
      AND json_extract(event."payload_json", '$.actor_id') =
          NEW."adopted_by"
      AND json_extract(event."payload_json", '$.actor_type') =
          event."actor_type"
      AND julianday(
          json_extract(event."payload_json", '$.occurred_at')
      ) = julianday(NEW."adopted_at")"""
        if verify_full_adoption_evidence
        else ""
    )
    unlink_full_evidence = (
        """
      AND length(NEW."unlink_digest") = 64
      AND event."actor_type" = activity."actor_type"
      AND event."actor_type" IN ('agent', 'user', 'system')
      AND activity."actor_name" = NEW."unlinked_by"
      AND json_type(event."payload_json") = 'object'
      AND (
          SELECT COUNT(*)
          FROM json_each(event."payload_json")
      ) = 28
      AND json_extract(
          event."payload_json", '$.event_schema_version'
      ) = 'guideline-impact/v2'
      AND json_extract(event."payload_json", '$.event_id') =
          NEW."event_id"
      AND julianday(
          json_extract(event."payload_json", '$.occurred_at')
      ) = julianday(NEW."unlinked_at")"""
        if verify_full_unlink_evidence
        else ""
    )
    retirement_full_evidence = (
        """
      AND retirement."request_digest" = NEW."request_digest"
      AND event."actor_type" = activity."actor_type"
      AND event."actor_type" IN ('agent', 'user', 'system')
      AND activity."actor_name" = NEW."retired_by"
      AND json_type(event."payload_json") = 'object'
      AND (
          SELECT COUNT(*)
          FROM json_each(event."payload_json")
      ) = 25"""
        if verify_full_retirement_evidence
        else ""
    )

    immutable_receipt_columns = tuple(
        column.name
        for column in GuidelineImpactReceiptRow.__table__.columns
        if column.name != "sealed"
    )
    unchanged_receipt = "\n    AND ".join(
        f'NEW."{column_name}" IS OLD."{column_name}"'
        for column_name in immutable_receipt_columns
    )
    receipt_insert_name = (
        f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{receipt_table}_insert"
    )
    manifest[receipt_insert_name] = (
        receipt_table,
        f'''CREATE TRIGGER "{receipt_insert_name}"
BEFORE INSERT ON "{receipt_table}"
WHEN NEW."sealed" <> 0
  OR json_type(NEW."proposed_metric_threshold_overrides") <> 'object'
  OR json_type(NEW."added_metric_ids") <> 'array'
  OR json_type(NEW."changed_metric_ids") <> 'array'
  OR json_type(NEW."removed_metric_ids") <> 'array'
  OR EXISTS (
      SELECT 1
      FROM json_each(
          NEW."proposed_metric_threshold_overrides"
      ) AS override
      WHERE override.type <> 'integer'
         OR override.value < 0
         OR override.value > 100
         OR NOT EXISTS (
             SELECT 1
             FROM "semantic_guideline_revisions" AS revision,
                  json_each(revision.metrics) AS metric
             WHERE revision.guideline_id = NEW.guideline_id
               AND revision.revision_id = NEW.to_revision_id
               AND revision.revision_digest =
                   NEW.to_revision_digest
               AND json_extract(metric.value, '$.code') =
                   override.key
         )
  )
  OR EXISTS (
      SELECT 1
      FROM (
          SELECT value, type, 'added' AS bucket
          FROM json_each(NEW."added_metric_ids")
          UNION ALL
          SELECT value, type, 'changed' AS bucket
          FROM json_each(NEW."changed_metric_ids")
          UNION ALL
          SELECT value, type, 'removed' AS bucket
          FROM json_each(NEW."removed_metric_ids")
      ) AS metric
      WHERE metric.type <> 'text'
         OR length(trim(metric.value)) = 0
  )
  OR (
      SELECT COUNT(*)
      FROM (
          SELECT value FROM json_each(NEW."added_metric_ids")
          UNION ALL
          SELECT value FROM json_each(NEW."changed_metric_ids")
          UNION ALL
          SELECT value FROM json_each(NEW."removed_metric_ids")
      )
  ) <> (
      SELECT COUNT(DISTINCT value)
      FROM (
          SELECT value FROM json_each(NEW."added_metric_ids")
          UNION ALL
          SELECT value FROM json_each(NEW."changed_metric_ids")
          UNION ALL
          SELECT value FROM json_each(NEW."removed_metric_ids")
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_receipt_v2_invalid');
END''',
    )
    receipt_update_name = (
        f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{receipt_table}_update"
    )
    manifest[receipt_update_name] = (
        receipt_table,
        f'''CREATE TRIGGER "{receipt_update_name}"
BEFORE UPDATE ON "{receipt_table}"
WHEN NOT (
    OLD."sealed" = 0
    AND NEW."sealed" = 1
    AND {unchanged_receipt}
    AND NEW."item_count" = (
        SELECT COUNT(*)
        FROM "{item_table}" AS item
        WHERE item."impact_receipt_id" = OLD."impact_receipt_id"
    )
)
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_evidence_immutable');
END''',
    )

    item_insert_name = (
        f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{item_table}_insert"
    )
    manifest[item_insert_name] = (
        item_table,
        f'''CREATE TRIGGER "{item_insert_name}"
BEFORE INSERT ON "{item_table}"
WHEN NOT EXISTS (
    SELECT 1
    FROM "{receipt_table}" AS receipt
    WHERE receipt."impact_receipt_id" = NEW."impact_receipt_id"
      AND receipt."board_id" = NEW."board_id"
      AND receipt."guideline_id" = NEW."guideline_id"
      AND receipt."sealed" = 0
)
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_evidence_sealed');
END''',
    )

    adoption_insert_name = (
        f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{adoption_table}_insert"
    )
    manifest[adoption_insert_name] = (
        adoption_table,
        f'''CREATE TRIGGER "{adoption_insert_name}"
BEFORE INSERT ON "{adoption_table}"
WHEN NOT EXISTS (
    SELECT 1
    FROM "{receipt_table}" AS receipt
    JOIN "guideline_board_bindings" AS binding
      ON binding."binding_id" = NEW."binding_id"
     AND binding."binding_revision" = NEW."binding_revision"
     AND binding."impact_receipt_id" = NEW."impact_receipt_id"
     AND binding."impact_adoption_id" = NEW."adoption_id"
     AND binding."board_id" = NEW."board_id"
     AND binding."guideline_id" = NEW."guideline_id"
     AND binding."state" = 'active'
     AND binding."revision_id" = receipt."to_revision_id"
     AND binding."semantic_version" = receipt."to_semantic_version"
     AND binding."priority" = receipt."proposed_priority"
     AND binding."enforcement" =
         receipt."proposed_enforcement"
     AND binding."adopted_by" = NEW."adopted_by"
     AND binding."adopted_at" = NEW."adopted_at"
    JOIN "semantic_guideline_binding_configurations" AS configuration
      ON configuration."binding_id" = binding."binding_id"
     AND configuration."binding_revision" =
         binding."binding_revision"
     AND configuration."board_id" = binding."board_id"
     AND configuration."guideline_id" = binding."guideline_id"
     AND configuration."revision_id" = binding."revision_id"
     AND configuration."revision_digest" =
         receipt."to_revision_digest"
     AND configuration."enforcement" =
         receipt."proposed_enforcement"
     AND configuration."minimum_confidence" =
         receipt."proposed_minimum_confidence"
     AND json(configuration."metric_threshold_overrides") =
         json(receipt."proposed_metric_threshold_overrides")
    JOIN "domain_events" AS event
      ON event."id" = NEW."event_id"
     AND event."event_type" = 'board.semantic_guideline_adoption_changed.v2'
     AND event."board_id" = NEW."board_id"
     AND event."actor_id" = NEW."adopted_by"
     AND event."occurred_at" = NEW."adopted_at"
    JOIN "activity_logs" AS activity
      ON activity."id" = NEW."activity_id"
     AND activity."board_id" = NEW."board_id"
     AND activity."card_id" IS NULL
     AND activity."action" = 'guideline_revision_adopted'
     AND activity."actor_id" = NEW."adopted_by"
     AND activity."actor_type" = event."actor_type"
     AND activity."created_at" = NEW."adopted_at"
     AND json(activity."details") = json(event."payload_json")
    WHERE receipt."impact_receipt_id" = NEW."impact_receipt_id"
      AND receipt."board_id" = NEW."board_id"
      AND receipt."guideline_id" = NEW."guideline_id"
      AND receipt."binding_id" = NEW."binding_id"
      AND receipt."impact_digest" = NEW."impact_digest"
      AND receipt."binding_digest" = NEW."binding_digest"
      AND receipt."expected_binding_revision"
          IS NEW."expected_binding_revision"
      AND receipt."sealed" = 1
      AND json_extract(event."payload_json", '$.operation') = 'adopt'
      AND json_extract(event."payload_json", '$.board_id') = NEW."board_id"
      AND json_extract(event."payload_json", '$.guideline_id') =
          NEW."guideline_id"
      AND json_extract(event."payload_json", '$.binding_id') =
          NEW."binding_id"
      AND json_extract(event."payload_json", '$.binding_revision') =
          NEW."binding_revision"
      AND json_extract(event."payload_json", '$.impact_receipt_id') =
          NEW."impact_receipt_id"
      AND json_extract(event."payload_json", '$.impact_digest') =
          NEW."impact_digest"
      AND json_extract(
          event."payload_json", '$.policy_set_digest_before'
      ) = receipt."policy_set_digest_before"
      AND json_extract(
          event."payload_json", '$.policy_set_digest_after'
      ) = receipt."policy_set_digest_after"{adoption_full_evidence}
)
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_adoption_evidence_invalid');
END''',
    )

    if include_unlink:
        unlink_insert_name = (
            f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{unlink_table}_insert"
        )
        manifest[unlink_insert_name] = (
            unlink_table,
            f'''CREATE TRIGGER "{unlink_insert_name}"
BEFORE INSERT ON "{unlink_table}"
WHEN NOT EXISTS (
    SELECT 1
    FROM "guideline_board_bindings" AS binding
    JOIN "guideline_board_bindings" AS previous
      ON previous."binding_id" = NEW."binding_id"
     AND previous."binding_revision" = NEW."previous_binding_revision"
     AND previous."board_id" = NEW."board_id"
     AND previous."guideline_id" = NEW."guideline_id"
     AND previous."state" = 'active'
    JOIN "semantic_guideline_binding_configurations" AS configuration
      ON configuration."binding_id" = binding."binding_id"
     AND configuration."binding_revision" =
         binding."binding_revision"
    JOIN "semantic_guideline_binding_configurations" AS previous_configuration
      ON previous_configuration."binding_id" =
         previous."binding_id"
     AND previous_configuration."binding_revision" =
         previous."binding_revision"
     AND previous_configuration."revision_digest" =
         configuration."revision_digest"
     AND previous_configuration."enforcement" =
         configuration."enforcement"
     AND previous_configuration."minimum_confidence" =
         configuration."minimum_confidence"
     AND json(previous_configuration."metric_threshold_overrides") =
         json(configuration."metric_threshold_overrides")
     AND previous_configuration."configuration_digest" =
         configuration."configuration_digest"
    JOIN "domain_events" AS event
      ON event."id" = NEW."event_id"
     AND event."event_type" = 'board.semantic_guideline_adoption_changed.v2'
     AND event."board_id" = NEW."board_id"
     AND event."actor_id" = NEW."unlinked_by"
     AND event."actor_type" = NEW."actor_type"
     AND event."occurred_at" = NEW."unlinked_at"
    JOIN "activity_logs" AS activity
      ON activity."id" = NEW."activity_id"
     AND activity."board_id" = NEW."board_id"
     AND activity."card_id" IS NULL
     AND activity."action" = 'guideline_unlinked'
     AND activity."actor_id" = NEW."unlinked_by"
     AND activity."actor_type" = NEW."actor_type"
     AND activity."created_at" = NEW."unlinked_at"
     AND json(activity."details") = json(event."payload_json")
    WHERE binding."binding_id" = NEW."binding_id"
      AND binding."binding_revision" = NEW."binding_revision"
      AND binding."board_id" = NEW."board_id"
      AND binding."guideline_id" = NEW."guideline_id"
      AND binding."state" = 'unlinked'
      AND binding."impact_receipt_id" IS NULL
      AND binding."impact_adoption_id" IS NULL
      AND binding."impact_unlink_id" = NEW."unlink_id"
      AND binding."binding_revision" =
          previous."binding_revision" + 1
      AND binding."revision_id" = previous."revision_id"
      AND binding."semantic_version" = previous."semantic_version"
      AND binding."revision_digest" = previous."revision_digest"
      AND binding."priority" = previous."priority"
      AND binding."enforcement" =
          previous."enforcement"
      AND binding."source_kind" = previous."source_kind"
      AND binding."binding_origin" = previous."binding_origin"
      AND binding."legacy_source_id" IS previous."legacy_source_id"
      AND binding."legacy_guideline_version"
          IS previous."legacy_guideline_version"
      AND binding."legacy_template_id" IS previous."legacy_template_id"
      AND binding."legacy_template_version"
          IS previous."legacy_template_version"
      AND binding."legacy_version_unresolvable" =
          previous."legacy_version_unresolvable"
      AND binding."adopted_by" = NEW."unlinked_by"
      AND binding."adopted_at" = NEW."unlinked_at"
      AND binding."idempotency_key" = NEW."idempotency_key"
      AND binding."request_digest" = NEW."request_digest"
      AND json_extract(event."payload_json", '$.operation') = 'unlink'
      AND json_extract(event."payload_json", '$.board_id') =
          NEW."board_id"
      AND json_extract(event."payload_json", '$.guideline_id') =
          NEW."guideline_id"
      AND json_extract(event."payload_json", '$.binding_id') =
          NEW."binding_id"
      AND json_extract(
          event."payload_json", '$.previous_binding_revision'
      ) = NEW."previous_binding_revision"
      AND json_extract(event."payload_json", '$.binding_revision') =
          NEW."binding_revision"
      AND json_extract(event."payload_json", '$.from_revision_id') =
          previous."revision_id"
      AND json_extract(
          event."payload_json", '$.from_semantic_version'
      ) = previous."semantic_version"
      AND json_extract(event."payload_json", '$.from_revision_digest') =
          previous_configuration."revision_digest"
      AND json_type(event."payload_json", '$.to_revision_id') = 'null'
      AND json_type(
          event."payload_json", '$.to_semantic_version'
      ) = 'null'
      AND json_type(
          event."payload_json", '$.to_revision_digest'
      ) = 'null'
      AND json_type(
          event."payload_json", '$.impact_receipt_id'
      ) = 'null'
      AND json_type(event."payload_json", '$.impact_digest') = 'null'
      AND json_extract(
          event."payload_json", '$.binding_digest_before'
      ) = NEW."binding_digest_before"
      AND json_extract(
          event."payload_json", '$.binding_head_digest_before'
      ) = NEW."binding_head_digest_before"
      AND json_extract(
          event."payload_json", '$.binding_head_digest_after'
      ) = NEW."binding_head_digest_after"
      AND json_extract(
          event."payload_json", '$.policy_set_digest_before'
      ) = NEW."policy_set_digest_before"
      AND json_extract(
          event."payload_json", '$.policy_set_digest_after'
      ) = NEW."policy_set_digest_after"
      AND json_extract(
          event."payload_json", '$.policy_set_digest'
      ) = NEW."policy_set_digest_after"
      AND json(json_extract(
          event."payload_json", '$.added_metric_ids'
      )) =
          json('[]')
      AND json(json_extract(
          event."payload_json", '$.changed_metric_ids'
      )) =
          json('[]')
      AND json(json_extract(
          event."payload_json", '$.removed_metric_ids'
      )) =
          json(NEW."removed_metric_ids")
      AND json_extract(event."payload_json", '$.actor_id') =
          NEW."unlinked_by"
      AND json_extract(event."payload_json", '$.actor_type') =
          NEW."actor_type"{unlink_full_evidence}
)
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_unlink_evidence_invalid');
END''',
        )

    if include_retirement:
        retirement_insert_name = (
            f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_"
            f"{retirement_impact_table}_insert"
        )
        manifest[retirement_insert_name] = (
            retirement_impact_table,
            f'''CREATE TRIGGER "{retirement_insert_name}"
BEFORE INSERT ON "{retirement_impact_table}"
WHEN NOT EXISTS (
    SELECT 1
    FROM "guideline_retirements" AS retirement
    JOIN "guideline_board_bindings" AS binding
      ON binding."binding_id" = NEW."binding_id"
     AND binding."binding_revision" = NEW."binding_revision"
     AND binding."board_id" = NEW."board_id"
     AND binding."guideline_id" = NEW."guideline_id"
     AND binding."state" = 'active'
     AND binding."revision_id" = NEW."revision_id"
     AND binding."semantic_version" = NEW."semantic_version"
    JOIN "semantic_guideline_binding_configurations" AS configuration
      ON configuration."binding_id" = binding."binding_id"
     AND configuration."binding_revision" =
         binding."binding_revision"
     AND configuration."revision_id" = NEW."revision_id"
     AND configuration."revision_digest" = NEW."revision_digest"
    JOIN "domain_events" AS event
      ON event."id" = NEW."event_id"
     AND event."event_type" = 'board.semantic_guideline_retirement_changed.v2'
     AND event."board_id" = NEW."board_id"
     AND event."actor_id" = NEW."retired_by"
     AND event."actor_type" = NEW."actor_type"
     AND event."occurred_at" = NEW."retired_at"
    JOIN "activity_logs" AS activity
      ON activity."id" = NEW."activity_id"
     AND activity."board_id" = NEW."board_id"
     AND activity."card_id" IS NULL
     AND activity."action" = 'guideline_retired'
     AND activity."actor_id" = NEW."retired_by"
     AND activity."actor_type" = NEW."actor_type"
     AND activity."created_at" = NEW."retired_at"
     AND json(activity."details") = json(event."payload_json")
    WHERE retirement."retirement_id" = NEW."retirement_id"
      AND retirement."guideline_id" = NEW."guideline_id"
      AND retirement."status" = NEW."retirement_status"
      AND retirement."superseded_by_guideline_id"
          IS NEW."superseded_by_guideline_id"{retirement_head_match}
      AND retirement."retired_by" = NEW."retired_by"
      AND retirement."retired_at" = NEW."retired_at"
      AND NOT EXISTS (
          SELECT 1
          FROM "guideline_board_bindings" AS later
          WHERE later."board_id" = binding."board_id"
            AND later."guideline_id" = binding."guideline_id"
            AND later."binding_revision" > binding."binding_revision"
      )
      AND json_extract(
          event."payload_json", '$.event_schema_version'
      ) = 'guideline-impact/v2'
      AND json_extract(event."payload_json", '$.event_id') =
          NEW."event_id"
      AND json_extract(event."payload_json", '$.operation') = 'retire'
      AND json_extract(event."payload_json", '$.board_id') =
          NEW."board_id"
      AND json_extract(event."payload_json", '$.guideline_id') =
          NEW."guideline_id"
      AND json_extract(event."payload_json", '$.retirement_id') =
          NEW."retirement_id"
      AND json_extract(
          event."payload_json", '$.retirement_status'
      ) = NEW."retirement_status"
      AND (
          (
              NEW."superseded_by_guideline_id" IS NULL
              AND json_type(
                  event."payload_json", '$.superseded_by_guideline_id'
              ) = 'null'
          )
          OR json_extract(
              event."payload_json", '$.superseded_by_guideline_id'
          ) = NEW."superseded_by_guideline_id"
      )
      AND json_extract(event."payload_json", '$.binding_id') =
          NEW."binding_id"
      AND json_extract(event."payload_json", '$.binding_revision') =
          NEW."binding_revision"
      AND json_extract(event."payload_json", '$.revision_id') =
          NEW."revision_id"
      AND json_extract(event."payload_json", '$.revision_number') =
          NEW."revision_number"
      AND json_extract(event."payload_json", '$.semantic_version') =
          NEW."semantic_version"
      AND json_extract(event."payload_json", '$.revision_digest') =
          NEW."revision_digest"
      AND json_extract(
          event."payload_json", '$.binding_digest_before'
      ) = NEW."binding_digest_before"
      AND json_extract(
          event."payload_json", '$.binding_head_digest_before'
      ) = NEW."binding_head_digest_before"
      AND json_extract(
          event."payload_json", '$.binding_head_digest_after'
      ) = NEW."binding_head_digest_after"
      AND json_extract(
          event."payload_json", '$.policy_set_digest_before'
      ) = NEW."policy_set_digest_before"
      AND json_extract(
          event."payload_json", '$.policy_set_digest_after'
      ) = NEW."policy_set_digest_after"
      AND json_extract(
          event."payload_json", '$.policy_set_digest'
      ) = NEW."policy_set_digest_after"
      AND json(json_extract(
          event."payload_json", '$.removed_metric_ids'
      )) = json(NEW."removed_metric_ids")
      AND json_extract(event."payload_json", '$.actor_id') =
          NEW."retired_by"
      AND json_extract(event."payload_json", '$.actor_type') =
          NEW."actor_type"
      AND julianday(
          json_extract(event."payload_json", '$.occurred_at')
      ) = julianday(NEW."retired_at")
      AND json_extract(event."payload_json", '$.request_digest') =
          NEW."request_digest"{retirement_full_evidence}
)
BEGIN
    SELECT RAISE(
        ABORT,
        'guideline_retirement_impact_evidence_invalid'
    );
END''',
        )

    default_materialization_proof = (
        """
          AND EXISTS (
              SELECT 1
              FROM "boards" AS board
              JOIN "default_board_configurations" AS template
                ON template."id" = json_extract(
                    board."default_config_snapshot", '$.template_id'
                )
               AND template."version" = json_extract(
                    board."default_config_snapshot", '$.template_version'
                )
              JOIN json_each(template."guideline_default_refs") AS ref
              WHERE board."id" = NEW."board_id"
                AND NEW."legacy_template_id" = template."id"
                AND NEW."legacy_template_version" =
                    template."version"
                AND json_extract(ref."value", '$.guideline_id') =
                    NEW."guideline_id"
                AND json_extract(ref."value", '$.revision_id') =
                    NEW."revision_id"
                AND json_extract(ref."value", '$.semantic_version') =
                    NEW."semantic_version"
                AND json_extract(ref."value", '$.revision_digest') =
                    NEW."revision_digest"
                AND json_extract(ref."value", '$.priority') =
                    NEW."priority"
                AND json_extract(ref."value", '$.revision_number') =
                    NEW."legacy_guideline_version"
          )"""
        if verify_default_materialization
        else ""
    )
    binding_scope = (
        """(
      NEW."state" = 'active'
      AND EXISTS (
          SELECT 1
          FROM "guidelines" AS guideline
          WHERE guideline."id" = NEW."guideline_id"
            AND guideline."scope" = 'global'
            AND guideline."board_id" IS NULL
      )
  )
  OR NEW."state" = 'unlinked'"""
        if include_unlink
        else """NEW."state" = 'active'
  AND EXISTS (
      SELECT 1
      FROM "guidelines" AS guideline
      WHERE guideline."id" = NEW."guideline_id"
        AND guideline."scope" = 'global'
        AND guideline."board_id" IS NULL
  )"""
    )
    unlink_binding_branch = (
        """
      OR (
          NEW."state" = 'unlinked'
          AND NEW."impact_receipt_id" IS NULL
          AND NEW."impact_adoption_id" IS NULL
          AND NEW."impact_unlink_id" IS NOT NULL
          AND NEW."binding_revision" > 1
          AND EXISTS (
              SELECT 1
              FROM "guideline_board_bindings" AS previous
              WHERE previous."binding_id" = NEW."binding_id"
                AND previous."binding_revision" =
                    NEW."binding_revision" - 1
                AND previous."board_id" = NEW."board_id"
                AND previous."guideline_id" = NEW."guideline_id"
                AND previous."state" = 'active'
                AND previous."revision_id" = NEW."revision_id"
                AND previous."semantic_version" =
                    NEW."semantic_version"
                AND previous."revision_digest" =
                    NEW."revision_digest"
                AND previous."priority" = NEW."priority"
                AND previous."enforcement" =
                    NEW."enforcement"
                AND previous."source_kind" = NEW."source_kind"
                AND previous."binding_origin" = NEW."binding_origin"
                AND previous."legacy_source_id" IS
                    NEW."legacy_source_id"
                AND previous."legacy_guideline_version" IS
                    NEW."legacy_guideline_version"
                AND previous."legacy_template_id" IS
                    NEW."legacy_template_id"
                AND previous."legacy_template_version" IS
                    NEW."legacy_template_version"
                AND previous."legacy_version_unresolvable" =
                    NEW."legacy_version_unresolvable"
          )
      )"""
        if include_unlink
        else ""
    )

    binding_insert_name = (
        f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_binding_insert"
    )
    manifest[binding_insert_name] = (
        "guideline_board_bindings",
        f'''CREATE TRIGGER "{binding_insert_name}"
BEFORE INSERT ON "guideline_board_bindings"
WHEN ({binding_scope})
  AND NOT (
      (
          NEW."state" = 'active'
          AND NEW."binding_origin" = 'default_materialization'
          AND NEW."binding_revision" = 1
          AND NEW."impact_receipt_id" IS NULL
          AND NEW."impact_adoption_id" IS NULL
          {unlink_absent}
          AND NEW."enforcement" = 'advisory'
          AND NEW."legacy_version_unresolvable" = 0
          AND NOT EXISTS (
              SELECT 1
              FROM "guideline_board_bindings" AS previous
              WHERE previous."board_id" = NEW."board_id"
                AND previous."guideline_id" = NEW."guideline_id"
          )
          {default_materialization_proof}
      )
      OR (
          NEW."state" = 'active'
          AND NEW."impact_adoption_id" IS NOT NULL
          {unlink_absent}
          AND EXISTS (
              SELECT 1
              FROM "{receipt_table}" AS receipt
              WHERE receipt."impact_receipt_id" = NEW."impact_receipt_id"
                AND receipt."board_id" = NEW."board_id"
                AND receipt."guideline_id" = NEW."guideline_id"
                AND receipt."binding_id" = NEW."binding_id"
                AND NEW."impact_adoption_id" IS NOT NULL
                AND receipt."to_revision_id" = NEW."revision_id"
                AND receipt."to_semantic_version" =
                    NEW."semantic_version"
                AND receipt."proposed_priority" = NEW."priority"
                AND receipt."proposed_enforcement" =
                    NEW."enforcement"
                AND receipt."sealed" = 1
                AND (
                    (
                        NEW."binding_revision" = 1
                        AND receipt."expected_binding_revision" IS NULL
                        AND receipt."expected_binding_state" IS NULL
                    )
                    OR (
                        NEW."binding_revision" > 1
                        AND receipt."expected_binding_revision" =
                            NEW."binding_revision" - 1
                        AND EXISTS (
                            SELECT 1
                            FROM "guideline_board_bindings" AS previous
                            WHERE previous."binding_id" = NEW."binding_id"
                              AND previous."binding_revision" =
                                  NEW."binding_revision" - 1
                              AND previous."board_id" = NEW."board_id"
                              AND previous."guideline_id" =
                                  NEW."guideline_id"
                              AND previous."revision_id" =
                                  receipt."from_revision_id"
                              AND previous."semantic_version" =
                                  receipt."from_semantic_version"
                              AND EXISTS (
                                  SELECT 1
                                  FROM "semantic_guideline_binding_configurations"
                                       AS previous_configuration
                                  WHERE previous_configuration."binding_id" =
                                            previous."binding_id"
                                    AND previous_configuration."binding_revision" =
                                            previous."binding_revision"
                                    AND previous_configuration."revision_digest" =
                                            receipt."from_revision_digest"
                              )
                              AND previous."state" =
                                  receipt."expected_binding_state"
                       )
                   )
           )
       )
      )
      {unlink_binding_branch}
  )
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_preview_required');
END''',
    )

    immutable_tables = [receipt_table, item_table, adoption_table]
    if include_unlink:
        immutable_tables.append(unlink_table)
    if include_retirement:
        immutable_tables.append(retirement_impact_table)
    for table_name in immutable_tables:
        for operation in ("update", "delete"):
            if table_name == receipt_table and operation == "update":
                continue
            trigger_name = (
                f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            when = ""
            if allow_board_erasure and operation == "delete":
                if table_name in {
                    receipt_table,
                    adoption_table,
                    unlink_table,
                    retirement_impact_table,
                }:
                    allowed = (
                        "SELECT 1 "
                        f'FROM "{permit_table}" AS permit '
                        "WHERE permit.board_id = OLD.board_id"
                    )
                else:
                    allowed = (
                        "SELECT 1 "
                        f'FROM "{permit_table}" AS permit '
                        f'JOIN "{receipt_table}" AS receipt '
                        "ON receipt.board_id = permit.board_id "
                        "WHERE receipt.impact_receipt_id = "
                        "OLD.impact_receipt_id"
                    )
                when = f"\nWHEN NOT EXISTS (\n    {allowed}\n)"
            manifest[trigger_name] = (
                table_name,
                f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_evidence_immutable');
END''',
            )
    for table_name, id_column in (
        ("domain_events", "event_id"),
        ("activity_logs", "activity_id"),
    ):
        for operation in ("update", "delete"):
            trigger_name = (
                f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            referenced = (
                "EXISTS (\n"
                f'    SELECT 1 FROM "{adoption_table}" AS adoption\n'
                f'    WHERE adoption."{id_column}" = OLD."id"\n'
                + (
                    "    UNION ALL\n"
                    f'    SELECT 1 FROM "{unlink_table}" AS unlink\n'
                    f'    WHERE unlink."{id_column}" = OLD."id"\n'
                    if include_unlink
                    else ""
                )
                + (
                    "    UNION ALL\n"
                    f'    SELECT 1 FROM "{retirement_impact_table}" '
                    "AS retirement\n"
                    f'    WHERE retirement."{id_column}" = OLD."id"\n'
                    if include_retirement
                    else ""
                )
                + ")"
            )
            if table_name == "domain_events" and protect_materialized_events:
                referenced = (
                    f"({referenced} OR OLD.\"event_type\" = "
                    f"'{POLICY_BINDING_MATERIALIZED_EVENT_TYPE}' OR "
                    f"OLD.\"event_type\" = "
                    f"'{SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE}')"
                )
            if allow_board_erasure and operation == "delete":
                referenced = (
                    f"({referenced}) AND NOT EXISTS (\n"
                    f'    SELECT 1 FROM "{permit_table}" AS permit\n'
                    '    WHERE permit."board_id" = OLD."board_id"\n'
                    ")"
                )
            when = f"\nWHEN {referenced}"
            manifest[trigger_name] = (
                table_name,
                f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_impact_audit_evidence_immutable');
END''',
            )
    execution_trigger_name = (
        f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_"
        f"{execution_table}_policy_constraint_insert"
    )
    manifest[execution_trigger_name] = (
        execution_table,
        f'''CREATE TRIGGER "{execution_trigger_name}"
BEFORE INSERT ON "{execution_table}"
WHEN NEW."handler_name" = 'PolicyConstraintProjectionHandler'
  AND NOT EXISTS (
      SELECT 1
      FROM "domain_events" AS event
      WHERE event."id" = NEW."event_id"
        AND json_type(event."payload_json") = 'object'
        AND (
            (
                event."event_type" = '{PolicyAdoptionChanged.event_type}'
                AND json_extract(
                    event."payload_json", '$.event_schema_version'
                ) = 'guideline-impact/v2'
                AND json_extract(event."payload_json", '$.operation')
                    IN ('adopt', 'unlink')
                AND json_type(
                    event."payload_json", '$.guideline_id'
                ) = 'text'
            )
            OR (
                event."event_type" = '{PolicyRetirementChanged.event_type}'
                AND json_extract(
                    event."payload_json", '$.event_schema_version'
                ) = 'guideline-impact/v2'
                AND json_extract(event."payload_json", '$.operation')
                    = 'retire'
                AND json_type(
                    event."payload_json", '$.revision_id'
                ) = 'text'
            )
            OR (
                event."event_type" =
                    '{POLICY_BINDING_MATERIALIZED_EVENT_TYPE}'
                AND (
                    SELECT COUNT(*)
                    FROM json_each(event."payload_json")
                ) = 13
                AND json_extract(
                    event."payload_json", '$.event_schema_version'
                ) = 'policy-binding-materialized/v2'
                AND json_extract(event."payload_json", '$.operation')
                    = 'adopt'
                AND json_type(
                    event."payload_json", '$.revision_id'
                ) = 'text'
                AND json_extract(event."payload_json", '$.source_kind')
                    IN ('native', 'default_materialization')
                AND json_extract(event."payload_json", '$.enforcement')
                    IN ('advisory', 'blocking')
                AND json_type(
                    event."payload_json", '$.minimum_confidence'
                ) = 'integer'
                AND json_extract(
                    event."payload_json", '$.minimum_confidence'
                ) BETWEEN 0 AND 100
                AND json_type(
                    event."payload_json",
                    '$.metric_threshold_overrides'
                ) = 'object'
            )
            OR (
                event."event_type" =
                    '{SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE}'
                AND (
                    SELECT COUNT(*)
                    FROM json_each(event."payload_json")
                ) = 6
                AND json_extract(
                    event."payload_json", '$.event_schema_version'
                ) = '{SEMANTIC_GUIDELINE_PROJECTION_SCHEMA_VERSION}'
                AND json_type(
                    event."payload_json", '$.causation_id'
                ) = 'text'
                AND json_extract(
                    event."payload_json", '$.entity_kind'
                ) IN (
                    'revision', 'metric_definition',
                    'binding_configuration', 'assessment_receipt',
                    'metric_result', 'waiver', 'skip'
                )
                AND json_type(
                    event."payload_json", '$.entity_id'
                ) = 'text'
                AND json_type(
                    event."payload_json", '$.entity_digest'
                ) = 'text'
                AND length(json_extract(
                    event."payload_json", '$.entity_digest'
                )) = 64
                AND json_extract(
                    event."payload_json", '$.operation'
                ) IN ('upsert', 'terminate')
            )
        )
  )
BEGIN
    SELECT RAISE(
        ABORT,
        'policy_constraint_execution_event_invalid'
    );
END''',
    )
    return manifest


def policy_compliance_immutability_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
    allow_aggregate_sealing: bool = True,
) -> dict[str, tuple[str, str]]:
    """Exact SQLite guards for immutable B07 receipts and rule evidence."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        PolicyComplianceAdoptedRevisionRow,
        PolicyComplianceFindingRow,
        PolicyComplianceReceiptRow,
    )

    permit_table = BoardErasurePermit.__tablename__
    receipt_table = PolicyComplianceReceiptRow.__tablename__
    adopted_table = PolicyComplianceAdoptedRevisionRow.__tablename__
    finding_table = PolicyComplianceFindingRow.__tablename__
    manifest: dict[str, tuple[str, str]] = {}

    if allow_aggregate_sealing:
        immutable_receipt_columns = tuple(
            column.name
            for column in PolicyComplianceReceiptRow.__table__.columns
            if column.name != "sealed"
        )
        unchanged_receipt = "\n    AND ".join(
            f'NEW."{column_name}" IS OLD."{column_name}"'
            for column_name in immutable_receipt_columns
        )
        receipt_update_name = (
            f"{POLICY_COMPLIANCE_IMMUTABILITY_TRIGGER_PREFIX}_{receipt_table}_update"
        )
        manifest[receipt_update_name] = (
            receipt_table,
            f'''CREATE TRIGGER "{receipt_update_name}"
BEFORE UPDATE ON "{receipt_table}"
WHEN NOT (
    OLD."sealed" = 0
    AND NEW."sealed" = 1
    AND {unchanged_receipt}
)
BEGIN
    SELECT RAISE(ABORT, 'policy_compliance_evidence_immutable');
END''',
        )

    for table_name in (receipt_table, adopted_table, finding_table):
        for operation in ("update", "delete"):
            if (
                allow_aggregate_sealing
                and table_name == receipt_table
                and operation == "update"
            ):
                continue
            trigger_name = (
                f"{POLICY_COMPLIANCE_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            when = ""
            if allow_board_erasure and operation == "delete":
                if table_name in {receipt_table, finding_table}:
                    allowed = (
                        "SELECT 1 "
                        f'FROM "{permit_table}" AS permit '
                        "WHERE permit.board_id = OLD.board_id"
                    )
                else:
                    allowed = (
                        "SELECT 1 "
                        f'FROM "{permit_table}" AS permit '
                        f'JOIN "{receipt_table}" AS receipt '
                        "ON receipt.board_id = permit.board_id "
                        "WHERE receipt.receipt_id = OLD.receipt_id"
                    )
                when = f"\nWHEN NOT EXISTS (\n    {allowed}\n)"
            manifest[trigger_name] = (
                table_name,
                f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{when}
BEGIN
    SELECT RAISE(ABORT, 'policy_compliance_evidence_immutable');
END''',
            )
    if allow_aggregate_sealing:
        for table_name in (adopted_table, finding_table):
            trigger_name = (
                f"{POLICY_COMPLIANCE_IMMUTABILITY_TRIGGER_PREFIX}_{table_name}_insert"
            )
            manifest[trigger_name] = (
                table_name,
                f'''CREATE TRIGGER "{trigger_name}"
BEFORE INSERT ON "{table_name}"
WHEN EXISTS (
    SELECT 1
    FROM "{receipt_table}" AS receipt
    WHERE receipt.receipt_id = NEW.receipt_id
      AND receipt.sealed = 1
)
BEGIN
    SELECT RAISE(ABORT, 'policy_compliance_evidence_sealed');
END''',
            )
    return manifest


def policy_waiver_immutability_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Exact SQLite CAS and append-only guards for ``waiver-event/v1``."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        PolicyWaiverEventRow,
        PolicyWaiverRow,
    )

    head_table = PolicyWaiverRow.__tablename__
    event_table = PolicyWaiverEventRow.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    manifest: dict[str, tuple[str, str]] = {}
    immutable_head_columns = (
        "waiver_id",
        "board_id",
        "finding_id",
        "receipt_id",
        "guideline_id",
        "revision_id",
        "rule_id",
        "entity_type",
        "subject_id",
        "subject_version",
        "scope_digest",
        "justification",
        "evidence_refs",
        "requested_by",
        "requested_at",
        "original_expires_at",
        "idempotency_key",
        "request_digest",
    )
    unchanged = "\n    AND ".join(
        f'NEW."{column}" IS OLD."{column}"' for column in immutable_head_columns
    )
    head_insert_name = f"{POLICY_WAIVER_TRIGGER_PREFIX}_head_insert"
    manifest[head_insert_name] = (
        head_table,
        f'''CREATE TRIGGER "{head_insert_name}"
BEFORE INSERT ON "{head_table}"
WHEN NEW."status" <> 'requested'
  OR NEW."waiver_revision" <> 1
  OR NEW."last_event_type" <> 'request'
  OR NEW."requested_at" >= NEW."expires_at"
  OR json_array_length(NEW."evidence_refs") = 0
  OR NOT EXISTS (
      SELECT 1
      FROM "policy_compliance_findings" AS finding
      JOIN "policy_compliance_receipts" AS receipt
        ON receipt.receipt_id = finding.receipt_id
       AND receipt.board_id = finding.board_id
       AND receipt.sealed = 1
      WHERE finding.finding_id = NEW.finding_id
        AND finding.receipt_id = NEW.receipt_id
        AND finding.board_id = NEW.board_id
        AND finding.guideline_id = NEW.guideline_id
        AND finding.revision_id = NEW.revision_id
        AND finding.rule_id = NEW.rule_id
        AND finding.entity_type = NEW.entity_type
        AND finding.subject_id = NEW.subject_id
        AND finding.subject_version = NEW.subject_version
        AND finding.outcome = 'fail'
        AND finding.waiver_id IS NULL
        AND finding.created_at <= NEW.requested_at
  )
  OR EXISTS (
      SELECT 1
      FROM "{head_table}" AS active
      WHERE active.board_id = NEW.board_id
        AND active.guideline_id = NEW.guideline_id
        AND active.revision_id = NEW.revision_id
        AND active.rule_id = NEW.rule_id
        AND active.entity_type = NEW.entity_type
        AND active.subject_id = NEW.subject_id
        AND active.subject_version = NEW.subject_version
        AND active.status IN ('requested', 'approved')
        AND active.expires_at > NEW.requested_at
  )
BEGIN
    SELECT RAISE(ABORT, 'policy_waiver_request_invalid');
END''',
    )
    head_update_name = f"{POLICY_WAIVER_TRIGGER_PREFIX}_head_update"
    manifest[head_update_name] = (
        head_table,
        f'''CREATE TRIGGER "{head_update_name}"
BEFORE UPDATE ON "{head_table}"
WHEN NOT (
    {unchanged}
    AND NEW."waiver_revision" = OLD."waiver_revision" + 1
    AND NEW."last_event_id" <> OLD."last_event_id"
    AND NEW."last_event_at" >= OLD."last_event_at"
)
BEGIN
    SELECT RAISE(ABORT, 'policy_waiver_head_cas_invalid');
END''',
    )
    head_delete_name = f"{POLICY_WAIVER_TRIGGER_PREFIX}_head_delete"
    head_delete_when = ""
    if allow_board_erasure:
        head_delete_when = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            "    WHERE permit.board_id = OLD.board_id\n"
            ")"
        )
    manifest[head_delete_name] = (
        head_table,
        f'''CREATE TRIGGER "{head_delete_name}"
BEFORE DELETE ON "{head_table}"{head_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'policy_waiver_head_immutable');
END''',
    )
    event_insert_name = f"{POLICY_WAIVER_TRIGGER_PREFIX}_event_insert"
    manifest[event_insert_name] = (
        event_table,
        f'''CREATE TRIGGER "{event_insert_name}"
BEFORE INSERT ON "{event_table}"
WHEN length(trim(NEW."actor_id")) = 0
  OR length(trim(NEW."reason")) = 0
  OR json_array_length(NEW."evidence_refs") = 0
  OR NOT EXISTS (
      SELECT 1
      FROM "{head_table}" AS head
      WHERE head.waiver_id = NEW.waiver_id
        AND head.board_id = NEW.board_id
        AND head.waiver_revision = NEW.waiver_revision
        AND head.last_event_id = NEW.event_id
        AND head.last_event_type = NEW.event_type
        AND head.last_event_at = NEW.occurred_at
        AND head.status = NEW.to_status
        AND head.expires_at = NEW.expires_at
        AND head.expire_reason_code IS NEW.expire_reason_code
        AND head.scope_digest = NEW.scope_digest
        AND head.head_digest = NEW.waiver_digest
        AND head.reviewed_by IS NEW.reviewed_by
        AND head.reviewed_at IS NEW.reviewed_at
        AND head.review_reason IS NEW.review_reason
        AND head.revoked_by IS NEW.revoked_by
        AND head.revoked_at IS NEW.revoked_at
        AND (
            NEW.event_type <> 'request'
            OR (
                NEW.actor_id = head.requested_by
                AND NEW.occurred_at = head.requested_at
                AND NEW.reason = head.justification
                AND json(NEW.evidence_refs) = json(head.evidence_refs)
                AND NEW.expires_at = head.original_expires_at
                AND NEW.idempotency_key = head.idempotency_key
                AND NEW.request_digest = head.request_digest
            )
        )
        AND (
            NEW.event_type NOT IN ('approve', 'reject', 'revalidate')
            OR (
                NEW.actor_id = head.reviewed_by
                AND NEW.occurred_at = head.reviewed_at
                AND NEW.reason = head.review_reason
            )
        )
        AND (
            NEW.event_type <> 'revoke'
            OR (
                NEW.actor_id = head.revoked_by
                AND NEW.occurred_at = head.revoked_at
            )
        )
  )
  OR (
      NEW."event_type" = 'request'
      AND (
          NEW."predecessor_event_id" IS NOT NULL
          OR EXISTS (
              SELECT 1 FROM "{event_table}" AS prior
              WHERE prior.waiver_id = NEW.waiver_id
          )
      )
  )
  OR (
      NEW."event_type" <> 'request'
      AND NOT EXISTS (
          SELECT 1
          FROM "{event_table}" AS predecessor
          WHERE predecessor.event_id = NEW.predecessor_event_id
            AND predecessor.waiver_id = NEW.waiver_id
            AND predecessor.board_id = NEW.board_id
            AND predecessor.waiver_revision = NEW.waiver_revision - 1
            AND predecessor.to_status = NEW.from_status
            AND (
                (
                    NEW.event_type = 'revalidate'
                    AND NEW.expires_at > predecessor.expires_at
                    AND (
                        (
                            predecessor.to_status = 'approved'
                            AND NEW.occurred_at < predecessor.expires_at
                        )
                        OR (
                            predecessor.to_status = 'expired'
                            AND predecessor.expire_reason_code
                                = 'scheduled_expiry'
                        )
                    )
                )
                OR (
                    NEW.event_type <> 'revalidate'
                    AND NEW.expires_at = predecessor.expires_at
                )
            )
            AND (
                NEW.event_type IN ('approve', 'reject', 'revalidate')
                OR (
                    NEW.reviewed_by IS predecessor.reviewed_by
                    AND NEW.reviewed_at IS predecessor.reviewed_at
                    AND NEW.review_reason IS predecessor.review_reason
                )
            )
            AND (
                NEW.event_type = 'revoke'
                OR (
                    NEW.revoked_by IS predecessor.revoked_by
                    AND NEW.revoked_at IS predecessor.revoked_at
                )
            )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'policy_waiver_event_append_invalid');
END''',
    )
    event_update_name = f"{POLICY_WAIVER_TRIGGER_PREFIX}_event_update"
    manifest[event_update_name] = (
        event_table,
        f'''CREATE TRIGGER "{event_update_name}"
BEFORE UPDATE ON "{event_table}"
BEGIN
    SELECT RAISE(ABORT, 'policy_waiver_event_immutable');
END''',
    )
    event_delete_name = f"{POLICY_WAIVER_TRIGGER_PREFIX}_event_delete"
    event_delete_when = ""
    if allow_board_erasure:
        event_delete_when = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            "    WHERE permit.board_id = OLD.board_id\n"
            ")"
        )
    manifest[event_delete_name] = (
        event_table,
        f'''CREATE TRIGGER "{event_delete_name}"
BEFORE DELETE ON "{event_table}"{event_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'policy_waiver_event_immutable');
END''',
    )
    return manifest


def policy_waiver_postgresql_immutability_ddl() -> tuple[str, ...]:
    """PostgreSQL counterpart of the exact SQLite B09 guards."""

    function_name = "policy_waiver_guard_v2"
    return (
        f"""
CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'policy_waiver_events' THEN
        IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'policy_waiver_event_immutable';
        END IF;
        IF TG_OP = 'DELETE' THEN
            IF EXISTS (
                SELECT 1 FROM kg_board_erasure_permits AS permit
                WHERE permit.board_id = OLD.board_id
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'policy_waiver_event_immutable';
        END IF;
        IF length(btrim(NEW.actor_id)) = 0
           OR length(btrim(NEW.reason)) = 0
           OR json_array_length(NEW.evidence_refs) = 0
           OR NOT EXISTS (
               SELECT 1 FROM policy_waivers AS head
               WHERE head.waiver_id = NEW.waiver_id
                 AND head.board_id = NEW.board_id
                 AND head.waiver_revision = NEW.waiver_revision
                 AND head.last_event_id = NEW.event_id
                 AND head.last_event_type = NEW.event_type
                 AND head.last_event_at = NEW.occurred_at
                 AND head.status = NEW.to_status
                 AND head.expires_at = NEW.expires_at
                 AND head.expire_reason_code
                     IS NOT DISTINCT FROM NEW.expire_reason_code
                 AND head.scope_digest = NEW.scope_digest
                 AND head.head_digest = NEW.waiver_digest
                 AND head.reviewed_by IS NOT DISTINCT FROM NEW.reviewed_by
                 AND head.reviewed_at IS NOT DISTINCT FROM NEW.reviewed_at
                 AND head.review_reason
                     IS NOT DISTINCT FROM NEW.review_reason
                 AND head.revoked_by IS NOT DISTINCT FROM NEW.revoked_by
                 AND head.revoked_at IS NOT DISTINCT FROM NEW.revoked_at
                 AND (
                     NEW.event_type <> 'request'
                     OR (
                         NEW.actor_id = head.requested_by
                         AND NEW.occurred_at = head.requested_at
                         AND NEW.reason = head.justification
                         AND NEW.evidence_refs::jsonb
                             = head.evidence_refs::jsonb
                         AND NEW.expires_at = head.original_expires_at
                         AND NEW.idempotency_key = head.idempotency_key
                         AND NEW.request_digest = head.request_digest
                     )
                 )
                 AND (
                     NEW.event_type NOT IN ('approve', 'reject', 'revalidate')
                     OR (
                         NEW.actor_id = head.reviewed_by
                         AND NEW.occurred_at = head.reviewed_at
                         AND NEW.reason = head.review_reason
                     )
                 )
                 AND (
                     NEW.event_type <> 'revoke'
                     OR (
                         NEW.actor_id = head.revoked_by
                         AND NEW.occurred_at = head.revoked_at
                     )
                 )
           )
        THEN
            RAISE EXCEPTION 'policy_waiver_event_append_invalid';
        END IF;
        IF NEW.event_type = 'request' THEN
            IF NEW.predecessor_event_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM policy_waiver_events AS prior
                WHERE prior.waiver_id = NEW.waiver_id
            ) THEN
                RAISE EXCEPTION 'policy_waiver_event_append_invalid';
            END IF;
        ELSIF NOT EXISTS (
            SELECT 1 FROM policy_waiver_events AS predecessor
            WHERE predecessor.event_id = NEW.predecessor_event_id
              AND predecessor.waiver_id = NEW.waiver_id
              AND predecessor.board_id = NEW.board_id
              AND predecessor.waiver_revision = NEW.waiver_revision - 1
              AND predecessor.to_status = NEW.from_status
              AND (
                  (
                      NEW.event_type = 'revalidate'
                      AND NEW.expires_at > predecessor.expires_at
                      AND (
                          (
                              predecessor.to_status = 'approved'
                              AND NEW.occurred_at < predecessor.expires_at
                          )
                          OR (
                              predecessor.to_status = 'expired'
                              AND predecessor.expire_reason_code
                                  = 'scheduled_expiry'
                          )
                      )
                  )
                  OR (
                      NEW.event_type <> 'revalidate'
                      AND NEW.expires_at = predecessor.expires_at
                  )
              )
              AND (
                  NEW.event_type IN ('approve', 'reject', 'revalidate')
                  OR (
                      NEW.reviewed_by
                          IS NOT DISTINCT FROM predecessor.reviewed_by
                      AND NEW.reviewed_at
                          IS NOT DISTINCT FROM predecessor.reviewed_at
                      AND NEW.review_reason
                          IS NOT DISTINCT FROM predecessor.review_reason
                  )
              )
              AND (
                  NEW.event_type = 'revoke'
                  OR (
                      NEW.revoked_by
                          IS NOT DISTINCT FROM predecessor.revoked_by
                      AND NEW.revoked_at
                          IS NOT DISTINCT FROM predecessor.revoked_at
                  )
              )
        ) THEN
            RAISE EXCEPTION 'policy_waiver_event_append_invalid';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'requested'
           OR NEW.waiver_revision <> 1
           OR NEW.last_event_type <> 'request'
           OR NEW.requested_at >= NEW.expires_at
           OR json_array_length(NEW.evidence_refs) = 0
           OR NOT EXISTS (
               SELECT 1
               FROM policy_compliance_findings AS finding
               JOIN policy_compliance_receipts AS receipt
                 ON receipt.receipt_id = finding.receipt_id
                AND receipt.board_id = finding.board_id
                AND receipt.sealed = TRUE
               WHERE finding.finding_id = NEW.finding_id
                 AND finding.receipt_id = NEW.receipt_id
                 AND finding.board_id = NEW.board_id
                 AND finding.guideline_id = NEW.guideline_id
                 AND finding.revision_id = NEW.revision_id
                 AND finding.rule_id = NEW.rule_id
                 AND finding.entity_type = NEW.entity_type
                 AND finding.subject_id = NEW.subject_id
                 AND finding.subject_version = NEW.subject_version
                 AND finding.outcome = 'fail'
                 AND finding.waiver_id IS NULL
                 AND finding.created_at <= NEW.requested_at
           )
           OR EXISTS (
               SELECT 1 FROM policy_waivers AS active
               WHERE active.board_id = NEW.board_id
                 AND active.guideline_id = NEW.guideline_id
                 AND active.revision_id = NEW.revision_id
                 AND active.rule_id = NEW.rule_id
                 AND active.entity_type = NEW.entity_type
                 AND active.subject_id = NEW.subject_id
                 AND active.subject_version = NEW.subject_version
                 AND active.status IN ('requested', 'approved')
                 AND active.expires_at > NEW.requested_at
           )
        THEN
            RAISE EXCEPTION 'policy_waiver_request_invalid';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1 FROM kg_board_erasure_permits AS permit
            WHERE permit.board_id = OLD.board_id
        ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'policy_waiver_head_immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        ROW(
            NEW.waiver_id, NEW.board_id, NEW.finding_id, NEW.receipt_id,
            NEW.guideline_id, NEW.revision_id, NEW.rule_id, NEW.entity_type,
            NEW.subject_id, NEW.subject_version, NEW.scope_digest,
            NEW.justification, NEW.evidence_refs::jsonb, NEW.requested_by,
            NEW.requested_at, NEW.original_expires_at, NEW.idempotency_key,
            NEW.request_digest
        ) IS DISTINCT FROM ROW(
            OLD.waiver_id, OLD.board_id, OLD.finding_id, OLD.receipt_id,
            OLD.guideline_id, OLD.revision_id, OLD.rule_id, OLD.entity_type,
            OLD.subject_id, OLD.subject_version, OLD.scope_digest,
            OLD.justification, OLD.evidence_refs::jsonb, OLD.requested_by,
            OLD.requested_at, OLD.original_expires_at, OLD.idempotency_key,
            OLD.request_digest
        )
        OR NEW.waiver_revision <> OLD.waiver_revision + 1
        OR NEW.last_event_id = OLD.last_event_id
        OR NEW.last_event_at < OLD.last_event_at
    ) THEN
        RAISE EXCEPTION 'policy_waiver_head_cas_invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
""".strip(),
        f'''CREATE TRIGGER "{POLICY_WAIVER_TRIGGER_PREFIX}_head"
BEFORE INSERT OR UPDATE OR DELETE ON "policy_waivers"
FOR EACH ROW EXECUTE FUNCTION {function_name}()''',
        f'''CREATE TRIGGER "{POLICY_WAIVER_TRIGGER_PREFIX}_event"
BEFORE INSERT OR UPDATE OR DELETE ON "policy_waiver_events"
FOR EACH ROW EXECUTE FUNCTION {function_name}()''',
    )


def semantic_guideline_sqlite_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Return exact SQLite guards for ``semantic-guideline/v3`` authority."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        Guideline,
        GuidelineBoardBindingRow,
        SemanticGuidelineAssessmentReceiptRow,
        SemanticGuidelineBindingConfigurationRow,
        SemanticGuidelineFindingRow,
        SemanticGuidelineLegacyMigrationRow,
        SemanticGuidelineMetricResultRow,
        SemanticGuidelineRevisionRow,
        SemanticGuidelineSkipRow,
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
        SemanticSubjectVersionEventRow,
        SemanticSubjectVersionRow,
    )

    permit_table = BoardErasurePermit.__tablename__
    guideline_table = Guideline.__tablename__
    legacy_binding_table = GuidelineBoardBindingRow.__tablename__
    revision_table = SemanticGuidelineRevisionRow.__tablename__
    binding_table = SemanticGuidelineBindingConfigurationRow.__tablename__
    subject_event_table = SemanticSubjectVersionEventRow.__tablename__
    subject_head_table = SemanticSubjectVersionRow.__tablename__
    receipt_table = SemanticGuidelineAssessmentReceiptRow.__tablename__
    result_table = SemanticGuidelineMetricResultRow.__tablename__
    finding_table = SemanticGuidelineFindingRow.__tablename__
    waiver_table = SemanticGuidelineWaiverRow.__tablename__
    waiver_event_table = SemanticGuidelineWaiverEventRow.__tablename__
    skip_table = SemanticGuidelineSkipRow.__tablename__
    migration_table = SemanticGuidelineLegacyMigrationRow.__tablename__
    manifest: dict[str, tuple[str, str]] = {}

    def add_immutable(
        *,
        table_name: str,
        board_expression: str | None,
        message: str = "semantic_guideline_evidence_immutable",
    ) -> None:
        update_name = (
            f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_{table_name}_update"
        )
        manifest[update_name] = (
            table_name,
            f'''CREATE TRIGGER "{update_name}"
BEFORE UPDATE ON "{table_name}"
BEGIN
    SELECT RAISE(ABORT, '{message}');
END''',
        )
        delete_name = (
            f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_{table_name}_delete"
        )
        delete_when = ""
        if allow_board_erasure and board_expression is not None:
            delete_when = f'''
WHEN NOT EXISTS (
    SELECT 1
    FROM "{permit_table}" AS permit
    WHERE permit.board_id = {board_expression}
)'''
        manifest[delete_name] = (
            table_name,
            f'''CREATE TRIGGER "{delete_name}"
BEFORE DELETE ON "{table_name}"{delete_when}
BEGIN
    SELECT RAISE(ABORT, '{message}');
END''',
        )

    revision_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_revision_insert"
    )
    manifest[revision_insert_name] = (
        revision_table,
        f'''CREATE TRIGGER "{revision_insert_name}"
BEFORE INSERT ON "{revision_table}"
WHEN json_type(NEW."metrics") <> 'array'
  OR (
      NEW."authority_state" <> 'native'
      AND json_array_length(NEW."metrics") <> 0
  )
  OR EXISTS (
      SELECT 1
      FROM json_each(NEW."metrics") AS metric
      WHERE json_type(metric.value) <> 'object'
         OR json_type(metric.value, '$.metric_id') <> 'text'
         OR length(trim(json_extract(metric.value, '$.metric_id'))) = 0
         OR json_extract(metric.value, '$.metric_id')
                <> trim(json_extract(metric.value, '$.metric_id'))
         OR length(json_extract(metric.value, '$.metric_id')) > 64
         OR lower(trim(json_extract(metric.value, '$.metric_id')))
                = 'confidence'
         OR json_type(metric.value, '$.code') <> 'text'
         OR length(trim(json_extract(metric.value, '$.code'))) = 0
         OR json_extract(metric.value, '$.code')
                <> trim(json_extract(metric.value, '$.code'))
         OR length(json_extract(metric.value, '$.code')) > 128
         OR lower(trim(json_extract(metric.value, '$.code')))
                = 'confidence'
         OR substr(json_extract(metric.value, '$.code'), 1, 1)
                NOT GLOB '[A-Za-z]'
         OR json_extract(metric.value, '$.code')
                GLOB '*[^A-Za-z0-9_.:-]*'
         OR json_type(metric.value, '$.title') <> 'text'
         OR length(trim(json_extract(metric.value, '$.title'))) = 0
         OR json_extract(metric.value, '$.title')
                <> trim(json_extract(metric.value, '$.title'))
         OR length(json_extract(metric.value, '$.title')) > 500
         OR lower(trim(json_extract(metric.value, '$.title')))
                = 'confidence'
         OR json_type(metric.value, '$.description') <> 'text'
         OR length(trim(json_extract(metric.value, '$.description'))) = 0
         OR json_extract(metric.value, '$.description')
                <> trim(json_extract(metric.value, '$.description'))
         OR json_type(metric.value, '$.evaluation_rubric') <> 'text'
         OR length(trim(json_extract(metric.value, '$.evaluation_rubric'))) = 0
         OR json_extract(metric.value, '$.evaluation_rubric')
                <> trim(json_extract(metric.value, '$.evaluation_rubric'))
         OR json_type(metric.value, '$.target_entity_types') <> 'array'
         OR json_array_length(
                json_extract(metric.value, '$.target_entity_types')
            ) = 0
         OR json_extract(metric.value, '$.direction')
                NOT IN ('minimum', 'maximum')
         OR json_type(metric.value, '$.default_threshold') <> 'integer'
         OR json_extract(metric.value, '$.default_threshold') < 0
         OR json_extract(metric.value, '$.default_threshold') > 100
         OR EXISTS (
             SELECT 1
             FROM json_each(
                 json_extract(metric.value, '$.target_entity_types')
             ) AS target
             WHERE target.value NOT IN (
                  'ideation', 'refinement', 'spec', 'card', 'sprint',
                  'test_scenario'
              )
                OR target.type <> 'text'
         )
         OR (
             SELECT COUNT(*)
             FROM json_each(
                 json_extract(metric.value, '$.target_entity_types')
             )
         ) <> (
             SELECT COUNT(DISTINCT target.value)
             FROM json_each(
                 json_extract(metric.value, '$.target_entity_types')
             ) AS target
         )
         OR EXISTS (
             SELECT 1
             FROM json_each(
                 json_extract(metric.value, '$.target_entity_types')
             ) AS left_target
             JOIN json_each(
                 json_extract(metric.value, '$.target_entity_types')
             ) AS right_target
               ON left_target.key < right_target.key
              AND left_target.value > right_target.value
         )
  )
  OR EXISTS (
      SELECT 1
      FROM json_each(NEW."metrics") AS left_metric
      JOIN json_each(NEW."metrics") AS right_metric
        ON left_metric.key < right_metric.key
       AND (
           json_extract(left_metric.value, '$.metric_id')
               = json_extract(right_metric.value, '$.metric_id')
           OR lower(json_extract(left_metric.value, '$.code'))
               = lower(json_extract(right_metric.value, '$.code'))
       )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_metrics_invalid');
END''',
    )
    add_immutable(
        table_name=revision_table,
        board_expression=None,
        message="semantic_guideline_revision_immutable",
    )
    # Replace the unconditional revision DELETE guard with an inline-board
    # erasure-aware variant. Global revisions remain immutable.
    revision_delete_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_{revision_table}_delete"
    )
    revision_delete_when = ""
    if allow_board_erasure:
        revision_delete_when = f'''
WHEN NOT EXISTS (
    SELECT 1
    FROM "{permit_table}" AS permit
    JOIN "{guideline_table}" AS guideline
      ON guideline.board_id = permit.board_id
    WHERE guideline.id = OLD.guideline_id
      AND guideline.scope = 'inline'
)'''
    manifest[revision_delete_name] = (
        revision_table,
        f'''CREATE TRIGGER "{revision_delete_name}"
BEFORE DELETE ON "{revision_table}"{revision_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_revision_immutable');
END''',
    )

    binding_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_binding_insert"
    )
    manifest[binding_insert_name] = (
        binding_table,
        f'''CREATE TRIGGER "{binding_insert_name}"
BEFORE INSERT ON "{binding_table}"
WHEN json_type(NEW."metric_threshold_overrides") <> 'object'
  OR NOT EXISTS (
      SELECT 1
      FROM "{legacy_binding_table}" AS legacy
      WHERE legacy.binding_id = NEW.binding_id
        AND legacy.binding_revision = NEW.binding_revision
        AND legacy.board_id = NEW.board_id
        AND legacy.guideline_id = NEW.guideline_id
        AND legacy.revision_id = NEW.revision_id
  )
  OR EXISTS (
      SELECT 1
      FROM "{revision_table}" AS revision
      WHERE revision.guideline_id = NEW.guideline_id
        AND revision.revision_id = NEW.revision_id
        AND revision.revision_digest = NEW.revision_digest
        AND revision.authority_state = 'legacy_incompatible'
  )
  OR EXISTS (
      SELECT 1
      FROM json_each(NEW."metric_threshold_overrides") AS override
      WHERE override.type <> 'integer'
         OR override.value < 0
         OR override.value > 100
         OR NOT EXISTS (
             SELECT 1
             FROM "{revision_table}" AS revision,
                  json_each(revision.metrics) AS metric
             WHERE revision.guideline_id = NEW.guideline_id
               AND revision.revision_id = NEW.revision_id
               AND revision.revision_digest = NEW.revision_digest
               AND json_extract(metric.value, '$.code') = override.key
         )
  )
  OR EXISTS (
      SELECT 1
      FROM "{legacy_binding_table}" AS legacy
      WHERE legacy.binding_id = NEW.binding_id
        AND legacy.binding_revision = NEW.binding_revision
        AND legacy.impact_adoption_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM "guideline_impact_receipts" AS receipt
            WHERE receipt.impact_receipt_id =
                    legacy.impact_receipt_id
              AND receipt.board_id = NEW.board_id
              AND receipt.guideline_id = NEW.guideline_id
              AND receipt.to_revision_id = NEW.revision_id
              AND receipt.to_revision_digest =
                    NEW.revision_digest
              AND receipt.proposed_enforcement =
                    NEW.enforcement
              AND receipt.proposed_minimum_confidence =
                    NEW.minimum_confidence
              AND json(
                    receipt.proposed_metric_threshold_overrides
                  ) = json(NEW.metric_threshold_overrides)
              AND receipt.sealed = 1
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_binding_configuration_invalid');
END''',
    )
    add_immutable(
        table_name=binding_table,
        board_expression='OLD."board_id"',
        message="semantic_guideline_binding_configuration_immutable",
    )

    subject_head_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_subject_head_insert"
    )
    manifest[subject_head_insert_name] = (
        subject_head_table,
        f'''CREATE TRIGGER "{subject_head_insert_name}"
BEFORE INSERT ON "{subject_head_table}"
WHEN NEW."head_revision" <> 1
BEGIN
    SELECT RAISE(ABORT, 'semantic_subject_head_initial_revision_invalid');
END''',
    )
    subject_head_immutable = (
        "NEW.\"board_id\" IS OLD.\"board_id\"\n"
        "    AND NEW.\"subject_type\" IS OLD.\"subject_type\"\n"
        "    AND NEW.\"subject_id\" IS OLD.\"subject_id\""
    )
    subject_head_update_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_subject_head_update"
    )
    manifest[subject_head_update_name] = (
        subject_head_table,
        f'''CREATE TRIGGER "{subject_head_update_name}"
BEFORE UPDATE ON "{subject_head_table}"
WHEN NOT (
    {subject_head_immutable}
    AND NEW."head_revision" = OLD."head_revision" + 1
    AND NEW."last_event_id" <> OLD."last_event_id"
    AND NEW."subject_version" >= OLD."subject_version"
    AND NEW."updated_at" >= OLD."updated_at"
)
BEGIN
    SELECT RAISE(ABORT, 'semantic_subject_head_cas_invalid');
END''',
    )
    subject_head_delete_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_subject_head_delete"
    )
    subject_head_delete_when = ""
    if allow_board_erasure:
        subject_head_delete_when = f'''
WHEN NOT EXISTS (
    SELECT 1 FROM "{permit_table}" AS permit
    WHERE permit.board_id = OLD.board_id
)'''
    manifest[subject_head_delete_name] = (
        subject_head_table,
        f'''CREATE TRIGGER "{subject_head_delete_name}"
BEFORE DELETE ON "{subject_head_table}"{subject_head_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'semantic_subject_head_immutable');
END''',
    )
    subject_event_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_subject_event_insert"
    )
    manifest[subject_event_insert_name] = (
        subject_event_table,
        f'''CREATE TRIGGER "{subject_event_insert_name}"
BEFORE INSERT ON "{subject_event_table}"
WHEN NOT EXISTS (
      SELECT 1
      FROM "{subject_head_table}" AS head
      WHERE head.board_id = NEW.board_id
        AND head.subject_type = NEW.subject_type
        AND head.subject_id = NEW.subject_id
        AND head.subject_version = NEW.subject_version
        AND head.content_digest = NEW.content_digest
        AND head.last_semantic_editor_id = NEW.last_semantic_editor_id
        AND head.editor_source = NEW.editor_source
        AND head.head_revision = NEW.head_revision
        AND head.last_event_id = NEW.event_id
        AND head.updated_at = NEW.changed_at
  )
  OR (
      NEW."head_revision" = 1
      AND (
          NEW."predecessor_event_id" IS NOT NULL
          OR (
              NEW."editor_source" = 'legacy_unknown'
              AND NEW."event_type" <> 'legacy_bootstrap'
          )
          OR (
              NEW."editor_source" = 'authoritative'
              AND NEW."event_type" <> 'semantic_mutation'
          )
      )
  )
  OR (
      NEW."head_revision" > 1
      AND (
          NEW."event_type" <> 'semantic_mutation'
          OR NOT EXISTS (
              SELECT 1
              FROM "{subject_event_table}" AS predecessor
              WHERE predecessor.event_id = NEW.predecessor_event_id
                AND predecessor.board_id = NEW.board_id
                AND predecessor.subject_type = NEW.subject_type
                AND predecessor.subject_id = NEW.subject_id
                AND predecessor.head_revision = NEW.head_revision - 1
                AND predecessor.subject_version <= NEW.subject_version
                AND predecessor.changed_at <= NEW.changed_at
          )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_subject_event_append_invalid');
END''',
    )
    add_immutable(
        table_name=subject_event_table,
        board_expression='OLD."board_id"',
        message="semantic_subject_event_immutable",
    )

    receipt_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_assessment_insert"
    )
    manifest[receipt_insert_name] = (
        receipt_table,
        f'''CREATE TRIGGER "{receipt_insert_name}"
BEFORE INSERT ON "{receipt_table}"
WHEN NEW."sealed" <> 0
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_assessment_initially_unsealed');
END''',
    )
    receipt_columns = tuple(
        column.name
        for column in SemanticGuidelineAssessmentReceiptRow.__table__.columns
        if column.name != "sealed"
    )
    receipt_unchanged = "\n    AND ".join(
        f'NEW."{column}" IS OLD."{column}"' for column in receipt_columns
    )
    receipt_update_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_assessment_seal"
    )
    manifest[receipt_update_name] = (
        receipt_table,
        f'''CREATE TRIGGER "{receipt_update_name}"
BEFORE UPDATE ON "{receipt_table}"
WHEN NOT (
    OLD."sealed" = 0
    AND NEW."sealed" = 1
    AND {receipt_unchanged}
    AND NEW."metric_result_count" = (
        SELECT COUNT(*)
        FROM "{revision_table}" AS revision,
             json_each(revision.metrics) AS metric,
             json_each(
                 json_extract(metric.value, '$.target_entity_types')
             ) AS target
        WHERE revision.guideline_id = NEW.guideline_id
          AND revision.revision_id = NEW.revision_id
          AND revision.revision_digest = NEW.revision_digest
          AND target.value = NEW.subject_type
    )
    AND NEW."metric_result_count" = (
        SELECT COUNT(*)
        FROM "{result_table}" AS result
        WHERE result.receipt_id = NEW.receipt_id
    )
    AND NEW."failed_metric_count" = (
        SELECT COUNT(*)
        FROM "{result_table}" AS result
        WHERE result.receipt_id = NEW.receipt_id
          AND result.outcome = 'fail'
    )
    AND NEW."failed_metric_count" = (
        SELECT COUNT(*)
        FROM "{finding_table}" AS finding
        WHERE finding.receipt_id = NEW.receipt_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_assessment_seal_invalid');
END''',
    )
    receipt_delete_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_{receipt_table}_delete"
    )
    receipt_delete_when = ""
    if allow_board_erasure:
        receipt_delete_when = f'''
WHEN NOT EXISTS (
    SELECT 1 FROM "{permit_table}" AS permit
    WHERE permit.board_id = OLD.board_id
)'''
    manifest[receipt_delete_name] = (
        receipt_table,
        f'''CREATE TRIGGER "{receipt_delete_name}"
BEFORE DELETE ON "{receipt_table}"{receipt_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_assessment_immutable');
END''',
    )

    result_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_metric_result_insert"
    )
    manifest[result_insert_name] = (
        result_table,
        f'''CREATE TRIGGER "{result_insert_name}"
BEFORE INSERT ON "{result_table}"
WHEN NOT EXISTS (
      SELECT 1
      FROM "{receipt_table}" AS receipt
      JOIN "{binding_table}" AS binding
        ON binding.binding_id = receipt.binding_id
       AND binding.binding_revision = receipt.binding_revision
       AND binding.configuration_digest = receipt.configuration_digest
      JOIN "{revision_table}" AS revision
        ON revision.guideline_id = receipt.guideline_id
       AND revision.revision_id = receipt.revision_id
       AND revision.revision_digest = receipt.revision_digest,
           json_each(revision.metrics) AS metric
      WHERE receipt.receipt_id = NEW.receipt_id
        AND receipt.sealed = 0
        AND receipt.receipt_digest = NEW.receipt_digest
        AND json_extract(metric.value, '$.metric_id') = NEW.metric_id
        AND json_extract(metric.value, '$.code') = NEW.metric_code
        AND json_extract(metric.value, '$.direction') = NEW.direction
        AND json_extract(metric.value, '$.default_threshold')
            = NEW.default_threshold
        AND EXISTS (
            SELECT 1
            FROM json_each(
                json_extract(metric.value, '$.target_entity_types')
            ) AS target
            WHERE target.value = receipt.subject_type
        )
        AND NEW.effective_threshold = COALESCE(
            (
                SELECT override.value
                FROM json_each(
                    binding.metric_threshold_overrides
                ) AS override
                WHERE override.key = NEW.metric_code
            ),
            json_extract(metric.value, '$.default_threshold')
        )
        AND NEW.threshold_source = CASE
            WHEN EXISTS (
                SELECT 1
                FROM json_each(
                    binding.metric_threshold_overrides
                ) AS override
                WHERE override.key = NEW.metric_code
            ) THEN 'override'
            ELSE 'default'
        END
  )
  OR json_type(NEW."evidence_refs") <> 'array'
  OR json_array_length(NEW."evidence_refs") = 0
  OR json_type(NEW."pinpoints") <> 'array'
  OR json_array_length(NEW."pinpoints") = 0
  OR EXISTS (
      SELECT 1
      FROM json_each(NEW."evidence_refs") AS evidence
      WHERE json_type(evidence.value) <> 'object'
         OR json_type(evidence.value, '$.source_type') <> 'text'
         OR length(trim(json_extract(
                evidence.value, '$.source_type'
            ))) = 0
         OR json_type(evidence.value, '$.source_id') <> 'text'
         OR length(trim(json_extract(
                evidence.value, '$.source_id'
            ))) = 0
         OR json_type(evidence.value, '$.source_version') <> 'integer'
         OR json_extract(evidence.value, '$.source_version') < 1
         OR json_type(evidence.value, '$.content_hash') <> 'text'
         OR length(json_extract(evidence.value, '$.content_hash')) <> 64
         OR json_extract(evidence.value, '$.content_hash')
                GLOB '*[^0-9a-f]*'
  )
  OR EXISTS (
      SELECT 1
      FROM json_each(NEW."pinpoints") AS pinpoint
      WHERE json_type(pinpoint.value) <> 'object'
         OR json_extract(pinpoint.value, '$.anchor_type') NOT IN (
             'whole_artifact', 'field', 'structured_child', 'qa'
         )
         OR (
             json_type(pinpoint.value, '$.anchor_ref')
                 NOT IN ('text', 'null')
         )
         OR (
              json_type(pinpoint.value, '$.excerpt_hash')
                  NOT IN ('text', 'null')
          )
          OR json_type(pinpoint.value, '$.subject') <> 'object'
          OR json_type(pinpoint.value, '$.subject.board_id') <> 'text'
          OR json_extract(pinpoint.value, '$.subject.board_id')
                 <> NEW.board_id
          OR json_type(pinpoint.value, '$.subject.subject_type') <> 'text'
          OR json_extract(pinpoint.value, '$.subject.subject_type')
                 <> NEW.subject_type
          OR json_type(pinpoint.value, '$.subject.subject_id') <> 'text'
          OR json_extract(pinpoint.value, '$.subject.subject_id')
                 <> NEW.subject_id
          OR json_type(pinpoint.value, '$.subject.subject_version')
                 <> 'integer'
          OR json_extract(pinpoint.value, '$.subject.subject_version')
                 <> NEW.subject_version
         OR json_type(pinpoint.value, '$.input_digest') <> 'text'
         OR json_extract(pinpoint.value, '$.input_digest') <> (
             SELECT receipt.input_digest
             FROM "{receipt_table}" AS receipt
             WHERE receipt.receipt_id = NEW.receipt_id
         )
         OR (
             json_extract(pinpoint.value, '$.anchor_type')
                 = 'whole_artifact'
             AND json_type(pinpoint.value, '$.anchor_ref') IS NOT 'null'
         )
         OR (
             json_extract(pinpoint.value, '$.anchor_type')
                 <> 'whole_artifact'
             AND (
                 json_type(pinpoint.value, '$.anchor_ref') <> 'text'
                 OR length(trim(json_extract(
                        pinpoint.value, '$.anchor_ref'
                    ))) = 0
             )
         )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_metric_result_invalid');
END''',
    )
    add_immutable(
        table_name=result_table,
        board_expression='OLD."board_id"',
        message="semantic_guideline_metric_result_immutable",
    )

    finding_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_finding_insert"
    )
    manifest[finding_insert_name] = (
        finding_table,
        f'''CREATE TRIGGER "{finding_insert_name}"
BEFORE INSERT ON "{finding_table}"
WHEN NOT EXISTS (
    SELECT 1
    FROM "{result_table}" AS result
    JOIN "{receipt_table}" AS receipt
      ON receipt.receipt_id = result.receipt_id
     AND receipt.sealed = 0
     AND receipt.receipt_digest = NEW.receipt_digest
    WHERE result.result_id = NEW.metric_result_id
      AND result.receipt_id = NEW.receipt_id
      AND result.board_id = NEW.board_id
      AND result.subject_type = NEW.subject_type
      AND result.subject_id = NEW.subject_id
      AND result.subject_version = NEW.subject_version
      AND result.subject_content_digest = NEW.subject_content_digest
      AND result.receipt_digest = NEW.receipt_digest
      AND result.guideline_id = NEW.guideline_id
      AND result.revision_id = NEW.revision_id
      AND result.revision_digest = NEW.revision_digest
      AND result.binding_id = NEW.binding_id
      AND result.binding_revision = NEW.binding_revision
      AND result.configuration_digest = NEW.configuration_digest
      AND result.metric_id = NEW.metric_id
      AND result.metric_code = NEW.metric_code
      AND result.result_digest = NEW.metric_result_digest
      AND result.outcome = 'fail'
      AND result.rationale = NEW.rationale
      AND result.evidence_refs IS NEW.evidence_refs
      AND result.pinpoints IS NEW.pinpoints
      AND result.created_at = NEW.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_finding_invalid');
END''',
    )
    add_immutable(
        table_name=finding_table,
        board_expression='OLD."board_id"',
        message="semantic_guideline_finding_immutable",
    )

    waiver_head_immutable_columns = (
        "waiver_id",
        "board_id",
        "metric_result_id",
        "finding_id",
        "receipt_id",
        "assessment_assessor_id",
        "subject_type",
        "subject_id",
        "subject_version",
        "subject_content_digest",
        "receipt_digest",
        "guideline_id",
        "revision_id",
        "revision_digest",
        "binding_id",
        "binding_revision",
        "configuration_digest",
        "metric_id",
        "metric_code",
        "metric_result_digest",
        "finding_digest",
        "scope_digest",
        "justification",
        "evidence_refs",
        "requested_by",
        "requested_at",
        "original_expires_at",
        "idempotency_key",
        "request_digest",
    )
    waiver_unchanged = "\n    AND ".join(
        f'NEW."{column}" IS OLD."{column}"'
        for column in waiver_head_immutable_columns
    )
    waiver_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_waiver_insert"
    )
    manifest[waiver_insert_name] = (
        waiver_table,
        f'''CREATE TRIGGER "{waiver_insert_name}"
BEFORE INSERT ON "{waiver_table}"
WHEN NEW."status" <> 'requested'
  OR NEW."waiver_revision" <> 1
  OR NEW."last_event_type" <> 'request'
  OR length(trim(NEW."assessment_assessor_id")) = 0
  OR (
      NEW."expires_at" IS NOT NULL
      AND NEW."requested_at" >= NEW."expires_at"
  )
  OR json_type(NEW."evidence_refs") <> 'array'
  OR json_array_length(NEW."evidence_refs") = 0
  OR json_type(
      NEW."last_revalidation_currentness_reasons"
  ) <> 'array'
  OR json_array_length(
      NEW."last_revalidation_currentness_reasons"
  ) <> 0
  OR NOT EXISTS (
      SELECT 1
      FROM "{result_table}" AS result
      JOIN "{finding_table}" AS finding
       ON finding.metric_result_id = result.result_id
       AND finding.receipt_id = result.receipt_id
       AND finding.receipt_digest = NEW.receipt_digest
       AND finding.metric_result_digest = result.result_digest
      JOIN "{receipt_table}" AS receipt
        ON receipt.receipt_id = result.receipt_id
       AND receipt.sealed = 1
       AND receipt.receipt_digest = NEW.receipt_digest
       AND receipt.assessor_agent_id = NEW.assessment_assessor_id
      WHERE result.result_id = NEW.metric_result_id
        AND result.receipt_id = NEW.receipt_id
        AND result.receipt_digest = NEW.receipt_digest
        AND result.board_id = NEW.board_id
        AND result.subject_type = NEW.subject_type
        AND result.subject_id = NEW.subject_id
        AND result.subject_version = NEW.subject_version
        AND result.subject_content_digest = NEW.subject_content_digest
        AND result.guideline_id = NEW.guideline_id
        AND result.revision_id = NEW.revision_id
        AND result.revision_digest = NEW.revision_digest
        AND result.binding_id = NEW.binding_id
        AND result.binding_revision = NEW.binding_revision
        AND result.configuration_digest = NEW.configuration_digest
        AND result.metric_id = NEW.metric_id
        AND result.metric_code = NEW.metric_code
        AND result.result_digest = NEW.metric_result_digest
        AND finding.finding_id = NEW.finding_id
        AND finding.finding_digest = NEW.finding_digest
        AND result.outcome = 'fail'
        AND result.created_at <= NEW.requested_at
  )
  OR EXISTS (
      SELECT 1
      FROM "{waiver_table}" AS active
      WHERE active.board_id = NEW.board_id
        AND active.scope_digest = NEW.scope_digest
        AND active.metric_result_id = NEW.metric_result_id
        AND active.metric_result_digest = NEW.metric_result_digest
        AND active.finding_id = NEW.finding_id
        AND active.finding_digest = NEW.finding_digest
        AND active.receipt_id = NEW.receipt_id
        AND active.receipt_digest = NEW.receipt_digest
        AND active.assessment_assessor_id =
            NEW.assessment_assessor_id
        AND active.binding_id = NEW.binding_id
        AND active.binding_revision = NEW.binding_revision
        AND active.configuration_digest = NEW.configuration_digest
        AND active.metric_id = NEW.metric_id
        AND active.subject_type = NEW.subject_type
        AND active.subject_id = NEW.subject_id
        AND active.subject_version = NEW.subject_version
        AND active.subject_content_digest = NEW.subject_content_digest
        AND active.guideline_id = NEW.guideline_id
        AND active.revision_id = NEW.revision_id
        AND active.revision_digest = NEW.revision_digest
        AND active.status IN ('requested', 'approved')
        AND (
            active.expires_at IS NULL
            OR active.expires_at > NEW.requested_at
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_waiver_request_invalid');
END''',
    )
    waiver_scope_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_waiver_scope_insert"
    )
    manifest[waiver_scope_insert_name] = (
        waiver_table,
        f'''CREATE TRIGGER "{waiver_scope_insert_name}"
BEFORE INSERT ON "{waiver_table}"
WHEN EXISTS (
    SELECT 1
    FROM "{waiver_table}" AS active
    WHERE active.board_id = NEW.board_id
      AND active.scope_digest = NEW.scope_digest
      AND active.status IN ('requested', 'approved')
      AND (
          active.expires_at IS NULL
          OR active.expires_at > NEW.requested_at
      )
)
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_waiver_scope_conflict');
END''',
    )
    waiver_update_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_waiver_update"
    )
    manifest[waiver_update_name] = (
        waiver_table,
        f'''CREATE TRIGGER "{waiver_update_name}"
BEFORE UPDATE ON "{waiver_table}"
WHEN NOT (
    {waiver_unchanged}
    AND NEW."waiver_revision" = OLD."waiver_revision" + 1
    AND NEW."last_event_id" <> OLD."last_event_id"
    AND NEW."last_event_at" >= OLD."last_event_at"
    AND (
        NEW."last_event_type" = 'revalidate'
        OR (
            NEW."last_revalidation_status"
                IS OLD."last_revalidation_status"
            AND NEW."last_revalidation_current"
                IS OLD."last_revalidation_current"
            AND NEW."last_revalidation_reason_code"
                IS OLD."last_revalidation_reason_code"
            AND NEW."last_revalidation_evaluated_at"
                IS OLD."last_revalidation_evaluated_at"
            AND json(NEW."last_revalidation_currentness_reasons") =
                json(OLD."last_revalidation_currentness_reasons")
            AND NEW."last_revalidation_scheduled_expiry_observed" =
                OLD."last_revalidation_scheduled_expiry_observed"
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_waiver_head_cas_invalid');
END''',
    )
    waiver_delete_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_waiver_delete"
    )
    waiver_delete_when = ""
    if allow_board_erasure:
        waiver_delete_when = f'''
WHEN NOT EXISTS (
    SELECT 1 FROM "{permit_table}" AS permit
    WHERE permit.board_id = OLD.board_id
)'''
    manifest[waiver_delete_name] = (
        waiver_table,
        f'''CREATE TRIGGER "{waiver_delete_name}"
BEFORE DELETE ON "{waiver_table}"{waiver_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_waiver_immutable');
END''',
    )
    waiver_event_insert_name = (
        f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_waiver_event_insert"
    )
    manifest[waiver_event_insert_name] = (
        waiver_event_table,
        f'''CREATE TRIGGER "{waiver_event_insert_name}"
BEFORE INSERT ON "{waiver_event_table}"
WHEN length(trim(NEW."actor_id")) = 0
  OR length(trim(NEW."reason")) = 0
  OR json_type(NEW."evidence_refs") <> 'array'
  OR json_array_length(NEW."evidence_refs") = 0
  OR json_type(NEW."currentness_reasons") <> 'array'
  OR (
      NEW."event_type" <> 'revalidate'
      AND json_array_length(NEW."currentness_reasons") <> 0
  )
  OR (
      NEW."event_type" = 'revalidate'
      AND (
          NEW."scheduled_expiry_observed" <>
              (
                  NEW."expires_at" IS NOT NULL
                  AND NEW."expires_at" <= NEW."evaluated_at"
              )
          OR (
              NEW."revalidation_status" IN ('approved', 'expired')
              AND json_array_length(NEW."currentness_reasons") <> 0
          )
          OR EXISTS (
              SELECT 1
              FROM json_each(NEW."currentness_reasons") AS reason
              WHERE reason.value NOT IN (
                  'current_snapshot_missing',
                  'subject_version_changed',
                  'subject_content_changed',
                  'guideline_revision_changed',
                  'guideline_revision_digest_changed',
                  'binding_revision_changed',
                  'binding_configuration_changed',
                  'policy_set_changed',
                  'binding_head_changed',
                  'input_digest_changed'
              )
          )
          OR EXISTS (
              SELECT 1
              FROM json_each(NEW."currentness_reasons") AS reason
              GROUP BY reason.value
              HAVING count(*) > 1
          )
          OR EXISTS (
              SELECT 1
              FROM json_each(NEW."currentness_reasons") AS left_reason
              JOIN json_each(NEW."currentness_reasons") AS right_reason
                ON CAST(left_reason.key AS INTEGER) <
                   CAST(right_reason.key AS INTEGER)
              WHERE
                CASE left_reason.value
                  WHEN 'current_snapshot_missing' THEN 1
                  WHEN 'subject_version_changed' THEN 2
                  WHEN 'subject_content_changed' THEN 3
                  WHEN 'guideline_revision_changed' THEN 4
                  WHEN 'guideline_revision_digest_changed' THEN 5
                  WHEN 'binding_revision_changed' THEN 6
                  WHEN 'binding_configuration_changed' THEN 7
                  WHEN 'policy_set_changed' THEN 8
                  WHEN 'binding_head_changed' THEN 9
                  WHEN 'input_digest_changed' THEN 10
                END >
                CASE right_reason.value
                  WHEN 'current_snapshot_missing' THEN 1
                  WHEN 'subject_version_changed' THEN 2
                  WHEN 'subject_content_changed' THEN 3
                  WHEN 'guideline_revision_changed' THEN 4
                  WHEN 'guideline_revision_digest_changed' THEN 5
                  WHEN 'binding_revision_changed' THEN 6
                  WHEN 'binding_configuration_changed' THEN 7
                  WHEN 'policy_set_changed' THEN 8
                  WHEN 'binding_head_changed' THEN 9
                  WHEN 'input_digest_changed' THEN 10
                END
          )
      )
  )
  OR NOT EXISTS (
      SELECT 1
      FROM "{waiver_table}" AS head
      WHERE head.waiver_id = NEW.waiver_id
        AND head.board_id = NEW.board_id
        AND head.waiver_revision = NEW.waiver_revision
        AND head.last_event_id = NEW.event_id
        AND head.last_event_type = NEW.event_type
        AND head.last_event_at = NEW.occurred_at
        AND head.last_event_idempotency_key = NEW.idempotency_key
        AND head.status = NEW.to_status
        AND head.expires_at IS NEW.expires_at
        AND head.expire_reason_code IS NEW.expire_reason_code
        AND head.scope_digest = NEW.scope_digest
        AND head.head_digest = NEW.waiver_digest
        AND head.reviewed_by IS NEW.reviewed_by
        AND head.reviewed_at IS NEW.reviewed_at
        AND head.review_reason IS NEW.review_reason
        AND head.revoked_by IS NEW.revoked_by
        AND head.revoked_at IS NEW.revoked_at
        AND (
            NEW.event_type <> 'revalidate'
            OR (
                head.last_revalidation_status IS
                    NEW.revalidation_status
                AND head.last_revalidation_current IS
                    NEW.revalidation_current
                AND head.last_revalidation_reason_code IS
                    NEW.revalidation_reason_code
                AND head.last_revalidation_evaluated_at IS
                    NEW.evaluated_at
                AND json(
                    head.last_revalidation_currentness_reasons
                ) = json(NEW.currentness_reasons)
                AND
                    head.last_revalidation_scheduled_expiry_observed =
                    NEW.scheduled_expiry_observed
            )
        )
        AND (
            NEW.event_type <> 'request'
            OR (
                NEW.actor_id = head.requested_by
                AND NEW.occurred_at = head.requested_at
                AND NEW.reason = head.justification
                AND json(NEW.evidence_refs) = json(head.evidence_refs)
                AND NEW.expires_at IS head.original_expires_at
                AND NEW.idempotency_key = head.idempotency_key
                AND NEW.request_digest = head.request_digest
            )
        )
        AND (
            NEW.event_type NOT IN ('approve', 'reject')
            OR (
                NEW.actor_id = head.reviewed_by
                AND NEW.actor_id <> head.requested_by
                AND NEW.actor_id <> head.assessment_assessor_id
                AND NEW.occurred_at = head.reviewed_at
                AND NEW.reason = head.review_reason
                AND (
                    NEW.expires_at IS NULL
                    OR NEW.expires_at > NEW.occurred_at
                )
            )
        )
        AND (
            NEW.event_type <> 'revalidate'
            OR (
                NEW.actor_id <> head.requested_by
                AND NEW.actor_id <> head.assessment_assessor_id
                AND NEW.reason = NEW.revalidation_reason_code
            )
        )
        AND (
            NEW.event_type <> 'revoke'
            OR (
                NEW.actor_id = head.revoked_by
                AND NEW.occurred_at = head.revoked_at
            )
        )
  )
  OR (
      NEW."event_type" = 'request'
      AND (
          NEW."predecessor_event_id" IS NOT NULL
          OR EXISTS (
              SELECT 1 FROM "{waiver_event_table}" AS prior
              WHERE prior.waiver_id = NEW.waiver_id
          )
      )
  )
  OR (
      NEW."event_type" <> 'request'
      AND NOT EXISTS (
          SELECT 1
          FROM "{waiver_event_table}" AS predecessor
          WHERE predecessor.event_id = NEW.predecessor_event_id
            AND predecessor.waiver_id = NEW.waiver_id
            AND predecessor.board_id = NEW.board_id
            AND predecessor.waiver_revision = NEW.waiver_revision - 1
            AND predecessor.to_status = NEW.from_status
            AND (
                NEW.event_type <> 'revalidate'
                OR NEW.evaluated_at >= predecessor.occurred_at
            )
            AND (
                (
                    NEW.event_type <> 'approve'
                    AND NEW.expires_at IS predecessor.expires_at
                )
                OR (
                    NEW.event_type = 'approve'
                    AND (
                        NEW.expires_at IS NULL
                        OR NEW.expires_at > NEW.occurred_at
                    )
                )
            )
      )
  )
  OR (
      NEW."event_type" = 'expire'
      AND NEW."expire_reason_code" = 'scheduled_expiry'
      AND (
          NEW."expires_at" IS NULL
          OR NEW."occurred_at" < NEW."expires_at"
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_waiver_event_append_invalid');
END''',
    )
    add_immutable(
        table_name=waiver_event_table,
        board_expression='OLD."board_id"',
        message="semantic_guideline_waiver_event_immutable",
    )

    skip_insert_name = f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}_skip_insert"
    manifest[skip_insert_name] = (
        skip_table,
        f'''CREATE TRIGGER "{skip_insert_name}"
BEFORE INSERT ON "{skip_table}"
WHEN length(trim(NEW."reason")) = 0
  OR length(trim(NEW."actor_id")) = 0
  OR NEW."actor_kind" <> 'human'
  OR (
      NEW."event_type" = 'create'
      AND (
          NEW."from_status" IS NOT NULL
          OR NEW."created_by" <> NEW."actor_id"
          OR NEW."created_at" <> NEW."occurred_at"
          OR NEW."revoked_by" IS NOT NULL
          OR NEW."revoked_at" IS NOT NULL
          OR NEW."revocation_reason" IS NOT NULL
          OR
          EXISTS (
              SELECT 1
              FROM "{skip_table}" AS prior
              WHERE prior.skip_id = NEW.skip_id
          )
          OR EXISTS (
              SELECT 1
              FROM "{skip_table}" AS active
              WHERE active.board_id = NEW.board_id
                AND active.scope_digest = NEW.scope_digest
                AND active.binding_id = NEW.binding_id
                AND active.binding_revision = NEW.binding_revision
                AND active.configuration_digest = NEW.configuration_digest
                AND active.subject_type = NEW.subject_type
                AND active.subject_id = NEW.subject_id
                AND active.subject_version = NEW.subject_version
                AND active.subject_content_digest
                    = NEW.subject_content_digest
                AND active.guideline_id = NEW.guideline_id
                AND active.revision_id = NEW.revision_id
                AND active.revision_digest = NEW.revision_digest
                AND active.status = 'active'
                AND NOT EXISTS (
                    SELECT 1
                    FROM "{skip_table}" AS successor
                    WHERE successor.predecessor_event_id = active.event_id
                )
          )
      )
  )
  OR (
      NEW."event_type" = 'revoke'
      AND NOT EXISTS (
          SELECT 1
          FROM "{skip_table}" AS predecessor
          WHERE predecessor.event_id = NEW.predecessor_event_id
            AND predecessor.skip_id = NEW.skip_id
            AND predecessor.skip_revision = NEW.skip_revision - 1
            AND predecessor.status = 'active'
            AND NEW.from_status = predecessor.status
            AND predecessor.board_id = NEW.board_id
            AND predecessor.binding_id = NEW.binding_id
            AND predecessor.binding_revision = NEW.binding_revision
            AND predecessor.configuration_digest
                = NEW.configuration_digest
            AND predecessor.subject_type = NEW.subject_type
            AND predecessor.subject_id = NEW.subject_id
            AND predecessor.subject_version = NEW.subject_version
            AND predecessor.subject_content_digest
                = NEW.subject_content_digest
            AND predecessor.guideline_id = NEW.guideline_id
            AND predecessor.revision_id = NEW.revision_id
            AND predecessor.revision_digest = NEW.revision_digest
            AND predecessor.scope_digest = NEW.scope_digest
            AND NEW.reason IS predecessor.reason
            AND NEW.created_by IS predecessor.created_by
            AND NEW.created_at IS predecessor.created_at
            AND NEW.revoked_by IS NEW.actor_id
            AND NEW.revoked_at IS NEW.occurred_at
            AND length(trim(NEW.revocation_reason)) > 0
            AND predecessor.occurred_at <= NEW.occurred_at
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'semantic_guideline_skip_invalid');
END''',
    )
    add_immutable(
        table_name=skip_table,
        board_expression='OLD."board_id"',
        message="semantic_guideline_skip_immutable",
    )
    add_immutable(
        table_name=migration_table,
        board_expression='OLD."board_id"',
        message="semantic_guideline_migration_audit_immutable",
    )
    return manifest


def semantic_guideline_postgresql_ddl(
) -> tuple[str, dict[str, tuple[str, str, int]]]:
    """Return PostgreSQL guards equivalent to the SQLite SK-B3 authority.

    The returned trigger map stores ``table, operation clause, tgtype``.  Names
    are intentionally short because PostgreSQL truncates identifiers beyond
    63 bytes, which would otherwise make convergence auditing ambiguous.
    """

    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineAssessmentReceiptRow,
        SemanticGuidelineBindingConfigurationRow,
        SemanticGuidelineFindingRow,
        SemanticGuidelineLegacyMigrationRow,
        SemanticGuidelineMetricResultRow,
        SemanticGuidelineRevisionRow,
        SemanticGuidelineSkipRow,
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
        SemanticSubjectVersionEventRow,
        SemanticSubjectVersionRow,
    )

    function_name = "semantic_guideline_guard_v3"
    function_sql = f"""
CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
DECLARE
    metric jsonb;
    target text;
    override_entry record;
BEGIN
    IF TG_TABLE_NAME = 'semantic_guideline_revisions' THEN
        IF TG_OP = 'INSERT' THEN
            IF jsonb_typeof(NEW.metrics::jsonb) <> 'array'
               OR (
                   NEW.authority_state <> 'native'
                   AND jsonb_array_length(NEW.metrics::jsonb) <> 0
               )
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(NEW.metrics::jsonb) AS item(value)
                   WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'object'
                       OR jsonb_typeof(item.value->'metric_id')
                          IS DISTINCT FROM 'string'
                       OR btrim(item.value->>'metric_id') = ''
                       OR item.value->>'metric_id'
                          <> btrim(item.value->>'metric_id')
                       OR char_length(item.value->>'metric_id') > 64
                       OR lower(btrim(item.value->>'metric_id'))
                          = 'confidence'
                       OR jsonb_typeof(item.value->'code')
                          IS DISTINCT FROM 'string'
                       OR btrim(item.value->>'code') = ''
                       OR item.value->>'code' <> btrim(item.value->>'code')
                       OR char_length(item.value->>'code') > 128
                       OR lower(btrim(item.value->>'code')) = 'confidence'
                       OR item.value->>'code'
                          !~ '^[A-Za-z][A-Za-z0-9_.:-]*$'
                       OR jsonb_typeof(item.value->'title')
                          IS DISTINCT FROM 'string'
                       OR btrim(item.value->>'title') = ''
                       OR item.value->>'title' <> btrim(item.value->>'title')
                       OR char_length(item.value->>'title') > 500
                       OR lower(btrim(item.value->>'title')) = 'confidence'
                      OR jsonb_typeof(item.value->'description')
                         IS DISTINCT FROM 'string'
                      OR btrim(item.value->>'description') = ''
                      OR item.value->>'description'
                         <> btrim(item.value->>'description')
                      OR jsonb_typeof(item.value->'evaluation_rubric')
                         IS DISTINCT FROM 'string'
                      OR btrim(item.value->>'evaluation_rubric') = ''
                      OR item.value->>'evaluation_rubric'
                         <> btrim(item.value->>'evaluation_rubric')
                      OR jsonb_typeof(item.value->'target_entity_types')
                         IS DISTINCT FROM 'array'
                      OR jsonb_array_length(
                             item.value->'target_entity_types'
                         ) = 0
                      OR item.value->>'direction'
                         NOT IN ('minimum', 'maximum')
                      OR jsonb_typeof(item.value->'default_threshold')
                         IS DISTINCT FROM 'number'
                      OR (item.value->>'default_threshold') !~ '^[0-9]+$'
                      OR (item.value->>'default_threshold')::integer
                         NOT BETWEEN 0 AND 100
                      OR EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements_text(
                              item.value->'target_entity_types'
                          ) AS entity(value)
                           WHERE entity.value NOT IN (
                               'ideation', 'refinement', 'spec', 'card',
                               'sprint', 'test_scenario'
                           )
                       )
                       OR (
                           SELECT count(*)
                           FROM jsonb_array_elements_text(
                               item.value->'target_entity_types'
                           )
                       ) <> (
                           SELECT count(DISTINCT entity.value)
                           FROM jsonb_array_elements_text(
                               item.value->'target_entity_types'
                           ) AS entity(value)
                       )
                       OR item.value->'target_entity_types' <> (
                           SELECT jsonb_agg(entity.value ORDER BY entity.value)
                           FROM jsonb_array_elements_text(
                               item.value->'target_entity_types'
                           ) AS entity(value)
                       )
               )
               OR (
                   SELECT count(*)
                   FROM jsonb_array_elements(NEW.metrics::jsonb)
               ) <> (
                   SELECT count(DISTINCT item.value->>'metric_id')
                   FROM jsonb_array_elements(NEW.metrics::jsonb) AS item(value)
               )
               OR (
                   SELECT count(*)
                   FROM jsonb_array_elements(NEW.metrics::jsonb)
               ) <> (
                   SELECT count(DISTINCT lower(item.value->>'code'))
                   FROM jsonb_array_elements(NEW.metrics::jsonb) AS item(value)
               )
            THEN
                RAISE EXCEPTION 'semantic_guideline_metrics_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            IF EXISTS (
                SELECT 1
                FROM kg_board_erasure_permits AS permit
                JOIN guidelines AS guideline
                  ON guideline.board_id = permit.board_id
                WHERE guideline.id = OLD.guideline_id
                  AND guideline.scope = 'inline'
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'semantic_guideline_revision_immutable';
        END IF;
        RAISE EXCEPTION 'semantic_guideline_revision_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_binding_configurations' THEN
        IF TG_OP = 'INSERT' THEN
            IF jsonb_typeof(NEW.metric_threshold_overrides::jsonb) <> 'object'
               OR NOT EXISTS (
                   SELECT 1
                   FROM guideline_board_bindings AS legacy
                   WHERE legacy.binding_id = NEW.binding_id
                     AND legacy.binding_revision = NEW.binding_revision
                     AND legacy.board_id = NEW.board_id
                     AND legacy.guideline_id = NEW.guideline_id
                     AND legacy.revision_id = NEW.revision_id
               )
               OR EXISTS (
                   SELECT 1
                   FROM semantic_guideline_revisions AS revision
                   WHERE revision.guideline_id = NEW.guideline_id
                     AND revision.revision_id = NEW.revision_id
                     AND revision.revision_digest = NEW.revision_digest
                     AND revision.authority_state = 'legacy_incompatible'
               )
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_each(
                       NEW.metric_threshold_overrides::jsonb
                   ) AS item(key, value)
                   WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'number'
                      OR item.value::text !~ '^[0-9]+$'
                      OR item.value::text::integer NOT BETWEEN 0 AND 100
                      OR NOT EXISTS (
                          SELECT 1
                          FROM semantic_guideline_revisions AS revision,
                               jsonb_array_elements(
                                   revision.metrics::jsonb
                               ) AS metric(value)
                          WHERE revision.guideline_id = NEW.guideline_id
                            AND revision.revision_id = NEW.revision_id
                            AND revision.revision_digest = NEW.revision_digest
                            AND metric.value->>'code' = item.key
                      )
               )
               OR EXISTS (
                   SELECT 1
                   FROM guideline_board_bindings AS legacy
                   WHERE legacy.binding_id = NEW.binding_id
                     AND legacy.binding_revision = NEW.binding_revision
                     AND legacy.impact_adoption_id IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1
                         FROM guideline_impact_receipts AS receipt
                         WHERE receipt.impact_receipt_id =
                                 legacy.impact_receipt_id
                           AND receipt.board_id = NEW.board_id
                           AND receipt.guideline_id = NEW.guideline_id
                           AND receipt.to_revision_id = NEW.revision_id
                           AND receipt.to_revision_digest =
                                 NEW.revision_digest
                           AND receipt.proposed_enforcement =
                                 NEW.enforcement
                           AND receipt.proposed_minimum_confidence =
                                 NEW.minimum_confidence
                           AND receipt.proposed_metric_threshold_overrides::jsonb
                               = NEW.metric_threshold_overrides::jsonb
                           AND receipt.sealed = true
                     )
               )
            THEN
                RAISE EXCEPTION
                    'semantic_guideline_binding_configuration_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION
            'semantic_guideline_binding_configuration_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_subject_versions' THEN
        IF TG_OP = 'INSERT' THEN
            IF NEW.head_revision <> 1 THEN
                RAISE EXCEPTION
                    'semantic_subject_head_initial_revision_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.board_id IS NOT DISTINCT FROM OLD.board_id
               AND NEW.subject_type IS NOT DISTINCT FROM OLD.subject_type
               AND NEW.subject_id IS NOT DISTINCT FROM OLD.subject_id
               AND NEW.head_revision = OLD.head_revision + 1
               AND NEW.last_event_id <> OLD.last_event_id
               AND NEW.subject_version >= OLD.subject_version
               AND NEW.updated_at >= OLD.updated_at
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'semantic_subject_head_cas_invalid';
        ELSIF EXISTS (
            SELECT 1 FROM kg_board_erasure_permits AS permit
            WHERE permit.board_id = OLD.board_id
        ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_subject_head_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_subject_version_events' THEN
        IF TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
                   SELECT 1
                   FROM semantic_subject_versions AS head
                   WHERE head.board_id = NEW.board_id
                     AND head.subject_type = NEW.subject_type
                     AND head.subject_id = NEW.subject_id
                     AND head.subject_version = NEW.subject_version
                     AND head.content_digest = NEW.content_digest
                     AND head.last_semantic_editor_id =
                         NEW.last_semantic_editor_id
                     AND head.editor_source = NEW.editor_source
                     AND head.head_revision = NEW.head_revision
                     AND head.last_event_id = NEW.event_id
                     AND head.updated_at = NEW.changed_at
               )
               OR (
                   NEW.head_revision = 1
                   AND (
                       NEW.predecessor_event_id IS NOT NULL
                       OR (
                           NEW.editor_source = 'legacy_unknown'
                           AND NEW.event_type <> 'legacy_bootstrap'
                       )
                       OR (
                           NEW.editor_source = 'authoritative'
                           AND NEW.event_type <> 'semantic_mutation'
                       )
                   )
               )
               OR (
                   NEW.head_revision > 1
                   AND (
                       NEW.event_type <> 'semantic_mutation'
                       OR NOT EXISTS (
                           SELECT 1
                           FROM semantic_subject_version_events AS predecessor
                           WHERE predecessor.event_id =
                               NEW.predecessor_event_id
                             AND predecessor.board_id = NEW.board_id
                             AND predecessor.subject_type = NEW.subject_type
                             AND predecessor.subject_id = NEW.subject_id
                             AND predecessor.head_revision =
                                 NEW.head_revision - 1
                             AND predecessor.subject_version <=
                                 NEW.subject_version
                             AND predecessor.changed_at <= NEW.changed_at
                       )
                   )
               )
            THEN
                RAISE EXCEPTION 'semantic_subject_event_append_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_subject_event_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_assessment_receipts' THEN
        IF TG_OP = 'INSERT' THEN
            IF NEW.sealed THEN
                RAISE EXCEPTION
                    'semantic_guideline_assessment_initially_unsealed';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'UPDATE'
           AND OLD.sealed = FALSE
           AND NEW.sealed = TRUE
           AND (to_jsonb(NEW) - 'sealed')
               IS NOT DISTINCT FROM (to_jsonb(OLD) - 'sealed')
           AND NEW.metric_result_count = (
               SELECT count(*)
               FROM semantic_guideline_revisions AS revision,
                    jsonb_array_elements(
                        revision.metrics::jsonb
                    ) AS item(value)
               WHERE revision.guideline_id = NEW.guideline_id
                 AND revision.revision_id = NEW.revision_id
                 AND revision.revision_digest = NEW.revision_digest
                 AND item.value->'target_entity_types' ? NEW.subject_type
           )
           AND NEW.metric_result_count = (
               SELECT count(*)
               FROM semantic_guideline_metric_results AS result
               WHERE result.receipt_id = NEW.receipt_id
           )
           AND NEW.failed_metric_count = (
               SELECT count(*)
               FROM semantic_guideline_metric_results AS result
               WHERE result.receipt_id = NEW.receipt_id
                 AND result.outcome = 'fail'
           )
           AND NEW.failed_metric_count = (
               SELECT count(*)
               FROM semantic_guideline_findings AS finding
               WHERE finding.receipt_id = NEW.receipt_id
           )
        THEN
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_assessment_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_metric_results' THEN
        IF TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
                   SELECT 1
                   FROM semantic_guideline_assessment_receipts AS receipt
                   JOIN semantic_guideline_binding_configurations AS binding
                     ON binding.binding_id = receipt.binding_id
                    AND binding.binding_revision = receipt.binding_revision
                    AND binding.configuration_digest =
                        receipt.configuration_digest
                   JOIN semantic_guideline_revisions AS revision
                     ON revision.guideline_id = receipt.guideline_id
                    AND revision.revision_id = receipt.revision_id
                    AND revision.revision_digest = receipt.revision_digest,
                        jsonb_array_elements(
                            revision.metrics::jsonb
                        ) AS item(value)
                   WHERE receipt.receipt_id = NEW.receipt_id
                     AND receipt.sealed = FALSE
                     AND receipt.receipt_digest = NEW.receipt_digest
                     AND item.value->>'metric_id' = NEW.metric_id
                     AND item.value->>'code' = NEW.metric_code
                     AND item.value->>'direction' = NEW.direction
                     AND (item.value->>'default_threshold')::integer =
                         NEW.default_threshold
                     AND item.value->'target_entity_types' ?
                         receipt.subject_type
                     AND NEW.effective_threshold = COALESCE(
                         (
                             binding.metric_threshold_overrides::jsonb
                                 ->> NEW.metric_code
                         )::integer,
                         (item.value->>'default_threshold')::integer
                     )
                     AND NEW.threshold_source = CASE
                         WHEN binding.metric_threshold_overrides::jsonb
                                  ? NEW.metric_code
                         THEN 'override'
                         ELSE 'default'
                     END
               )
               OR jsonb_typeof(NEW.evidence_refs::jsonb) <> 'array'
               OR jsonb_array_length(NEW.evidence_refs::jsonb) = 0
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(
                       NEW.evidence_refs::jsonb
                   ) AS evidence(value)
                   WHERE jsonb_typeof(evidence.value)
                         IS DISTINCT FROM 'object'
                      OR jsonb_typeof(evidence.value->'source_type')
                         IS DISTINCT FROM 'string'
                      OR btrim(evidence.value->>'source_type') = ''
                      OR jsonb_typeof(evidence.value->'source_id')
                         IS DISTINCT FROM 'string'
                      OR btrim(evidence.value->>'source_id') = ''
                      OR jsonb_typeof(evidence.value->'source_version')
                         IS DISTINCT FROM 'number'
                      OR evidence.value->>'source_version' !~ '^[1-9][0-9]*$'
                      OR jsonb_typeof(evidence.value->'content_hash')
                         IS DISTINCT FROM 'string'
                      OR evidence.value->>'content_hash' !~ '^[0-9a-f]{{64}}$'
               )
               OR jsonb_typeof(NEW.pinpoints::jsonb) <> 'array'
               OR jsonb_array_length(NEW.pinpoints::jsonb) = 0
               OR EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(
                       NEW.pinpoints::jsonb
                   ) AS pinpoint(value)
                   WHERE jsonb_typeof(pinpoint.value)
                         IS DISTINCT FROM 'object'
                      OR pinpoint.value->>'anchor_type' NOT IN (
                          'whole_artifact', 'field', 'structured_child', 'qa'
                      )
                      OR jsonb_typeof(pinpoint.value->'subject')
                         IS DISTINCT FROM 'object'
                      OR jsonb_typeof(pinpoint.value->'subject'->'board_id')
                         IS DISTINCT FROM 'string'
                      OR pinpoint.value->'subject'->>'board_id'
                         <> NEW.board_id
                      OR jsonb_typeof(
                          pinpoint.value->'subject'->'subject_type'
                      )
                         IS DISTINCT FROM 'string'
                      OR pinpoint.value->'subject'->>'subject_type'
                         <> NEW.subject_type
                      OR jsonb_typeof(
                          pinpoint.value->'subject'->'subject_id'
                      )
                         IS DISTINCT FROM 'string'
                      OR pinpoint.value->'subject'->>'subject_id'
                         <> NEW.subject_id
                      OR jsonb_typeof(
                          pinpoint.value->'subject'->'subject_version'
                      )
                         IS DISTINCT FROM 'number'
                      OR (
                          pinpoint.value->'subject'->>'subject_version'
                      )::integer <>
                         NEW.subject_version
                      OR jsonb_typeof(pinpoint.value->'input_digest')
                         IS DISTINCT FROM 'string'
                      OR pinpoint.value->>'input_digest' <> (
                          SELECT receipt.input_digest
                          FROM semantic_guideline_assessment_receipts
                              AS receipt
                          WHERE receipt.receipt_id = NEW.receipt_id
                      )
                      OR (
                          pinpoint.value->>'anchor_type' = 'whole_artifact'
                          AND pinpoint.value ? 'anchor_ref'
                          AND jsonb_typeof(pinpoint.value->'anchor_ref') <>
                              'null'
                      )
                      OR (
                          pinpoint.value->>'anchor_type' <> 'whole_artifact'
                          AND (
                              jsonb_typeof(pinpoint.value->'anchor_ref')
                                  IS DISTINCT FROM 'string'
                              OR btrim(pinpoint.value->>'anchor_ref') = ''
                          )
                      )
               )
            THEN
                RAISE EXCEPTION
                    'semantic_guideline_metric_result_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_metric_result_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_findings' THEN
        IF TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
                SELECT 1
                FROM semantic_guideline_metric_results AS result
                JOIN semantic_guideline_assessment_receipts AS receipt
                  ON receipt.receipt_id = result.receipt_id
                 AND receipt.sealed = FALSE
                 AND receipt.receipt_digest = NEW.receipt_digest
                WHERE result.result_id = NEW.metric_result_id
                  AND result.receipt_id = NEW.receipt_id
                  AND result.board_id = NEW.board_id
                  AND result.subject_type = NEW.subject_type
                  AND result.subject_id = NEW.subject_id
                  AND result.subject_version = NEW.subject_version
                  AND result.subject_content_digest =
                      NEW.subject_content_digest
                  AND result.receipt_digest = NEW.receipt_digest
                  AND result.guideline_id = NEW.guideline_id
                  AND result.revision_id = NEW.revision_id
                  AND result.revision_digest = NEW.revision_digest
                  AND result.binding_id = NEW.binding_id
                  AND result.binding_revision = NEW.binding_revision
                  AND result.configuration_digest =
                      NEW.configuration_digest
                  AND result.metric_id = NEW.metric_id
                  AND result.metric_code = NEW.metric_code
                  AND result.result_digest = NEW.metric_result_digest
                  AND result.outcome = 'fail'
                  AND result.rationale = NEW.rationale
                  AND result.evidence_refs::jsonb =
                      NEW.evidence_refs::jsonb
                  AND result.pinpoints::jsonb = NEW.pinpoints::jsonb
                  AND result.created_at = NEW.created_at
            ) THEN
                RAISE EXCEPTION 'semantic_guideline_finding_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_finding_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_waivers' THEN
        IF TG_OP = 'INSERT' THEN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'semantic-guideline-waiver:' || NEW.board_id || ':' ||
                    NEW.scope_digest || ':' || NEW.metric_result_id || ':' ||
                    NEW.metric_result_digest || ':' || NEW.finding_id || ':' ||
                    NEW.finding_digest || ':' || NEW.receipt_id || ':' ||
                    NEW.receipt_digest,
                    0
                )
            );
            IF EXISTS (
                SELECT 1
                FROM semantic_guideline_waivers AS active
                WHERE active.board_id = NEW.board_id
                  AND active.scope_digest = NEW.scope_digest
                  AND active.status IN ('requested', 'approved')
                  AND (
                      active.expires_at IS NULL
                      OR active.expires_at > NEW.requested_at
                  )
            ) THEN
                RAISE EXCEPTION
                    'semantic_guideline_waiver_scope_conflict';
            END IF;
            IF NEW.status <> 'requested'
               OR NEW.waiver_revision <> 1
               OR NEW.last_event_type <> 'request'
               OR btrim(NEW.assessment_assessor_id) = ''
               OR (
                   NEW.expires_at IS NOT NULL
                   AND NEW.requested_at >= NEW.expires_at
               )
               OR jsonb_typeof(NEW.evidence_refs::jsonb) <> 'array'
               OR jsonb_array_length(NEW.evidence_refs::jsonb) = 0
               OR jsonb_typeof(
                   NEW.last_revalidation_currentness_reasons::jsonb
               ) <> 'array'
               OR jsonb_array_length(
                   NEW.last_revalidation_currentness_reasons::jsonb
               ) <> 0
               OR NOT EXISTS (
                   SELECT 1
                   FROM semantic_guideline_metric_results AS result
                   JOIN semantic_guideline_findings AS finding
                     ON finding.metric_result_id = result.result_id
                    AND finding.receipt_id = result.receipt_id
                    AND finding.receipt_digest = NEW.receipt_digest
                    AND finding.metric_result_digest = result.result_digest
                   JOIN semantic_guideline_assessment_receipts AS receipt
                     ON receipt.receipt_id = result.receipt_id
                    AND receipt.sealed = TRUE
                    AND receipt.receipt_digest = NEW.receipt_digest
                    AND receipt.assessor_agent_id =
                        NEW.assessment_assessor_id
                   WHERE result.result_id = NEW.metric_result_id
                     AND result.receipt_id = NEW.receipt_id
                     AND result.receipt_digest = NEW.receipt_digest
                     AND result.board_id = NEW.board_id
                     AND result.subject_type = NEW.subject_type
                     AND result.subject_id = NEW.subject_id
                     AND result.subject_version = NEW.subject_version
                     AND result.subject_content_digest =
                         NEW.subject_content_digest
                     AND result.guideline_id = NEW.guideline_id
                     AND result.revision_id = NEW.revision_id
                     AND result.revision_digest = NEW.revision_digest
                     AND result.binding_id = NEW.binding_id
                     AND result.binding_revision = NEW.binding_revision
                     AND result.configuration_digest =
                         NEW.configuration_digest
                     AND result.metric_id = NEW.metric_id
                     AND result.metric_code = NEW.metric_code
                     AND result.result_digest = NEW.metric_result_digest
                     AND finding.finding_id = NEW.finding_id
                     AND finding.finding_digest = NEW.finding_digest
                     AND result.outcome = 'fail'
                     AND result.created_at <= NEW.requested_at
               )
               OR EXISTS (
                   SELECT 1
                   FROM semantic_guideline_waivers AS active
                   WHERE active.board_id = NEW.board_id
                     AND active.scope_digest = NEW.scope_digest
                     AND active.metric_result_id = NEW.metric_result_id
                     AND active.metric_result_digest =
                         NEW.metric_result_digest
                     AND active.finding_id = NEW.finding_id
                     AND active.finding_digest = NEW.finding_digest
                     AND active.receipt_id = NEW.receipt_id
                     AND active.receipt_digest = NEW.receipt_digest
                     AND active.assessment_assessor_id =
                         NEW.assessment_assessor_id
                     AND active.binding_id = NEW.binding_id
                     AND active.binding_revision = NEW.binding_revision
                     AND active.configuration_digest =
                         NEW.configuration_digest
                     AND active.metric_id = NEW.metric_id
                     AND active.subject_type = NEW.subject_type
                     AND active.subject_id = NEW.subject_id
                     AND active.subject_version = NEW.subject_version
                     AND active.subject_content_digest =
                         NEW.subject_content_digest
                     AND active.guideline_id = NEW.guideline_id
                     AND active.revision_id = NEW.revision_id
                     AND active.revision_digest = NEW.revision_digest
                     AND active.status IN ('requested', 'approved')
                     AND (
                         active.expires_at IS NULL
                         OR active.expires_at > NEW.requested_at
                     )
               )
            THEN
                RAISE EXCEPTION
                    'semantic_guideline_waiver_request_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'UPDATE' THEN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'semantic-guideline-waiver:' || NEW.board_id || ':' ||
                    NEW.scope_digest || ':' || NEW.metric_result_id || ':' ||
                    NEW.metric_result_digest || ':' || NEW.finding_id || ':' ||
                    NEW.finding_digest || ':' || NEW.receipt_id || ':' ||
                    NEW.receipt_digest,
                    0
                )
            );
            IF (to_jsonb(NEW) - ARRAY[
                    'status', 'waiver_revision', 'expires_at',
                    'last_event_id', 'last_event_type', 'last_event_at',
                    'last_event_idempotency_key',
                    'reviewed_by', 'reviewed_at', 'review_reason',
                    'revoked_by', 'revoked_at', 'expire_reason_code',
                    'last_revalidation_status',
                    'last_revalidation_current',
                    'last_revalidation_reason_code',
                    'last_revalidation_evaluated_at',
                    'last_revalidation_currentness_reasons',
                    'last_revalidation_scheduled_expiry_observed',
                    'head_digest'
                ])
               IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                    'status', 'waiver_revision', 'expires_at',
                    'last_event_id', 'last_event_type', 'last_event_at',
                    'last_event_idempotency_key',
                    'reviewed_by', 'reviewed_at', 'review_reason',
                    'revoked_by', 'revoked_at', 'expire_reason_code',
                    'last_revalidation_status',
                    'last_revalidation_current',
                    'last_revalidation_reason_code',
                    'last_revalidation_evaluated_at',
                    'last_revalidation_currentness_reasons',
                    'last_revalidation_scheduled_expiry_observed',
                    'head_digest'
                ])
               AND NEW.waiver_revision = OLD.waiver_revision + 1
               AND NEW.last_event_id <> OLD.last_event_id
               AND NEW.last_event_at >= OLD.last_event_at
               AND (
                   NEW.last_event_type = 'revalidate'
                   OR (
                       NEW.last_revalidation_status IS NOT DISTINCT FROM
                           OLD.last_revalidation_status
                       AND NEW.last_revalidation_current IS NOT DISTINCT FROM
                           OLD.last_revalidation_current
                       AND NEW.last_revalidation_reason_code
                           IS NOT DISTINCT FROM
                           OLD.last_revalidation_reason_code
                       AND NEW.last_revalidation_evaluated_at
                           IS NOT DISTINCT FROM
                           OLD.last_revalidation_evaluated_at
                       AND NEW.last_revalidation_currentness_reasons::jsonb =
                           OLD.last_revalidation_currentness_reasons::jsonb
                       AND
                           NEW.last_revalidation_scheduled_expiry_observed =
                           OLD.last_revalidation_scheduled_expiry_observed
                   )
               )
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'semantic_guideline_waiver_head_cas_invalid';
        ELSIF EXISTS (
            SELECT 1 FROM kg_board_erasure_permits AS permit
            WHERE permit.board_id = OLD.board_id
        ) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_waiver_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_waiver_events' THEN
        IF TG_OP = 'INSERT' THEN
            IF btrim(NEW.actor_id) = ''
               OR btrim(NEW.reason) = ''
               OR jsonb_typeof(NEW.evidence_refs::jsonb) <> 'array'
               OR jsonb_array_length(NEW.evidence_refs::jsonb) = 0
               OR jsonb_typeof(NEW.currentness_reasons::jsonb) <> 'array'
               OR (
                   NEW.event_type <> 'revalidate'
                   AND jsonb_array_length(
                       NEW.currentness_reasons::jsonb
                   ) <> 0
               )
               OR (
                   NEW.event_type = 'revalidate'
                   AND (
                       NEW.scheduled_expiry_observed <>
                           (
                               NEW.expires_at IS NOT NULL
                               AND NEW.expires_at <= NEW.evaluated_at
                           )
                       OR (
                           NEW.revalidation_status IN (
                               'approved', 'expired'
                           )
                           AND jsonb_array_length(
                               NEW.currentness_reasons::jsonb
                           ) <> 0
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM jsonb_array_elements_text(
                               NEW.currentness_reasons::jsonb
                           ) AS reason(value)
                           WHERE reason.value NOT IN (
                               'current_snapshot_missing',
                               'subject_version_changed',
                               'subject_content_changed',
                               'guideline_revision_changed',
                               'guideline_revision_digest_changed',
                               'binding_revision_changed',
                               'binding_configuration_changed',
                               'policy_set_changed',
                               'binding_head_changed',
                               'input_digest_changed'
                           )
                       )
                       OR jsonb_array_length(
                           NEW.currentness_reasons::jsonb
                       ) <> (
                           SELECT count(DISTINCT reason.value)
                           FROM jsonb_array_elements_text(
                               NEW.currentness_reasons::jsonb
                           ) AS reason(value)
                       )
                       OR NEW.currentness_reasons::jsonb <> (
                           SELECT COALESCE(
                               jsonb_agg(
                                   reason.value
                                   ORDER BY CASE reason.value
                                     WHEN 'current_snapshot_missing' THEN 1
                                     WHEN 'subject_version_changed' THEN 2
                                     WHEN 'subject_content_changed' THEN 3
                                     WHEN 'guideline_revision_changed' THEN 4
                                     WHEN
                                       'guideline_revision_digest_changed'
                                       THEN 5
                                     WHEN 'binding_revision_changed' THEN 6
                                     WHEN
                                       'binding_configuration_changed'
                                       THEN 7
                                     WHEN 'policy_set_changed' THEN 8
                                     WHEN 'binding_head_changed' THEN 9
                                     WHEN 'input_digest_changed' THEN 10
                                   END
                               ),
                               '[]'::jsonb
                           )
                           FROM jsonb_array_elements_text(
                               NEW.currentness_reasons::jsonb
                           ) AS reason(value)
                       )
                   )
               )
               OR NOT EXISTS (
                   SELECT 1
                   FROM semantic_guideline_waivers AS head
                   WHERE head.waiver_id = NEW.waiver_id
                     AND head.board_id = NEW.board_id
                     AND head.waiver_revision = NEW.waiver_revision
                     AND head.last_event_id = NEW.event_id
                     AND head.last_event_type = NEW.event_type
                     AND head.last_event_at = NEW.occurred_at
                     AND head.last_event_idempotency_key =
                         NEW.idempotency_key
                     AND head.status = NEW.to_status
                     AND head.expires_at IS NOT DISTINCT FROM NEW.expires_at
                     AND head.expire_reason_code IS NOT DISTINCT FROM
                         NEW.expire_reason_code
                     AND head.scope_digest = NEW.scope_digest
                     AND head.head_digest = NEW.waiver_digest
                     AND head.reviewed_by IS NOT DISTINCT FROM NEW.reviewed_by
                     AND head.reviewed_at IS NOT DISTINCT FROM NEW.reviewed_at
                     AND head.review_reason IS NOT DISTINCT FROM
                         NEW.review_reason
                     AND head.revoked_by IS NOT DISTINCT FROM NEW.revoked_by
                     AND head.revoked_at IS NOT DISTINCT FROM NEW.revoked_at
                     AND (
                         NEW.event_type <> 'revalidate'
                         OR (
                             head.last_revalidation_status
                                 IS NOT DISTINCT FROM
                                 NEW.revalidation_status
                             AND head.last_revalidation_current
                                 IS NOT DISTINCT FROM
                                 NEW.revalidation_current
                             AND head.last_revalidation_reason_code
                                 IS NOT DISTINCT FROM
                                 NEW.revalidation_reason_code
                             AND head.last_revalidation_evaluated_at
                                 IS NOT DISTINCT FROM NEW.evaluated_at
                             AND
                                 head.last_revalidation_currentness_reasons
                                 ::jsonb =
                                 NEW.currentness_reasons::jsonb
                             AND
                                 head.last_revalidation_scheduled_expiry_observed =
                                 NEW.scheduled_expiry_observed
                         )
                     )
                     AND (
                         NEW.event_type <> 'request'
                         OR (
                             NEW.actor_id = head.requested_by
                             AND NEW.occurred_at = head.requested_at
                             AND NEW.reason = head.justification
                             AND NEW.evidence_refs::jsonb =
                                 head.evidence_refs::jsonb
                             AND NEW.expires_at IS NOT DISTINCT FROM
                                 head.original_expires_at
                             AND NEW.idempotency_key = head.idempotency_key
                             AND NEW.request_digest = head.request_digest
                         )
                     )
                     AND (
                         NEW.event_type NOT IN ('approve', 'reject')
                         OR (
                             NEW.actor_id = head.reviewed_by
                             AND NEW.actor_id <> head.requested_by
                             AND NEW.actor_id <>
                                 head.assessment_assessor_id
                             AND NEW.occurred_at = head.reviewed_at
                             AND NEW.reason = head.review_reason
                             AND (
                                 NEW.expires_at IS NULL
                                 OR NEW.expires_at > NEW.occurred_at
                             )
                         )
                     )
                     AND (
                         NEW.event_type <> 'revalidate'
                         OR (
                             NEW.actor_id <> head.requested_by
                             AND NEW.actor_id <>
                                 head.assessment_assessor_id
                             AND NEW.reason =
                                 NEW.revalidation_reason_code
                         )
                     )
                     AND (
                         NEW.event_type <> 'revoke'
                         OR (
                             NEW.actor_id = head.revoked_by
                             AND NEW.occurred_at = head.revoked_at
                         )
                     )
               )
               OR (
                   NEW.event_type = 'request'
                   AND (
                       NEW.predecessor_event_id IS NOT NULL
                       OR EXISTS (
                           SELECT 1
                           FROM semantic_guideline_waiver_events AS prior
                           WHERE prior.waiver_id = NEW.waiver_id
                       )
                   )
               )
               OR (
                   NEW.event_type <> 'request'
                   AND NOT EXISTS (
                       SELECT 1
                       FROM semantic_guideline_waiver_events AS predecessor
                       WHERE predecessor.event_id =
                           NEW.predecessor_event_id
                         AND predecessor.waiver_id = NEW.waiver_id
                         AND predecessor.board_id = NEW.board_id
                         AND predecessor.waiver_revision =
                             NEW.waiver_revision - 1
                         AND predecessor.to_status = NEW.from_status
                         AND (
                             NEW.event_type <> 'revalidate'
                             OR NEW.evaluated_at >=
                                 predecessor.occurred_at
                         )
                         AND (
                             (
                                 NEW.event_type <> 'approve'
                                 AND NEW.expires_at IS NOT DISTINCT FROM
                                     predecessor.expires_at
                             )
                             OR (
                                 NEW.event_type = 'approve'
                                 AND (
                                     NEW.expires_at IS NULL
                                     OR NEW.expires_at > NEW.occurred_at
                                 )
                             )
                         )
                   )
               )
               OR (
                   NEW.event_type = 'expire'
                   AND NEW.expire_reason_code = 'scheduled_expiry'
                   AND (
                       NEW.expires_at IS NULL
                       OR NEW.occurred_at < NEW.expires_at
                   )
               )
            THEN
                RAISE EXCEPTION
                    'semantic_guideline_waiver_event_append_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_waiver_event_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_skips' THEN
        IF TG_OP = 'INSERT' THEN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'semantic-guideline-skip:' || NEW.board_id || ':' ||
                    NEW.scope_digest,
                    0
                )
            );
            IF btrim(NEW.reason) = ''
               OR btrim(NEW.actor_id) = ''
               OR NEW.actor_kind <> 'human'
               OR (
                   NEW.event_type = 'create'
                   AND (
                       NEW.from_status IS NOT NULL
                       OR NEW.created_by <> NEW.actor_id
                       OR NEW.created_at <> NEW.occurred_at
                       OR NEW.revoked_by IS NOT NULL
                       OR NEW.revoked_at IS NOT NULL
                       OR NEW.revocation_reason IS NOT NULL
                       OR EXISTS (
                           SELECT 1
                           FROM semantic_guideline_skips AS prior
                           WHERE prior.skip_id = NEW.skip_id
                       )
                       OR EXISTS (
                           SELECT 1
                            FROM semantic_guideline_skips AS active
                            WHERE active.board_id = NEW.board_id
                              AND active.scope_digest = NEW.scope_digest
                             AND active.binding_id = NEW.binding_id
                             AND active.binding_revision =
                                 NEW.binding_revision
                             AND active.configuration_digest =
                                 NEW.configuration_digest
                             AND active.subject_type = NEW.subject_type
                             AND active.subject_id = NEW.subject_id
                             AND active.subject_version =
                                 NEW.subject_version
                             AND active.subject_content_digest =
                                 NEW.subject_content_digest
                             AND active.guideline_id = NEW.guideline_id
                             AND active.revision_id = NEW.revision_id
                             AND active.revision_digest =
                                 NEW.revision_digest
                             AND active.status = 'active'
                             AND NOT EXISTS (
                                 SELECT 1
                                 FROM semantic_guideline_skips AS successor
                                 WHERE successor.predecessor_event_id =
                                     active.event_id
                             )
                       )
                   )
               )
               OR (
                   NEW.event_type = 'revoke'
                   AND NOT EXISTS (
                       SELECT 1
                       FROM semantic_guideline_skips AS predecessor
                       WHERE predecessor.event_id =
                           NEW.predecessor_event_id
                         AND predecessor.skip_id = NEW.skip_id
                         AND predecessor.skip_revision =
                             NEW.skip_revision - 1
                          AND predecessor.status = 'active'
                          AND NEW.from_status = predecessor.status
                         AND predecessor.board_id = NEW.board_id
                         AND predecessor.binding_id = NEW.binding_id
                         AND predecessor.binding_revision =
                             NEW.binding_revision
                         AND predecessor.configuration_digest =
                             NEW.configuration_digest
                         AND predecessor.subject_type = NEW.subject_type
                         AND predecessor.subject_id = NEW.subject_id
                         AND predecessor.subject_version =
                             NEW.subject_version
                         AND predecessor.subject_content_digest =
                             NEW.subject_content_digest
                         AND predecessor.guideline_id = NEW.guideline_id
                         AND predecessor.revision_id = NEW.revision_id
                          AND predecessor.revision_digest =
                              NEW.revision_digest
                          AND predecessor.scope_digest = NEW.scope_digest
                          AND NEW.reason IS NOT DISTINCT FROM
                              predecessor.reason
                          AND NEW.created_by IS NOT DISTINCT FROM
                              predecessor.created_by
                          AND NEW.created_at IS NOT DISTINCT FROM
                              predecessor.created_at
                          AND NEW.revoked_by = NEW.actor_id
                          AND NEW.revoked_at = NEW.occurred_at
                          AND btrim(NEW.revocation_reason) <> ''
                          AND predecessor.occurred_at <= NEW.occurred_at
                   )
               )
            THEN
                RAISE EXCEPTION 'semantic_guideline_skip_invalid';
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE'
              AND EXISTS (
                  SELECT 1 FROM kg_board_erasure_permits AS permit
                  WHERE permit.board_id = OLD.board_id
              )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_skip_immutable';
    END IF;

    IF TG_TABLE_NAME = 'semantic_guideline_legacy_migrations' THEN
        IF TG_OP = 'DELETE'
           AND OLD.board_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM kg_board_erasure_permits AS permit
               WHERE permit.board_id = OLD.board_id
           )
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'semantic_guideline_migration_audit_immutable';
    END IF;

    RAISE EXCEPTION 'semantic_guideline_guard_unknown_table';
END;
$$ LANGUAGE plpgsql
""".strip()

    all_operations = "INSERT OR UPDATE OR DELETE"
    mutation_operations = "UPDATE OR DELETE"
    trigger_specs: dict[str, tuple[str, str, int]] = {
        "trg_sgv3_revision": (
            SemanticGuidelineRevisionRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_binding": (
            SemanticGuidelineBindingConfigurationRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_subject_head": (
            SemanticSubjectVersionRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_subject_event": (
            SemanticSubjectVersionEventRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_receipt": (
            SemanticGuidelineAssessmentReceiptRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_metric": (
            SemanticGuidelineMetricResultRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_finding": (
            SemanticGuidelineFindingRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_waiver": (
            SemanticGuidelineWaiverRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_waiver_event": (
            SemanticGuidelineWaiverEventRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_skip": (
            SemanticGuidelineSkipRow.__tablename__,
            all_operations,
            31,
        ),
        "trg_sgv3_migration": (
            SemanticGuidelineLegacyMigrationRow.__tablename__,
            mutation_operations,
            27,
        ),
    }
    if any(len(name.encode("utf-8")) > 63 for name in trigger_specs):
        raise RuntimeError(
            "semantic guideline PostgreSQL trigger name exceeds 63 bytes"
        )
    return function_sql, trigger_specs


def cognitive_source_immutability_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Return the exact SQLite guard manifest for the append-only ledger."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        KGCognitiveSource,
        KGCognitiveSourceRevision,
    )

    expected: dict[str, tuple[str, str]] = {}
    for table_name in (
        KGCognitiveSource.__tablename__,
        KGCognitiveSourceRevision.__tablename__,
    ):
        for operation in ("update", "delete"):
            trigger_name = (
                f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{table_name}_{operation}"
            )
            erasure_guard = ""
            if allow_board_erasure and operation == "delete":
                if table_name == KGCognitiveSource.__tablename__:
                    erasure_guard = (
                        "\nWHEN NOT EXISTS (\n"
                        "    SELECT 1\n"
                        f'    FROM "{BoardErasurePermit.__tablename__}" AS permit\n'
                        "    WHERE permit.board_id = OLD.board_id\n"
                        ")"
                    )
                else:
                    erasure_guard = (
                        "\nWHEN NOT EXISTS (\n"
                        "    SELECT 1\n"
                        f'    FROM "{BoardErasurePermit.__tablename__}" AS permit\n'
                        f'    JOIN "{KGCognitiveSource.__tablename__}" AS source\n'
                        "      ON source.board_id = permit.board_id\n"
                        "    WHERE source.id = OLD.cognitive_source_id\n"
                        ")"
                    )
            trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{erasure_guard}
BEGIN
    SELECT RAISE(ABORT, 'kg_cognitive_source_immutable');
END'''
            expected[trigger_name] = (table_name, trigger_sql)
    return expected


def guideline_policy_immutability_trigger_manifest(
    *,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Return exact SQLite guards for the SK-B append-only authority."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        Guideline,
        GuidelineBoardBindingRow,
        GuidelineHeadRow,
        GuidelineRetirementRow,
        GuidelineRevisionRow,
    )

    revision_table = GuidelineRevisionRow.__tablename__
    head_table = GuidelineHeadRow.__tablename__
    binding_table = GuidelineBoardBindingRow.__tablename__
    retirement_table = GuidelineRetirementRow.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    guideline_table = Guideline.__tablename__
    expected: dict[str, tuple[str, str]] = {}

    revision_insert = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_revision_insert"
    expected[revision_insert] = (
        revision_table,
        f'''CREATE TRIGGER "{revision_insert}"
BEFORE INSERT ON "{revision_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_retired')
    WHERE EXISTS (
        SELECT 1
        FROM "{retirement_table}" AS retirement
        WHERE retirement.guideline_id = NEW.guideline_id
    );
END''',
    )
    revision_update = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_revision_update"
    expected[revision_update] = (
        revision_table,
        f'''CREATE TRIGGER "{revision_update}"
BEFORE UPDATE ON "{revision_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_revision_immutable');
END''',
    )
    revision_delete = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_revision_delete"
    revision_delete_when = ""
    if allow_board_erasure:
        revision_delete_when = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            f'    JOIN "{guideline_table}" AS guideline\n'
            "      ON guideline.board_id = permit.board_id\n"
            "    WHERE guideline.id = OLD.guideline_id\n"
            "      AND guideline.scope = 'inline'\n"
            ")"
        )
    expected[revision_delete] = (
        revision_table,
        f'''CREATE TRIGGER "{revision_delete}"
BEFORE DELETE ON "{revision_table}"{revision_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_revision_immutable');
END''',
    )

    head_update = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_head_update"
    expected[head_update] = (
        head_table,
        f'''CREATE TRIGGER "{head_update}"
BEFORE UPDATE ON "{head_table}"
WHEN EXISTS (
    SELECT 1
    FROM "{retirement_table}" AS retirement
    WHERE retirement.guideline_id = OLD.guideline_id
)
  OR NEW.guideline_id <> OLD.guideline_id
  OR NEW.head_revision <> OLD.head_revision + 1
  OR NEW.revision_number <> OLD.revision_number + 1
  OR NEW.revision_id = OLD.revision_id
BEGIN
    SELECT RAISE(ABORT, 'guideline_head_cas_invalid');
END''',
    )
    head_delete = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_head_delete"
    head_delete_when = ""
    if allow_board_erasure:
        head_delete_when = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            f'    JOIN "{guideline_table}" AS guideline\n'
            "      ON guideline.board_id = permit.board_id\n"
            "    WHERE guideline.id = OLD.guideline_id\n"
            "      AND guideline.scope = 'inline'\n"
            ")"
        )
    expected[head_delete] = (
        head_table,
        f'''CREATE TRIGGER "{head_delete}"
BEFORE DELETE ON "{head_table}"{head_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_head_immutable');
END''',
    )

    binding_insert = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_binding_insert"
    expected[binding_insert] = (
        binding_table,
        f'''CREATE TRIGGER "{binding_insert}"
BEFORE INSERT ON "{binding_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_retired')
    WHERE EXISTS (
        SELECT 1
        FROM "{retirement_table}" AS retirement
        WHERE retirement.guideline_id = NEW.guideline_id
    )
      AND NOT (
        NEW.state = 'unlinked'
        AND NEW.binding_revision > 1
        AND (
            SELECT previous.state
            FROM "{binding_table}" AS previous
            WHERE previous.binding_id = NEW.binding_id
              AND previous.binding_revision = NEW.binding_revision - 1
        ) = 'active'
      );
    SELECT RAISE(ABORT, 'guideline_binding_scope_invalid')
    WHERE NOT EXISTS (
        SELECT 1
        FROM "{guideline_table}" AS guideline
        WHERE guideline.id = NEW.guideline_id
          AND (
            (guideline.scope = 'global' AND guideline.board_id IS NULL)
            OR (
                guideline.scope = 'inline'
                AND guideline.board_id = NEW.board_id
            )
          )
    );
    SELECT RAISE(ABORT, 'guideline_binding_identity_reused')
    WHERE EXISTS (
        SELECT 1
        FROM "{binding_table}" AS previous
        WHERE previous.binding_id = NEW.binding_id
          AND (
            previous.board_id <> NEW.board_id
            OR previous.guideline_id <> NEW.guideline_id
          )
    );
    SELECT RAISE(ABORT, 'guideline_binding_sequence_invalid')
    WHERE (
        NEW.binding_revision = 1
        AND EXISTS (
            SELECT 1
            FROM "{binding_table}" AS previous
            WHERE previous.binding_id = NEW.binding_id
        )
    ) OR (
        NEW.binding_revision > 1
        AND (
            COALESCE((
                SELECT MAX(previous.binding_revision)
                FROM "{binding_table}" AS previous
                WHERE previous.binding_id = NEW.binding_id
            ), 0) <> NEW.binding_revision - 1
            OR NOT EXISTS (
                SELECT 1
                FROM "{binding_table}" AS previous
                WHERE previous.binding_id = NEW.binding_id
                  AND previous.binding_revision = NEW.binding_revision - 1
                  AND previous.board_id = NEW.board_id
                  AND previous.guideline_id = NEW.guideline_id
            )
        )
    );
    SELECT RAISE(ABORT, 'guideline_binding_state_transition_invalid')
    WHERE (
        NEW.binding_revision = 1
        AND NEW.state <> 'active'
    ) OR (
        NEW.binding_revision > 1
        AND (
            (
                NEW.state = 'unlinked'
                AND (
                    NEW.revision_id <> (
                        SELECT previous.revision_id
                        FROM "{binding_table}" AS previous
                        WHERE previous.binding_id = NEW.binding_id
                          AND previous.binding_revision = NEW.binding_revision - 1
                    )
                    OR NEW.semantic_version <> (
                        SELECT previous.semantic_version
                        FROM "{binding_table}" AS previous
                        WHERE previous.binding_id = NEW.binding_id
                          AND previous.binding_revision = NEW.binding_revision - 1
                    )
                    OR NEW.revision_digest <> (
                        SELECT previous.revision_digest
                        FROM "{binding_table}" AS previous
                        WHERE previous.binding_id = NEW.binding_id
                          AND previous.binding_revision = NEW.binding_revision - 1
                    )
                    OR NEW.priority <> (
                        SELECT previous.priority
                        FROM "{binding_table}" AS previous
                        WHERE previous.binding_id = NEW.binding_id
                          AND previous.binding_revision = NEW.binding_revision - 1
                    )
                    OR NEW.enforcement <> (
                        SELECT previous.enforcement
                        FROM "{binding_table}" AS previous
                        WHERE previous.binding_id = NEW.binding_id
                          AND previous.binding_revision = NEW.binding_revision - 1
                    )
                )
            )
            OR (
            (
                SELECT previous.state
                FROM "{binding_table}" AS previous
                WHERE previous.binding_id = NEW.binding_id
                  AND previous.binding_revision = NEW.binding_revision - 1
            ) = 'unlinked'
            AND NEW.state <> 'active'
            )
        )
    );
END''',
    )
    binding_update = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_binding_update"
    expected[binding_update] = (
        binding_table,
        f'''CREATE TRIGGER "{binding_update}"
BEFORE UPDATE ON "{binding_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_binding_immutable');
END''',
    )
    binding_delete = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_binding_delete"
    binding_delete_when = ""
    if allow_board_erasure:
        binding_delete_when = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            "    WHERE permit.board_id = OLD.board_id\n"
            ")"
        )
    expected[binding_delete] = (
        binding_table,
        f'''CREATE TRIGGER "{binding_delete}"
BEFORE DELETE ON "{binding_table}"{binding_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_binding_immutable');
END''',
    )
    retirement_insert = (
        f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_retirement_insert"
    )
    expected[retirement_insert] = (
        retirement_table,
        f'''CREATE TRIGGER "{retirement_insert}"
BEFORE INSERT ON "{retirement_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_retirement_head_conflict')
    WHERE NOT EXISTS (
        SELECT 1
        FROM "{head_table}" AS head
        WHERE head.guideline_id = NEW.guideline_id
          AND head.revision_id = NEW.retired_revision_id
          AND head.revision_number = NEW.retired_revision_number
          AND head.semantic_version = NEW.retired_semantic_version
          AND head.head_revision = NEW.retired_head_revision
    );
    SELECT RAISE(ABORT, 'guideline_supersedence_successor_invalid')
    WHERE NEW.status = 'superseded'
      AND NOT EXISTS (
        SELECT 1
        FROM "{guideline_table}" AS successor
        WHERE successor.id = NEW.superseded_by_guideline_id
          AND successor.scope = 'global'
          AND successor.board_id IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM "{retirement_table}" AS successor_retirement
            WHERE successor_retirement.guideline_id = successor.id
          )
      );
END''',
    )
    retirement_update = (
        f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_retirement_update"
    )
    expected[retirement_update] = (
        retirement_table,
        f'''CREATE TRIGGER "{retirement_update}"
BEFORE UPDATE ON "{retirement_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_retirement_immutable');
END''',
    )
    retirement_delete = (
        f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_retirement_delete"
    )
    retirement_delete_when = ""
    if allow_board_erasure:
        retirement_delete_when = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            f'    JOIN "{guideline_table}" AS guideline\n'
            "      ON guideline.board_id = permit.board_id\n"
            "    WHERE guideline.id = OLD.guideline_id\n"
            "      AND guideline.scope = 'inline'\n"
            ")"
        )
    expected[retirement_delete] = (
        retirement_table,
        f'''CREATE TRIGGER "{retirement_delete}"
BEFORE DELETE ON "{retirement_table}"{retirement_delete_when}
BEGIN
    SELECT RAISE(ABORT, 'guideline_retirement_immutable');
END''',
    )
    return expected


def guideline_policy_b03_sqlite_trigger_predecessors() -> dict[str, tuple[str, str]]:
    """Exact B03 trigger bodies replaced by the B04 lifecycle contract.

    Only these two installed B03 triggers changed shape.  Keeping their exact
    predecessor SQL makes the upgrade convergent without accepting arbitrary
    trigger drift under the owned prefix.
    """

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineBoardBindingRow,
        GuidelineHeadRow,
    )

    prefix = GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX
    head_table = GuidelineHeadRow.__tablename__
    binding_table = GuidelineBoardBindingRow.__tablename__
    head_update = f"{prefix}_head_update"
    binding_insert = f"{prefix}_binding_insert"
    return {
        head_update: (
            head_table,
            f'''CREATE TRIGGER "{head_update}"
BEFORE UPDATE ON "{head_table}"
WHEN NEW.guideline_id <> OLD.guideline_id
  OR NEW.head_revision <> OLD.head_revision + 1
  OR NEW.revision_number <> OLD.revision_number + 1
  OR NEW.revision_id = OLD.revision_id
BEGIN
    SELECT RAISE(ABORT, 'guideline_head_cas_invalid');
END''',
        ),
        binding_insert: (
            binding_table,
            f'''CREATE TRIGGER "{binding_insert}"
BEFORE INSERT ON "{binding_table}"
BEGIN
    SELECT RAISE(ABORT, 'guideline_binding_scope_invalid')
    WHERE NOT EXISTS (
        SELECT 1
        FROM "guidelines" AS guideline
        WHERE guideline.id = NEW.guideline_id
          AND (
            (guideline.scope = 'global' AND guideline.board_id IS NULL)
            OR (
                guideline.scope = 'inline'
                AND guideline.board_id = NEW.board_id
            )
          )
    );
    SELECT RAISE(ABORT, 'guideline_binding_identity_reused')
    WHERE EXISTS (
        SELECT 1
        FROM "{binding_table}" AS previous
        WHERE previous.binding_id = NEW.binding_id
          AND (
            previous.board_id <> NEW.board_id
            OR previous.guideline_id <> NEW.guideline_id
          )
    );
    SELECT RAISE(ABORT, 'guideline_binding_sequence_invalid')
    WHERE (
        NEW.binding_revision = 1
        AND EXISTS (
            SELECT 1
            FROM "{binding_table}" AS previous
            WHERE previous.binding_id = NEW.binding_id
        )
    ) OR (
        NEW.binding_revision > 1
        AND (
            COALESCE((
                SELECT MAX(previous.binding_revision)
                FROM "{binding_table}" AS previous
                WHERE previous.binding_id = NEW.binding_id
            ), 0) <> NEW.binding_revision - 1
            OR NOT EXISTS (
                SELECT 1
                FROM "{binding_table}" AS previous
                WHERE previous.binding_id = NEW.binding_id
                  AND previous.binding_revision = NEW.binding_revision - 1
                  AND previous.board_id = NEW.board_id
                  AND previous.guideline_id = NEW.guideline_id
            )
        )
    );
END''',
        ),
    }


def guideline_policy_postgresql_immutability_ddl() -> tuple[str, ...]:
    """Return PostgreSQL functions/triggers equivalent to the SQLite guards."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        Guideline,
        GuidelineBoardBindingRow,
        GuidelineHeadRow,
        GuidelineRetirementRow,
        GuidelineRevisionRow,
    )

    revision_table = GuidelineRevisionRow.__tablename__
    head_table = GuidelineHeadRow.__tablename__
    binding_table = GuidelineBoardBindingRow.__tablename__
    retirement_table = GuidelineRetirementRow.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    guideline_table = Guideline.__tablename__
    revision_function = "pulse_guideline_revision_immutable_guard"
    head_function = "pulse_guideline_head_guard"
    binding_function = "pulse_guideline_binding_immutable_guard"
    retirement_function = "pulse_guideline_retirement_immutable_guard"
    revision_trigger = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_revision_guard"
    head_trigger = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_head_guard"
    binding_trigger = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_binding_guard"
    retirement_trigger = (
        f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_retirement_guard"
    )
    return (
        f'''CREATE OR REPLACE FUNCTION "{revision_function}"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF EXISTS (
            SELECT 1
            FROM "{retirement_table}" AS retirement
            WHERE retirement.guideline_id = NEW.guideline_id
        ) THEN
            RAISE EXCEPTION 'guideline_retired'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM "{permit_table}" AS permit
        JOIN "{guideline_table}" AS guideline
          ON guideline.board_id = permit.board_id
        WHERE guideline.id = OLD.guideline_id
          AND guideline.scope = 'inline'
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guideline_revision_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$''',
        f'''CREATE OR REPLACE FUNCTION "{head_function}"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.guideline_id = OLD.guideline_id
       AND NEW.head_revision = OLD.head_revision + 1
       AND NEW.revision_number = OLD.revision_number + 1
       AND NEW.revision_id <> OLD.revision_id
       AND NOT EXISTS (
           SELECT 1
           FROM "{retirement_table}" AS retirement
           WHERE retirement.guideline_id = OLD.guideline_id
       )
    THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM "{permit_table}" AS permit
        JOIN "{guideline_table}" AS guideline
          ON guideline.board_id = permit.board_id
        WHERE guideline.id = OLD.guideline_id
          AND guideline.scope = 'inline'
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guideline_head_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$''',
        f'''CREATE OR REPLACE FUNCTION "{binding_function}"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    identity_scope text;
    identity_board_id text;
    previous_max integer;
    previous_state text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT previous.state
        INTO previous_state
        FROM "{binding_table}" AS previous
        WHERE previous.binding_id = NEW.binding_id
          AND previous.binding_revision = NEW.binding_revision - 1;
        IF EXISTS (
            SELECT 1
            FROM "{retirement_table}" AS retirement
            WHERE retirement.guideline_id = NEW.guideline_id
        ) AND NOT (
            NEW.state = 'unlinked'
            AND NEW.binding_revision > 1
            AND previous_state = 'active'
        ) THEN
            RAISE EXCEPTION 'guideline_retired'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT guideline.scope, guideline.board_id
        INTO identity_scope, identity_board_id
        FROM "{guideline_table}" AS guideline
        WHERE guideline.id = NEW.guideline_id;
        IF NOT FOUND OR NOT (
            (identity_scope = 'global' AND identity_board_id IS NULL)
            OR (
                identity_scope = 'inline'
                AND identity_board_id = NEW.board_id
            )
        ) THEN
            RAISE EXCEPTION 'guideline_binding_scope_invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT MAX(previous.binding_revision)
        INTO previous_max
        FROM "{binding_table}" AS previous
        WHERE previous.binding_id = NEW.binding_id;
        IF NEW.binding_revision = 1 THEN
            IF previous_max IS NOT NULL THEN
                RAISE EXCEPTION 'guideline_binding_identity_reused'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSIF previous_max IS DISTINCT FROM NEW.binding_revision - 1
           OR NOT EXISTS (
               SELECT 1
               FROM "{binding_table}" AS previous
               WHERE previous.binding_id = NEW.binding_id
                 AND previous.binding_revision = NEW.binding_revision - 1
                 AND previous.board_id = NEW.board_id
                 AND previous.guideline_id = NEW.guideline_id
           )
        THEN
            RAISE EXCEPTION 'guideline_binding_sequence_invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.binding_revision > 1 AND EXISTS (
            SELECT 1
            FROM "{binding_table}" AS previous
            WHERE previous.binding_id = NEW.binding_id
              AND previous.binding_revision = NEW.binding_revision - 1
              AND NEW.state = 'unlinked'
              AND (
                  NEW.revision_id IS DISTINCT FROM previous.revision_id
                  OR NEW.semantic_version
                     IS DISTINCT FROM previous.semantic_version
                  OR NEW.revision_digest
                     IS DISTINCT FROM previous.revision_digest
                  OR NEW.priority IS DISTINCT FROM previous.priority
                  OR NEW.enforcement
                     IS DISTINCT FROM previous.enforcement
              )
        ) THEN
            RAISE EXCEPTION 'guideline_binding_state_transition_invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF (NEW.binding_revision = 1 AND NEW.state <> 'active')
           OR (
               NEW.binding_revision > 1
               AND previous_state = 'unlinked'
               AND NEW.state <> 'active'
           )
        THEN
            RAISE EXCEPTION 'guideline_binding_state_transition_invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM "{permit_table}" AS permit
        WHERE permit.board_id = OLD.board_id
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guideline_binding_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$''',
        f'''CREATE OR REPLACE FUNCTION "{retirement_function}"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM "{head_table}" AS head
            WHERE head.guideline_id = NEW.guideline_id
              AND head.revision_id = NEW.retired_revision_id
              AND head.revision_number = NEW.retired_revision_number
              AND head.semantic_version = NEW.retired_semantic_version
              AND head.head_revision = NEW.retired_head_revision
        ) THEN
            RAISE EXCEPTION 'guideline_retirement_head_conflict'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'superseded' AND NOT EXISTS (
            SELECT 1
            FROM "{guideline_table}" AS successor
            WHERE successor.id = NEW.superseded_by_guideline_id
              AND successor.scope = 'global'
              AND successor.board_id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM "{retirement_table}" AS successor_retirement
                  WHERE successor_retirement.guideline_id = successor.id
              )
        ) THEN
            RAISE EXCEPTION 'guideline_supersedence_successor_invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM "{permit_table}" AS permit
        JOIN "{guideline_table}" AS guideline
          ON guideline.board_id = permit.board_id
        WHERE guideline.id = OLD.guideline_id
          AND guideline.scope = 'inline'
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guideline_retirement_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$''',
        f'''CREATE TRIGGER "{revision_trigger}"
BEFORE INSERT OR UPDATE OR DELETE ON "{revision_table}"
FOR EACH ROW EXECUTE FUNCTION "{revision_function}"()''',
        f'''CREATE TRIGGER "{head_trigger}"
BEFORE UPDATE OR DELETE ON "{head_table}"
FOR EACH ROW EXECUTE FUNCTION "{head_function}"()''',
        f'''CREATE TRIGGER "{binding_trigger}"
BEFORE INSERT OR UPDATE OR DELETE ON "{binding_table}"
FOR EACH ROW EXECUTE FUNCTION "{binding_function}"()''',
        f'''CREATE TRIGGER "{retirement_trigger}"
BEFORE INSERT OR UPDATE OR DELETE ON "{retirement_table}"
FOR EACH ROW EXECUTE FUNCTION "{retirement_function}"()''',
    )


def guideline_policy_postgresql_trigger_contracts() -> dict[str, dict[str, object]]:
    """Return the exact non-internal PostgreSQL trigger catalog contract."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineBoardBindingRow,
        GuidelineHeadRow,
        GuidelineRetirementRow,
        GuidelineRevisionRow,
    )

    prefix = GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX
    return {
        f"{prefix}_revision_guard": {
            "table_name": GuidelineRevisionRow.__tablename__,
            "function_name": "pulse_guideline_revision_immutable_guard",
            "tgtype": 31,  # ROW | BEFORE | INSERT | DELETE | UPDATE
        },
        f"{prefix}_head_guard": {
            "table_name": GuidelineHeadRow.__tablename__,
            "function_name": "pulse_guideline_head_guard",
            "tgtype": 27,
        },
        f"{prefix}_binding_guard": {
            "table_name": GuidelineBoardBindingRow.__tablename__,
            "function_name": "pulse_guideline_binding_immutable_guard",
            "tgtype": 31,  # ROW | BEFORE | INSERT | DELETE | UPDATE
        },
        f"{prefix}_retirement_guard": {
            "table_name": GuidelineRetirementRow.__tablename__,
            "function_name": "pulse_guideline_retirement_immutable_guard",
            "tgtype": 31,
        },
    }


def audit_guideline_policy_postgresql_trigger_rows(
    rows: list[object] | tuple[object, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Audit exact PG catalog rows and identify only the known predecessor."""

    expected = guideline_policy_postgresql_trigger_contracts()
    existing = {str(row["name"]): row for row in rows}
    unexpected = set(existing) - set(expected)
    if unexpected:
        raise RuntimeError(
            "guideline policy has unexpected PostgreSQL owned triggers: "
            + ", ".join(sorted(unexpected))
        )
    missing = tuple(sorted(set(expected) - set(existing)))
    predecessors: list[str] = []
    binding_name = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_binding_guard"
    revision_name = f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_revision_guard"
    for trigger_name, row in existing.items():
        contract = expected[trigger_name]
        common_exact = (
            str(row["table_name"]) == contract["table_name"]
            and str(row["function_name"]) == contract["function_name"]
            and str(row["tgenabled"]) == "O"
            and row["tgqual"] is None
        )
        observed_type = int(row["tgtype"])
        if common_exact and observed_type == contract["tgtype"]:
            continue
        if (
            trigger_name in {binding_name, revision_name}
            and common_exact
            and observed_type == 27
        ):
            predecessors.append(trigger_name)
            continue
        raise RuntimeError(
            "guideline policy PostgreSQL trigger is corrupt: " + trigger_name
        )
    return missing, tuple(sorted(predecessors))


def _knowledge_propagation_v2_trigger_manifest(
    *,
    include_snapshot_governance_metadata: bool,
    allow_board_erasure: bool = True,
) -> dict[str, tuple[str, str]]:
    """Return SQLite guards owned by the selective-propagation schema.

    Canonical mutation results and attempt observations are append-only.
    Assignment, snapshot, and tombstone history may be closed exactly once,
    then linked to a successor exactly once after closure; neither temporal
    field may subsequently be reopened, retimed, cleared, or relinked.  The
    tombstone guards additionally make the global anti-resurrection marker
    mutually exclusive with per-root current markers.
    """

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        KnowledgeAssignmentRecord,
        KnowledgeMutationAttemptRecord,
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
        KnowledgeSnapshotRecord,
        KnowledgeTombstoneRecord,
    )

    expected: dict[str, tuple[str, str]] = {}
    permit_table = BoardErasurePermit.__tablename__
    scope_table = KnowledgePropagationScopeRecord.__tablename__
    for table_name in (
        KnowledgeMutationLedgerRecord.__tablename__,
        KnowledgeMutationAttemptRecord.__tablename__,
    ):
        for operation in ("update", "delete"):
            trigger_name = (
                f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_{operation}"
            )
            erasure_guard = ""
            if allow_board_erasure and operation == "delete":
                erasure_guard = (
                    "\nWHEN NOT EXISTS (\n"
                    "    SELECT 1\n"
                    f'    FROM "{permit_table}" AS permit\n'
                    "    WHERE permit.board_id = OLD.board_id\n"
                    ")"
                )
            trigger_sql = f'''CREATE TRIGGER "{trigger_name}"
BEFORE {operation.upper()} ON "{table_name}"{erasure_guard}
BEGIN
    SELECT RAISE(ABORT, 'knowledge_mutation_ledger_immutable');
END'''
            expected[trigger_name] = (table_name, trigger_sql)

    activation_insert = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{scope_table}_activation_insert"
    )
    expected[activation_insert] = (
        scope_table,
        f'''CREATE TRIGGER "{activation_insert}"
BEFORE INSERT ON "{scope_table}"
WHEN (
        NEW.v2_active = 1
        AND NEW.v2_activated_at IS NULL
    )
    OR (
        NEW.v2_active = 0
        AND NEW.v2_activated_at IS NOT NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_v2_activation_invalid'
    );
END''',
    )
    activation_update = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{scope_table}_activation_update"
    )
    expected[activation_update] = (
        scope_table,
        f'''CREATE TRIGGER "{activation_update}"
BEFORE UPDATE OF v2_active, v2_activated_at ON "{scope_table}"
WHEN (
        NEW.v2_active = 0
        AND NEW.v2_activated_at IS NOT NULL
    )
    OR (
        OLD.v2_activated_at IS NOT NULL
        AND NEW.v2_activated_at IS NOT OLD.v2_activated_at
    )
    OR (
        OLD.v2_activated_at IS NULL
        AND NEW.v2_activated_at IS NOT NULL
        AND NOT (
            OLD.v2_active = 0
            AND NEW.v2_active = 1
        )
    )
    OR (
        NEW.v2_active = 1
        AND NEW.v2_activated_at IS NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_v2_activation_immutable'
    );
END''',
    )

    def add_temporal_transition_guards(
        table_name: str,
        history_kind: str,
    ) -> None:
        closure_name = (
            f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_closure_update"
        )
        expected[closure_name] = (
            table_name,
            f'''CREATE TRIGGER "{closure_name}"
BEFORE UPDATE OF effective_to ON "{table_name}"
WHEN OLD.effective_to IS NOT NULL
    AND NEW.effective_to IS NOT OLD.effective_to
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_{history_kind}_closure_immutable'
    );
END''',
        )
        supersession_name = (
            f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_"
            f"{table_name}_supersession_update"
        )
        expected[supersession_name] = (
            table_name,
            f'''CREATE TRIGGER "{supersession_name}"
BEFORE UPDATE OF superseded_by_id ON "{table_name}"
WHEN NEW.superseded_by_id IS NOT OLD.superseded_by_id
    AND NOT (
        OLD.superseded_by_id IS NULL
        AND NEW.superseded_by_id IS NOT NULL
        AND OLD.effective_to IS NOT NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_{history_kind}_supersession_immutable'
    );
END''',
        )

    temporal_guards = {
        KnowledgeAssignmentRecord.__tablename__: (
            "assignment_id, scope_id, source_knowledge_id, root_id, "
            "immediate_parent_id, source_revision, source_content_sha256, "
            "mode, state, origin_class, actor_id, revision, justification, "
            "relevance_links, effective_from",
            (
                "NEW.assignment_id IS NOT OLD.assignment_id\n"
                "    OR NEW.scope_id IS NOT OLD.scope_id\n"
                "    OR NEW.source_knowledge_id IS NOT OLD.source_knowledge_id\n"
                "    OR NEW.root_id IS NOT OLD.root_id\n"
                "    OR NEW.immediate_parent_id IS NOT OLD.immediate_parent_id\n"
                "    OR NEW.source_revision IS NOT OLD.source_revision\n"
                "    OR NEW.source_content_sha256 IS NOT "
                "OLD.source_content_sha256\n"
                "    OR NEW.mode IS NOT OLD.mode\n"
                "    OR NEW.state IS NOT OLD.state\n"
                "    OR NEW.origin_class IS NOT OLD.origin_class\n"
                "    OR NEW.actor_id IS NOT OLD.actor_id\n"
                "    OR NEW.revision IS NOT OLD.revision\n"
                "    OR NEW.justification IS NOT OLD.justification\n"
                "    OR NEW.relevance_links IS NOT OLD.relevance_links\n"
                "    OR NEW.effective_from IS NOT OLD.effective_from"
            ),
            "knowledge_propagation_assignment_history_immutable",
        ),
        KnowledgeSnapshotRecord.__tablename__: (
            "snapshot_id, scope_id, assignment_id, root_id, "
            "immediate_parent_id, source_revision, source_content_sha256, "
            "content_bytes, effective_from"
            + (", governance_metadata" if include_snapshot_governance_metadata else ""),
            (
                "NEW.snapshot_id IS NOT OLD.snapshot_id\n"
                "    OR NEW.scope_id IS NOT OLD.scope_id\n"
                "    OR NEW.assignment_id IS NOT OLD.assignment_id\n"
                "    OR NEW.root_id IS NOT OLD.root_id\n"
                "    OR NEW.immediate_parent_id IS NOT OLD.immediate_parent_id\n"
                "    OR NEW.source_revision IS NOT OLD.source_revision\n"
                "    OR NEW.source_content_sha256 IS NOT "
                "OLD.source_content_sha256\n"
                "    OR NEW.content_bytes IS NOT OLD.content_bytes\n"
                "    OR NEW.effective_from IS NOT OLD.effective_from"
                + (
                    "\n    OR NEW.governance_metadata IS NOT OLD.governance_metadata"
                    if include_snapshot_governance_metadata
                    else ""
                )
            ),
            "knowledge_propagation_snapshot_history_immutable",
        ),
    }
    for table_name, (
        protected_columns,
        changed_predicate,
        error_code,
    ) in temporal_guards.items():
        update_name = (
            f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_content_update"
        )
        expected[update_name] = (
            table_name,
            f'''CREATE TRIGGER "{update_name}"
BEFORE UPDATE OF {protected_columns} ON "{table_name}"
WHEN {changed_predicate}
BEGIN
    SELECT RAISE(ABORT, '{error_code}');
END''',
        )
        delete_name = f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table_name}_delete"
        erasure_guard = ""
        if allow_board_erasure:
            erasure_guard = (
                "\nWHEN NOT EXISTS (\n"
                "    SELECT 1\n"
                f'    FROM "{permit_table}" AS permit\n'
                f'    JOIN "{scope_table}" AS scope\n'
                "      ON scope.board_id = permit.board_id\n"
                "    WHERE scope.id = OLD.scope_id\n"
                ")"
            )
        expected[delete_name] = (
            table_name,
            f'''CREATE TRIGGER "{delete_name}"
BEFORE DELETE ON "{table_name}"{erasure_guard}
BEGIN
    SELECT RAISE(ABORT, '{error_code}');
END''',
        )
        add_temporal_transition_guards(
            table_name,
            "assignment"
            if table_name == KnowledgeAssignmentRecord.__tablename__
            else "snapshot",
        )

    tombstone_table = KnowledgeTombstoneRecord.__tablename__
    add_temporal_transition_guards(tombstone_table, "tombstone")
    conflict_insert = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_tombstone_current_conflict_insert"
    )
    expected[conflict_insert] = (
        tombstone_table,
        f'''CREATE TRIGGER "{conflict_insert}"
BEFORE INSERT ON "{tombstone_table}"
WHEN NEW.effective_to IS NULL
    AND EXISTS (
        SELECT 1
        FROM "{tombstone_table}" AS current_marker
        WHERE current_marker.scope_id = NEW.scope_id
          AND current_marker.effective_to IS NULL
          AND (
              NEW.root_id IS NULL
              OR current_marker.root_id IS NULL
          )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_current_global_tombstone_conflict'
    );
END''',
    )
    identity_update = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_tombstone_identity_update"
    )
    expected[identity_update] = (
        tombstone_table,
        f'''CREATE TRIGGER "{identity_update}"
BEFORE UPDATE OF tombstone_id, scope_id, root_id, actor_id, justification,
    effective_from ON "{tombstone_table}"
WHEN NEW.tombstone_id IS NOT OLD.tombstone_id
    OR NEW.scope_id IS NOT OLD.scope_id
    OR NEW.root_id IS NOT OLD.root_id
    OR NEW.actor_id IS NOT OLD.actor_id
    OR NEW.justification IS NOT OLD.justification
    OR NEW.effective_from IS NOT OLD.effective_from
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_tombstone_identity_immutable'
    );
END''',
    )
    tombstone_delete = f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_tombstone_delete"
    erasure_guard = ""
    if allow_board_erasure:
        erasure_guard = (
            "\nWHEN NOT EXISTS (\n"
            "    SELECT 1\n"
            f'    FROM "{permit_table}" AS permit\n'
            f'    JOIN "{scope_table}" AS scope\n'
            "      ON scope.board_id = permit.board_id\n"
            "    WHERE scope.id = OLD.scope_id\n"
            ")"
        )
    expected[tombstone_delete] = (
        tombstone_table,
        f'''CREATE TRIGGER "{tombstone_delete}"
BEFORE DELETE ON "{tombstone_table}"{erasure_guard}
BEGIN
    SELECT RAISE(
        ABORT,
        'knowledge_propagation_tombstone_history_immutable'
    );
END''',
    )
    return expected


def knowledge_propagation_v2_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return the current selective-propagation SQLite trigger contract."""

    return _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=True,
    )


def _knowledge_propagation_migration_checkpoint(stage: str) -> None:
    """Deterministic fault-injection seam used by migration replay tests."""

    del stage


def _normalize_sqlite_contract_ddl(raw: object) -> str:
    value = "" if raw is None else str(raw)
    return re.sub(r'[\s"`\[\]]+', "", value.lower())


def _normalize_sqlite_contract_type(raw: object) -> str:
    value = "" if raw is None else str(raw)
    return re.sub(r"\s+", "", value.lower())


def _normalize_sqlite_contract_default(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return re.sub(r"\s+", "", value).lower()


def _expected_sqlite_server_default(
    sync_conn: object,
    column: object,
) -> str | None:
    default = column.server_default
    if default is None:
        return None
    argument = default.arg
    if isinstance(argument, str):
        raw = "'" + argument.replace("'", "''") + "'"
    else:
        compile_value = getattr(argument, "compile", None)
        raw = (
            str(
                compile_value(
                    dialect=sync_conn.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
            if callable(compile_value)
            else str(argument)
        )
    return _normalize_sqlite_contract_default(raw)


def _sqlite_owned_table_contract(
    sync_conn: object,
    table: object,
) -> dict[str, dict[str, object]]:
    """Return exact expected/observed SQLite contracts for an ORM table."""

    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(sync_conn)
    expected_columns = tuple(
        (
            str(column.name),
            _normalize_sqlite_contract_type(
                column.type.compile(dialect=sync_conn.dialect)
            ),
            bool(column.nullable),
            _expected_sqlite_server_default(sync_conn, column),
        )
        for column in table.columns
    )
    observed_columns = tuple(
        (
            str(column["name"]),
            _normalize_sqlite_contract_type(column["type"]),
            bool(column["nullable"]),
            _normalize_sqlite_contract_default(column.get("default")),
        )
        for column in inspector.get_columns(table.name)
    )
    expected_unique = tuple(
        sorted(
            [
                (
                    constraint.name,
                    tuple(str(column.name) for column in constraint.columns),
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
            ],
            key=repr,
        )
    )
    observed_unique = tuple(
        sorted(
            [
                (
                    constraint.get("name"),
                    tuple(
                        str(column) for column in constraint.get("column_names") or ()
                    ),
                )
                for constraint in inspector.get_unique_constraints(table.name)
            ],
            key=repr,
        )
    )
    expected_checks = tuple(
        sorted(
            [
                (
                    constraint.name,
                    _normalize_sqlite_contract_ddl(constraint.sqltext),
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
            ],
            key=repr,
        )
    )
    observed_checks = tuple(
        sorted(
            [
                (
                    constraint.get("name"),
                    _normalize_sqlite_contract_ddl(constraint.get("sqltext")),
                )
                for constraint in inspector.get_check_constraints(table.name)
            ],
            key=repr,
        )
    )
    expected_indexes = tuple(
        sorted(
            [
                (
                    index.name,
                    bool(index.unique),
                    tuple(
                        str(getattr(expression, "name", expression))
                        for expression in index.expressions
                    ),
                )
                for index in table.indexes
            ],
            key=repr,
        )
    )
    observed_indexes = tuple(
        sorted(
            [
                (
                    index.get("name"),
                    bool(index.get("unique")),
                    tuple(str(column) for column in index.get("column_names") or ()),
                )
                for index in inspector.get_indexes(table.name)
            ],
            key=repr,
        )
    )

    expected_foreign_keys = []
    for constraint in table.foreign_key_constraints:
        elements = tuple(constraint.elements)
        remote_table = elements[0].column.table if elements else None
        expected_foreign_keys.append(
            (
                constraint.name,
                tuple(str(element.parent.name) for element in elements),
                getattr(remote_table, "schema", None),
                getattr(remote_table, "name", None),
                tuple(str(element.column.name) for element in elements),
                (
                    str(elements[0].ondelete).upper()
                    if elements and elements[0].ondelete
                    else None
                ),
                (
                    str(elements[0].onupdate).upper()
                    if elements and elements[0].onupdate
                    else None
                ),
            )
        )
    inline_impact_columns = {
        "impact_receipt_id",
        "impact_adoption_id",
        "impact_unlink_id",
    }
    pragma_foreign_keys: dict[str, list[dict[str, object]]] = {}
    table_ddl = ""
    if table.name == "guideline_board_bindings":
        pragma_rows = (
            sync_conn.exec_driver_sql(f'PRAGMA foreign_key_list("{table.name}")')
            .mappings()
            .all()
        )
        for row in pragma_rows:
            pragma_foreign_keys.setdefault(str(row["from"]), []).append(dict(row))
        table_ddl = str(
            sync_conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table.name,),
            ).scalar_one()
        )

    def _inline_constraint_name(column_name: str) -> str | None:
        identifier = (
            rf'(?:"{re.escape(column_name)}"|'
            rf"`{re.escape(column_name)}`|"
            rf"\[{re.escape(column_name)}\]|"
            rf"{re.escape(column_name)})"
        )
        name = (
            r'(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|'
            r"([A-Za-z_][A-Za-z0-9_$]*))"
        )
        inline = re.search(
            rf"(?is)(?:\(|,)\s*{identifier}\s+[^,]*?"
            rf"\bCONSTRAINT\s+{name}\s+REFERENCES\b",
            table_ddl,
        )
        if inline is not None:
            return next(
                (value for value in inline.groups() if value is not None),
                None,
            )
        table_level = re.search(
            rf"(?is)\bCONSTRAINT\s+{name}\s+FOREIGN\s+KEY\s*"
            rf"\(\s*{identifier}\s*\)",
            table_ddl,
        )
        if table_level is None:
            return None
        return next(
            (value for value in table_level.groups() if value is not None),
            None,
        )

    observed_foreign_keys = []
    for constraint in inspector.get_foreign_keys(table.name):
        options = constraint.get("options") or {}
        constrained_columns = tuple(
            str(column) for column in constraint.get("constrained_columns") or ()
        )
        constraint_name = constraint.get("name")
        referred_schema = constraint.get("referred_schema")
        referred_table = constraint.get("referred_table")
        referred_columns = tuple(
            str(column) for column in constraint.get("referred_columns") or ()
        )
        ondelete = (
            str(options.get("ondelete")).upper() if options.get("ondelete") else None
        )
        onupdate = (
            str(options.get("onupdate")).upper() if options.get("onupdate") else None
        )
        if (
            len(constrained_columns) == 1
            and constrained_columns[0] in inline_impact_columns
            and pragma_foreign_keys
        ):
            pragma_rows = pragma_foreign_keys.get(
                constrained_columns[0],
                [],
            )
            if len(pragma_rows) == 1:
                pragma = pragma_rows[0]
                constraint_name = _inline_constraint_name(constrained_columns[0])
                referred_schema = None
                referred_table = str(pragma["table"])
                referred_columns = (str(pragma["to"]),)
                pragma_ondelete = str(pragma["on_delete"]).upper()
                pragma_onupdate = str(pragma["on_update"]).upper()
                ondelete = None if pragma_ondelete == "NO ACTION" else pragma_ondelete
                onupdate = None if pragma_onupdate == "NO ACTION" else pragma_onupdate
        observed_foreign_keys.append(
            (
                constraint_name,
                constrained_columns,
                referred_schema,
                referred_table,
                referred_columns,
                ondelete,
                onupdate,
            )
        )

    primary_key = inspector.get_pk_constraint(table.name)
    return {
        "expected": {
            "columns": expected_columns,
            "primary_key": (
                table.primary_key.name,
                tuple(str(column.name) for column in table.primary_key.columns),
            ),
            "unique_constraints": expected_unique,
            "checks": expected_checks,
            "indexes": expected_indexes,
            "foreign_keys": tuple(sorted(expected_foreign_keys, key=repr)),
        },
        "observed": {
            "columns": observed_columns,
            "primary_key": (
                primary_key.get("name"),
                tuple(
                    str(column)
                    for column in primary_key.get("constrained_columns") or ()
                ),
            ),
            "unique_constraints": observed_unique,
            "checks": observed_checks,
            "indexes": observed_indexes,
            "foreign_keys": tuple(sorted(observed_foreign_keys, key=repr)),
        },
    }


def _postgresql_owned_table_contract(
    sync_conn: object,
    table: object,
) -> dict[str, dict[str, object]]:
    """Return the exact PostgreSQL structure owned by one ORM table.

    PostgreSQL assigns implementation-specific names to primary-key indexes and
    may expose ``NO ACTION`` instead of an omitted referential action.  Those
    two representation details are normalized; columns, named uniques/checks,
    explicit indexes, and every FK option (including deferred cycles) remain
    exact.
    """

    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(sync_conn)

    def _compiled_type(value: object) -> str:
        compile_value = getattr(value, "compile")
        return re.sub(
            r"\s+",
            " ",
            str(compile_value(dialect=sync_conn.dialect)).strip().lower(),
        )

    def _strip_outer_parentheses(value: str) -> str:
        normalized = value.strip()
        while normalized.startswith("(") and normalized.endswith(")"):
            depth = 0
            wraps_complete_expression = True
            for position, character in enumerate(normalized):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0 and position != len(normalized) - 1:
                        wraps_complete_expression = False
                        break
            if not wraps_complete_expression or depth != 0:
                break
            normalized = normalized[1:-1].strip()
        return normalized

    def _normalized_expression(value: object) -> str:
        normalized = "" if value is None else str(value)
        normalized = re.sub(
            r"::\s*(?:character\s+varying(?:\(\d+\))?|"
            r"varchar(?:\(\d+\))?|text|smallint|integer|bigint|"
            r"boolean|"
            r"timestamp(?:\s+with(?:out)?\s+time\s+zone)?)"
            r"(?:\[\])?",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"\bTRIM\s*\(\s*BOTH\s+FROM\s+"
            r"(?P<value>[A-Za-z_][A-Za-z0-9_$.]*)\s*\)",
            lambda match: f"TRIM({match.group('value')})",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"\(\s*(?P<left>[A-Za-z_][A-Za-z0-9_$.]*)\s*\)"
            r"(?=\s*=\s*ANY\b)",
            lambda match: match.group("left"),
            normalized,
            flags=re.IGNORECASE,
        )

        # PostgreSQL rewrites ``x IN (...)`` as ``x = ANY (ARRAY[...])``.
        # Balance the ANY call explicitly: the catalog may add one or more
        # wrappers around ARRAY and a non-balancing regex can consume the
        # predicate's closing parenthesis, corrupting the contract fingerprint.
        def _matching_parenthesis(expression: str, opening: int) -> int | None:
            depth = 0
            quote: str | None = None
            position = opening
            while position < len(expression):
                character = expression[position]
                if quote is not None:
                    if character == quote:
                        if (
                            quote == "'"
                            and position + 1 < len(expression)
                            and expression[position + 1] == quote
                        ):
                            position += 2
                            continue
                        quote = None
                    position += 1
                    continue
                if character in {"'", '"'}:
                    quote = character
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        return position
                position += 1
            return None

        any_call = re.compile(
            r"(?P<left>[A-Za-z0-9_\".]+)\s*=\s*ANY\s*\(",
            flags=re.IGNORECASE,
        )
        search_from = 0
        while match := any_call.search(normalized, search_from):
            opening = match.end() - 1
            closing = _matching_parenthesis(normalized, opening)
            if closing is None:
                break
            body = _strip_outer_parentheses(
                normalized[opening + 1 : closing]
            )
            array = re.fullmatch(
                r"ARRAY\s*\[(?P<values>.*)\]",
                body,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if array is None:
                search_from = closing + 1
                continue
            replacement = (
                f"{match.group('left')} IN ({array.group('values')})"
            )
            normalized = (
                normalized[: match.start()]
                + replacement
                + normalized[closing + 1 :]
            )
            search_from = match.start() + len(replacement)

        # SQLAlchemy's PostgreSQL inspector removes the outer ``CHECK (...)``
        # wrapper.  When pg_get_constraintdef parenthesizes individual ANY
        # predicates, that trimming can leave one orphan close at the start and
        # one orphan open at the end.  Restore only those demonstrably missing
        # endpoints; deleting an orphan open could erase an intentional final
        # ``(a OR b)`` grouping.
        unmatched_closes = 0
        stack: list[int] = []
        quote: str | None = None
        position = 0
        while position < len(normalized):
            character = normalized[position]
            if quote is not None:
                if character == quote:
                    if (
                        quote == "'"
                        and position + 1 < len(normalized)
                        and normalized[position + 1] == quote
                    ):
                        position += 2
                        continue
                    quote = None
                position += 1
                continue
            if character in {"'", '"'}:
                quote = character
            elif character == "(":
                stack.append(position)
            elif character == ")":
                if stack:
                    stack.pop()
                else:
                    unmatched_closes += 1
            position += 1
        if unmatched_closes or stack:
            normalized = (
                "(" * unmatched_closes
                + normalized
                + ")" * len(stack)
            )

        comparison = re.compile(
            r"(?:<>|<=|>=|=|<|>|\bIS\b|\bIN\b)",
            flags=re.IGNORECASE,
        )
        boolean_operator = re.compile(
            r"\b(?:AND|OR)\b",
            flags=re.IGNORECASE,
        )
        arithmetic_operator = re.compile(r"(?:\+|-|\*|/|%)")
        comparison_suffix = re.compile(r"(?:<>|<=|>=|=|<|>)\s*$")

        def _parenthesis_pairs(expression: str) -> list[tuple[int, int]]:
            pairs: list[tuple[int, int]] = []
            stack: list[int] = []
            quote: str | None = None
            position = 0
            while position < len(expression):
                character = expression[position]
                if quote is not None:
                    if character == quote:
                        if (
                            quote == "'"
                            and position + 1 < len(expression)
                            and expression[position + 1] == quote
                        ):
                            position += 2
                            continue
                        quote = None
                    position += 1
                    continue
                if character in {"'", '"'}:
                    quote = character
                elif character == "(":
                    stack.append(position)
                elif character == ")" and stack:
                    pairs.append((stack.pop(), position))
                position += 1
            return pairs

        def _top_level_sql(expression: str) -> str:
            top_level: list[str] = []
            depth = 0
            quote: str | None = None
            position = 0
            while position < len(expression):
                character = expression[position]
                if quote is not None:
                    if character == quote:
                        if (
                            quote == "'"
                            and position + 1 < len(expression)
                            and expression[position + 1] == quote
                        ):
                            position += 2
                            continue
                        quote = None
                    top_level.append(" ")
                    position += 1
                    continue
                if character in {"'", '"'}:
                    quote = character
                    top_level.append(" ")
                elif character == "(":
                    depth += 1
                    top_level.append(" ")
                elif character == ")":
                    depth = max(0, depth - 1)
                    top_level.append(" ")
                elif depth == 0:
                    top_level.append(character)
                else:
                    top_level.append(" ")
                position += 1
            return "".join(top_level)

        def _split_boolean(
            expression: str,
            keyword: str,
        ) -> tuple[str, ...]:
            parts: list[str] = []
            depth = 0
            quote: str | None = None
            start = 0
            position = 0
            upper = expression.upper()
            while position < len(expression):
                character = expression[position]
                if quote is not None:
                    if character == quote:
                        if (
                            quote == "'"
                            and position + 1 < len(expression)
                            and expression[position + 1] == quote
                        ):
                            position += 2
                            continue
                        quote = None
                    position += 1
                    continue
                if character in {"'", '"'}:
                    quote = character
                    position += 1
                    continue
                if character == "(":
                    depth += 1
                    position += 1
                    continue
                if character == ")":
                    depth = max(0, depth - 1)
                    position += 1
                    continue
                end = position + len(keyword)
                if (
                    depth == 0
                    and upper[position:end] == keyword
                    and (
                        position == 0
                        or not (
                            expression[position - 1].isalnum()
                            or expression[position - 1] == "_"
                        )
                    )
                    and (
                        end == len(expression)
                        or not (
                            expression[end].isalnum()
                            or expression[end] == "_"
                        )
                    )
                ):
                    parts.append(expression[start:position].strip())
                    start = end
                    position = end
                    continue
                position += 1
            if not parts:
                return (expression,)
            parts.append(expression[start:].strip())
            return tuple(parts)

        simple_atom = (
            r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$.]*|'
            r"'(?:''|[^'])*')"
        )

        def _normalized_atom(expression: str) -> str:
            atom = _strip_outer_parentheses(expression)
            while True:
                without_redundant_atom_parentheses = re.sub(
                    rf"\(\s*({simple_atom})\s*\)",
                    r"\1",
                    atom,
                )
                if without_redundant_atom_parentheses == atom:
                    break
                atom = without_redundant_atom_parentheses
            for start, end in _parenthesis_pairs(atom):
                body = atom[start + 1 : end].strip()
                top_level = _top_level_sql(body)
                if (
                    comparison_suffix.search(atom[:start])
                    and arithmetic_operator.search(top_level)
                    and not comparison.search(top_level)
                    and not boolean_operator.search(top_level)
                ):
                    atom = atom[:start] + body + atom[end + 1 :]
                    break
            return _normalize_sqlite_contract_ddl(atom)

        def _boolean_tree(expression: str) -> tuple[object, ...]:
            expression = _strip_outer_parentheses(expression)
            disjunction = _split_boolean(expression, "OR")
            if len(disjunction) > 1:
                children: list[tuple[object, ...]] = []
                for part in disjunction:
                    child = _boolean_tree(part)
                    if child[0] == "or":
                        children.extend(child[1:])  # type: ignore[arg-type]
                    else:
                        children.append(child)
                return ("or", *children)
            conjunction = _split_boolean(expression, "AND")
            if len(conjunction) > 1:
                children = []
                for part in conjunction:
                    child = _boolean_tree(part)
                    if child[0] == "and":
                        children.extend(child[1:])  # type: ignore[arg-type]
                    else:
                        children.append(child)
                return ("and", *children)
            negation = re.match(r"^\s*NOT\b", expression, re.IGNORECASE)
            if negation is not None:
                return (
                    "not",
                    _boolean_tree(expression[negation.end() :]),
                )
            return ("atom", _normalized_atom(expression))

        def _render_boolean(tree: tuple[object, ...]) -> str:
            operator = tree[0]
            if operator == "atom":
                return str(tree[1])
            if operator == "not":
                return "not(" + _render_boolean(tree[1]) + ")"  # type: ignore[arg-type]
            return "(" + str(operator).join(
                _render_boolean(child)  # type: ignore[arg-type]
                for child in tree[1:]
            ) + ")"

        return _render_boolean(_boolean_tree(normalized))

    def _server_default(value: object) -> str | None:
        if value is None:
            return None
        argument = getattr(value, "arg", value)
        compile_value = getattr(argument, "compile", None)
        raw = (
            str(
                compile_value(
                    dialect=sync_conn.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
            if callable(compile_value)
            else str(argument)
        )
        # PostgreSQL catalogs materialize JSON defaults with an explicit
        # ``::json``/``::jsonb`` cast even when the ORM default is the same
        # uncast literal. Column type is audited separately, so normalize
        # that catalog-only representation without weakening CHECK auditing.
        normalized = _normalized_expression(
            re.sub(
                r"::\s*jsonb?(?:\[\])?",
                "",
                raw,
                flags=re.IGNORECASE,
            )
        )
        if normalized in {"'true'", "true"}:
            return "true"
        if normalized in {"'false'", "false"}:
            return "false"
        return normalized

    def _action(value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        return None if normalized in {"", "NO ACTION"} else normalized

    def _fk_contract(constraint: object) -> tuple[object, ...]:
        elements = tuple(constraint.elements)
        remote_table = elements[0].column.table if elements else None
        return (
            constraint.name,
            tuple(str(element.parent.name) for element in elements),
            getattr(remote_table, "schema", None),
            getattr(remote_table, "name", None),
            tuple(str(element.column.name) for element in elements),
            _action(getattr(constraint, "ondelete", None)),
            _action(getattr(constraint, "onupdate", None)),
            (
                bool(constraint.deferrable)
                if constraint.deferrable is not None
                else None
            ),
            (
                str(constraint.initially).upper()
                if constraint.initially is not None
                else None
            ),
        )

    unnamed_foreign_keys = {
        (
            tuple(str(element.parent.name) for element in constraint.elements),
            getattr(
                constraint.elements[0].column.table,
                "schema",
                None,
            )
            if constraint.elements
            else None,
            getattr(
                constraint.elements[0].column.table,
                "name",
                None,
            )
            if constraint.elements
            else None,
            tuple(str(element.column.name) for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
        if constraint.name is None
    }

    def _observed_fk_contract(constraint: dict[str, object]) -> tuple[object, ...]:
        options = constraint.get("options") or {}
        deferrable = options.get("deferrable")
        constrained_columns = tuple(
            str(column) for column in constraint.get("constrained_columns") or ()
        )
        referred_columns = tuple(
            str(column) for column in constraint.get("referred_columns") or ()
        )
        identity = (
            constrained_columns,
            constraint.get("referred_schema"),
            constraint.get("referred_table"),
            referred_columns,
        )
        return (
            None if identity in unnamed_foreign_keys else constraint.get("name"),
            constrained_columns,
            constraint.get("referred_schema"),
            constraint.get("referred_table"),
            referred_columns,
            _action(options.get("ondelete")),
            _action(options.get("onupdate")),
            bool(deferrable) if deferrable is not None else None,
            (
                str(options.get("initially")).upper()
                if options.get("initially") is not None
                else None
            ),
        )

    expected = {
        "columns": tuple(
            (
                str(column.name),
                _compiled_type(column.type),
                bool(column.nullable),
                _server_default(column.server_default),
            )
            for column in table.columns
        ),
        "primary_key": tuple(str(column.name) for column in table.primary_key.columns),
        "unique_constraints": tuple(
            sorted(
                [
                    (
                        constraint.name,
                        tuple(str(column.name) for column in constraint.columns),
                    )
                    for constraint in table.constraints
                    if constraint.__class__.__name__ == "UniqueConstraint"
                ],
                key=repr,
            )
        ),
        "checks": tuple(
            sorted(
                [
                    (
                        constraint.name,
                        _normalized_expression(constraint.sqltext),
                    )
                    for constraint in table.constraints
                    if constraint.__class__.__name__ == "CheckConstraint"
                ],
                key=repr,
            )
        ),
        "indexes": tuple(
            sorted(
                [
                    (
                        index.name,
                        bool(index.unique),
                        tuple(
                            str(getattr(expression, "name", expression))
                            for expression in index.expressions
                        ),
                    )
                    for index in table.indexes
                ],
                key=repr,
            )
        ),
        "foreign_keys": tuple(
            sorted(
                [
                    _fk_contract(constraint)
                    for constraint in table.foreign_key_constraints
                ],
                key=repr,
            )
        ),
    }
    primary_key = inspector.get_pk_constraint(table.name)
    observed = {
        "columns": tuple(
            (
                str(column["name"]),
                _compiled_type(column["type"]),
                bool(column["nullable"]),
                _server_default(column.get("default")),
            )
            for column in inspector.get_columns(table.name)
        ),
        "primary_key": tuple(
            str(column) for column in primary_key.get("constrained_columns") or ()
        ),
        "unique_constraints": tuple(
            sorted(
                [
                    (
                        constraint.get("name"),
                        tuple(
                            str(column)
                            for column in constraint.get("column_names") or ()
                        ),
                    )
                    for constraint in inspector.get_unique_constraints(table.name)
                ],
                key=repr,
            )
        ),
        "checks": tuple(
            sorted(
                [
                    (
                        constraint.get("name"),
                        _normalized_expression(constraint.get("sqltext")),
                    )
                    for constraint in inspector.get_check_constraints(table.name)
                ],
                key=repr,
            )
        ),
        "indexes": tuple(
            sorted(
                [
                    (
                        index.get("name"),
                        bool(index.get("unique")),
                        tuple(
                            str(column) for column in index.get("column_names") or ()
                        ),
                    )
                    for index in inspector.get_indexes(table.name)
                    if not index.get("duplicates_constraint")
                ],
                key=repr,
            )
        ),
        "foreign_keys": tuple(
            sorted(
                [
                    _observed_fk_contract(constraint)
                    for constraint in inspector.get_foreign_keys(table.name)
                ],
                key=repr,
            )
        ),
    }
    return {"expected": expected, "observed": observed}


async def create_all_boundary() -> None:
    """Create all ORM tables through the Community declarative metadata."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # The pre-create migration is a no-op for a fresh database. Converge realm
    # indexes after create_all as well so the first lifecycle run is terminal.
    from okto_pulse.community.adapters.realm_migration import backfill_local_realm

    await backfill_local_realm(get_engine())


async def _migrate_add_consolidation_work_kinds() -> str | None:
    """Upgrade ``consolidation_queue`` to the governed multi-kind contract.

    SQLite cannot drop the legacy ``UNIQUE(board_id, artifact_type,
    artifact_id)`` constraint in place.  The migration therefore rebuilds the
    table transactionally from the ORM contract, preserving every legacy row
    and backfilling it as ``work_kind='consolidate', generation=0``.  The new
    partial unique indexes allow immutable ``stale_reconcile`` generations and
    one board-scoped ``stale_sweep`` while retaining legacy consolidate
    deduplication.

    This step intentionally runs immediately after ``create_all`` and before
    the Global Discovery source-fence step.  Rebuilding an existing queue drops
    its source-revision triggers; the following control-plane migration then
    recreates and audits that trigger manifest in the same startup lifecycle.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue

    queue_table = ConsolidationQueue.__table__
    table_name = queue_table.name
    backup_name = "consolidation_queue_governed_delete_legacy"
    required_legacy_columns = {
        "id",
        "board_id",
        "artifact_type",
        "artifact_id",
        "priority",
        "source",
        "status",
    }
    governed_columns = {
        "work_kind",
        "generation",
        "payload",
        "delete_event_id",
        "claim_token",
    }
    expected_indexes = {
        "ix_consolidation_queue_delete_event_id": None,
        "uq_queue_consolidate_board_artifact": "work_kind='consolidate'",
        "uq_queue_stale_reconcile_generation": "work_kind='stale_reconcile'",
        "uq_queue_stale_sweep_board": "work_kind='stale_sweep'",
        "ix_queue_drain_work": None,
    }

    def _normalize_ddl(raw: object) -> str:
        return re.sub(r'[\s"`\[\]]+', "", str(raw or "").lower())

    def _contract(sync_conn: object) -> dict[str, object]:
        inspector = sa_inspect(sync_conn)
        columns = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }
        unique_constraints = {
            tuple(str(name) for name in constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        checks = {
            _normalize_ddl(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(table_name)
        }
        indexes = {
            str(row["name"]): {
                "unique": False,
                "sql": str(row["sql"] or ""),
            }
            for row in sync_conn.exec_driver_sql(
                "SELECT name, sql "
                "FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL",
                (table_name,),
            ).mappings()
        }
        # sqlite_master's computed alias is not stable across SQLAlchemy
        # versions; derive uniqueness from the canonical SQL string instead.
        for value in indexes.values():
            value["unique"] = _normalize_ddl(value["sql"]).startswith(
                "createuniqueindex"
            )
        return {
            "columns": columns,
            "unique_constraints": unique_constraints,
            "checks": checks,
            "indexes": indexes,
        }

    def _rebuild(sync_conn: object, old_columns: set[str]) -> None:
        inspector = sa_inspect(sync_conn)
        if backup_name in inspector.get_table_names():
            raise RuntimeError(
                "governed queue migration found an unexpected backup table"
            )
        missing_required = required_legacy_columns - old_columns
        if missing_required:
            raise RuntimeError(
                "legacy consolidation_queue is missing required columns: "
                + ", ".join(sorted(missing_required))
            )

        # Named indexes keep their global SQLite names after a table rename and
        # would collide with the canonical indexes created for the replacement.
        for index in inspector.get_indexes(table_name):
            index_name = str(index.get("name") or "")
            if index_name and not index_name.startswith("sqlite_autoindex_"):
                escaped = index_name.replace('"', '""')
                sync_conn.exec_driver_sql(f'DROP INDEX "{escaped}"')

        sync_conn.exec_driver_sql(
            f'ALTER TABLE "{table_name}" RENAME TO "{backup_name}"'
        )
        queue_table.create(sync_conn, checkfirst=False)

        insert_columns: list[str] = []
        select_expressions: list[str] = []
        for column in queue_table.columns:
            name = str(column.name)
            if name in old_columns:
                insert_columns.append(f'"{name}"')
                if name == "work_kind":
                    select_expressions.append(
                        "COALESCE(NULLIF(TRIM(work_kind), ''), 'consolidate')"
                    )
                elif name == "generation":
                    select_expressions.append("COALESCE(generation, 0)")
                else:
                    select_expressions.append(f'"{name}"')
            elif name == "work_kind":
                insert_columns.append('"work_kind"')
                select_expressions.append("'consolidate'")
            elif name == "generation":
                insert_columns.append('"generation"')
                select_expressions.append("0")
            elif name == "payload":
                insert_columns.append('"payload"')
                select_expressions.append("NULL")
            elif name == "delete_event_id":
                insert_columns.append('"delete_event_id"')
                select_expressions.append("NULL")
            elif name == "claim_token":
                insert_columns.append('"claim_token"')
                select_expressions.append("NULL")

        sync_conn.exec_driver_sql(
            f'INSERT INTO "{table_name}" ({", ".join(insert_columns)}) '
            f'SELECT {", ".join(select_expressions)} FROM "{backup_name}"'
        )
        sync_conn.exec_driver_sql(f'DROP TABLE "{backup_name}"')

    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "governed consolidation queue migration requires Community SQLite"
            )

        before = await conn.run_sync(_contract)
        before_columns = set(before["columns"])
        legacy_unique = (
            "board_id",
            "artifact_type",
            "artifact_id",
        ) in before["unique_constraints"]
        has_work_kind_check = any(
            "work_kindin('consolidate','stale_reconcile','stale_sweep')" in check
            for check in before["checks"]
        )
        if (
            not governed_columns.issubset(before_columns)
            or legacy_unique
            or not has_work_kind_check
        ):
            await conn.run_sync(lambda sync_conn: _rebuild(sync_conn, before_columns))
            changed = True

        backfill_kind = await conn.execute(
            sa_text(
                "UPDATE consolidation_queue SET work_kind='consolidate' "
                "WHERE work_kind IS NULL OR TRIM(work_kind)=''"
            )
        )
        backfill_generation = await conn.execute(
            sa_text(
                "UPDATE consolidation_queue SET generation=0 WHERE generation IS NULL"
            )
        )
        changed = changed or int(backfill_kind.rowcount or 0) > 0
        changed = changed or int(backfill_generation.rowcount or 0) > 0

        current = await conn.run_sync(_contract)
        current_indexes = current["indexes"]
        for index in queue_table.indexes:
            index_name = str(index.name)
            if index_name not in expected_indexes or index_name in current_indexes:
                continue
            await conn.run_sync(
                lambda sync_conn, owned_index=index: owned_index.create(
                    sync_conn, checkfirst=False
                )
            )
            changed = True

        final = await conn.run_sync(_contract)
        final_columns = final["columns"]
        if not governed_columns.issubset(final_columns):
            raise RuntimeError("governed consolidation queue columns are incomplete")
        if not bool(final_columns["claim_token"].get("nullable")):
            raise RuntimeError(
                "governed consolidation queue claim token must be nullable"
            )
        if (
            "board_id",
            "artifact_type",
            "artifact_id",
        ) in final["unique_constraints"]:
            raise RuntimeError("legacy consolidation queue uniqueness still exists")
        invalid_rows = int(
            (
                await conn.execute(
                    sa_text(
                        "SELECT COUNT(*) FROM consolidation_queue "
                        "WHERE work_kind NOT IN "
                        "('consolidate','stale_reconcile','stale_sweep') "
                        "OR generation IS NULL"
                    )
                )
            ).scalar_one()
        )
        if invalid_rows:
            raise RuntimeError("governed consolidation queue backfill is incomplete")
        for index_name, predicate in expected_indexes.items():
            observed = final["indexes"].get(index_name)
            if observed is None:
                raise RuntimeError(
                    f"governed consolidation queue index missing: {index_name}"
                )
            normalized_sql = _normalize_ddl(observed["sql"])
            if index_name.startswith("uq_") and not observed["unique"]:
                raise RuntimeError(
                    f"governed consolidation queue index is not unique: {index_name}"
                )
            if predicate and _normalize_ddl(predicate) not in normalized_sql:
                raise RuntimeError(
                    f"governed consolidation queue predicate drift: {index_name}"
                )

    return None if changed else "skipped"


async def _migrate_global_discovery_delivery_contract() -> str | None:
    """Converge the durable GD delivery ledger and physical attempt key.

    ``create_all_boundary`` creates the additive ledger table.  Existing
    installations still declare ``global_update_outbox.event_id`` as
    ``VARCHAR(36)``, while governed delivery uses the literal physical key
    ``{delivery_key}:attempt:{n}``.  SQLite cannot alter that declared type in
    place, so this post-boundary step rebuilds only the outbox table, preserves
    every row, and then proves both relational contracts.  It intentionally
    precedes the Global Discovery control-plane step, which recreates any
    source-revision triggers removed with the legacy outbox table.
    """

    from sqlalchemy import inspect as sa_inspect

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GlobalDiscoveryDeliveryLedger,
        GlobalDiscoveryDeliveryRedriveControl,
        GlobalDiscoveryDeliveryWatchdogControl,
        GlobalUpdateOutbox,
    )

    outbox_table = GlobalUpdateOutbox.__table__
    ledger_table = GlobalDiscoveryDeliveryLedger.__table__
    redrive_control_table = GlobalDiscoveryDeliveryRedriveControl.__table__
    watchdog_control_table = GlobalDiscoveryDeliveryWatchdogControl.__table__
    outbox_name = outbox_table.name
    ledger_name = ledger_table.name
    redrive_control_name = redrive_control_table.name
    watchdog_control_name = watchdog_control_table.name
    backup_name = "global_update_outbox_delivery_key_legacy"
    outbox_columns = tuple(str(column.name) for column in outbox_table.columns)
    ledger_columns = tuple(str(column.name) for column in ledger_table.columns)
    redrive_control_columns = tuple(
        str(column.name) for column in redrive_control_table.columns
    )
    watchdog_control_columns = tuple(
        str(column.name) for column in watchdog_control_table.columns
    )
    expected_ledger_uniques = {
        (
            "board_id",
            "artifact_type",
            "artifact_id",
            "generation",
        ),
        ("delete_event_id",),
        ("attempt_event_key",),
    }
    expected_ledger_checks = {
        "generation>=1",
        "statein('outbox_persisted','delivered','delivery_debt')",
        "attempt>=0",
        "state!='outbox_persisted'orattempt_event_keyisnotnull",
    }
    expected_ledger_indexes = {
        "ix_gd_delivery_ledger_state_retry": (
            "state",
            "next_retry_at",
            "updated_at",
            "delivery_key",
        ),
        "ix_gd_delivery_ledger_board_state": ("board_id", "state"),
    }

    def _normalize_ddl(raw: object) -> str:
        return re.sub(r'[\s"`\[\]]+', "", str(raw or "").lower())

    def _contract(sync_conn: object) -> dict[str, object]:
        inspector = sa_inspect(sync_conn)
        table_names = set(inspector.get_table_names())
        missing_tables = {
            outbox_name,
            ledger_name,
            redrive_control_name,
            watchdog_control_name,
        } - table_names
        if missing_tables:
            raise RuntimeError(
                "global discovery delivery schema is missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        outbox_observed_columns = {
            str(column["name"]): column for column in inspector.get_columns(outbox_name)
        }
        ledger_observed_columns = {
            str(column["name"]): column for column in inspector.get_columns(ledger_name)
        }
        redrive_control_observed_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(redrive_control_name)
        }
        watchdog_control_observed_columns = {
            str(column["name"]): column
            for column in inspector.get_columns(watchdog_control_name)
        }
        ledger_uniques = {
            tuple(str(name) for name in constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(ledger_name)
        }
        ledger_checks = {
            _normalize_ddl(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(ledger_name)
        }
        ledger_indexes = {
            str(index.get("name") or ""): tuple(
                str(name) for name in index.get("column_names") or ()
            )
            for index in inspector.get_indexes(ledger_name)
        }
        ledger_foreign_keys = tuple(inspector.get_foreign_keys(ledger_name))
        return {
            "outbox_columns": outbox_observed_columns,
            "outbox_uniques": {
                tuple(str(name) for name in constraint.get("column_names") or ())
                for constraint in inspector.get_unique_constraints(outbox_name)
            },
            "ledger_columns": ledger_observed_columns,
            "ledger_pk": tuple(
                str(name)
                for name in (
                    inspector.get_pk_constraint(ledger_name).get("constrained_columns")
                    or ()
                )
            ),
            "ledger_uniques": ledger_uniques,
            "ledger_checks": ledger_checks,
            "ledger_indexes": ledger_indexes,
            "ledger_foreign_keys": ledger_foreign_keys,
            "redrive_control_columns": redrive_control_observed_columns,
            "redrive_control_pk": tuple(
                str(name)
                for name in (
                    inspector.get_pk_constraint(redrive_control_name).get(
                        "constrained_columns"
                    )
                    or ()
                )
            ),
            "redrive_control_checks": {
                _normalize_ddl(constraint.get("sqltext"))
                for constraint in inspector.get_check_constraints(redrive_control_name)
            },
            "watchdog_control_columns": watchdog_control_observed_columns,
            "watchdog_control_pk": tuple(
                str(name)
                for name in (
                    inspector.get_pk_constraint(watchdog_control_name).get(
                        "constrained_columns"
                    )
                    or ()
                )
            ),
            "watchdog_control_checks": {
                _normalize_ddl(constraint.get("sqltext"))
                for constraint in inspector.get_check_constraints(watchdog_control_name)
            },
            "watchdog_control_foreign_keys": tuple(
                inspector.get_foreign_keys(watchdog_control_name)
            ),
            "outbox_physical": _sqlite_owned_table_contract(
                sync_conn,
                outbox_table,
            ),
            "ledger_physical": _sqlite_owned_table_contract(
                sync_conn,
                ledger_table,
            ),
            "redrive_control_physical": _sqlite_owned_table_contract(
                sync_conn,
                redrive_control_table,
            ),
            "watchdog_control_physical": _sqlite_owned_table_contract(
                sync_conn,
                watchdog_control_table,
            ),
        }

    def _observed_columns(sync_conn: object, table_name: str) -> dict[str, object]:
        return {
            str(column["name"]): column
            for column in sa_inspect(sync_conn).get_columns(table_name)
        }

    def _validate_rebuild_columns(
        observed_columns: set[str],
        *,
        table_name: str,
    ) -> None:
        expected_columns = set(outbox_columns)
        missing_columns = expected_columns - observed_columns
        extra_columns = observed_columns - expected_columns
        if missing_columns or extra_columns:
            raise RuntimeError(
                f"{table_name} columns cannot be rebuilt safely: "
                f"missing={sorted(missing_columns)} extra={sorted(extra_columns)}"
            )

    def _drop_named_indexes(sync_conn: object, table_name: str) -> None:
        # Named SQLite indexes are database-global and would collide with the
        # canonical indexes created for the replacement table. Auto-indexes
        # belong to the table and disappear with it.
        for index in sa_inspect(sync_conn).get_indexes(table_name):
            index_name = str(index.get("name") or "")
            if index_name and not index_name.startswith("sqlite_autoindex_"):
                escaped = index_name.replace('"', '""')
                sync_conn.exec_driver_sql(f'DROP INDEX "{escaped}"')

    def _outbox_rows_match(sync_conn: object) -> bool:
        quoted_columns = ", ".join(f'"{name}"' for name in outbox_columns)
        difference = sync_conn.exec_driver_sql(
            "SELECT "
            "NOT EXISTS (SELECT 1 FROM ("
            f'SELECT {quoted_columns} FROM "{backup_name}" '
            "EXCEPT "
            f'SELECT {quoted_columns} FROM "{outbox_name}")) '
            "AND NOT EXISTS (SELECT 1 FROM ("
            f'SELECT {quoted_columns} FROM "{outbox_name}" '
            "EXCEPT "
            f'SELECT {quoted_columns} FROM "{backup_name}"))'
        ).scalar_one()
        return bool(difference)

    def _rebuild_outbox(sync_conn: object) -> None:
        inspector = sa_inspect(sync_conn)
        table_names = set(inspector.get_table_names())
        if backup_name not in table_names:
            if outbox_name not in table_names:
                raise RuntimeError(
                    "global delivery migration found neither the canonical "
                    "outbox nor its resumable backup"
                )
            _validate_rebuild_columns(
                set(_observed_columns(sync_conn, outbox_name)),
                table_name=outbox_name,
            )
            sync_conn.exec_driver_sql(
                f'ALTER TABLE "{outbox_name}" RENAME TO "{backup_name}"'
            )
        else:
            _validate_rebuild_columns(
                set(_observed_columns(sync_conn, backup_name)),
                table_name=backup_name,
            )

        # A process killed under the historical non-transactional DDL path can
        # leave both names behind. The backup is the authoritative source. An
        # empty or byte-equivalent replacement is safe to rebuild; any other
        # non-empty target is ambiguous and must fail closed.
        table_names = set(sa_inspect(sync_conn).get_table_names())
        if outbox_name in table_names:
            observed_target = _observed_columns(sync_conn, outbox_name)
            _validate_rebuild_columns(
                set(observed_target),
                table_name=outbox_name,
            )
            target_event_type = observed_target["event_id"]["type"]
            if getattr(target_event_type, "length", None) != 255:
                raise RuntimeError(
                    "resumable global_update_outbox target must be VARCHAR(255)"
                )
            target_count = int(
                sync_conn.exec_driver_sql(
                    f'SELECT COUNT(*) FROM "{outbox_name}"'
                ).scalar_one()
            )
            if target_count and not _outbox_rows_match(sync_conn):
                raise RuntimeError(
                    "resumable global_update_outbox target contains divergent rows"
                )
            sync_conn.exec_driver_sql(f'DROP TABLE "{outbox_name}"')

        _drop_named_indexes(sync_conn, backup_name)
        outbox_table.create(sync_conn, checkfirst=False)
        quoted_columns = ", ".join(f'"{name}"' for name in outbox_columns)
        sync_conn.exec_driver_sql(
            f'INSERT INTO "{outbox_name}" ({quoted_columns}) '
            f'SELECT {quoted_columns} FROM "{backup_name}"'
        )
        if not _outbox_rows_match(sync_conn):
            raise RuntimeError(
                "global_update_outbox rebuild did not preserve every row"
            )
        sync_conn.exec_driver_sql(f'DROP TABLE "{backup_name}"')

    def _validate(contract: dict[str, object]) -> None:
        for contract_key, label in (
            ("outbox_physical", "global_update_outbox"),
            ("ledger_physical", "global discovery delivery ledger"),
            (
                "redrive_control_physical",
                "global discovery delivery redrive control",
            ),
            (
                "watchdog_control_physical",
                "global discovery delivery watchdog control",
            ),
        ):
            physical = contract[contract_key]
            expected = physical["expected"]
            observed = physical["observed"]
            drift = tuple(
                section
                for section in expected
                if observed.get(section) != expected[section]
            )
            if drift:
                raise RuntimeError(
                    f"{label} physical contract drift: " + ", ".join(drift)
                )

        observed_outbox_columns = contract["outbox_columns"]
        if set(observed_outbox_columns) != set(outbox_columns):
            raise RuntimeError("global_update_outbox column contract drift")
        event_type = observed_outbox_columns["event_id"]["type"]
        if getattr(event_type, "length", None) != 255:
            raise RuntimeError("global_update_outbox.event_id must be VARCHAR(255)")
        if ("event_id",) not in contract["outbox_uniques"]:
            raise RuntimeError("global_update_outbox.event_id must remain unique")

        if set(contract["ledger_columns"]) != set(ledger_columns):
            raise RuntimeError("global discovery delivery ledger column drift")
        if contract["ledger_pk"] != ("delivery_key",):
            raise RuntimeError("global discovery delivery ledger primary key drift")
        if not expected_ledger_uniques.issubset(contract["ledger_uniques"]):
            raise RuntimeError("global discovery delivery ledger uniqueness drift")
        if not expected_ledger_checks.issubset(contract["ledger_checks"]):
            raise RuntimeError("global discovery delivery ledger check drift")
        for index_name, columns in expected_ledger_indexes.items():
            if contract["ledger_indexes"].get(index_name) != columns:
                raise RuntimeError(
                    f"global discovery delivery ledger index drift: {index_name}"
                )
        board_foreign_key = any(
            tuple(str(name) for name in fk.get("constrained_columns") or ())
            == ("board_id",)
            and str(fk.get("referred_table") or "") == "boards"
            and str((fk.get("options") or {}).get("ondelete") or "").upper()
            == "CASCADE"
            for fk in contract["ledger_foreign_keys"]
        )
        if not board_foreign_key:
            raise RuntimeError(
                "global discovery delivery ledger board foreign key drift"
            )
        if set(contract["redrive_control_columns"]) != set(redrive_control_columns):
            raise RuntimeError("global discovery delivery redrive control column drift")
        if contract["redrive_control_pk"] != ("id",):
            raise RuntimeError(
                "global discovery delivery redrive control primary key drift"
            )
        expected_redrive_checks = {
            "id='_global'",
            "checkpoint_version>=0",
        }
        if not expected_redrive_checks.issubset(contract["redrive_control_checks"]):
            raise RuntimeError("global discovery delivery redrive control check drift")
        if set(contract["watchdog_control_columns"]) != set(watchdog_control_columns):
            raise RuntimeError(
                "global discovery delivery watchdog control column drift"
            )
        if contract["watchdog_control_pk"] != ("board_id",):
            raise RuntimeError(
                "global discovery delivery watchdog control primary key drift"
            )
        if "checkpoint_version>=0" not in contract["watchdog_control_checks"]:
            raise RuntimeError("global discovery delivery watchdog control check drift")
        watchdog_board_foreign_key = any(
            tuple(str(name) for name in fk.get("constrained_columns") or ())
            == ("board_id",)
            and str(fk.get("referred_table") or "") == "boards"
            and str((fk.get("options") or {}).get("ondelete") or "").upper()
            == "CASCADE"
            for fk in contract["watchdog_control_foreign_keys"]
        )
        if not watchdog_board_foreign_key:
            raise RuntimeError(
                "global discovery delivery watchdog control board foreign key drift"
            )

    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "global discovery delivery migration requires Community SQLite"
            )

        # Python's sqlite3 legacy transaction mode does not BEGIN for DDL.
        # Force a physical write transaction before any rename/create/drop so
        # a failed copy restores the original table, indexes, triggers, and
        # rows instead of committing a split-table intermediate state.
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        missing_delivery_tables = {
            ledger_name,
            redrive_control_name,
            watchdog_control_name,
        } - table_names
        if missing_delivery_tables:
            raise RuntimeError(
                "global discovery delivery schema is missing tables: "
                + ", ".join(sorted(missing_delivery_tables))
            )
        if backup_name in table_names:
            await conn.run_sync(_rebuild_outbox)
            changed = True
        else:
            before = await conn.run_sync(_contract)
            event_type = before["outbox_columns"]["event_id"]["type"]
            if getattr(event_type, "length", None) != 255:
                await conn.run_sync(_rebuild_outbox)
                changed = True
        final = await conn.run_sync(_contract)
        _validate(final)

    return None if changed else "skipped"


async def _migrate_cognitive_source_revision_ledger() -> str | None:
    """Audit the additive revision table and install immutable row guards.

    ``create_all_boundary`` creates the child ledger without touching the
    existing revision-zero rows.  This post-boundary step is deliberately
    non-repairing: an unexpected physical contract or a modified owned
    trigger fails startup instead of rewriting durable cognitive history.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KGCognitiveSource,
        KGCognitiveSourceRevision,
    )

    base_table = KGCognitiveSource.__table__
    revision_table = KGCognitiveSourceRevision.__table__
    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "cognitive source revision ledger requires SQLite trigger semantics"
            )
        # Python's sqlite3 legacy transaction mode does not begin for DDL.
        # Pin trigger convergence and its final audit to one writer transaction.
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        missing_tables = {
            base_table.name,
            revision_table.name,
        } - table_names
        if missing_tables:
            raise RuntimeError(
                "cognitive source revision migration requires the canonical "
                "create_all boundary; missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        contract = await conn.run_sync(
            lambda sync_conn: _sqlite_owned_table_contract(sync_conn, revision_table)
        )
        if contract["observed"] != contract["expected"]:
            raise RuntimeError(
                "cognitive source revision table has a non-canonical contract"
            )

        expected_triggers = cognitive_source_immutability_trigger_manifest()
        predecessor_triggers = cognitive_source_immutability_trigger_manifest(
            allow_board_erasure=False,
        )
        trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(row["name"]): row for row in trigger_rows}
        unexpected = set(existing_triggers) - set(expected_triggers)
        if unexpected:
            raise RuntimeError(
                "cognitive source revision ledger has unexpected owned triggers: "
                + ", ".join(sorted(unexpected))
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            existing = existing_triggers.get(trigger_name)
            if existing is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            observed_table = str(existing["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                existing["sql"]
            )
            if observed_table == table_name and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(trigger_sql)
            ):
                continue
            predecessor_table, predecessor_sql = predecessor_triggers[trigger_name]
            if observed_table == predecessor_table and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(predecessor_sql)
            ):
                await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            else:
                raise RuntimeError(
                    f"cognitive source immutability trigger {trigger_name} is corrupt"
                )

        final_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{COGNITIVE_SOURCE_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        final_triggers = {str(row["name"]): row for row in final_trigger_rows}
        if set(final_triggers) != set(expected_triggers):
            raise RuntimeError(
                "cognitive source immutability trigger installation is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            observed = final_triggers[trigger_name]
            if str(
                observed["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                observed["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    "cognitive source immutability trigger audit failed: "
                    + trigger_name
                )

    return None if changed else "skipped"


async def _migrate_global_discovery_recovery_control_plane() -> str | None:
    """Converge the R5 control plane and transactional source fence.

    ``create_all_boundary`` remains the only table-creation boundary.  This
    post-boundary step upgrades legacy attempt rows, proves the owned table
    shapes, seeds the singleton revision, and installs restart-safe SQLite
    triggers which advance it in the same transaction as every preparation
    input mutation.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
        GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES,
        GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
        GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX,
        GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
        GlobalDiscoveryRecoveryAttempt,
        GlobalDiscoveryRecoveryDispatch,
        GlobalDiscoveryRecoverySlot,
        GlobalDiscoveryRecoveryTransition,
        GlobalDiscoverySourceRevision,
    )

    attempt_table = GlobalDiscoveryRecoveryAttempt.__table__
    slot_table = GlobalDiscoveryRecoverySlot.__table__
    dispatch_table = GlobalDiscoveryRecoveryDispatch.__table__
    transition_table = GlobalDiscoveryRecoveryTransition.__table__
    revision_table = GlobalDiscoverySourceRevision.__table__
    owned_tables = (
        attempt_table,
        slot_table,
        dispatch_table,
        transition_table,
        revision_table,
    )
    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "global recovery source revision requires SQLite trigger semantics"
            )
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        missing_tables = {table.name for table in owned_tables} - table_names
        if missing_tables:
            raise RuntimeError(
                "global recovery control-plane migration requires the canonical "
                "create_all boundary; missing tables: "
                + ", ".join(sorted(missing_tables))
            )

        existing_columns = await conn.run_sync(
            lambda sync_conn: {
                str(column["name"])
                for column in sa_inspect(sync_conn).get_columns(attempt_table.name)
            }
        )
        preparation_state_was_missing = "preparation_state" not in existing_columns
        requester_audit_was_missing = "requester_actor_ids_json" not in existing_columns
        additive_columns = {
            "attempt_id": "VARCHAR(512) NOT NULL DEFAULT ''",
            "preparation_state": "VARCHAR(32) NOT NULL DEFAULT 'queued'",
            "confirmation_state": "VARCHAR(32) NOT NULL DEFAULT 'unconfirmed'",
            "requester_actor_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "request_count": "BIGINT NOT NULL DEFAULT 0",
            "replay_count": "BIGINT NOT NULL DEFAULT 0",
            "requester_actor_overflow_count": "BIGINT NOT NULL DEFAULT 0",
            "first_requested_at": "TIMESTAMP",
            "last_requested_at": "TIMESTAMP",
            "boards_total": "INTEGER NOT NULL DEFAULT 0",
            "boards_scanned": "INTEGER NOT NULL DEFAULT 0",
            "attempt_budget_ms": "INTEGER NOT NULL DEFAULT 600000",
            "prepared_at": "TIMESTAMP",
            "expires_at": "TIMESTAMP",
            "snapshot_fingerprint": "VARCHAR(255)",
            "confirmed_by_actor_id": "VARCHAR(255)",
            "confirmation_consumed_at": "TIMESTAMP",
            "audit_reason": "VARCHAR(512)",
            "cancel_requested_by_actor_id": "VARCHAR(255)",
            "cancel_reason": "VARCHAR(512)",
            "resume_requested_at": "TIMESTAMP",
            "resume_requested_by_actor_id": "VARCHAR(255)",
            "resume_audit_reason": "VARCHAR(512)",
            "physical_journal_phase": "VARCHAR(128)",
            "physical_pointer_replaced": "BOOLEAN",
            "physical_rollback_performed": "BOOLEAN",
            "physical_evidence_ref": "VARCHAR(1024)",
        }
        for column_name, definition in additive_columns.items():
            if column_name in existing_columns:
                continue
            await conn.execute(
                sa_text(
                    f'ALTER TABLE "{attempt_table.name}" '
                    f'ADD COLUMN "{column_name}" {definition}'
                )
            )
            changed = True

        attempt_identity = "run_id || '/attempt-' || CAST(epoch AS VARCHAR)"
        conflicting_identity = (
            await conn.execute(
                sa_text(
                    f"SELECT {attempt_identity} AS expected_attempt_id, COUNT(*) "
                    f'FROM "{attempt_table.name}" '
                    "GROUP BY expected_attempt_id HAVING COUNT(*) > 1 LIMIT 1"
                )
            )
        ).first()
        if conflicting_identity is not None:
            raise RuntimeError(
                "global recovery attempt identities cannot be repaired without "
                "a collision"
            )
        inconsistent_attempt_count = int(
            (
                await conn.execute(
                    sa_text(
                        f'SELECT COUNT(*) FROM "{attempt_table.name}" '
                        f"WHERE attempt_id IS NULL OR attempt_id <> {attempt_identity}"
                    )
                )
            ).scalar_one()
        )
        if inconsistent_attempt_count:
            attempt_indexes = await conn.run_sync(
                lambda sync_conn: {
                    str(index.get("name"))
                    for index in sa_inspect(sync_conn).get_indexes(attempt_table.name)
                    if index.get("name")
                }
            )
            identity_index = "uq_global_discovery_recovery_attempt_identity"
            if identity_index in attempt_indexes:
                await conn.execute(sa_text(f'DROP INDEX "{identity_index}"'))
            # Two-phase re-keying avoids transient unique-index swaps.  Slot and
            # dispatch references derive from their own immutable run/epoch
            # binding, so the whole repair remains transactional.
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" '
                    "SET attempt_id = '__r5_rekey__/' || CAST(rowid AS VARCHAR)"
                )
            )
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" SET attempt_id = {attempt_identity}'
                )
            )
            for related_table in (slot_table, dispatch_table):
                await conn.execute(
                    sa_text(
                        f'UPDATE "{related_table.name}" SET attempt_id = '
                        "run_id || '/attempt-' || CAST(epoch AS VARCHAR)"
                    )
                )
            changed = True
        final_inconsistent_attempts = int(
            (
                await conn.execute(
                    sa_text(
                        f'SELECT COUNT(*) FROM "{attempt_table.name}" '
                        f"WHERE attempt_id <> {attempt_identity}"
                    )
                )
            ).scalar_one()
        )
        if final_inconsistent_attempts:
            raise RuntimeError(
                "global recovery attempt identity repair did not converge"
            )
        for related_table in (slot_table, dispatch_table):
            related_identity = "run_id || '/attempt-' || CAST(epoch AS VARCHAR)"
            inconsistent_related = int(
                (
                    await conn.execute(
                        sa_text(
                            f'SELECT COUNT(*) FROM "{related_table.name}" '
                            f"WHERE attempt_id <> {related_identity}"
                        )
                    )
                ).scalar_one()
            )
            if not inconsistent_related:
                continue
            await conn.execute(
                sa_text(
                    f'UPDATE "{related_table.name}" SET attempt_id = {related_identity}'
                )
            )
            changed = True
        if preparation_state_was_missing:
            # Every pre-R5 row was admitted only after synchronous preparation
            # and confirmation, so preserve that historical truth during the
            # two-stage split instead of presenting it as newly queued work.
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" '
                    "SET preparation_state = 'prepared', "
                    "confirmation_state = 'consumed'"
                )
            )
        if requester_audit_was_missing:
            # Only rows that existed before the R5 requester ledger are
            # backfilled.  New rows retain the unambiguous empty defaults until
            # admission writes the initial requester atomically.
            await conn.execute(
                sa_text(
                    f'UPDATE "{attempt_table.name}" '
                    "SET requester_actor_ids_json = json_array(actor_id), "
                    "request_count = CASE WHEN request_count < 1 THEN 1 "
                    "ELSE request_count END, "
                    "first_requested_at = COALESCE(first_requested_at, "
                    "started_at, CURRENT_TIMESTAMP), "
                    "last_requested_at = COALESCE(last_requested_at, updated_at, "
                    "CURRENT_TIMESTAMP)"
                )
            )

        final_columns = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_columns(attempt_table.name)
        )
        final_column_names = {str(column["name"]) for column in final_columns}
        missing = set(attempt_table.columns.keys()) - final_column_names
        if missing:
            raise RuntimeError(
                "global recovery control-plane migration left missing columns: "
                + ", ".join(sorted(missing))
            )

        def _normalize_ddl(raw: object) -> str | None:
            if raw is None:
                return None
            value = str(raw).strip()
            while value.startswith("(") and value.endswith(")"):
                value = value[1:-1].strip()
            return re.sub(r"\s+", "", value).lower()

        def _expected_default(sync_conn: object, column: object) -> str | None:
            default = column.server_default
            if default is None:
                return None
            argument = default.arg
            compile_value = getattr(argument, "compile", None)
            if callable(compile_value):
                raw = str(
                    compile_value(
                        dialect=sync_conn.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
            else:
                raw = str(argument)
            return _normalize_ddl(raw)

        def _owned_table_contract(
            sync_conn: object, table: object
        ) -> dict[str, object]:
            inspector = sa_inspect(sync_conn)
            actual_columns = inspector.get_columns(table.name)
            expected_columns = tuple(
                (
                    column.name,
                    _normalize_ddl(column.type.compile(dialect=sync_conn.dialect)),
                    bool(column.nullable),
                    _expected_default(sync_conn, column),
                )
                for column in table.columns
            )
            observed_columns = tuple(
                (
                    str(column["name"]),
                    _normalize_ddl(column["type"]),
                    bool(column["nullable"]),
                    _normalize_ddl(column.get("default")),
                )
                for column in actual_columns
            )
            expected_indexes = {
                str(index.name): (
                    bool(index.unique),
                    tuple(column.name for column in index.columns),
                )
                for index in table.indexes
            }
            observed_indexes = {
                str(index["name"]): (
                    bool(index.get("unique")),
                    tuple(str(column) for column in index.get("column_names") or ()),
                )
                for index in inspector.get_indexes(table.name)
                if index.get("name")
            }
            expected_unique = {
                str(constraint.name): tuple(
                    column.name for column in constraint.columns
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
                and constraint.name
            }
            observed_unique = {
                str(constraint["name"]): tuple(
                    str(column) for column in constraint.get("column_names") or ()
                )
                for constraint in inspector.get_unique_constraints(table.name)
                if constraint.get("name")
            }
            expected_checks = {
                str(constraint.name): _normalize_ddl(constraint.sqltext)
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
                and constraint.name
            }
            observed_checks = {
                str(constraint["name"]): _normalize_ddl(constraint.get("sqltext"))
                for constraint in inspector.get_check_constraints(table.name)
                if constraint.get("name")
            }
            return {
                "columns": observed_columns,
                "expected_columns": expected_columns,
                "pk": tuple(
                    str(column)
                    for column in (
                        inspector.get_pk_constraint(table.name).get(
                            "constrained_columns"
                        )
                        or ()
                    )
                ),
                "expected_pk": tuple(
                    column.name for column in table.primary_key.columns
                ),
                "indexes": observed_indexes,
                "expected_indexes": expected_indexes,
                "unique": observed_unique,
                "expected_unique": expected_unique,
                "checks": observed_checks,
                "expected_checks": expected_checks,
            }

        attempt_contract = await conn.run_sync(
            lambda sync_conn: _owned_table_contract(sync_conn, attempt_table)
        )
        if set(final_column_names) != set(attempt_table.columns.keys()):
            raise RuntimeError(
                "global recovery attempt table contains non-canonical extra columns"
            )
        attempt_requires_rebuild = (
            attempt_contract["columns"] != attempt_contract["expected_columns"]
            or attempt_contract["pk"] != attempt_contract["expected_pk"]
            or attempt_contract["unique"] != attempt_contract["expected_unique"]
            or attempt_contract["checks"] != attempt_contract["expected_checks"]
        )
        if attempt_requires_rebuild:
            # SQLite cannot repair nullability, type, PK, or defaults in place.
            # Rebuild this owned history table transactionally; any invalid row
            # that cannot satisfy the canonical DDL aborts and rolls back.
            def _rebuild_attempt_table(sync_conn: object) -> None:
                inspector = sa_inspect(sync_conn)
                backup = f"{attempt_table.name}__r5_contract_rebuild"
                if backup in set(inspector.get_table_names()):
                    raise RuntimeError(
                        "global recovery attempt contract rebuild found stale backup"
                    )
                for index in inspector.get_indexes(attempt_table.name):
                    name = str(index.get("name") or "")
                    if name and not name.startswith("sqlite_autoindex_"):
                        sync_conn.exec_driver_sql(f'DROP INDEX "{name}"')
                sync_conn.exec_driver_sql(
                    f'ALTER TABLE "{attempt_table.name}" RENAME TO "{backup}"'
                )
                attempt_table.create(sync_conn, checkfirst=False)
                columns = ", ".join(
                    f'"{column.name}"' for column in attempt_table.columns
                )
                sync_conn.exec_driver_sql(
                    f'INSERT INTO "{attempt_table.name}" ({columns}) '
                    f'SELECT {columns} FROM "{backup}"'
                )
                sync_conn.exec_driver_sql(f'DROP TABLE "{backup}"')

            await conn.run_sync(_rebuild_attempt_table)
            changed = True

        # Missing named indexes are independently convergent.  Every other
        # column/PK/unique/check/index mismatch on these fence tables is a hard
        # startup failure rather than a best-effort partial repair.
        for table in owned_tables:
            contract = await conn.run_sync(
                lambda sync_conn, owned_table=table: _owned_table_contract(
                    sync_conn, owned_table
                )
            )
            if (
                contract["columns"] != contract["expected_columns"]
                or contract["pk"] != contract["expected_pk"]
                or contract["unique"] != contract["expected_unique"]
                or contract["checks"] != contract["expected_checks"]
            ):
                raise RuntimeError(
                    f"global recovery owned table {table.name} has a "
                    "non-canonical column/constraint contract"
                )
            observed_indexes = dict(contract["indexes"])
            expected_indexes = dict(contract["expected_indexes"])
            missing_indexes = set(expected_indexes) - set(observed_indexes)
            unexpected_indexes = set(observed_indexes) - set(expected_indexes)
            if unexpected_indexes:
                raise RuntimeError(
                    f"global recovery owned table {table.name} has unexpected "
                    "indexes: " + ", ".join(sorted(unexpected_indexes))
                )
            for index in table.indexes:
                if str(index.name) not in missing_indexes:
                    continue
                await conn.run_sync(
                    lambda sync_conn, owned_index=index: owned_index.create(
                        sync_conn, checkfirst=False
                    )
                )
                changed = True
            final_contract = await conn.run_sync(
                lambda sync_conn, owned_table=table: _owned_table_contract(
                    sync_conn, owned_table
                )
            )
            if final_contract["indexes"] != final_contract["expected_indexes"]:
                raise RuntimeError(
                    f"global recovery owned table {table.name} has a "
                    "non-canonical index contract"
                )

        row_insert = await conn.execute(
            sa_text(
                f'INSERT OR IGNORE INTO "{revision_table.name}" '
                "(scope_id, fence_version, trigger_manifest_version, "
                "incarnation_id, revision, mutation_nonce, updated_at) "
                "VALUES (:scope_id, :fence_version, :trigger_manifest_version, "
                "lower(hex(randomblob(32))), 0, lower(hex(randomblob(32))), "
                "CURRENT_TIMESTAMP)"
            ),
            {
                "scope_id": GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
                "fence_version": GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
                "trigger_manifest_version": (
                    GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
                ),
            },
        )
        row_was_inserted = int(row_insert.rowcount or 0) > 0
        if row_was_inserted:
            changed = True
        revision_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT scope_id, fence_version, trigger_manifest_version, "
                        f'incarnation_id, revision, mutation_nonce FROM "{revision_table.name}"'
                    )
                )
            )
            .mappings()
            .all()
        )
        if (
            len(revision_rows) != 1
            or str(revision_rows[0]["scope_id"])
            != GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID
            or isinstance(revision_rows[0]["revision"], bool)
            or not isinstance(revision_rows[0]["revision"], int)
            or int(revision_rows[0]["revision"]) < 0
            or len(str(revision_rows[0]["incarnation_id"])) != 64
            or len(str(revision_rows[0]["mutation_nonce"])) != 64
        ):
            raise RuntimeError(
                "global recovery source revision singleton is missing or corrupt"
            )

        for table_name in GLOBAL_DISCOVERY_SOURCE_REVISION_INPUT_TABLES:
            if table_name not in table_names:
                raise RuntimeError(
                    "global recovery source revision input table is missing: "
                    + table_name
                )
        expected_triggers = global_discovery_source_revision_trigger_manifest()

        existing_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%"},
                )
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(row["name"]): row for row in existing_trigger_rows}
        unexpected_triggers = set(existing_triggers) - set(expected_triggers)
        if unexpected_triggers:
            raise RuntimeError(
                "global recovery source revision has unexpected owned triggers: "
                + ", ".join(sorted(unexpected_triggers))
            )

        repaired_trigger_manifest = False
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            existing = existing_triggers.get(trigger_name)
            if existing is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                repaired_trigger_manifest = True
                continue
            if str(
                existing["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                existing["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    f"global recovery source revision trigger {trigger_name} is corrupt"
                )

        stored_fence_version = str(revision_rows[0]["fence_version"])
        stored_trigger_version = str(revision_rows[0]["trigger_manifest_version"])
        version_changed = (
            stored_fence_version != GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION
            or stored_trigger_version
            != GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
        )
        if version_changed or (repaired_trigger_manifest and not row_was_inserted):
            # A trigger-set repair or governed manifest upgrade invalidates all
            # previously issued preparation fingerprints, even when the source
            # revision itself did not move.
            await conn.execute(
                sa_text(
                    f'UPDATE "{revision_table.name}" '
                    "SET fence_version = :fence_version, "
                    "trigger_manifest_version = :trigger_manifest_version, "
                    "incarnation_id = lower(hex(randomblob(32))), "
                    "mutation_nonce = lower(hex(randomblob(32))), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE scope_id = :scope_id"
                ),
                {
                    "scope_id": GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID,
                    "fence_version": GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION,
                    "trigger_manifest_version": (
                        GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
                    ),
                },
            )
            changed = True

        final_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": f"{GLOBAL_DISCOVERY_SOURCE_REVISION_TRIGGER_PREFIX}%"},
                )
            )
            .mappings()
            .all()
        )
        if {str(row["name"]) for row in final_trigger_rows} != set(expected_triggers):
            raise RuntimeError(
                "global recovery source revision trigger installation is incomplete"
            )
        final_revision = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT scope_id, fence_version, trigger_manifest_version, "
                        "incarnation_id, revision, mutation_nonce "
                        f'FROM "{revision_table.name}"'
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(final_revision) != 1:
            raise RuntimeError("global recovery source revision singleton audit failed")
        final_row = final_revision[0]
        hex_values = (
            str(final_row["incarnation_id"]),
            str(final_row["mutation_nonce"]),
        )
        if (
            str(final_row["scope_id"]) != GLOBAL_DISCOVERY_SOURCE_REVISION_SCOPE_ID
            or str(final_row["fence_version"]) != GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION
            or str(final_row["trigger_manifest_version"])
            != GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION
            or isinstance(final_row["revision"], bool)
            or not isinstance(final_row["revision"], int)
            or int(final_row["revision"]) < 0
            or any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hex_values
            )
        ):
            raise RuntimeError("global recovery source revision singleton audit failed")

    return None if changed else "skipped"


async def _migrate_card_statuses() -> None:
    """Migrate card status enum values from Portuguese to English."""
    from sqlalchemy import text as sa_text

    status_map = {
        "nao_iniciado": "not_started",
        "iniciado": "started",
        "em_andamento": "in_progress",
        "em_pendencia": "on_hold",
        "finalizado": "done",
        "cancelado": "cancelled",
    }

    async with get_engine().begin() as conn:
        try:
            await conn.execute(sa_text("SELECT 1 FROM cards LIMIT 0"))
        except Exception:
            return

        for old_val, new_val in status_map.items():
            await conn.execute(
                sa_text(
                    f"UPDATE cards SET status = '{new_val}' WHERE LOWER(status) = '{old_val}'"
                )
            )


async def _migrate_add_priority_column() -> None:
    """Add priority column to cards table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE cards ADD COLUMN priority VARCHAR(50) DEFAULT 'none' NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_realm_id() -> None:
    """Add, backfill and index the Community local realm idempotently."""
    from okto_pulse.community.adapters.realm_migration import backfill_local_realm

    await backfill_local_realm(get_engine())


async def _migrate_add_comment_choice_columns() -> None:
    """Add choice board columns to comments table if they don't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        for stmt in [
            "ALTER TABLE comments ADD COLUMN comment_type VARCHAR(20) NOT NULL DEFAULT 'text'",
            "ALTER TABLE comments ADD COLUMN choices JSON",
            "ALTER TABLE comments ADD COLUMN responses JSON",
            "ALTER TABLE comments ADD COLUMN allow_free_text BOOLEAN NOT NULL DEFAULT 0",
        ]:
            try:
                await conn.execute(sa_text(stmt))
            except Exception:
                pass


async def _migrate_add_bug_card_columns() -> None:
    """Add bug card columns to cards table if they don't exist."""
    from sqlalchemy import text as sa_text

    columns = [
        ("card_type", "VARCHAR(50) DEFAULT 'normal' NOT NULL"),
        ("origin_task_id", "VARCHAR(36)"),
        ("severity", "VARCHAR(50)"),
        ("expected_behavior", "TEXT"),
        ("observed_behavior", "TEXT"),
        ("steps_to_reproduce", "TEXT"),
        ("action_plan", "TEXT"),
        ("linked_test_task_ids", "JSON"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE cards ADD COLUMN {col_name} {col_type}")
                )
            except Exception:
                pass


async def _migrate_add_task_requirement_gate_card_column() -> None:
    """Add the human-controlled task requirement gate skip to cards."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE cards ADD COLUMN "
                    "skip_task_requirement_link_gate BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_skip_rules_coverage() -> None:
    """Add skip_rules_coverage column to specs table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE specs ADD COLUMN skip_rules_coverage BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_skip_trs_coverage() -> None:
    """Add skip_trs_coverage column to specs table if it doesn't exist."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE specs ADD COLUMN skip_trs_coverage BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_decisions_columns() -> None:
    """Add decisions JSON column and skip_decisions_coverage flag to specs.

    Spec 0eb51d3e+decisions formalization — idempotent, defaults preserve
    backward-compat (skip=True means no gate change on existing specs).
    """
    from sqlalchemy import text as sa_text

    columns = [
        ("decisions", "JSON"),
        ("skip_decisions_coverage", "BOOLEAN DEFAULT true NOT NULL"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                col_type_sqlite = col_type.replace("true", "1").replace("false", "0")
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    )
                )
            except Exception:
                pass


async def _migrate_decisions_default_false() -> None:
    """Ideação #10 Fase 1: flip spec.skip_decisions_coverage default from True→False.

    Backward-compat: only NEW inserts get False; existing rows keep their
    current value. SQLite does not support changing a column default in place,
    so the model default handles future ORM inserts. This step is an idempotent
    no-op for existing Local First databases.
    """
    return None


async def _migrate_add_archive_columns() -> None:
    """Add archived and pre_archive_status columns to ideations, refinements, specs, cards."""
    from sqlalchemy import text as sa_text

    tables = ["ideations", "refinements", "specs", "cards"]
    columns = [
        ("archived", "BOOLEAN DEFAULT false NOT NULL"),
        ("pre_archive_status", "VARCHAR(50)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_add_spec_edition() -> None:
    """Add the human-facing Spec edition counter with a legacy-safe backfill."""

    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE specs ADD COLUMN edition INTEGER DEFAULT 1 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_spec_validation_columns() -> None:
    """Add spec validation columns: skip_contract_coverage, skip_qualitative_validation, validation_threshold, evaluations."""
    from sqlalchemy import text as sa_text

    columns = [
        ("skip_contract_coverage", "BOOLEAN DEFAULT false NOT NULL"),
        ("skip_qualitative_validation", "BOOLEAN DEFAULT false NOT NULL"),
        ("validation_threshold", "INTEGER"),
        ("evaluations", "JSON"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                col_type_sqlite = col_type.replace("false", "0")
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_ir_or_columns() -> None:
    """Add first-class IR/OR JSON columns and coverage flags to specs."""
    from sqlalchemy import text as sa_text

    columns = [
        ("integration_requirements", "JSON"),
        ("observability_requirements", "JSON"),
        ("skip_ir_coverage", "BOOLEAN DEFAULT false NOT NULL"),
        ("skip_or_coverage", "BOOLEAN DEFAULT false NOT NULL"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                col_type_sqlite = col_type.replace("false", "0")
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE specs ADD COLUMN {col_name} {col_type_sqlite}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_spec_validation_gate_columns() -> None:
    """Add Spec Validation Gate columns: validations (JSON history) and current_validation_id (pointer).

    Grandfathered: specs already in validated/in_progress/done status get validations=[] and
    current_validation_id=NULL — no retroactive lock applied.
    """
    from sqlalchemy import text as sa_text

    columns = [
        ("validations", "JSON"),
        ("current_validation_id", "VARCHAR(32)"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in columns:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE specs ADD COLUMN {col_name} {col_type}")
                )
            except Exception:
                pass


async def _migrate_add_ideation_skip_ambiguity_gate() -> None:
    """Add skip_ambiguity_gate column to the ideations table if it doesn't exist.

    Spec 2485780b (Max ambiguity gate) — TR3/TR13: an explicit top-level
    per-ideation boolean opt-out of the board ambiguity gate, default false.
    Idempotent via SQLite duplicate-column handling. Existing ideations read as
    false after migration (legacy-safe).
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE ideations ADD COLUMN skip_ambiguity_gate BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_refinement_skip_ambiguity_gate() -> None:
    """Add the legacy-safe, human-only Refinement ambiguity override."""

    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE refinements ADD COLUMN "
                    "skip_ambiguity_gate BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
        except Exception:
            pass


async def _migrate_heal_task_validation_field_names() -> None:
    """One-shot healing for pre-existing card.validations records that used legacy
    field names (estimated_completeness, estimated_drift, outcome, reviewer_id,
    general_justification) without the clean frontend aliases.

    Adds the clean aliases (completeness, drift, verdict, evaluator_id, summary)
    to every legacy record in-place. Also populates card.conclusions with a
    derived entry when a success validation exists but no conclusion was recorded
    (fixes the gap where submit_task_validation auto-routed to done without
    populating the Conclusion tab).

    Idempotent: safe to run multiple times. Records that already have the clean
    aliases are left untouched.
    """
    import json as _json
    from datetime import datetime, timezone
    from sqlalchemy import JSON as sa_JSON
    from sqlalchemy import bindparam, text as sa_text

    async with get_session_factory()() as db:
        # Load all cards that have any validations or might need healing.
        # Using raw SQL to avoid ORM overhead for this migration.
        try:
            result = await db.execute(
                sa_text(
                    "SELECT id, validations, conclusions FROM cards WHERE validations IS NOT NULL"
                )
            )
            rows = result.fetchall()
        except Exception:
            # Table doesn't exist yet — nothing to heal
            return

        if not rows:
            return

        healed_count = 0
        for row in rows:
            card_id = row[0]
            raw_validations = row[1]
            raw_conclusions = row[2]

            # Legacy SQLite rows may expose JSON as text or decoded mappings.
            if isinstance(raw_validations, str):
                try:
                    validations = _json.loads(raw_validations)
                except Exception:
                    continue
            else:
                validations = raw_validations

            if not validations:
                continue

            modified = False
            latest_success_validation = None

            for v in validations:
                if not isinstance(v, dict):
                    continue
                # Add clean aliases if missing
                if "completeness" not in v and "estimated_completeness" in v:
                    v["completeness"] = v["estimated_completeness"]
                    modified = True
                if "drift" not in v and "estimated_drift" in v:
                    v["drift"] = v["estimated_drift"]
                    modified = True
                if "verdict" not in v and "outcome" in v:
                    v["verdict"] = "pass" if v["outcome"] == "success" else "fail"
                    modified = True
                if "evaluator_id" not in v and "reviewer_id" in v:
                    v["evaluator_id"] = v["reviewer_id"]
                    modified = True
                if "summary" not in v and "general_justification" in v:
                    v["summary"] = v["general_justification"]
                    modified = True
                # Track the latest success validation for conclusion auto-population
                if v.get("outcome") == "success" or v.get("verdict") == "pass":
                    latest_success_validation = v

            # Conclusion auto-population: if we have a success validation but no
            # conclusions, derive one from the validation.
            if isinstance(raw_conclusions, str):
                try:
                    conclusions = (
                        _json.loads(raw_conclusions) if raw_conclusions else []
                    )
                except Exception:
                    conclusions = []
            else:
                conclusions = raw_conclusions or []

            needs_conclusion = latest_success_validation is not None and (
                not conclusions or len(conclusions) == 0
            )
            if needs_conclusion:
                v = latest_success_validation
                conclusions = [
                    {
                        "text": v.get("general_justification")
                        or v.get("summary")
                        or "",
                        "author_id": v.get("reviewer_id")
                        or v.get("evaluator_id")
                        or "",
                        "created_at": v.get("created_at")
                        or datetime.now(timezone.utc).isoformat(),
                        "completeness": v.get(
                            "completeness", v.get("estimated_completeness", 0)
                        ),
                        "completeness_justification": v.get(
                            "completeness_justification", ""
                        ),
                        "drift": v.get("drift", v.get("estimated_drift", 0)),
                        "drift_justification": v.get("drift_justification", ""),
                        "source": "task_validation_heal",
                        "validation_id": v.get("id"),
                    }
                ]
                modified = True

            if modified:
                if needs_conclusion:
                    stmt = sa_text(
                        "UPDATE cards "
                        "SET validations = :validations, conclusions = :conclusions "
                        "WHERE id = :id"
                    ).bindparams(
                        bindparam("validations", type_=sa_JSON),
                        bindparam("conclusions", type_=sa_JSON),
                    )
                    await db.execute(
                        stmt,
                        {
                            "id": card_id,
                            "validations": validations,
                            "conclusions": conclusions,
                        },
                    )
                else:
                    stmt = sa_text(
                        "UPDATE cards SET validations = :validations WHERE id = :id"
                    ).bindparams(bindparam("validations", type_=sa_JSON))
                    await db.execute(
                        stmt,
                        {"id": card_id, "validations": validations},
                    )
                healed_count += 1

        if healed_count > 0:
            await db.commit()
            import logging

            logging.getLogger("okto_pulse.migrations").info(
                f"Task validation healing: patched {healed_count} card(s) with clean "
                f"aliases and/or auto-populated conclusions."
            )


async def _migrate_status_renames() -> None:
    """Migrate old status values to new ones.

    - Ideation: 'refined' → 'done' (removed status)
    - Refinement: 'in_progress' → 'review' (renamed)
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        # Ideation: 'refined' no longer exists — map to 'done'
        try:
            await conn.execute(
                sa_text("UPDATE ideations SET status = 'done' WHERE status = 'refined'")
            )
        except Exception:
            pass

        # Refinement: 'in_progress' renamed to 'review'
        try:
            await conn.execute(
                sa_text(
                    "UPDATE refinements SET status = 'review' WHERE status = 'in_progress'"
                )
            )
        except Exception:
            pass


async def _migrate_add_permission_columns() -> None:
    """Add permission_flags and preset_id to agents, permission_overrides to agent_boards."""
    from sqlalchemy import text as sa_text

    agent_columns = [
        ("permission_flags", "JSON"),
        ("preset_id", "VARCHAR(36)"),
    ]
    board_columns = [
        ("permission_overrides", "JSON"),
    ]
    async with get_engine().begin() as conn:
        for col_name, col_type in agent_columns:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE agents ADD COLUMN {col_name} {col_type}")
                )
            except Exception:
                pass
        for col_name, col_type in board_columns:
            try:
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE agent_boards ADD COLUMN {col_name} {col_type}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_event_tables() -> None:
    """Create domain_events + domain_event_handler_executions tables.

    Idempotent: uses CREATE TABLE IF NOT EXISTS. Must run BEFORE
    Base.metadata.create_all so the two tables exist by the time the
    dispatcher starts consuming them.
    """
    from sqlalchemy import text as sa_text

    ts_type = "TIMESTAMP"
    json_type = "JSON"

    async with get_engine().begin() as conn:
        await conn.execute(
            sa_text(
                f"""
            CREATE TABLE IF NOT EXISTS domain_events (
                id VARCHAR(36) PRIMARY KEY,
                event_type VARCHAR(100) NOT NULL,
                board_id VARCHAR(36) NOT NULL,
                actor_id VARCHAR(36),
                actor_type VARCHAR(20) NOT NULL DEFAULT 'user',
                payload_json {json_type} NOT NULL,
                occurred_at {ts_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (board_id) REFERENCES boards(id) ON DELETE CASCADE
            )
        """
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_domain_events_event_type "
                "ON domain_events(event_type)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_domain_events_board_id "
                "ON domain_events(board_id)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_domain_events_occurred_at "
                "ON domain_events(occurred_at)"
            )
        )

        await conn.execute(
            sa_text(
                f"""
            CREATE TABLE IF NOT EXISTS domain_event_handler_executions (
                id VARCHAR(36) PRIMARY KEY,
                event_id VARCHAR(36) NOT NULL,
                handler_name VARCHAR(100) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error VARCHAR(500),
                processed_at {ts_type},
                next_attempt_at {ts_type},
                FOREIGN KEY (event_id) REFERENCES domain_events(id) ON DELETE CASCADE,
                CONSTRAINT uq_deh_event_handler UNIQUE (event_id, handler_name)
            )
        """
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_deh_status_next_attempt "
                "ON domain_event_handler_executions(status, next_attempt_at)"
            )
        )


async def _migrate_story_ideation_single_link() -> None:
    """Enforce one Ideation link per Story while preserving many Stories per Ideation."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(sa_text("SELECT 1 FROM story_ideation_links LIMIT 0"))
        except Exception:
            return

        await conn.execute(
            sa_text(
                """
            DELETE FROM story_ideation_links
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY story_id
                            ORDER BY created_at, id
                        ) AS rn
                    FROM story_ideation_links
                ) ranked
                WHERE ranked.rn > 1
            )
            """
            )
        )
        await conn.execute(
            sa_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_story_ideation_link_story "
                "ON story_ideation_links (story_id)"
            )
        )


async def _migrate_add_card_sprint_id() -> None:
    """Add sprint_id FK column to cards table."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE cards ADD COLUMN sprint_id VARCHAR(36) REFERENCES sprints(id) ON DELETE SET NULL"
                )
            )
        except Exception:
            pass


async def _migrate_add_card_knowledge_bases() -> None:
    """Add knowledge_bases JSON column to cards table."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text("ALTER TABLE cards ADD COLUMN knowledge_bases JSON")
            )
        except Exception:
            pass


async def _migrate_add_knowledge_source_columns() -> None:
    """Add provenance columns to entity knowledge base tables."""
    from sqlalchemy import text as sa_text

    tables = [
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    ]
    columns = [
        ("source_type", "VARCHAR(50)"),
        ("source_id", "VARCHAR(36)"),
        ("source_title", "VARCHAR(500)"),
        ("source_version", "INTEGER"),
        ("source_kb_id", "VARCHAR(36)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_add_kb_lineage_columns() -> None:
    """Add multi-hop lineage and immutable-content identity to entity KBs.

    ``root_source_kb_id`` = the INITIAL canonical origin KB (preserved across
    ideation->refinement->spec hops); ``immediate_parent_kb_id`` = the direct
    parent KB. Additive + idempotent; ``source_kb_id`` stays the immediate parent
    for back-compat. ``content_hash`` remains nullable so legacy rows are not
    rewritten or assigned fabricated persisted revisions. Existing columns are
    introspected before mutation and the complete contract is post-validated;
    DDL failures are never swallowed."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    tables = (
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    )
    columns = {
        "root_source_kb_id": "varchar(36)",
        "immediate_parent_kb_id": "varchar(36)",
        "content_hash": "varchar(64)",
    }

    def _contracts(sync_conn: object) -> dict[str, dict[str, dict[str, object]]]:
        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        missing_tables = sorted(set(tables) - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "KB lineage migration is missing target tables: "
                + ", ".join(missing_tables)
            )
        return {
            table_name: {
                str(column["name"]): column
                for column in inspector.get_columns(table_name)
                if str(column["name"]) in columns
            }
            for table_name in tables
        }

    def _require_canonical(
        contracts: dict[str, dict[str, dict[str, object]]],
        *,
        require_present: bool,
    ) -> None:
        for table_name, observed_columns in contracts.items():
            for column_name, expected_type in columns.items():
                column = observed_columns.get(column_name)
                if column is None:
                    if require_present:
                        raise RuntimeError(
                            "KB lineage migration left a missing column: "
                            f"{table_name}.{column_name}"
                        )
                    continue
                observed_type = _normalize_sqlite_contract_type(column.get("type"))
                observed_nullable = bool(column.get("nullable"))
                observed_default = _normalize_sqlite_contract_default(
                    column.get("default")
                )
                if (
                    observed_type != expected_type
                    or not observed_nullable
                    or observed_default is not None
                ):
                    raise RuntimeError(
                        "KB lineage column is non-canonical: "
                        f"{table_name}.{column_name} "
                        f"type={observed_type!r} nullable={observed_nullable!r} "
                        f"default={observed_default!r}"
                    )

    async with get_engine().begin() as conn:
        if conn.dialect.name == "sqlite":
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        before = await conn.run_sync(_contracts)
        _require_canonical(before, require_present=False)

        for table_name, observed_columns in before.items():
            for column_name, column_type in columns.items():
                if column_name in observed_columns:
                    continue
                await conn.execute(
                    sa_text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {column_type.upper()}'
                    )
                )

        after = await conn.run_sync(_contracts)
        _require_canonical(after, require_present=True)


async def _migrate_add_kb_governance_metadata() -> str | None:
    """Add the optional governance metadata envelope to entity KB tables.

    Introspection happens before any mutation so a pre-existing malformed
    column fails closed without extending the remaining tables.  A second
    introspection validates the complete physical contract after the additive
    changes, making replay both idempotent and observable.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    table_names = (
        "ideation_knowledge_bases",
        "refinement_knowledge_bases",
        "spec_knowledge_bases",
    )
    column_name = "governance_metadata"

    def _contracts(sync_conn: object) -> dict[str, dict[str, object] | None]:
        inspector = sa_inspect(sync_conn)
        existing_tables = set(inspector.get_table_names())
        missing_tables = sorted(set(table_names) - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "KB governance metadata migration is missing target tables: "
                + ", ".join(missing_tables)
            )

        contracts: dict[str, dict[str, object] | None] = {}
        for table_name in table_names:
            columns = {
                str(column["name"]): column
                for column in inspector.get_columns(table_name)
            }
            contracts[table_name] = columns.get(column_name)
        return contracts

    def _require_canonical(
        contracts: dict[str, dict[str, object] | None],
        *,
        require_present: bool,
    ) -> None:
        for table_name, column in contracts.items():
            if column is None:
                if require_present:
                    raise RuntimeError(
                        "KB governance metadata migration left a missing column: "
                        f"{table_name}.{column_name}"
                    )
                continue
            observed_type = _normalize_sqlite_contract_type(column.get("type"))
            observed_nullable = bool(column.get("nullable"))
            observed_default = _normalize_sqlite_contract_default(column.get("default"))
            if (
                observed_type != "json"
                or not observed_nullable
                or observed_default is not None
            ):
                raise RuntimeError(
                    "KB governance metadata column is non-canonical: "
                    f"{table_name}.{column_name} "
                    f"type={observed_type!r} nullable={observed_nullable!r} "
                    f"default={observed_default!r}"
                )

    changed = False
    async with get_engine().begin() as conn:
        if conn.dialect.name == "sqlite":
            # Python's sqlite3 legacy transaction mode does not BEGIN for DDL.
            # Pin all three ALTERs and the postcondition audit to one physical
            # transaction so a mid-step failure cannot leave a partial schema.
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        before = await conn.run_sync(_contracts)
        _require_canonical(before, require_present=False)

        for table_name, column in before.items():
            if column is not None:
                continue
            await conn.execute(
                sa_text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" JSON')
            )
            changed = True

        after = await conn.run_sync(_contracts)
        _require_canonical(after, require_present=True)

    return None if changed else "skipped"


async def _upgrade_knowledge_propagation_scope_board_audit_identity(
    engine: object,
) -> bool:
    """Remove the historical board CASCADE FK without losing audit rows.

    ``knowledge_propagation_scopes.board_id`` is an immutable audit identity,
    not ownership. A board delete must therefore leave the whole propagation
    cluster reconstructible. SQLite cannot drop a foreign key in place, so a
    database created by an earlier IMP3 build is rebuilt atomically while
    foreign-key actions are disabled on this one migration connection. Every
    child FK is checked again before the connection is returned to the pool.
    """

    from sqlalchemy import MetaData
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateIndex, CreateTable

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgePropagationScopeRecord,
    )

    scope_table = KnowledgePropagationScopeRecord.__table__
    temporary_name = f"{scope_table.name}__audit_identity_upgrade"

    def _scope_upgrade_state(
        sync_conn: object,
    ) -> tuple[tuple[dict[str, object], ...], bool]:
        inspector = sa_inspect(sync_conn)
        if scope_table.name not in set(inspector.get_table_names()):
            return (), False
        contract = _sqlite_owned_table_contract(sync_conn, scope_table)
        expected = dict(contract["expected"])
        observed = dict(contract["observed"])
        expected_columns = expected.pop("columns")
        observed_columns = observed.pop("columns")
        expected.pop("foreign_keys")
        observed.pop("foreign_keys")
        if observed != expected:
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "found unrelated contract drift"
            )
        columns_reordered = observed_columns != expected_columns
        if columns_reordered and tuple(
            sorted(observed_columns, key=lambda item: str(item[0]))
        ) != tuple(sorted(expected_columns, key=lambda item: str(item[0]))):
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "found non-canonical column drift"
            )
        return (
            tuple(inspector.get_foreign_keys(scope_table.name)),
            columns_reordered,
        )

    def _rebuild_scope(sync_conn: object) -> None:
        inspector = sa_inspect(sync_conn)
        if temporary_name in set(inspector.get_table_names()):
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "found a stale temporary table"
            )
        before_count = int(
            sync_conn.exec_driver_sql(
                f'SELECT count(*) FROM "{scope_table.name}"'
            ).scalar_one()
        )
        temporary_metadata = MetaData()
        temporary_table = scope_table.to_metadata(
            temporary_metadata,
            name=temporary_name,
        )
        sync_conn.execute(CreateTable(temporary_table))
        quote = sync_conn.dialect.identifier_preparer.quote
        columns = ", ".join(quote(column.name) for column in scope_table.columns)
        sync_conn.exec_driver_sql(
            f'INSERT INTO "{temporary_name}" ({columns}) '
            f'SELECT {columns} FROM "{scope_table.name}"'
        )
        sync_conn.exec_driver_sql(f'DROP TABLE "{scope_table.name}"')
        sync_conn.exec_driver_sql(
            f'ALTER TABLE "{temporary_name}" RENAME TO "{scope_table.name}"'
        )
        for index in sorted(
            scope_table.indexes,
            key=lambda item: str(item.name),
        ):
            sync_conn.execute(CreateIndex(index))
        after_count = int(
            sync_conn.exec_driver_sql(
                f'SELECT count(*) FROM "{scope_table.name}"'
            ).scalar_one()
        )
        if after_count != before_count:
            raise RuntimeError(
                "knowledge propagation scope audit-identity upgrade "
                "did not preserve every scope row"
            )

    async with engine.connect() as conn:
        if conn.dialect.name != "sqlite":
            return False
        foreign_keys, columns_reordered = await conn.run_sync(_scope_upgrade_state)
        if not foreign_keys and not columns_reordered:
            return False
        if foreign_keys:
            board_foreign_keys = tuple(
                item
                for item in foreign_keys
                if tuple(item.get("constrained_columns") or ()) == ("board_id",)
                and item.get("referred_table") == "boards"
                and tuple(item.get("referred_columns") or ()) == ("id",)
            )
            if len(foreign_keys) != 1 or len(board_foreign_keys) != 1:
                raise RuntimeError(
                    "knowledge propagation scope has unexpected foreign-key drift"
                )
            options = board_foreign_keys[0].get("options") or {}
            if str(options.get("ondelete") or "").upper() != "CASCADE":
                raise RuntimeError(
                    "knowledge propagation scope board foreign key is non-canonical"
                )

        # Introspection can establish SQLAlchemy's logical transaction even
        # though SQLite has not started a physical writer transaction.
        await conn.rollback()
        original_foreign_keys = int(
            (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
        )
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()) != 0:
            raise RuntimeError(
                "knowledge propagation scope upgrade could not suspend "
                "foreign-key actions"
            )
        try:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            await conn.run_sync(_rebuild_scope)
            violations = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise RuntimeError(
                    "knowledge propagation scope audit-identity upgrade "
                    f"left foreign-key violations: {violations!r}"
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.exec_driver_sql(
                f"PRAGMA foreign_keys={1 if original_foreign_keys else 0}"
            )
            restored = int(
                (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if restored != original_foreign_keys:
                raise RuntimeError(
                    "knowledge propagation scope upgrade did not restore "
                    "foreign-key enforcement"
                )
    return True


async def _upgrade_knowledge_propagation_activation_boundary(
    engine: object,
) -> bool:
    """Add and conservatively backfill the first-v2 activation boundary.

    Existing inactive/grandfathered scopes deliberately retain ``NULL``.
    Existing active scopes predate the boundary column, so their earliest
    applied non-grandfather ledger timestamp is the strongest durable
    evidence available. ``updated_at``/``created_at`` are conservative
    fallbacks for installations whose historical ledger is incomplete.
    """

    from sqlalchemy import inspect as sa_inspect

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
    )

    scope_table = KnowledgePropagationScopeRecord.__table__
    ledger_table = KnowledgeMutationLedgerRecord.__table__
    column = scope_table.c.v2_activated_at

    def _state(sync_conn: object) -> tuple[bool, bool]:
        inspector = sa_inspect(sync_conn)
        tables = set(inspector.get_table_names())
        if scope_table.name not in tables:
            return False, ledger_table.name in tables
        columns = {
            str(item["name"]): item for item in inspector.get_columns(scope_table.name)
        }
        observed = columns.get(column.name)
        if observed is not None:
            expected_contract = (
                _normalize_sqlite_contract_type(
                    column.type.compile(dialect=sync_conn.dialect)
                ),
                bool(column.nullable),
                _expected_sqlite_server_default(sync_conn, column),
            )
            observed_contract = (
                _normalize_sqlite_contract_type(observed["type"]),
                bool(observed["nullable"]),
                _normalize_sqlite_contract_default(observed.get("default")),
            )
            if observed_contract != expected_contract:
                raise RuntimeError(
                    "knowledge propagation activation boundary column is non-canonical"
                )
        return observed is not None, ledger_table.name in tables

    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return False
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        column_present, ledger_present = await conn.run_sync(_state)
        scope_present = await conn.run_sync(
            lambda sync_conn: (
                scope_table.name in set(sa_inspect(sync_conn).get_table_names())
            )
        )
        if not scope_present:
            return False

        changed = False
        if not column_present:
            await conn.exec_driver_sql(
                f'ALTER TABLE "{scope_table.name}" '
                'ADD COLUMN "v2_activated_at" DATETIME'
            )
            changed = True

        if ledger_present:
            result = await conn.exec_driver_sql(
                f"""
UPDATE "{scope_table.name}" AS scope
SET v2_activated_at = COALESCE(
    (
        SELECT MIN(ledger.applied_at)
        FROM "{ledger_table.name}" AS ledger
        WHERE ledger.scope_id = scope.id
          AND ledger.outcome = 'applied'
          AND ledger.operation_kind <> 'grandfather'
    ),
    scope.updated_at,
    scope.created_at
)
WHERE scope.v2_active = 1
  AND scope.v2_activated_at IS NULL
"""
            )
        else:
            result = await conn.exec_driver_sql(
                f"""
UPDATE "{scope_table.name}"
SET v2_activated_at = COALESCE(updated_at, created_at)
WHERE v2_active = 1
  AND v2_activated_at IS NULL
"""
            )
        if int(getattr(result, "rowcount", 0) or 0) > 0:
            changed = True

        invalid_authority_boundary = int(
            (
                await conn.exec_driver_sql(
                    f'SELECT count(*) FROM "{scope_table.name}" '
                    "WHERE (v2_active = 1 AND v2_activated_at IS NULL) "
                    "OR (v2_active = 0 AND v2_activated_at IS NOT NULL)"
                )
            ).scalar_one()
        )
        if invalid_authority_boundary:
            raise RuntimeError(
                "knowledge propagation activation boundary backfill "
                "found "
                f"{invalid_authority_boundary} scope(s) with inconsistent "
                "v2 authority"
            )
        await conn.run_sync(_state)
        return changed


async def _upgrade_knowledge_propagation_relink_operation_kind(
    engine: object,
) -> bool:
    """Expand immutable ledger/attempt CHECKs for ``relink_reset``.

    SQLite cannot alter a CHECK constraint in place. Only the exact preceding
    IMP3 contract (without ``relink_reset``) is accepted for rebuild; any
    other drift fails closed. Rows, indexes, foreign keys, and append-only
    trigger ownership are re-audited by the enclosing schema migration.
    """

    from sqlalchemy import MetaData
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateIndex, CreateTable

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgeMutationAttemptRecord,
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
    )

    tables = (
        KnowledgeMutationLedgerRecord.__table__,
        KnowledgeMutationAttemptRecord.__table__,
    )

    def _previous_contract(expected: dict[str, object]) -> dict[str, object]:
        prior = dict(expected)
        checks = []
        replacement_count = 0
        for name, expression in expected["checks"]:
            prior_expression = str(expression).replace(
                ",'relink_reset'",
                "",
            )
            if prior_expression != expression:
                replacement_count += 1
            checks.append((name, prior_expression))
        if replacement_count != 1:
            raise RuntimeError(
                "knowledge propagation relink operation contract "
                "could not derive its predecessor"
            )
        prior["checks"] = tuple(checks)
        return prior

    def _tables_to_rebuild(sync_conn: object) -> tuple[object, ...]:
        existing = set(sa_inspect(sync_conn).get_table_names())
        rebuild: list[object] = []
        for table in tables:
            if table.name not in existing:
                continue
            contract = _sqlite_owned_table_contract(sync_conn, table)
            if contract["observed"] == contract["expected"]:
                continue
            if contract["observed"] != _previous_contract(contract["expected"]):
                raise RuntimeError(
                    "knowledge propagation relink operation migration "
                    f"found non-canonical drift in {table.name}"
                )
            rebuild.append(table)
        return tuple(rebuild)

    def _rebuild_table(sync_conn: object, table: object) -> None:
        temporary_name = f"{table.name}__relink_operation_upgrade"
        inspector = sa_inspect(sync_conn)
        if temporary_name in set(inspector.get_table_names()):
            raise RuntimeError(
                "knowledge propagation relink operation upgrade "
                f"found stale table {temporary_name}"
            )
        quote = sync_conn.dialect.identifier_preparer.quote
        before_ids = tuple(
            str(row[0])
            for row in sync_conn.exec_driver_sql(
                f"SELECT {quote(next(iter(table.primary_key.columns)).name)} "
                f'FROM "{table.name}" ORDER BY 1'
            ).all()
        )

        temporary_metadata = MetaData()
        KnowledgePropagationScopeRecord.__table__.to_metadata(temporary_metadata)
        temporary_table = table.to_metadata(
            temporary_metadata,
            name=temporary_name,
        )
        sync_conn.execute(CreateTable(temporary_table))
        columns = ", ".join(quote(column.name) for column in table.columns)
        sync_conn.exec_driver_sql(
            f'INSERT INTO "{temporary_name}" ({columns}) '
            f'SELECT {columns} FROM "{table.name}"'
        )
        sync_conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
        sync_conn.exec_driver_sql(
            f'ALTER TABLE "{temporary_name}" RENAME TO "{table.name}"'
        )
        for index in sorted(table.indexes, key=lambda item: str(item.name)):
            sync_conn.execute(CreateIndex(index))

        after_ids = tuple(
            str(row[0])
            for row in sync_conn.exec_driver_sql(
                f"SELECT {quote(next(iter(table.primary_key.columns)).name)} "
                f'FROM "{table.name}" ORDER BY 1'
            ).all()
        )
        if after_ids != before_ids:
            raise RuntimeError(
                "knowledge propagation relink operation upgrade "
                f"did not preserve every row in {table.name}"
            )
        contract = _sqlite_owned_table_contract(sync_conn, table)
        if contract["observed"] != contract["expected"]:
            raise RuntimeError(
                "knowledge propagation relink operation upgrade "
                f"left a non-canonical table: {table.name}"
            )

    async with engine.connect() as conn:
        if conn.dialect.name != "sqlite":
            return False
        rebuild = await conn.run_sync(_tables_to_rebuild)
        if not rebuild:
            return False

        await conn.rollback()
        original_foreign_keys = int(
            (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
        )
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()) != 0:
            raise RuntimeError(
                "knowledge propagation relink operation upgrade could not "
                "suspend foreign-key actions"
            )
        try:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            for table in rebuild:
                await conn.run_sync(
                    lambda sync_conn, owned_table=table: _rebuild_table(
                        sync_conn,
                        owned_table,
                    )
                )
            violations = (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise RuntimeError(
                    "knowledge propagation relink operation upgrade left "
                    f"foreign-key violations: {violations!r}"
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.exec_driver_sql(
                f"PRAGMA foreign_keys={1 if original_foreign_keys else 0}"
            )
            restored = int(
                (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if restored != original_foreign_keys:
                raise RuntimeError(
                    "knowledge propagation relink operation upgrade did not "
                    "restore foreign-key enforcement"
                )
    return True


async def _upgrade_knowledge_snapshot_governance_metadata(
    engine: object,
) -> bool:
    """Add immutable snapshot governance metadata from its exact predecessor.

    The predecessor is the current snapshot table contract with only the final
    nullable JSON column absent.  SQLite appends that column in place, which
    preserves every existing row, blob, and content hash byte-for-byte.  The
    sole trigger whose contract changes is accepted only in its exact previous
    or current form; any other owned table/trigger drift fails before DDL.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        KnowledgeSnapshotRecord,
    )

    table = KnowledgeSnapshotRecord.__table__
    column = table.c.governance_metadata
    current_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=True,
    )
    predecessor_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=False,
        allow_board_erasure=True,
    )
    legacy_current_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=True,
        allow_board_erasure=False,
    )
    legacy_predecessor_triggers = _knowledge_propagation_v2_trigger_manifest(
        include_snapshot_governance_metadata=False,
        allow_board_erasure=False,
    )
    content_trigger_name = (
        f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}_{table.name}_content_update"
    )
    current_content_trigger = current_triggers[content_trigger_name][1]

    def _state(sync_conn: object) -> tuple[str, str]:
        inspector = sa_inspect(sync_conn)
        if table.name not in set(inspector.get_table_names()):
            return "absent", "missing"

        contract = _sqlite_owned_table_contract(sync_conn, table)
        expected = dict(contract["expected"])
        observed = contract["observed"]
        expected_columns = tuple(expected["columns"])
        expected_column = (
            str(column.name),
            _normalize_sqlite_contract_type(
                column.type.compile(dialect=sync_conn.dialect)
            ),
            bool(column.nullable),
            _expected_sqlite_server_default(sync_conn, column),
        )
        if not expected_columns or expected_columns[-1] != expected_column:
            raise RuntimeError(
                "knowledge snapshot governance metadata must be the final "
                "canonical table column"
            )
        predecessor = dict(expected)
        predecessor["columns"] = expected_columns[:-1]
        if observed == expected:
            table_state = "current"
        elif observed == predecessor:
            table_state = "predecessor"
        else:
            raise RuntimeError(
                "knowledge snapshot governance metadata migration found "
                "non-canonical table drift"
            )

        trigger_rows = tuple(
            sync_conn.exec_driver_sql(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE ?",
                (f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%",),
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(trigger["name"]): trigger for trigger in trigger_rows}
        unexpected = set(existing_triggers) - set(current_triggers)
        if unexpected:
            raise RuntimeError(
                "knowledge snapshot governance metadata migration found "
                "unexpected owned triggers: " + ", ".join(sorted(unexpected))
            )

        content_trigger_state = "missing"
        for trigger_name, trigger in existing_triggers.items():
            expected_table, current_sql = current_triggers[trigger_name]
            predecessor_table, predecessor_sql = predecessor_triggers[trigger_name]
            observed_table = str(trigger["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                trigger["sql"]
            )
            current_match = (
                observed_table == expected_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(current_sql)
            )
            predecessor_match = (
                observed_table == predecessor_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(
                    predecessor_sql
                )
            )
            legacy_current_table, legacy_current_sql = legacy_current_triggers[
                trigger_name
            ]
            legacy_predecessor_table, legacy_predecessor_sql = (
                legacy_predecessor_triggers[trigger_name]
            )
            legacy_current_match = (
                observed_table == legacy_current_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(
                    legacy_current_sql
                )
            )
            legacy_predecessor_match = (
                observed_table == legacy_predecessor_table
                and observed_sql
                == normalize_global_discovery_source_revision_trigger_sql(
                    legacy_predecessor_sql
                )
            )
            if trigger_name == content_trigger_name:
                if current_match or legacy_current_match:
                    content_trigger_state = "current"
                elif predecessor_match or legacy_predecessor_match:
                    content_trigger_state = "predecessor"
                else:
                    raise RuntimeError(
                        "knowledge snapshot governance metadata migration found "
                        "non-canonical immutable trigger drift"
                    )
            elif not (current_match or legacy_current_match):
                raise RuntimeError(
                    "knowledge propagation v2 trigger is corrupt: " + trigger_name
                )

        if table_state == "predecessor" and content_trigger_state == "current":
            raise RuntimeError(
                "knowledge snapshot governance metadata migration found a "
                "current trigger on the predecessor table"
            )
        return table_state, content_trigger_state

    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return False
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_state, trigger_state = await conn.run_sync(_state)
        if table_state == "absent":
            return False

        changed = False
        if table_state == "predecessor":
            await conn.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" JSON'
            )
            changed = True

        if trigger_state == "predecessor":
            await conn.exec_driver_sql(f'DROP TRIGGER "{content_trigger_name}"')
            await conn.execute(sa_text(current_content_trigger))
            changed = True

        final_table_state, final_trigger_state = await conn.run_sync(_state)
        if final_table_state != "current" or final_trigger_state not in {
            "current",
            "missing",
        }:
            raise RuntimeError(
                "knowledge snapshot governance metadata migration postcondition failed"
            )
        return changed


async def _migrate_knowledge_propagation_v2_schema() -> str | None:
    """Converge and prove the additive selective-propagation schema.

    Each owned table is created independently with ``checkfirst`` and audited
    before the next checkpoint.  The enclosing ``BEGIN IMMEDIATE`` makes a
    fault at any checkpoint rollback-safe on SQLite, while replay can also
    resume a database in which an earlier process committed only a prefix.
    Existing legacy KB rows and card JSON are never selected, copied, updated,
    or deleted by this migration.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text
    from sqlalchemy.schema import CreateIndex

    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardErasurePermit,
        KnowledgeAssignmentRecord,
        KnowledgeMutationAttemptRecord,
        KnowledgeMutationLedgerRecord,
        KnowledgePropagationScopeRecord,
        KnowledgeSnapshotRecord,
        KnowledgeTombstoneRecord,
    )

    stages = (
        ("erasure_permit", BoardErasurePermit.__table__),
        ("scope", KnowledgePropagationScopeRecord.__table__),
        ("assignment", KnowledgeAssignmentRecord.__table__),
        ("snapshot", KnowledgeSnapshotRecord.__table__),
        ("tombstone", KnowledgeTombstoneRecord.__table__),
        ("ledger", KnowledgeMutationLedgerRecord.__table__),
        ("attempt", KnowledgeMutationAttemptRecord.__table__),
    )
    owned_tables = tuple(table for _, table in stages)

    def _create_table(sync_conn: object, table: object) -> None:
        table.create(sync_conn, checkfirst=True)

    def _table_names(sync_conn: object) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    def _expected_partial_indexes(sync_conn: object) -> dict[str, str]:
        expected: dict[str, str] = {}
        for table in owned_tables:
            for index in table.indexes:
                sqlite_where = index.dialect_options["sqlite"].get("where")
                if sqlite_where is None:
                    continue
                expected[str(index.name)] = _normalize_sqlite_contract_ddl(
                    CreateIndex(index).compile(
                        dialect=sync_conn.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
        return expected

    def _require_table_contract(sync_conn: object, table: object) -> None:
        contract = _sqlite_owned_table_contract(sync_conn, table)
        if contract["observed"] != contract["expected"]:
            raise RuntimeError(
                "knowledge propagation v2 table has a non-canonical contract: "
                + str(table.name)
            )

    engine = get_engine()
    changed = await _upgrade_knowledge_propagation_activation_boundary(engine)
    changed = (
        await _upgrade_knowledge_propagation_scope_board_audit_identity(engine)
        or changed
    )
    changed = (
        await _upgrade_knowledge_propagation_relink_operation_kind(engine) or changed
    )
    changed = await _upgrade_knowledge_snapshot_governance_metadata(engine) or changed
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            raise RuntimeError(
                "knowledge propagation v2 migration requires Community SQLite"
            )
        # sqlite3 legacy transaction mode does not begin for DDL.  Pin table
        # convergence, trigger installation, and every postcondition audit to
        # one physical writer transaction.
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        existing_tables = await conn.run_sync(_table_names)

        for stage, table in stages:
            if table.name not in existing_tables:
                await conn.run_sync(
                    lambda sync_conn, owned_table=table: _create_table(
                        sync_conn,
                        owned_table,
                    )
                )
                changed = True
                existing_tables.add(table.name)
            await conn.run_sync(
                lambda sync_conn, owned_table=table: _require_table_contract(
                    sync_conn,
                    owned_table,
                )
            )
            _knowledge_propagation_migration_checkpoint(stage)

        expected_partial_indexes = await conn.run_sync(_expected_partial_indexes)
        if expected_partial_indexes:
            placeholders = ", ".join(
                f":index_{position}"
                for position, _ in enumerate(expected_partial_indexes)
            )
            parameters = {
                f"index_{position}": index_name
                for position, index_name in enumerate(sorted(expected_partial_indexes))
            }
            rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, sql FROM sqlite_master "
                            "WHERE type = 'index' "
                            f"AND name IN ({placeholders})"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            observed_partial_indexes = {
                str(row["name"]): _normalize_sqlite_contract_ddl(row["sql"])
                for row in rows
            }
            if observed_partial_indexes != expected_partial_indexes:
                raise RuntimeError(
                    "knowledge propagation v2 partial-index contract drift"
                )

        expected_triggers = knowledge_propagation_v2_trigger_manifest()
        predecessor_triggers = _knowledge_propagation_v2_trigger_manifest(
            include_snapshot_governance_metadata=True,
            allow_board_erasure=False,
        )
        trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing_triggers = {str(row["name"]): row for row in trigger_rows}
        unexpected = set(existing_triggers) - set(expected_triggers)
        if unexpected:
            raise RuntimeError(
                "knowledge propagation v2 has unexpected owned triggers: "
                + ", ".join(sorted(unexpected))
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            existing = existing_triggers.get(trigger_name)
            if existing is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            observed_table = str(existing["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                existing["sql"]
            )
            if observed_table == table_name and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(trigger_sql)
            ):
                continue
            predecessor_table, predecessor_sql = predecessor_triggers[trigger_name]
            if observed_table == predecessor_table and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(predecessor_sql)
            ):
                await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            raise RuntimeError(
                "knowledge propagation v2 trigger is corrupt: " + trigger_name
            )

        final_trigger_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{KNOWLEDGE_PROPAGATION_V2_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        final_triggers = {str(row["name"]): row for row in final_trigger_rows}
        if set(final_triggers) != set(expected_triggers):
            raise RuntimeError(
                "knowledge propagation v2 trigger installation is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected_triggers.items():
            observed = final_triggers[trigger_name]
            if str(
                observed["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                observed["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    "knowledge propagation v2 trigger postcondition failed: "
                    + trigger_name
                )

        for table in owned_tables:
            await conn.run_sync(
                lambda sync_conn, owned_table=table: _require_table_contract(
                    sync_conn,
                    owned_table,
                )
            )

    return None if changed else "skipped"


async def _migrate_add_sprint_scope_fields() -> None:
    """Add objective and expected_outcome columns to sprints table."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        for col in ["objective", "expected_outcome"]:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE sprints ADD COLUMN {col} TEXT")
                )
            except Exception:
                pass


async def _migrate_add_sprint_lane_fields() -> None:
    """Add sprint lane metadata for normal and post-closure hotfix lanes."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE sprints ADD COLUMN lane_type VARCHAR(50) NOT NULL DEFAULT 'normal'"
                )
            )
        except Exception:
            pass
        for col in ["origin_sprint_id", "origin_bug_id"]:
            try:
                await conn.execute(
                    sa_text(f"ALTER TABLE sprints ADD COLUMN {col} VARCHAR(36)")
                )
            except Exception:
                pass

        try:
            await conn.execute(
                sa_text(
                    "UPDATE sprints SET lane_type = 'normal' WHERE lane_type IS NULL"
                )
            )
        except Exception:
            pass


async def _migrate_agent_boards() -> None:
    """Migrate existing agents with board_id to the agent_boards junction table."""
    from sqlalchemy import text as sa_text

    uuid_expr = (
        "lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' ||"
        " substr(hex(randomblob(2)),2) || '-' ||"
        " substr('89ab', abs(random()) % 4 + 1, 1) ||"
        " substr(hex(randomblob(2)),2) || '-' ||"
        " hex(randomblob(6)))"
    )

    async with get_engine().begin() as conn:
        await conn.execute(
            sa_text(
                f"""
            INSERT INTO agent_boards (id, agent_id, board_id, granted_by, granted_at)
            SELECT
                {uuid_expr},
                a.id,
                a.board_id,
                a.created_by,
                a.created_at
            FROM agents a
            WHERE a.board_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM agent_boards ab
                WHERE ab.agent_id = a.id AND ab.board_id = a.board_id
              )
            """
            )
        )


async def _migrate_add_task_validation_columns() -> None:
    """Add task validation gate columns to cards, specs, and sprints."""
    from sqlalchemy import text as sa_text

    # Cards: add validations JSON column
    card_columns = [
        ("validations", "JSON"),
    ]
    # Specs: add require_task_validation + threshold overrides
    spec_columns = [
        ("require_task_validation", "BOOLEAN"),
        ("validation_min_confidence", "INTEGER"),
        ("validation_min_completeness", "INTEGER"),
        ("validation_max_drift", "INTEGER"),
    ]
    # Sprints: same fields
    sprint_columns = [
        ("require_task_validation", "BOOLEAN"),
        ("validation_min_confidence", "INTEGER"),
        ("validation_min_completeness", "INTEGER"),
        ("validation_max_drift", "INTEGER"),
    ]

    migrations = [
        ("cards", card_columns),
        ("specs", spec_columns),
        ("sprints", sprint_columns),
    ]

    async with get_engine().begin() as conn:
        for table, columns in migrations:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_add_consolidation_resilience_columns() -> None:
    """Add resilience columns to consolidation_queue + create
    consolidation_dead_letter table.

    Spec bdcda842 (Consolidation Queue resilience) — TR1 + TR2:
        consolidation_queue gains worker_id, claim_timeout_at, attempts,
        next_retry_at so the at-least-once worker can claim with timeout
        recovery and route exhausted items to a dead-letter table.

    Idempotent via per-column duplicate handling in SQLite.
    create_all on Base.metadata builds the dead-letter table on first run.
    """
    from sqlalchemy import text as sa_text

    queue_columns = [
        ("worker_id", "VARCHAR(64)"),
        ("claim_timeout_at", "TIMESTAMP"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "TIMESTAMP"),
    ]

    async with get_engine().begin() as conn:
        for col_name, col_type in queue_columns:
            try:
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE consolidation_queue "
                        f"ADD COLUMN {col_name} {col_type}"
                    )
                )
            except Exception:
                pass


async def _migrate_add_kg_tick_boards_failed() -> None:
    """Add boards_failed column to kg_tick_runs table (spec R2b, IMPL-2/TR4).

    Tracks how many boards failed (graph corrupt/locked) during a tick without
    aborting the rest of the fleet (FR1/TR2). Idempotent.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE kg_tick_runs "
                    "ADD COLUMN boards_failed INTEGER NOT NULL DEFAULT 0"
                )
            )
        except Exception:
            pass


async def _migrate_drop_spec_skills() -> None:
    """Drop the legacy `spec_skills` table.

    Spec e12c4c20 — Skills removal: the feature is gone in its entirety.
    No data preservation (D1) — the table is dropped if it exists, no-op
    otherwise. Idempotent via `DROP TABLE IF EXISTS`.

    Reader-side defensive handling lives in BaseSchema (extra="ignore"),
    so any historical JSON payload still carrying a `skills` key is
    silently accepted. There is nothing to roll back: the drop is
    definitive.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        await conn.execute(sa_text("DROP TABLE IF EXISTS spec_skills"))


async def _migrate_add_default_config_snapshot() -> None:
    """Add default_config_snapshot JSON column to boards (spec 9df814bc / FR4).

    Stores the applied DefaultBoardConfiguration snapshot metadata OUTSIDE
    Board.settings. New table create happens via create_all; this only ALTERs the
    pre-existing boards table. Duplicate-column errors are swallowed for
    idempotent SQLite startup."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text("ALTER TABLE boards ADD COLUMN default_config_snapshot JSON")
            )
        except Exception:
            pass


async def _migrate_add_default_config_spec_checklist_mode() -> None:
    """Add the curated Spec checklist default to historical template tables.

    NULL is intentional for existing rows: Core projects it as Advisory, which
    preserves the new-board behavior from before this default was configurable.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text(
                    "ALTER TABLE default_board_configurations "
                    "ADD COLUMN spec_checklist_mode VARCHAR(20)"
                )
            )
        except Exception:
            pass


async def _migrate_add_agent_seen_board_id() -> None:
    """Board-scope seen markers so tenant predicates remain fail-closed.

    Legacy rows are backfilled from the referenced artifact when possible and
    then from the agent's legacy/default board. Unresolved rows stay NULL and
    are intentionally invisible to tenant-scoped reads.
    """
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        try:
            await conn.execute(
                sa_text("ALTER TABLE agent_seen_items ADD COLUMN board_id VARCHAR(36)")
            )
        except Exception:
            pass

        await conn.execute(
            sa_text(
                "UPDATE agent_seen_items SET board_id = COALESCE("
                "(SELECT c.board_id FROM comments x JOIN cards c ON c.id = x.card_id "
                " WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT c.board_id FROM qa_items x JOIN cards c ON c.id = x.card_id "
                " WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT s.board_id FROM spec_qa_items x JOIN specs s ON s.id = x.spec_id "
                " WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT i.board_id FROM ideation_qa_items x JOIN ideations i "
                " ON i.id = x.ideation_id WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT r.board_id FROM refinement_qa_items x JOIN refinements r "
                " ON r.id = x.refinement_id WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT s.board_id FROM sprint_qa_items x JOIN sprints s "
                " ON s.id = x.sprint_id WHERE x.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT c.board_id FROM cards c "
                " WHERE c.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT a.board_id FROM activity_logs a "
                " WHERE a.id = agent_seen_items.item_id LIMIT 1),"
                "(SELECT a.board_id FROM agents a "
                " WHERE a.id = agent_seen_items.agent_id LIMIT 1),"
                "(SELECT ab.board_id FROM agent_boards ab "
                " WHERE ab.agent_id = agent_seen_items.agent_id "
                " ORDER BY ab.granted_at ASC LIMIT 1)"
                ") WHERE board_id IS NULL"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE INDEX IF NOT EXISTS ix_agent_seen_items_board_id "
                "ON agent_seen_items (board_id)"
            )
        )


async def _migrate_add_board_guideline_provenance() -> None:
    """Add template provenance columns to board_guidelines (spec 8a2fad91 / FR3).

    ``template_id`` / ``template_version`` / ``guideline_version`` record which
    DefaultBoardConfiguration template (and guideline version) materialized a
    default link. All nullable — legacy/inline links keep NULL provenance (TR5,
    forward-only). Duplicate-column errors are swallowed for idempotent SQLite
    startup."""
    from sqlalchemy import text as sa_text

    columns = (
        ("template_id", "VARCHAR(36)"),
        ("template_version", "INTEGER"),
        ("guideline_version", "INTEGER"),
    )
    async with get_engine().begin() as conn:
        for name, sql_type in columns:
            try:
                await conn.execute(
                    sa_text(
                        f"ALTER TABLE board_guidelines ADD COLUMN {name} {sql_type}"
                    )
                )
            except Exception:
                pass


async def _migrate_guideline_policy_lifecycle_substrate() -> str | None:
    """Converge the additive B04 column/table before the strict B03 audit.

    Existing B03 databases do not have ``binding.state`` or the retirement
    table.  The following B03 convergence step selects the declarative binding
    table and performs an exact SQLite contract audit, so this raw/introspective
    substrate step must precede it in the ordered migration ledger.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineBoardBindingRow,
        GuidelineImportBindingCandidateRow,
        GuidelineRetirementRow,
        GuidelineRevisionNoopReplayRow,
        GuidelineRevisionRow,
    )

    engine = get_engine()
    changed = False
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "guideline policy lifecycle migration supports only SQLite "
                "and PostgreSQL"
            )
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        binding_table = GuidelineBoardBindingRow.__tablename__
        if binding_table not in table_names:
            raise RuntimeError(
                "guideline policy lifecycle migration requires create_all; "
                f"missing table: {binding_table}"
            )
        binding_columns = await conn.run_sync(
            lambda sync_conn: {
                str(column["name"])
                for column in sa_inspect(sync_conn).get_columns(binding_table)
            }
        )
        if "state" not in binding_columns:
            state_constraint = (
                " CONSTRAINT ck_guideline_binding_state "
                "CHECK (state IN ('active', 'unlinked'))"
                if dialect == "sqlite"
                else ""
            )
            await conn.execute(
                sa_text(
                    f'ALTER TABLE "{binding_table}" '
                    "ADD COLUMN state VARCHAR(20) "
                    "NOT NULL DEFAULT 'active'" + state_constraint
                )
            )
            if dialect == "postgresql":
                await conn.execute(
                    sa_text(
                        f'ALTER TABLE "{binding_table}" '
                        "ADD CONSTRAINT ck_guideline_binding_state "
                        "CHECK (state IN ('active', 'unlinked'))"
                    )
                )
            changed = True
        if GuidelineRetirementRow.__tablename__ not in table_names:
            await conn.run_sync(
                lambda sync_conn: GuidelineRetirementRow.__table__.create(
                    sync_conn,
                    checkfirst=True,
                )
            )
            changed = True
        noop_table = GuidelineRevisionNoopReplayRow.__tablename__
        if noop_table not in table_names:
            await conn.run_sync(
                lambda sync_conn: GuidelineRevisionNoopReplayRow.__table__.create(
                    sync_conn,
                    checkfirst=True,
                )
            )
            changed = True
        revision_table = GuidelineRevisionRow.__tablename__
        if revision_table not in table_names:
            raise RuntimeError(
                "guideline policy lifecycle migration requires create_all; "
                f"missing table: {revision_table}"
            )
        revision_columns = await conn.run_sync(
            lambda sync_conn: {
                str(column["name"])
                for column in sa_inspect(sync_conn).get_columns(
                    revision_table
                )
            }
        )
        if "legacy_version_text" not in revision_columns:
            await conn.execute(
                sa_text(
                    f'ALTER TABLE "{revision_table}" '
                    "ADD COLUMN legacy_version_text VARCHAR(64)"
                )
            )
            changed = True
        backfilled_legacy_versions = await conn.execute(
            sa_text(
                f'UPDATE "{revision_table}" '
                "SET legacy_version_text = CAST(legacy_version AS VARCHAR) "
                "WHERE legacy_version IS NOT NULL "
                "AND legacy_version_text IS NULL"
            )
        )
        changed = (
            changed or int(backfilled_legacy_versions.rowcount or 0) > 0
        )
        if GuidelineImportBindingCandidateRow.__tablename__ not in table_names:
            await conn.run_sync(
                lambda sync_conn: GuidelineImportBindingCandidateRow.__table__.create(
                    sync_conn,
                    checkfirst=True,
                )
            )
            changed = True
        if dialect == "sqlite":
            noop_contract = await conn.run_sync(
                lambda sync_conn: _sqlite_owned_table_contract(
                    sync_conn,
                    GuidelineRevisionNoopReplayRow.__table__,
                )
            )
            if noop_contract["observed"] != noop_contract["expected"]:
                raise RuntimeError(
                    "guideline revision no-op ledger has a non-canonical "
                    "contract"
                )
            expected_noop_triggers = guideline_revision_noop_trigger_manifest()
            noop_trigger_rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {
                            "prefix": (
                                f"{GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX}%"
                            )
                        },
                    )
                )
                .mappings()
                .all()
            )
            observed_noop_triggers = {
                str(row["name"]): row for row in noop_trigger_rows
            }
            if set(observed_noop_triggers) - set(expected_noop_triggers):
                raise RuntimeError(
                    "guideline revision no-op ledger has unexpected owned "
                    "triggers"
                )
            for trigger_name, (table_name, trigger_sql) in (
                expected_noop_triggers.items()
            ):
                observed = observed_noop_triggers.get(trigger_name)
                if observed is None:
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
                if (
                    str(observed["tbl_name"]) != table_name
                    or normalize_global_discovery_source_revision_trigger_sql(
                        observed["sql"]
                    )
                    != normalize_global_discovery_source_revision_trigger_sql(
                        trigger_sql
                    )
                ):
                    raise RuntimeError(
                        "guideline revision no-op trigger drift: "
                        + trigger_name
                    )
            candidate_contract = await conn.run_sync(
                lambda sync_conn: _sqlite_owned_table_contract(
                    sync_conn,
                    GuidelineImportBindingCandidateRow.__table__,
                )
            )
            if candidate_contract["observed"] != candidate_contract["expected"]:
                raise RuntimeError(
                    "guideline import binding candidate table has a "
                    "non-canonical contract"
                )
            expected_triggers = (
                guideline_import_binding_candidate_trigger_manifest()
            )
            trigger_rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {
                            "prefix": (
                                f"{GUIDELINE_IMPORT_CANDIDATE_TRIGGER_PREFIX}%"
                            )
                        },
                    )
                )
                .mappings()
                .all()
            )
            observed_triggers = {
                str(row["name"]): row for row in trigger_rows
            }
            unexpected = set(observed_triggers) - set(expected_triggers)
            if unexpected:
                raise RuntimeError(
                    "guideline import candidate has unexpected owned "
                    "triggers: "
                    + ", ".join(sorted(unexpected))
                )
            for trigger_name, (table_name, trigger_sql) in (
                expected_triggers.items()
            ):
                observed = observed_triggers.get(trigger_name)
                if observed is None:
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
                if (
                    str(observed["tbl_name"]) != table_name
                    or normalize_global_discovery_source_revision_trigger_sql(
                        observed["sql"]
                    )
                    != normalize_global_discovery_source_revision_trigger_sql(
                        trigger_sql
                    )
                ):
                    raise RuntimeError(
                        "guideline import candidate trigger drift: "
                        + trigger_name
                    )
        else:
            noop_function_ddl, noop_trigger_ddl = (
                guideline_revision_noop_postgresql_ddl()
            )
            await conn.execute(sa_text(noop_function_ddl))
            noop_trigger_rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT trigger.tgname AS name, "
                            "relation.relname AS table_name, "
                            "procedure.proname AS function_name, "
                            "trigger.tgenabled AS enabled, "
                            "trigger.tgtype AS trigger_type, "
                            "pg_get_triggerdef(trigger.oid, true) "
                            "AS definition "
                            "FROM pg_trigger AS trigger "
                            "JOIN pg_class AS relation "
                            "ON relation.oid = trigger.tgrelid "
                            "JOIN pg_proc AS procedure "
                            "ON procedure.oid = trigger.tgfoid "
                            "WHERE NOT trigger.tgisinternal "
                            "AND trigger.tgname = :name"
                        ),
                        {"name": GUIDELINE_REVISION_NOOP_TRIGGER_PREFIX},
                    )
                )
                .mappings()
                .all()
            )
            if not noop_trigger_rows:
                await conn.execute(sa_text(noop_trigger_ddl))
                changed = True
            elif (
                len(noop_trigger_rows) != 1
                or str(noop_trigger_rows[0]["table_name"]) != noop_table
                or str(noop_trigger_rows[0]["function_name"])
                != "pulse_guideline_revision_noop_guard"
                or str(noop_trigger_rows[0]["enabled"]) != "O"
                or int(noop_trigger_rows[0]["trigger_type"]) != 31
            ):
                raise RuntimeError(
                    "guideline revision no-op PostgreSQL trigger drift"
                )
            function_ddl, trigger_ddl = (
                guideline_import_binding_candidate_postgresql_ddl()
            )
            await conn.execute(sa_text(function_ddl))
            trigger_rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT trigger.tgname AS name, "
                            "relation.relname AS table_name, "
                            "procedure.proname AS function_name, "
                            "trigger.tgenabled AS enabled, "
                            "trigger.tgtype AS trigger_type, "
                            "pg_get_triggerdef(trigger.oid, true) "
                            "AS definition "
                            "FROM pg_trigger AS trigger "
                            "JOIN pg_class AS relation "
                            "ON relation.oid = trigger.tgrelid "
                            "JOIN pg_proc AS procedure "
                            "ON procedure.oid = trigger.tgfoid "
                            "WHERE NOT trigger.tgisinternal "
                            "AND trigger.tgname = :name"
                        ),
                        {"name": GUIDELINE_IMPORT_CANDIDATE_TRIGGER_PREFIX},
                    )
                )
                .mappings()
                .all()
            )
            if not trigger_rows:
                await conn.execute(sa_text(trigger_ddl))
                changed = True
            else:
                definition = " ".join(
                    str(trigger_rows[0]["definition"]).lower().split()
                )
                events_are_exact = (
                    " before delete or update " in definition
                    or " before update or delete " in definition
                )
                if (
                    len(trigger_rows) != 1
                    or str(trigger_rows[0]["table_name"])
                    != GuidelineImportBindingCandidateRow.__tablename__
                    or str(trigger_rows[0]["function_name"])
                    != "pulse_guideline_import_binding_candidate_guard"
                    or str(trigger_rows[0]["enabled"]) != "O"
                    # PostgreSQL pg_trigger bitmask: ROW(1) | BEFORE(2)
                    # | DELETE(8) | UPDATE(16).
                    or int(trigger_rows[0]["trigger_type"]) != 27
                    or not events_are_exact
                    or " for each row execute function " not in definition
                    or "pulse_guideline_import_binding_candidate_guard"
                    not in definition
                ):
                    raise RuntimeError(
                        "guideline import candidate PostgreSQL trigger drift"
                    )
        invalid_states = int(
            (
                await conn.execute(
                    sa_text(
                        f'SELECT COUNT(*) FROM "{binding_table}" '
                        "WHERE state IS NULL "
                        "OR state NOT IN ('active', 'unlinked')"
                    )
                )
            ).scalar_one()
        )
        if invalid_states:
            raise RuntimeError(
                "guideline policy lifecycle migration found invalid binding "
                f"states: {invalid_states}"
            )
    return None if changed else "skipped"


async def _migrate_guideline_policy_v1_schema() -> str | None:
    """Backfill the immutable SK-B guideline authority and install its guards.

    The legacy ``guidelines`` and ``board_guidelines`` tables remain readable
    during register-before-remove.  Each observed legacy guideline becomes one
    honest ``1.0.0`` baseline revision; counters above one are retained as
    provenance and explicitly marked unresolvable instead of being expanded
    into invented history.  Board links and inline guidelines are then pinned
    to that exact baseline.  Default-template JSON receives the same exact
    revision identity while retaining every pre-existing field and key order.

    The migration is replay-safe: stable UUID5 identities, scoped idempotency
    constraints, exact row comparisons, and audited owned triggers make a
    second execution a true ``skipped`` result with no row delta.
    """

    import hashlib
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
        guideline_revision_content_digest,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        BoardGuideline,
        DefaultBoardConfiguration,
        Guideline,
        GuidelineBoardBindingRow,
        GuidelineHeadRow,
        GuidelineImportBindingCandidateRow,
        GuidelineRetirementRow,
        GuidelineRevisionNoopReplayRow,
        GuidelineRevisionRow,
    )

    tables = (
        GuidelineRevisionRow.__table__,
        GuidelineHeadRow.__table__,
        GuidelineBoardBindingRow.__table__,
        GuidelineImportBindingCandidateRow.__table__,
        GuidelineRevisionNoopReplayRow.__table__,
        GuidelineRetirementRow.__table__,
    )

    def _stable_id(kind: str, *parts: object) -> str:
        material = "/".join(str(part) for part in parts)
        return str(
            uuid5(
                NAMESPACE_URL,
                f"okto-pulse/guideline-policy/v1/{kind}/{material}",
            )
        )

    def _request_digest(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _require_exact(
        *,
        kind: str,
        identity: str,
        observed: object,
        expected: dict[str, object],
    ) -> None:
        if observed is None:
            return

        def _comparable(value: object) -> object:
            if isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc)
            return value

        mismatches: list[str] = []
        for field, expected_value in expected.items():
            observed_value = getattr(observed, field)
            if _comparable(observed_value) != _comparable(expected_value):
                mismatches.append(
                    f"{field}={observed_value!r} (expected {expected_value!r})"
                )
        if mismatches:
            raise RuntimeError(
                f"guideline policy {kind} drift for {identity}: "
                + "; ".join(mismatches)
            )

    async def _fetch_one(conn: object, table: object, **criteria: object):
        statement = select(table)
        for column_name, value in criteria.items():
            statement = statement.where(table.c[column_name] == value)
        row = (await conn.execute(statement)).mappings().one_or_none()
        return SimpleNamespace(**dict(row)) if row is not None else None

    async def _fetch_all(
        conn: object,
        table: object,
        *,
        where: tuple[object, ...] = (),
        order_by: tuple[object, ...] = (),
    ) -> list[SimpleNamespace]:
        statement = select(table)
        if where:
            statement = statement.where(*where)
        if order_by:
            statement = statement.order_by(*order_by)
        return [
            SimpleNamespace(**dict(row))
            for row in (await conn.execute(statement)).mappings().all()
        ]

    async def _install_sqlite_triggers(conn: object) -> bool:
        expected = guideline_policy_immutability_trigger_manifest()
        predecessor = guideline_policy_immutability_trigger_manifest(
            allow_board_erasure=False,
        )
        b03_predecessors = guideline_policy_b03_sqlite_trigger_predecessors()
        rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing = {str(row["name"]): row for row in rows}
        unexpected = set(existing) - set(expected)
        if unexpected:
            raise RuntimeError(
                "guideline policy has unexpected owned triggers: "
                + ", ".join(sorted(unexpected))
            )
        changed = False
        for trigger_name, (table_name, trigger_sql) in expected.items():
            row = existing.get(trigger_name)
            if row is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            observed_table = str(row["tbl_name"])
            observed_sql = normalize_global_discovery_source_revision_trigger_sql(
                row["sql"]
            )
            expected_sql = normalize_global_discovery_source_revision_trigger_sql(
                trigger_sql
            )
            if observed_table == table_name and observed_sql == expected_sql:
                continue
            b03_predecessor = b03_predecessors.get(trigger_name)
            if b03_predecessor is not None:
                predecessor_table, predecessor_sql = b03_predecessor
                if observed_table == predecessor_table and observed_sql == (
                    normalize_global_discovery_source_revision_trigger_sql(
                        predecessor_sql
                    )
                ):
                    await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
            predecessor_table, predecessor_sql = predecessor[trigger_name]
            if observed_table == predecessor_table and observed_sql == (
                normalize_global_discovery_source_revision_trigger_sql(predecessor_sql)
            ):
                await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            raise RuntimeError(
                f"guideline policy immutability trigger {trigger_name} is corrupt"
            )

        final_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        final = {str(row["name"]): row for row in final_rows}
        if set(final) != set(expected):
            raise RuntimeError(
                "guideline policy immutability trigger installation is incomplete"
            )
        for trigger_name, (table_name, trigger_sql) in expected.items():
            row = final[trigger_name]
            if str(row["tbl_name"]) != table_name or (
                normalize_global_discovery_source_revision_trigger_sql(row["sql"])
                != normalize_global_discovery_source_revision_trigger_sql(trigger_sql)
            ):
                raise RuntimeError(
                    "guideline policy immutability trigger audit failed: "
                    + trigger_name
                )
        return changed

    async def _install_postgresql_triggers(conn: object) -> bool:
        ddl = guideline_policy_postgresql_immutability_ddl()
        # CREATE OR REPLACE functions are safe and keep the permit policy
        # convergent. Trigger identities are separately audited below.
        for function_ddl in ddl[:4]:
            await conn.execute(sa_text(function_ddl))
        expected_trigger_ddl = {
            f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_revision_guard": ddl[4],
            f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_head_guard": ddl[5],
            f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_binding_guard": ddl[6],
            f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}_retirement_guard": ddl[7],
        }

        async def _catalog_rows() -> list[object]:
            return list(
                (
                    (
                        await conn.execute(
                            sa_text(
                                "SELECT trigger.tgname AS name, "
                                "relation.relname AS table_name, "
                                "procedure.proname AS function_name, "
                                "trigger.tgenabled AS tgenabled, "
                                "trigger.tgtype::integer AS tgtype, "
                                "trigger.tgqual AS tgqual "
                                "FROM pg_trigger AS trigger "
                                "JOIN pg_class AS relation "
                                "ON relation.oid = trigger.tgrelid "
                                "JOIN pg_proc AS procedure "
                                "ON procedure.oid = trigger.tgfoid "
                                "WHERE NOT trigger.tgisinternal "
                                "AND trigger.tgname LIKE :prefix"
                            ),
                            {
                                "prefix": (
                                    f"{GUIDELINE_POLICY_IMMUTABILITY_TRIGGER_PREFIX}%"
                                )
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            )

        missing, predecessors = audit_guideline_policy_postgresql_trigger_rows(
            await _catalog_rows()
        )
        changed = False
        contracts = guideline_policy_postgresql_trigger_contracts()
        for trigger_name in predecessors:
            table_name = str(contracts[trigger_name]["table_name"])
            await conn.exec_driver_sql(
                f'DROP TRIGGER "{trigger_name}" ON "{table_name}"'
            )
            await conn.execute(sa_text(expected_trigger_ddl[trigger_name]))
            changed = True
        for trigger_name in missing:
            await conn.execute(sa_text(expected_trigger_ddl[trigger_name]))
            changed = True
        final_missing, final_predecessors = (
            audit_guideline_policy_postgresql_trigger_rows(await _catalog_rows())
        )
        if final_missing or final_predecessors:
            raise RuntimeError(
                "guideline policy PostgreSQL trigger convergence incomplete"
            )
        return changed

    engine = get_engine()
    changed = False
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "guideline policy migration supports only SQLite and PostgreSQL"
            )
        if dialect == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")

        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        required = {
            Guideline.__tablename__,
            BoardGuideline.__tablename__,
            DefaultBoardConfiguration.__tablename__,
            *(table.name for table in tables),
        }
        missing = required - table_names
        if missing:
            raise RuntimeError(
                "guideline policy migration requires create_all; missing tables: "
                + ", ".join(sorted(missing))
            )
        if dialect == "sqlite":
            for table in tables:
                contract = await conn.run_sync(
                    lambda sync_conn, owned=table: _sqlite_owned_table_contract(
                        sync_conn,
                        owned,
                    )
                )
                if contract["observed"] != contract["expected"]:
                    raise RuntimeError(
                        "guideline policy table has a non-canonical contract: "
                        + table.name
                    )

        guideline_rows = await _fetch_all(
            conn,
            Guideline.__table__,
            order_by=(Guideline.__table__.c.id.asc(),),
        )
        baselines: dict[str, GuidelineRevisionRow] = {}
        guidelines: dict[str, Guideline] = {}
        for legacy in guideline_rows:
            if legacy.scope not in {"global", "inline"}:
                raise RuntimeError(
                    f"guideline policy legacy scope invalid: {legacy.id}"
                )
            if legacy.scope == "global" and legacy.board_id is not None:
                raise RuntimeError(
                    f"guideline policy global guideline has board_id: {legacy.id}"
                )
            if legacy.scope == "inline" and legacy.board_id is None:
                raise RuntimeError(
                    f"guideline policy inline guideline lacks board_id: {legacy.id}"
                )
            if not isinstance(legacy.version, int) or legacy.version < 1:
                raise RuntimeError(
                    f"guideline policy legacy version invalid: {legacy.id}"
                )
            if (
                not isinstance(legacy.title, str)
                or not legacy.title.strip()
                or not isinstance(legacy.content, str)
                or not legacy.content.strip()
            ):
                raise RuntimeError(
                    f"guideline policy legacy semantic text invalid: {legacy.id}"
                )
            canonical_title = legacy.title.strip()
            canonical_content = legacy.content.strip()
            if legacy.tags is not None and (
                not isinstance(legacy.tags, list)
                or any(
                    not isinstance(tag, str) or not tag.strip() for tag in legacy.tags
                )
            ):
                raise RuntimeError(f"guideline policy legacy tags invalid: {legacy.id}")
            canonical_tags = tuple(sorted(tag.strip() for tag in (legacy.tags or ())))
            if len(set(canonical_tags)) != len(canonical_tags):
                raise RuntimeError(
                    f"guideline policy legacy tags duplicate: {legacy.id}"
                )
            guidelines[legacy.id] = legacy
            migration_revision_id = _stable_id(
                "guideline",
                legacy.id,
                "revision",
                "1.0.0",
            )
            content_digest = guideline_revision_content_digest(
                title=canonical_title,
                content=canonical_content,
                metrics=(),
                tags=canonical_tags,
                semantic_version="1.0.0",
            )
            revision_key = f"migration:guideline:{legacy.id}:baseline:1.0.0"
            revision_request_digest = _request_digest(
                {
                    "guideline_id": legacy.id,
                    "revision_id": migration_revision_id,
                    "title": canonical_title,
                    "content": canonical_content,
                    "legacy_title": legacy.title,
                    "legacy_content": legacy.content,
                    "content_digest": content_digest,
                    "legacy_version": legacy.version,
                    "legacy_tags": legacy.tags,
                }
            )
            revision_expected = {
                "guideline_id": legacy.id,
                "revision_number": 1,
                "semantic_version": "1.0.0",
                "title": canonical_title,
                "content": canonical_content,
                "content_digest": content_digest,
                "tags": list(canonical_tags),
                "rules": [],
                "created_by": legacy.owner_id,
                "created_at": legacy.created_at,
                "published_head_revision": 1,
                "published_head_updated_at": (legacy.updated_at or legacy.created_at),
                "parent_revision_id": None,
                "legacy_version": legacy.version,
                "legacy_version_unresolvable": legacy.version != 1,
                "legacy_tags": legacy.tags,
                "idempotency_key": revision_key,
                "request_digest": revision_request_digest,
                "legacy_version_text": str(legacy.version),
            }
            existing_revisions = await _fetch_all(
                conn,
                GuidelineRevisionRow.__table__,
                where=(GuidelineRevisionRow.__table__.c.guideline_id == legacy.id,),
                order_by=(
                    GuidelineRevisionRow.__table__.c.revision_number.asc(),
                    GuidelineRevisionRow.__table__.c.revision_id.asc(),
                ),
            )
            if not existing_revisions:
                await conn.execute(
                    GuidelineRevisionRow.__table__.insert().values(
                        revision_id=migration_revision_id,
                        **revision_expected,
                    )
                )
                changed = True
                existing_revisions = await _fetch_all(
                    conn,
                    GuidelineRevisionRow.__table__,
                    where=(GuidelineRevisionRow.__table__.c.guideline_id == legacy.id,),
                    order_by=(
                        GuidelineRevisionRow.__table__.c.revision_number.asc(),
                        GuidelineRevisionRow.__table__.c.revision_id.asc(),
                    ),
                )
            initial_revisions = [
                row for row in existing_revisions if row.revision_number == 1
            ]
            if len(initial_revisions) != 1:
                raise RuntimeError(
                    "guideline policy requires exactly one initial revision: "
                    + legacy.id
                )
            baseline = initial_revisions[0]
            is_migrated_legacy = baseline.legacy_version is not None
            if is_migrated_legacy:
                if baseline.revision_id != migration_revision_id:
                    raise RuntimeError(
                        "guideline policy migrated baseline identity drift: "
                        + legacy.id
                    )
                _require_exact(
                    kind="baseline revision",
                    identity=legacy.id,
                    observed=baseline,
                    expected=revision_expected,
                )
            else:
                # A native authority already owns revision #1.  Its revision
                # identity is caller-generated and must never be replaced by
                # the deterministic legacy UUID5 on a later startup.
                if (
                    baseline.parent_revision_id is not None
                    or baseline.semantic_version != "1.0.0"
                    or baseline.legacy_version_unresolvable
                    or baseline.legacy_tags is not None
                ):
                    raise RuntimeError(
                        "guideline policy native baseline contract invalid: "
                        + legacy.id
                    )
            previous_revision = None
            for position, revision in enumerate(existing_revisions, start=1):
                expected_parent = (
                    None if previous_revision is None else previous_revision.revision_id
                )
                if (
                    revision.revision_number != position
                    or revision.parent_revision_id != expected_parent
                    or revision.published_head_revision != position
                ):
                    raise RuntimeError(
                        "guideline policy revision lineage invalid: "
                        f"{legacy.id}:{revision.revision_id}"
                    )
                previous_revision = revision
            baselines[legacy.id] = baseline

            head = await _fetch_one(
                conn,
                GuidelineHeadRow.__table__,
                guideline_id=legacy.id,
            )
            if head is None:
                if len(existing_revisions) != 1 or not is_migrated_legacy:
                    raise RuntimeError(
                        "guideline policy cannot infer a missing head after "
                        f"native or post-baseline revisions: {legacy.id}"
                    )
                await conn.execute(
                    GuidelineHeadRow.__table__.insert().values(
                        guideline_id=legacy.id,
                        revision_id=baseline.revision_id,
                        revision_number=1,
                        semantic_version=baseline.semantic_version,
                        head_revision=1,
                        updated_at=legacy.updated_at or legacy.created_at,
                    )
                )
                changed = True
            else:
                pointed = existing_revisions[-1]
                if (
                    pointed.guideline_id != legacy.id
                    or pointed.revision_id != head.revision_id
                    or pointed.revision_number != head.revision_number
                    or pointed.semantic_version != head.semantic_version
                    or head.head_revision != pointed.revision_number
                ):
                    raise RuntimeError(
                        f"guideline policy head is not exact: {legacy.id}"
                    )

        link_rows = await _fetch_all(
            conn,
            BoardGuideline.__table__,
            order_by=(BoardGuideline.__table__.c.id.asc(),),
        )
        linked_inline_ids: set[str] = set()
        for link in link_rows:
            legacy = guidelines.get(link.guideline_id)
            baseline = baselines.get(link.guideline_id)
            if legacy is None or baseline is None:
                raise RuntimeError(
                    f"guideline policy link references missing guideline: {link.id}"
                )
            if link.priority < 0:
                raise RuntimeError(f"guideline policy link priority invalid: {link.id}")
            if legacy.scope == "inline":
                linked_inline_ids.add(legacy.id)
                raise RuntimeError(
                    f"guideline policy inline guideline also has a link: {link.id}"
                )
            binding_key = f"migration:board-guideline:{link.id}:binding:1"
            unresolved = bool(
                legacy.version != 1
                or (
                    link.guideline_version is not None
                    and link.guideline_version != legacy.version
                )
            )
            expected = {
                "binding_id": link.id,
                "binding_revision": 1,
                "board_id": link.board_id,
                "guideline_id": link.guideline_id,
                "revision_id": baseline.revision_id,
                "semantic_version": baseline.semantic_version,
                "revision_digest": baseline.content_digest,
                "priority": link.priority,
                "adopted_by": legacy.owner_id,
                "adopted_at": link.added_at,
                "enforcement": "advisory",
                "source_kind": "legacy_board_guideline",
                "legacy_source_id": link.id,
                "legacy_guideline_version": link.guideline_version,
                "legacy_template_id": link.template_id,
                "legacy_template_version": link.template_version,
                "legacy_version_unresolvable": unresolved,
                "idempotency_key": binding_key,
            }
            expected["request_digest"] = _request_digest(expected)
            expected["state"] = "active"
            existing = await _fetch_one(
                conn,
                GuidelineBoardBindingRow.__table__,
                binding_id=link.id,
                binding_revision=1,
            )
            if existing is None:
                await conn.execute(
                    GuidelineBoardBindingRow.__table__.insert().values(**expected)
                )
                changed = True
                existing = await _fetch_one(
                    conn,
                    GuidelineBoardBindingRow.__table__,
                    binding_id=link.id,
                    binding_revision=1,
                )
            _require_exact(
                kind="legacy board binding",
                identity=link.id,
                observed=existing,
                expected=expected,
            )

        for legacy in guideline_rows:
            if legacy.scope != "inline":
                continue
            if legacy.id in linked_inline_ids:
                raise RuntimeError(
                    f"guideline policy duplicate inline link: {legacy.id}"
                )
            baseline = baselines[legacy.id]
            binding_id = _stable_id(
                "board",
                legacy.board_id,
                "inline",
                legacy.id,
                "binding",
            )
            existing_inline_bindings = await _fetch_all(
                conn,
                GuidelineBoardBindingRow.__table__,
                where=(GuidelineBoardBindingRow.__table__.c.guideline_id == legacy.id,),
                order_by=(
                    GuidelineBoardBindingRow.__table__.c.binding_revision.asc(),
                    GuidelineBoardBindingRow.__table__.c.binding_id.asc(),
                ),
            )
            if existing_inline_bindings and not any(
                row.binding_id == binding_id and row.binding_revision == 1
                for row in existing_inline_bindings
            ):
                # Native inline creation already appended its exact ACTIVE
                # binding in the caller transaction.  Reuse that stable
                # identity rather than inventing the legacy UUID5 lineage.
                continue
            binding_key = f"migration:inline:{legacy.id}:binding:1"
            expected = {
                "binding_id": binding_id,
                "binding_revision": 1,
                "board_id": legacy.board_id,
                "guideline_id": legacy.id,
                "revision_id": baseline.revision_id,
                "semantic_version": baseline.semantic_version,
                "revision_digest": baseline.content_digest,
                "priority": 0,
                "adopted_by": legacy.owner_id,
                "adopted_at": legacy.created_at,
                "enforcement": "advisory",
                "source_kind": "legacy_inline_guideline",
                "legacy_source_id": None,
                "legacy_guideline_version": legacy.version,
                "legacy_template_id": None,
                "legacy_template_version": None,
                "legacy_version_unresolvable": legacy.version != 1,
                "idempotency_key": binding_key,
            }
            expected["request_digest"] = _request_digest(expected)
            expected["state"] = "active"
            existing = await _fetch_one(
                conn,
                GuidelineBoardBindingRow.__table__,
                binding_id=binding_id,
                binding_revision=1,
            )
            if existing is None:
                await conn.execute(
                    GuidelineBoardBindingRow.__table__.insert().values(**expected)
                )
                changed = True
                existing = await _fetch_one(
                    conn,
                    GuidelineBoardBindingRow.__table__,
                    binding_id=binding_id,
                    binding_revision=1,
                )
            _require_exact(
                kind="inline board binding",
                identity=legacy.id,
                observed=existing,
                expected=expected,
            )

        binding_rows = await _fetch_all(
            conn,
            GuidelineBoardBindingRow.__table__,
            order_by=(
                GuidelineBoardBindingRow.__table__.c.binding_id.asc(),
                GuidelineBoardBindingRow.__table__.c.binding_revision.asc(),
            ),
        )
        binding_lineage: dict[str, tuple[str, str, int]] = {}
        binding_states: dict[str, str] = {}
        binding_identity_by_pair: dict[tuple[str, str], str] = {}
        for binding in binding_rows:
            identity = guidelines.get(binding.guideline_id)
            valid_scope = identity is not None and (
                (identity.scope == "global" and identity.board_id is None)
                or (
                    identity.scope == "inline" and identity.board_id == binding.board_id
                )
            )
            if not valid_scope:
                raise RuntimeError(
                    "guideline policy existing binding scope invalid: "
                    + binding.binding_id
                )
            previous = binding_lineage.get(binding.binding_id)
            if previous is None:
                valid_lineage = (
                    binding.binding_revision == 1 and binding.state == "active"
                )
            else:
                previous_board, previous_guideline, previous_revision = previous
                previous_state = binding_states[binding.binding_id]
                valid_lineage = (
                    binding.board_id == previous_board
                    and binding.guideline_id == previous_guideline
                    and binding.binding_revision == previous_revision + 1
                    and not (previous_state == "unlinked" and binding.state != "active")
                )
            if not valid_lineage:
                raise RuntimeError(
                    "guideline policy existing binding lineage invalid: "
                    + binding.binding_id
                )
            binding_lineage[binding.binding_id] = (
                binding.board_id,
                binding.guideline_id,
                binding.binding_revision,
            )
            binding_states[binding.binding_id] = binding.state
            pair = (binding.board_id, binding.guideline_id)
            existing_identity = binding_identity_by_pair.setdefault(
                pair,
                binding.binding_id,
            )
            if existing_identity != binding.binding_id:
                raise RuntimeError(
                    "guideline policy binding pair has multiple identities: "
                    f"{binding.board_id}:{binding.guideline_id}"
                )

        defaults = await _fetch_all(
            conn,
            DefaultBoardConfiguration.__table__,
            order_by=(DefaultBoardConfiguration.__table__.c.id.asc(),),
        )
        for default in defaults:
            refs = default.guideline_default_refs
            if refs is None:
                continue
            if not isinstance(refs, list):
                raise RuntimeError(
                    "guideline policy default refs must be a JSON array: " + default.id
                )
            normalized_refs: list[object] = []
            refs_changed = False
            for position, raw_ref in enumerate(refs):
                if not isinstance(raw_ref, dict):
                    raise RuntimeError(
                        "guideline policy default ref must be an object: "
                        f"{default.id}:{position}"
                    )
                normalized = dict(raw_ref)
                guideline_id = normalized.get("guideline_id")
                if not isinstance(guideline_id, str) or not guideline_id.strip():
                    raise RuntimeError(
                        "guideline policy default ref lacks guideline_id: "
                        f"{default.id}:{position}"
                    )
                guideline_id = guideline_id.strip()
                priority = normalized.get("priority", 0)
                if type(priority) is not int or priority < 0:
                    raise RuntimeError(
                        "guideline policy default ref has an invalid priority: "
                        f"{default.id}:{position}"
                    )
                baseline = baselines.get(guideline_id)
                legacy = guidelines.get(guideline_id)
                if baseline is None or legacy is None:
                    if default.is_active or default.status == "active":
                        raise RuntimeError(
                            "guideline_policy_unresolved_active_reference:"
                            f"{default.id}:{position}:dangling_reference:"
                            f"{guideline_id}"
                        )
                    additions = {
                        "revision_id": None,
                        "legacy_version_unresolvable": True,
                    }
                elif legacy.scope == "inline":
                    if default.is_active or default.status == "active":
                        raise RuntimeError(
                            "guideline_policy_unresolved_active_reference:"
                            f"{default.id}:{position}:inline_reference:"
                            f"{guideline_id}"
                        )
                    if normalized.get("revision_id") is not None or any(
                        normalized.get(key) is not None
                        for key in ("semantic_version", "revision_digest")
                    ):
                        raise RuntimeError(
                            "guideline policy inactive inline default has an "
                            f"exact pin: {default.id}:{position}"
                        )
                    additions = {
                        "revision_id": None,
                        "legacy_version": normalized.get("guideline_version"),
                        "legacy_version_unresolvable": True,
                    }
                else:
                    supplied_revision = normalized.get("revision_id")
                    supplied_number = normalized.get("revision_number")
                    captured_version = normalized.get("guideline_version")
                    for field_name, value in (
                        ("revision_number", supplied_number),
                        ("guideline_version", captured_version),
                    ):
                        if value is None:
                            continue
                        if type(value) is not int or value < 1:
                            raise RuntimeError(
                                "guideline policy default ref has an invalid "
                                f"{field_name}: {default.id}:{position}"
                            )
                    supplied_number = normalized.get("revision_number")
                    captured_version = normalized.get("guideline_version")
                    migrated_legacy = baseline.legacy_version is not None
                    if supplied_revision is not None:
                        pointed = await _fetch_one(
                            conn,
                            GuidelineRevisionRow.__table__,
                            revision_id=str(supplied_revision),
                        )
                        if pointed is None or pointed.guideline_id != guideline_id:
                            raise RuntimeError(
                                "guideline policy default ref pins a foreign "
                                f"revision: {default.id}:{position}"
                            )
                        resolved = pointed
                    elif supplied_number is not None:
                        resolved = await _fetch_one(
                            conn,
                            GuidelineRevisionRow.__table__,
                            guideline_id=guideline_id,
                            revision_number=supplied_number,
                        )
                    elif migrated_legacy:
                        # ``guideline_version`` on the mutable predecessor was
                        # not an immutable revision selector.  Preserve it as
                        # legacy metadata while pinning the migrated baseline.
                        resolved = baseline
                    elif captured_version is not None:
                        resolved = await _fetch_one(
                            conn,
                            GuidelineRevisionRow.__table__,
                            guideline_id=guideline_id,
                            revision_number=captured_version,
                        )
                    else:
                        head = await _fetch_one(
                            conn,
                            GuidelineHeadRow.__table__,
                            guideline_id=guideline_id,
                        )
                        resolved = (
                            await _fetch_one(
                                conn,
                                GuidelineRevisionRow.__table__,
                                guideline_id=guideline_id,
                                revision_id=head.revision_id,
                            )
                            if head is not None
                            else None
                        )
                    if resolved is None:
                        raise RuntimeError(
                            "guideline policy default ref revision does not "
                            f"exist: {default.id}:{position}"
                        )
                    declared_fields = {
                        "semantic_version": normalized.get("semantic_version"),
                        "revision_digest": normalized.get("revision_digest"),
                        "revision_number": supplied_number,
                    }
                    actual_fields = {
                        "semantic_version": resolved.semantic_version,
                        "revision_digest": resolved.content_digest,
                        "revision_number": resolved.revision_number,
                    }
                    for field_name, declared in declared_fields.items():
                        if (
                            declared is not None
                            and declared != actual_fields[field_name]
                        ):
                            raise RuntimeError(
                                "guideline policy default ref exact pin drift: "
                                f"{default.id}:{position}:{field_name}"
                            )
                    existing_legacy_version = normalized.get("legacy_version")
                    legacy_input = migrated_legacy and (
                        existing_legacy_version is None
                        or existing_legacy_version == captured_version
                    )
                    if (
                        captured_version is not None
                        and captured_version != resolved.revision_number
                        and not legacy_input
                    ):
                        raise RuntimeError(
                            "guideline policy default ref exact pin drift: "
                            f"{default.id}:{position}:guideline_version"
                        )
                    legacy_version = (
                        existing_legacy_version
                        if existing_legacy_version is not None
                        else captured_version
                        if migrated_legacy
                        else None
                    )
                    normalized.update(
                        {
                            "revision_id": resolved.revision_id,
                            "semantic_version": resolved.semantic_version,
                            "revision_digest": resolved.content_digest,
                            "revision_number": resolved.revision_number,
                            "guideline_version": resolved.revision_number,
                            "legacy_version": legacy_version,
                            "legacy_version_unresolvable": bool(
                                migrated_legacy
                                and (
                                    legacy_version is None
                                    or legacy_version != resolved.revision_number
                                )
                            ),
                        }
                    )
                    additions = {}
                for key, value in additions.items():
                    if key in normalized:
                        if normalized[key] != value:
                            raise RuntimeError(
                                "guideline policy default ref exact pin drift: "
                                f"{default.id}:{position}:{key}"
                            )
                        continue
                    normalized[key] = value
                if normalized != raw_ref:
                    refs_changed = True
                normalized_refs.append(normalized)
            if refs_changed:
                await conn.execute(
                    DefaultBoardConfiguration.__table__.update()
                    .where(DefaultBoardConfiguration.id == default.id)
                    .values(guideline_default_refs=normalized_refs)
                )
                changed = True

        if dialect == "sqlite":
            changed = await _install_sqlite_triggers(conn) or changed
            violations = list(
                (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            )
            if violations:
                raise RuntimeError(
                    "guideline policy migration left foreign-key violations: "
                    + repr(violations[:10])
                )
        else:
            changed = await _install_postgresql_triggers(conn) or changed

    return None if changed else "skipped"


async def _migrate_guideline_impact_substrate() -> str | None:
    """Create B08 tables/pin before B03's strict binding-table audit."""

    from sqlalchemy import inspect as sa_inspect

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineBoardBindingRow,
        GuidelineImpactAdoptionRow,
        GuidelineImpactItemRow,
        GuidelineImpactReceiptRow,
        GuidelineImpactUnlinkRow,
        GuidelineRetirementImpactRow,
    )

    receipt_table = GuidelineImpactReceiptRow.__tablename__
    binding_table = GuidelineBoardBindingRow.__tablename__
    tables = (
        GuidelineImpactReceiptRow.__table__,
        GuidelineImpactItemRow.__table__,
        GuidelineImpactAdoptionRow.__table__,
        GuidelineImpactUnlinkRow.__table__,
        GuidelineRetirementImpactRow.__table__,
    )
    engine = get_engine()
    changed = False
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "guideline impact substrate supports only SQLite and PostgreSQL"
            )
        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        if binding_table not in table_names:
            raise RuntimeError(
                "guideline impact substrate requires create_all; missing table: "
                + binding_table
            )
        for table in tables:
            if table.name not in table_names:
                await conn.run_sync(
                    lambda sync_conn, owned=table: owned.create(
                        sync_conn,
                        checkfirst=True,
                    )
                )
                table_names.add(table.name)
                changed = True
        binding_columns = await conn.run_sync(
            lambda sync_conn: {
                str(column["name"])
                for column in sa_inspect(sync_conn).get_columns(binding_table)
            }
        )
        if "impact_receipt_id" not in binding_columns:
            if dialect == "sqlite":
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    'ADD COLUMN "impact_receipt_id" VARCHAR(64) '
                    "CONSTRAINT fk_guideline_binding_impact_receipt "
                    f'REFERENCES "{receipt_table}" ("impact_receipt_id") '
                    "ON DELETE RESTRICT ON UPDATE RESTRICT"
                )
            else:
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    'ADD COLUMN "impact_receipt_id" VARCHAR(64)'
                )
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    "ADD CONSTRAINT "
                    '"fk_guideline_binding_impact_receipt" '
                    'FOREIGN KEY ("impact_receipt_id") '
                    f'REFERENCES "{receipt_table}" ("impact_receipt_id") '
                    "ON DELETE RESTRICT ON UPDATE RESTRICT"
                )
            changed = True
        if "binding_origin" not in binding_columns:
            origin_check = (
                " CONSTRAINT ck_guideline_binding_origin "
                "CHECK (binding_origin IN "
                "('native', 'default_materialization'))"
                if dialect == "sqlite"
                else ""
            )
            await conn.exec_driver_sql(
                f'ALTER TABLE "{binding_table}" '
                'ADD COLUMN "binding_origin" VARCHAR(32) '
                "NOT NULL DEFAULT 'native'" + origin_check
            )
            if dialect == "postgresql":
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    "ADD CONSTRAINT ck_guideline_binding_origin "
                    "CHECK (binding_origin IN "
                    "('native', 'default_materialization'))"
                )
            changed = True
        if "impact_adoption_id" not in binding_columns:
            if dialect == "sqlite":
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    'ADD COLUMN "impact_adoption_id" VARCHAR(36) '
                    "CONSTRAINT fk_guideline_binding_impact_adoption "
                    'REFERENCES "guideline_impact_adoptions" '
                    '("adoption_id") '
                    "ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED"
                )
            else:
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    'ADD COLUMN "impact_adoption_id" VARCHAR(36)'
                )
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    "ADD CONSTRAINT "
                    '"fk_guideline_binding_impact_adoption" '
                    'FOREIGN KEY ("impact_adoption_id") '
                    'REFERENCES "guideline_impact_adoptions" '
                    '("adoption_id") '
                    "ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED"
                )
            changed = True
        if "impact_unlink_id" not in binding_columns:
            if dialect == "sqlite":
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    'ADD COLUMN "impact_unlink_id" VARCHAR(36) '
                    "CONSTRAINT fk_guideline_binding_impact_unlink "
                    'REFERENCES "guideline_impact_unlinks" '
                    '("unlink_id") '
                    "ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED"
                )
            else:
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    'ADD COLUMN "impact_unlink_id" VARCHAR(36)'
                )
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{binding_table}" '
                    "ADD CONSTRAINT "
                    '"fk_guideline_binding_impact_unlink" '
                    'FOREIGN KEY ("impact_unlink_id") '
                    'REFERENCES "guideline_impact_unlinks" '
                    '("unlink_id") '
                    "ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED"
                )
            changed = True
        binding_contract = await conn.run_sync(
            lambda sync_conn: (
                sa_inspect(sync_conn).get_columns(binding_table),
                sa_inspect(sync_conn).get_foreign_keys(binding_table),
            )
        )
        columns, foreign_keys = binding_contract
        sqlite_foreign_keys: dict[str, list[dict[str, object]]] = {}
        if dialect == "sqlite":
            # SQLAlchemy's SQLite inspector loses ON UPDATE/DELETE options for
            # inline references appended with ALTER TABLE ADD COLUMN.  SQLite's
            # own catalog is authoritative for those actions.
            pragma_rows = (
                (
                    await conn.exec_driver_sql(
                        f'PRAGMA foreign_key_list("{binding_table}")'
                    )
                )
                .mappings()
                .all()
            )
            for row in pragma_rows:
                sqlite_foreign_keys.setdefault(str(row["from"]), []).append(dict(row))
        impact_columns = [
            column for column in columns if str(column["name"]) == "impact_receipt_id"
        ]
        if len(impact_columns) != 1:
            raise RuntimeError(
                "guideline impact binding pin column is missing or duplicated"
            )
        impact_column = impact_columns[0]
        if (
            not bool(impact_column["nullable"])
            or getattr(impact_column["type"], "length", None) != 64
        ):
            raise RuntimeError("guideline impact binding pin column contract drift")
        if dialect == "sqlite":
            impact_foreign_keys = sqlite_foreign_keys.get(
                "impact_receipt_id",
                [],
            )
            if len(impact_foreign_keys) != 1:
                raise RuntimeError(
                    "guideline impact binding pin foreign key missing or duplicated"
                )
            impact_foreign_key = impact_foreign_keys[0]
            if (
                str(impact_foreign_key["table"]) != receipt_table
                or str(impact_foreign_key["to"]) != "impact_receipt_id"
                or str(impact_foreign_key["on_delete"]).upper() != "RESTRICT"
                or str(impact_foreign_key["on_update"]).upper() != "RESTRICT"
            ):
                raise RuntimeError(
                    "guideline impact binding pin foreign key contract drift"
                )
        else:
            impact_foreign_keys = [
                foreign_key
                for foreign_key in foreign_keys
                if tuple(foreign_key.get("constrained_columns") or ())
                == ("impact_receipt_id",)
            ]
            if len(impact_foreign_keys) != 1:
                raise RuntimeError(
                    "guideline impact binding pin foreign key missing or duplicated"
                )
            impact_foreign_key = impact_foreign_keys[0]
            options = {
                str(key).lower(): str(value).upper()
                for key, value in (impact_foreign_key.get("options") or {}).items()
            }
            if (
                impact_foreign_key.get("referred_table") != receipt_table
                or tuple(impact_foreign_key.get("referred_columns") or ())
                != ("impact_receipt_id",)
                or options.get("ondelete") != "RESTRICT"
                or options.get("onupdate") != "RESTRICT"
                or impact_foreign_key.get("name")
                != "fk_guideline_binding_impact_receipt"
            ):
                raise RuntimeError(
                    "guideline impact binding pin foreign key contract drift"
                )
        origin_columns = [
            column for column in columns if str(column["name"]) == "binding_origin"
        ]
        if (
            len(origin_columns) != 1
            or bool(origin_columns[0]["nullable"])
            or getattr(origin_columns[0]["type"], "length", None) != 32
        ):
            raise RuntimeError("guideline impact binding origin column contract drift")
        adoption_columns = [
            column for column in columns if str(column["name"]) == "impact_adoption_id"
        ]
        if (
            len(adoption_columns) != 1
            or not bool(adoption_columns[0]["nullable"])
            or getattr(adoption_columns[0]["type"], "length", None) != 36
        ):
            raise RuntimeError(
                "guideline impact binding adoption column contract drift"
            )
        if dialect == "sqlite":
            adoption_foreign_keys = sqlite_foreign_keys.get(
                "impact_adoption_id",
                [],
            )
            if len(adoption_foreign_keys) != 1:
                raise RuntimeError(
                    "guideline impact binding adoption foreign key drift"
                )
            adoption_foreign_key = adoption_foreign_keys[0]
            if (
                str(adoption_foreign_key["table"]) != "guideline_impact_adoptions"
                or str(adoption_foreign_key["to"]) != "adoption_id"
                or str(adoption_foreign_key["on_delete"]).upper() != "NO ACTION"
                or str(adoption_foreign_key["on_update"]).upper() != "RESTRICT"
            ):
                raise RuntimeError(
                    "guideline impact binding adoption foreign key drift"
                )
        else:
            adoption_foreign_keys = [
                foreign_key
                for foreign_key in foreign_keys
                if tuple(foreign_key.get("constrained_columns") or ())
                == ("impact_adoption_id",)
            ]
            if len(adoption_foreign_keys) != 1:
                raise RuntimeError(
                    "guideline impact binding adoption foreign key drift"
                )
            adoption_foreign_key = adoption_foreign_keys[0]
            adoption_options = {
                str(key).lower(): str(value).upper()
                for key, value in (adoption_foreign_key.get("options") or {}).items()
            }
            if (
                adoption_foreign_key.get("referred_table")
                != "guideline_impact_adoptions"
                or tuple(adoption_foreign_key.get("referred_columns") or ())
                != ("adoption_id",)
                or adoption_options.get("ondelete") is not None
                or adoption_options.get("onupdate") != "RESTRICT"
                or adoption_options.get("deferrable") != "TRUE"
                or adoption_options.get("initially") != "DEFERRED"
                or adoption_foreign_key.get("name")
                != "fk_guideline_binding_impact_adoption"
            ):
                raise RuntimeError(
                    "guideline impact binding adoption foreign key drift"
                )
        unlink_columns = [
            column for column in columns if str(column["name"]) == "impact_unlink_id"
        ]
        if (
            len(unlink_columns) != 1
            or not bool(unlink_columns[0]["nullable"])
            or getattr(unlink_columns[0]["type"], "length", None) != 36
        ):
            raise RuntimeError("guideline impact binding unlink column contract drift")
        if dialect == "sqlite":
            unlink_foreign_keys = sqlite_foreign_keys.get(
                "impact_unlink_id",
                [],
            )
            if len(unlink_foreign_keys) != 1:
                raise RuntimeError("guideline impact binding unlink foreign key drift")
            unlink_foreign_key = unlink_foreign_keys[0]
            if (
                str(unlink_foreign_key["table"]) != "guideline_impact_unlinks"
                or str(unlink_foreign_key["to"]) != "unlink_id"
                or str(unlink_foreign_key["on_delete"]).upper() != "NO ACTION"
                or str(unlink_foreign_key["on_update"]).upper() != "RESTRICT"
            ):
                raise RuntimeError("guideline impact binding unlink foreign key drift")
        else:
            unlink_foreign_keys = [
                foreign_key
                for foreign_key in foreign_keys
                if tuple(foreign_key.get("constrained_columns") or ())
                == ("impact_unlink_id",)
            ]
            if len(unlink_foreign_keys) != 1:
                raise RuntimeError("guideline impact binding unlink foreign key drift")
            unlink_foreign_key = unlink_foreign_keys[0]
            unlink_options = {
                str(key).lower(): str(value).upper()
                for key, value in (unlink_foreign_key.get("options") or {}).items()
            }
            if (
                unlink_foreign_key.get("referred_table") != "guideline_impact_unlinks"
                or tuple(unlink_foreign_key.get("referred_columns") or ())
                != ("unlink_id",)
                or unlink_options.get("ondelete") is not None
                or unlink_options.get("onupdate") != "RESTRICT"
                or unlink_options.get("deferrable") != "TRUE"
                or unlink_options.get("initially") != "DEFERRED"
                or unlink_foreign_key.get("name")
                != "fk_guideline_binding_impact_unlink"
            ):
                raise RuntimeError(
                    "guideline impact binding unlink foreign key contract drift"
                )
    return None if changed else "skipped"


async def _migrate_guideline_impact_v1_schema() -> str | None:
    """Converge sealed B08 impact receipts and explicit-adoption evidence."""

    from datetime import timezone

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import aliased

    from okto_pulse.community.adapters.sqlalchemy_models import (
        ActivityLog,
        BoardErasurePermit,
        DomainEventHandlerExecution,
        DomainEventRow,
        GuidelineBoardBindingRow,
        GuidelineImpactAdoptionRow,
        GuidelineImpactItemRow,
        GuidelineImpactReceiptRow,
        GuidelineImpactUnlinkRow,
        GuidelineRetirementImpactRow,
        GuidelineRetirementRow,
        SemanticGuidelineBindingConfigurationRow,
    )
    from okto_pulse.core.domain.quality_canonicalization import (
        canonical_sha256,
    )
    from okto_pulse.core.domain.guideline_lifecycle import (
        guideline_request_digest_v1,
    )
    from okto_pulse.core.events.types import (
        POLICY_BINDING_MATERIALIZED_EVENT_TYPE,
        PolicyAdoptionChanged,
        PolicyRetirementChanged,
        SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE,
        SEMANTIC_GUIDELINE_PROJECTION_SCHEMA_VERSION,
    )

    receipt_table = GuidelineImpactReceiptRow.__tablename__
    item_table = GuidelineImpactItemRow.__tablename__
    adoption_table = GuidelineImpactAdoptionRow.__tablename__
    unlink_table = GuidelineImpactUnlinkRow.__tablename__
    retirement_impact_table = GuidelineRetirementImpactRow.__tablename__
    binding_table = GuidelineBoardBindingRow.__tablename__
    permit_table = BoardErasurePermit.__tablename__
    execution_table = DomainEventHandlerExecution.__tablename__
    tables = (
        GuidelineImpactReceiptRow.__table__,
        GuidelineImpactItemRow.__table__,
        GuidelineImpactAdoptionRow.__table__,
        GuidelineImpactUnlinkRow.__table__,
        GuidelineRetirementImpactRow.__table__,
    )

    async def _install_sqlite_triggers(conn: object) -> bool:
        expected = guideline_impact_immutability_trigger_manifest()
        predecessors = tuple(
            guideline_impact_immutability_trigger_manifest(
                allow_board_erasure=allow_board_erasure,
                include_unlink=include_unlink,
                include_retirement=include_retirement,
                require_retirement_head_match=(require_retirement_head_match),
                verify_full_adoption_evidence=(verify_full_adoption_evidence),
                verify_full_unlink_evidence=(verify_full_unlink_evidence),
                verify_full_retirement_evidence=(verify_full_retirement_evidence),
                verify_default_materialization=verify_default_materialization,
                protect_materialized_events=protect_materialized_events,
            )
            for allow_board_erasure in (False, True)
            for include_unlink in (False, True)
            for include_retirement in (False, True)
            for require_retirement_head_match in (False, True)
            for verify_full_adoption_evidence in (False, True)
            for verify_full_unlink_evidence in (False, True)
            for verify_full_retirement_evidence in (False, True)
            for verify_default_materialization in (False, True)
            for protect_materialized_events in (False, True)
            if not (
                allow_board_erasure
                and include_unlink
                and include_retirement
                and not require_retirement_head_match
                and verify_full_adoption_evidence
                and verify_full_unlink_evidence
                and verify_full_retirement_evidence
                and verify_default_materialization
                and protect_materialized_events
            )
            and (include_unlink or not verify_full_unlink_evidence)
            and (include_retirement or not verify_full_retirement_evidence)
        )
        rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing = {str(row["name"]): row for row in rows}
        unexpected = set(existing) - set(expected)
        if unexpected:
            raise RuntimeError(
                "guideline impact has unexpected owned triggers: "
                + ", ".join(sorted(unexpected))
            )
        changed = False
        for trigger_name, (table_name, trigger_sql) in expected.items():
            row = existing.get(trigger_name)
            if row is None:
                await conn.execute(sa_text(trigger_sql))
                changed = True
                continue
            observed = normalize_global_discovery_source_revision_trigger_sql(
                row["sql"]
            )
            wanted = normalize_global_discovery_source_revision_trigger_sql(trigger_sql)
            if str(row["tbl_name"]) == table_name and observed == wanted:
                continue
            recognized_predecessor = any(
                trigger_name in predecessor
                and str(row["tbl_name"]) == predecessor[trigger_name][0]
                and observed
                == normalize_global_discovery_source_revision_trigger_sql(
                    predecessor[trigger_name][1]
                )
                for predecessor in predecessors
            )
            if not recognized_predecessor:
                raise RuntimeError(
                    "guideline impact immutability trigger drift: " + trigger_name
                )
            await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
            await conn.execute(sa_text(trigger_sql))
            changed = True
        final_rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        final = {str(row["name"]): row for row in final_rows}
        if set(final) != set(expected):
            raise RuntimeError("guideline impact trigger convergence is incomplete")
        for trigger_name, (table_name, trigger_sql) in expected.items():
            row = final[trigger_name]
            if str(
                row["tbl_name"]
            ) != table_name or normalize_global_discovery_source_revision_trigger_sql(
                row["sql"]
            ) != normalize_global_discovery_source_revision_trigger_sql(trigger_sql):
                raise RuntimeError(
                    "guideline impact trigger audit failed: " + trigger_name
                )
        return changed

    async def _install_postgresql_triggers(conn: object) -> bool:
        function_name = "guideline_impact_v2_guard"
        function_ddl = f'''
CREATE OR REPLACE FUNCTION "{function_name}"()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    permitted boolean;
    evidence_id varchar;
BEGIN
    IF TG_TABLE_NAME = '{execution_table}' THEN
        IF NEW.handler_name <> 'PolicyConstraintProjectionHandler' THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM domain_events AS event
            WHERE event.id = NEW.event_id
              AND (
                  (
                      event.event_type = '{PolicyAdoptionChanged.event_type}'
                      AND event.payload_json->>'event_schema_version' =
                          'guideline-impact/v2'
                      AND event.payload_json->>'operation'
                          IN ('adopt', 'unlink')
                      AND json_typeof(
                          event.payload_json->'guideline_id'
                      ) = 'string'
                  )
                  OR (
                      event.event_type =
                          '{PolicyRetirementChanged.event_type}'
                      AND event.payload_json->>'event_schema_version' =
                          'guideline-impact/v2'
                      AND event.payload_json->>'operation' = 'retire'
                      AND json_typeof(
                          event.payload_json->'revision_id'
                      ) = 'string'
                  )
                  OR (
                      event.event_type =
                          '{POLICY_BINDING_MATERIALIZED_EVENT_TYPE}'
                      AND (
                          SELECT COUNT(*)
                          FROM json_object_keys(event.payload_json)
                      ) = 13
                      AND event.payload_json->>'event_schema_version' =
                          'policy-binding-materialized/v2'
                      AND event.payload_json->>'operation' = 'adopt'
                      AND json_typeof(
                          event.payload_json->'revision_id'
                      ) = 'string'
                      AND event.payload_json->>'source_kind'
                          IN ('native', 'default_materialization')
                      AND event.payload_json->>'enforcement'
                          IN ('advisory', 'blocking')
                      AND (
                          event.payload_json->>'minimum_confidence'
                      ) ~ '^[0-9]+$'
                      AND (
                          event.payload_json->>'minimum_confidence'
                      )::integer BETWEEN 0 AND 100
                      AND jsonb_typeof(
                          event.payload_json
                          ->'metric_threshold_overrides'
                      ) = 'object'
                  )
                  OR (
                      event.event_type =
                          '{SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE}'
                      AND (
                          SELECT COUNT(*)
                          FROM json_object_keys(event.payload_json)
                      ) = 6
                      AND event.payload_json->>'event_schema_version' =
                          '{SEMANTIC_GUIDELINE_PROJECTION_SCHEMA_VERSION}'
                      AND json_typeof(
                          event.payload_json->'causation_id'
                      ) = 'string'
                      AND event.payload_json->>'entity_kind' IN (
                          'revision', 'metric_definition',
                          'binding_configuration', 'assessment_receipt',
                          'metric_result', 'waiver', 'skip'
                      )
                      AND json_typeof(
                          event.payload_json->'entity_id'
                      ) = 'string'
                      AND (
                          event.payload_json->>'entity_digest'
                      ) ~ '^[0-9a-f]{64}$'
                      AND event.payload_json->>'operation'
                          IN ('upsert', 'terminate')
                  )
              )
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'policy_constraint_execution_event_invalid';
    END IF;

    IF TG_TABLE_NAME = 'domain_events' THEN
        evidence_id := OLD.id;
        IF NOT EXISTS (
            SELECT 1 FROM "{adoption_table}" AS adoption
            WHERE adoption.event_id = evidence_id
            UNION ALL
            SELECT 1 FROM "{unlink_table}" AS unlink
            WHERE unlink.event_id = evidence_id
            UNION ALL
            SELECT 1 FROM "{retirement_impact_table}" AS retirement
            WHERE retirement.event_id = evidence_id
        ) AND OLD.event_type <> 'board.semantic_policy_binding_materialized.v2'
          AND OLD.event_type <>
              '{SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE}' THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
    ELSIF TG_TABLE_NAME = 'activity_logs' THEN
        evidence_id := OLD.id;
        IF NOT EXISTS (
            SELECT 1 FROM "{adoption_table}" AS adoption
            WHERE adoption.activity_id = evidence_id
            UNION ALL
            SELECT 1 FROM "{unlink_table}" AS unlink
            WHERE unlink.activity_id = evidence_id
            UNION ALL
            SELECT 1 FROM "{retirement_impact_table}" AS retirement
            WHERE retirement.activity_id = evidence_id
        ) THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT EXISTS (
            SELECT 1
            FROM "{permit_table}" AS permit
            WHERE permit.board_id = OLD.board_id
        ) INTO permitted;
        IF permitted THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'guideline_impact_evidence_immutable';
    END IF;

    IF TG_TABLE_NAME = '{receipt_table}' AND TG_OP = 'INSERT' THEN
        IF NEW.sealed = false
           AND jsonb_typeof(
               NEW.proposed_metric_threshold_overrides::jsonb
           ) = 'object'
           AND jsonb_typeof(NEW.added_metric_ids::jsonb) = 'array'
           AND jsonb_typeof(NEW.changed_metric_ids::jsonb) = 'array'
           AND jsonb_typeof(NEW.removed_metric_ids::jsonb) = 'array'
           AND NOT EXISTS (
               SELECT 1
               FROM jsonb_each(
                   NEW.proposed_metric_threshold_overrides::jsonb
               ) AS override(key, value)
               WHERE jsonb_typeof(override.value) <> 'number'
                  OR override.value::text !~ '^[0-9]+$'
                  OR override.value::text::integer NOT BETWEEN 0 AND 100
                  OR NOT EXISTS (
                      SELECT 1
                      FROM semantic_guideline_revisions AS revision,
                           jsonb_array_elements(
                               revision.metrics::jsonb
                           ) AS metric(value)
                      WHERE revision.guideline_id = NEW.guideline_id
                        AND revision.revision_id = NEW.to_revision_id
                        AND revision.revision_digest =
                            NEW.to_revision_digest
                        AND metric.value->>'code' = override.key
                  )
           )
           AND NOT EXISTS (
               SELECT 1
               FROM (
                   SELECT value
                   FROM jsonb_array_elements(
                       NEW.added_metric_ids::jsonb
                   )
                   UNION ALL
                   SELECT value
                   FROM jsonb_array_elements(
                       NEW.changed_metric_ids::jsonb
                   )
                   UNION ALL
                   SELECT value
                   FROM jsonb_array_elements(
                       NEW.removed_metric_ids::jsonb
                   )
               ) AS metric(value)
               WHERE jsonb_typeof(metric.value) <> 'string'
                  OR btrim(metric.value #>> '{{}}') = ''
           )
           AND (
               SELECT COUNT(*)
               FROM (
                   SELECT value
                   FROM jsonb_array_elements_text(
                       NEW.added_metric_ids::jsonb
                   )
                   UNION ALL
                   SELECT value
                   FROM jsonb_array_elements_text(
                       NEW.changed_metric_ids::jsonb
                   )
                   UNION ALL
                   SELECT value
                   FROM jsonb_array_elements_text(
                       NEW.removed_metric_ids::jsonb
                   )
               ) AS metric(value)
           ) = (
               SELECT COUNT(DISTINCT value)
               FROM (
                   SELECT value
                   FROM jsonb_array_elements_text(
                       NEW.added_metric_ids::jsonb
                   )
                   UNION ALL
                   SELECT value
                   FROM jsonb_array_elements_text(
                       NEW.changed_metric_ids::jsonb
                   )
                   UNION ALL
                   SELECT value
                   FROM jsonb_array_elements_text(
                       NEW.removed_metric_ids::jsonb
                   )
               ) AS metric(value)
           )
        THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'guideline_impact_receipt_v2_invalid';
    END IF;

    IF TG_TABLE_NAME = '{receipt_table}' AND TG_OP = 'UPDATE' THEN
        IF OLD.sealed = false
           AND NEW.sealed = true
           AND (to_jsonb(NEW) - 'sealed') = (to_jsonb(OLD) - 'sealed')
           AND NEW.item_count = (
               SELECT COUNT(*)
               FROM "{item_table}" AS item
               WHERE item.impact_receipt_id = OLD.impact_receipt_id
           )
        THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'guideline_impact_evidence_immutable';
    END IF;

    IF TG_TABLE_NAME = '{item_table}' AND TG_OP = 'INSERT' THEN
        IF EXISTS (
            SELECT 1
            FROM "{receipt_table}" AS receipt
            WHERE receipt.impact_receipt_id = NEW.impact_receipt_id
              AND receipt.board_id = NEW.board_id
              AND receipt.guideline_id = NEW.guideline_id
              AND receipt.sealed = false
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'guideline_impact_evidence_sealed';
    END IF;

    IF TG_TABLE_NAME = '{binding_table}' AND TG_OP = 'INSERT' THEN
        IF NEW.state NOT IN ('active', 'unlinked') THEN
            RETURN NEW;
        END IF;
        IF NEW.state = 'active'
           AND NOT EXISTS (
               SELECT 1 FROM guidelines AS guideline
               WHERE guideline.id = NEW.guideline_id
                 AND guideline.scope = 'global'
                 AND guideline.board_id IS NULL
           )
        THEN
            RETURN NEW;
        END IF;
        IF NEW.state = 'active'
           AND NEW.binding_origin = 'default_materialization'
           AND NEW.binding_revision = 1
           AND NEW.impact_receipt_id IS NULL
           AND NEW.impact_adoption_id IS NULL
           AND NEW.impact_unlink_id IS NULL
           AND NEW.enforcement = 'advisory'
           AND NEW.legacy_version_unresolvable = false
           AND NOT EXISTS (
               SELECT 1 FROM "{binding_table}" AS previous
               WHERE previous.board_id = NEW.board_id
                 AND previous.guideline_id = NEW.guideline_id
           )
           AND EXISTS (
               SELECT 1
               FROM boards AS board
               JOIN default_board_configurations AS template
                 ON template.id = (
                     board.default_config_snapshot::jsonb ->> 'template_id'
                 )
                AND template.version = (
                    board.default_config_snapshot::jsonb
                    ->> 'template_version'
                )::integer
               CROSS JOIN LATERAL jsonb_array_elements(
                   CASE
                       WHEN jsonb_typeof(
                           template.guideline_default_refs::jsonb
                       ) = 'array'
                       THEN template.guideline_default_refs::jsonb
                       ELSE '[]'::jsonb
                   END
               ) AS ref(value)
               WHERE board.id = NEW.board_id
                 AND NEW.legacy_template_id = template.id
                 AND NEW.legacy_template_version = template.version
                 AND ref.value ->> 'guideline_id' = NEW.guideline_id
                 AND ref.value ->> 'revision_id' = NEW.revision_id
                 AND ref.value ->> 'semantic_version' =
                     NEW.semantic_version
                 AND ref.value ->> 'revision_digest' =
                     NEW.revision_digest
                 AND (ref.value ->> 'priority')::integer =
                     NEW.priority
                 AND (ref.value ->> 'revision_number')::integer =
                     NEW.legacy_guideline_version
           )
        THEN
            RETURN NEW;
        END IF;
        IF NEW.state = 'active'
           AND NEW.impact_adoption_id IS NOT NULL
           AND NEW.impact_unlink_id IS NULL
           AND EXISTS (
               SELECT 1
               FROM "{receipt_table}" AS receipt
               WHERE receipt.impact_receipt_id = NEW.impact_receipt_id
                 AND receipt.board_id = NEW.board_id
                 AND receipt.guideline_id = NEW.guideline_id
                 AND receipt.binding_id = NEW.binding_id
                 AND receipt.to_revision_id = NEW.revision_id
                 AND receipt.to_semantic_version = NEW.semantic_version
                 AND receipt.proposed_priority = NEW.priority
                 AND receipt.proposed_enforcement =
                     NEW.enforcement
                 AND receipt.sealed = true
                 AND (
                     (
                         NEW.binding_revision = 1
                         AND receipt.expected_binding_revision IS NULL
                         AND receipt.expected_binding_state IS NULL
                     )
                     OR (
                         NEW.binding_revision > 1
                         AND receipt.expected_binding_revision =
                             NEW.binding_revision - 1
                         AND EXISTS (
                             SELECT 1
                             FROM "{binding_table}" AS previous
                             WHERE previous.binding_id = NEW.binding_id
                               AND previous.binding_revision =
                                   NEW.binding_revision - 1
                               AND previous.board_id = NEW.board_id
                               AND previous.guideline_id = NEW.guideline_id
                               AND previous.revision_id =
                                   receipt.from_revision_id
                               AND previous.semantic_version =
                                   receipt.from_semantic_version
                               AND EXISTS (
                                   SELECT 1
                                   FROM semantic_guideline_binding_configurations
                                        AS previous_configuration
                                   WHERE previous_configuration.binding_id =
                                             previous.binding_id
                                     AND previous_configuration.binding_revision =
                                             previous.binding_revision
                                     AND previous_configuration.revision_digest =
                                             receipt.from_revision_digest
                               )
                               AND previous.state =
                                   receipt.expected_binding_state
                         )
                     )
                 )
           )
        THEN
            RETURN NEW;
        END IF;
        IF NEW.state = 'unlinked'
           AND NEW.impact_receipt_id IS NULL
           AND NEW.impact_adoption_id IS NULL
           AND NEW.impact_unlink_id IS NOT NULL
           AND NEW.binding_revision > 1
           AND EXISTS (
               SELECT 1
               FROM "{binding_table}" AS previous
               WHERE previous.binding_id = NEW.binding_id
                 AND previous.binding_revision =
                     NEW.binding_revision - 1
                 AND previous.board_id = NEW.board_id
                 AND previous.guideline_id = NEW.guideline_id
                 AND previous.state = 'active'
                 AND previous.revision_id = NEW.revision_id
                 AND previous.semantic_version = NEW.semantic_version
                 AND previous.revision_digest = NEW.revision_digest
                 AND previous.priority = NEW.priority
                 AND previous.enforcement =
                     NEW.enforcement
                 AND previous.source_kind = NEW.source_kind
                 AND previous.binding_origin = NEW.binding_origin
                 AND previous.legacy_source_id
                     IS NOT DISTINCT FROM NEW.legacy_source_id
                 AND previous.legacy_guideline_version
                     IS NOT DISTINCT FROM NEW.legacy_guideline_version
                 AND previous.legacy_template_id
                     IS NOT DISTINCT FROM NEW.legacy_template_id
                 AND previous.legacy_template_version
                     IS NOT DISTINCT FROM NEW.legacy_template_version
                 AND previous.legacy_version_unresolvable =
                     NEW.legacy_version_unresolvable
           )
        THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'guideline_impact_preview_required';
    END IF;

    IF TG_TABLE_NAME = '{adoption_table}' AND TG_OP = 'INSERT' THEN
        IF EXISTS (
            SELECT 1
            FROM "{receipt_table}" AS receipt
            JOIN "{binding_table}" AS binding
              ON binding.binding_id = NEW.binding_id
             AND binding.binding_revision = NEW.binding_revision
             AND binding.impact_receipt_id = NEW.impact_receipt_id
             AND binding.impact_adoption_id = NEW.adoption_id
             AND binding.impact_unlink_id IS NULL
             AND binding.board_id = NEW.board_id
             AND binding.guideline_id = NEW.guideline_id
             AND binding.state = 'active'
             AND binding.revision_id = receipt.to_revision_id
             AND binding.semantic_version = receipt.to_semantic_version
             AND binding.priority = receipt.proposed_priority
             AND binding.enforcement =
                 receipt.proposed_enforcement
             AND binding.adopted_by = NEW.adopted_by
             AND binding.adopted_at = NEW.adopted_at
            JOIN semantic_guideline_binding_configurations AS configuration
              ON configuration.binding_id = binding.binding_id
             AND configuration.binding_revision =
                 binding.binding_revision
             AND configuration.board_id = binding.board_id
             AND configuration.guideline_id = binding.guideline_id
             AND configuration.revision_id = binding.revision_id
             AND configuration.revision_digest =
                 receipt.to_revision_digest
             AND configuration.enforcement =
                 receipt.proposed_enforcement
             AND configuration.minimum_confidence =
                 receipt.proposed_minimum_confidence
             AND configuration.metric_threshold_overrides::jsonb =
                 receipt.proposed_metric_threshold_overrides::jsonb
            JOIN domain_events AS event
              ON event.id = NEW.event_id
             AND event.event_type = 'board.semantic_guideline_adoption_changed.v2'
             AND event.board_id = NEW.board_id
             AND event.actor_id = NEW.adopted_by
             AND event.occurred_at = NEW.adopted_at
            JOIN activity_logs AS activity
              ON activity.id = NEW.activity_id
             AND activity.board_id = NEW.board_id
             AND activity.card_id IS NULL
             AND activity.action = 'guideline_revision_adopted'
             AND activity.actor_id = NEW.adopted_by
             AND activity.actor_type = event.actor_type
             AND activity.created_at = NEW.adopted_at
             AND activity.details::jsonb = event.payload_json::jsonb
            WHERE receipt.impact_receipt_id = NEW.impact_receipt_id
              AND receipt.board_id = NEW.board_id
              AND receipt.guideline_id = NEW.guideline_id
              AND receipt.binding_id = NEW.binding_id
              AND receipt.impact_digest = NEW.impact_digest
              AND receipt.binding_digest = NEW.binding_digest
              AND receipt.expected_binding_revision
                  IS NOT DISTINCT FROM NEW.expected_binding_revision
              AND receipt.sealed = true
              AND event.payload_json->>'operation' = 'adopt'
              AND event.payload_json->>'board_id' = NEW.board_id
              AND event.payload_json->>'guideline_id' = NEW.guideline_id
              AND event.payload_json->>'binding_id' = NEW.binding_id
              AND (event.payload_json->>'binding_revision')::integer =
                  NEW.binding_revision
              AND event.payload_json->>'impact_receipt_id' =
                  NEW.impact_receipt_id
              AND event.payload_json->>'impact_digest' = NEW.impact_digest
              AND event.payload_json->>'policy_set_digest_before' =
                  receipt.policy_set_digest_before
              AND event.payload_json->>'policy_set_digest_after' =
                  receipt.policy_set_digest_after
              AND receipt.requires_explicit_adoption = true
              AND binding.binding_revision =
                  COALESCE(receipt.expected_binding_revision, 0) + 1
              AND binding.idempotency_key = NEW.idempotency_key
              AND binding.request_digest = NEW.request_digest
              AND length(NEW.adoption_digest) = 64
              AND event.actor_type = activity.actor_type
              AND event.actor_type IN ('agent', 'user', 'system')
              AND activity.actor_name = NEW.adopted_by
              AND event.payload_json->>'event_schema_version' =
                  'guideline-impact/v2'
              AND event.payload_json->>'event_id' = NEW.event_id
              AND (
                  (
                      receipt.expected_binding_revision IS NULL
                      AND event.payload_json ?
                          'previous_binding_revision'
                      AND event.payload_json
                          ->'previous_binding_revision' = 'null'::jsonb
                  )
                  OR (
                      event.payload_json
                          ->>'previous_binding_revision'
                      )::integer =
                      receipt.expected_binding_revision
              )
              AND (
                  (
                      receipt.from_revision_id IS NULL
                      AND event.payload_json ? 'from_revision_id'
                      AND event.payload_json->'from_revision_id' =
                          'null'::jsonb
                  )
                  OR event.payload_json->>'from_revision_id' =
                      receipt.from_revision_id
              )
              AND (
                  (
                      receipt.from_semantic_version IS NULL
                      AND event.payload_json ? 'from_semantic_version'
                      AND event.payload_json->'from_semantic_version' =
                          'null'::jsonb
                  )
                  OR event.payload_json->>'from_semantic_version' =
                      receipt.from_semantic_version
              )
              AND (
                  (
                      receipt.from_revision_digest IS NULL
                      AND event.payload_json ? 'from_revision_digest'
                      AND event.payload_json->'from_revision_digest' =
                          'null'::jsonb
                  )
                  OR event.payload_json->>'from_revision_digest' =
                      receipt.from_revision_digest
              )
              AND event.payload_json->>'to_revision_id' =
                  receipt.to_revision_id
              AND event.payload_json->>'to_semantic_version' =
                  receipt.to_semantic_version
              AND event.payload_json->>'to_revision_digest' =
                  receipt.to_revision_digest
              AND event.payload_json->>'binding_digest_before' =
                  receipt.binding_digest
              AND event.payload_json->>'binding_head_digest_before' =
                  receipt.binding_head_digest_before
              AND event.payload_json->>'binding_head_digest_after' =
                  receipt.binding_head_digest_after
              AND event.payload_json->>'policy_set_digest' =
                  receipt.policy_set_digest_after
              AND event.payload_json->'added_metric_ids' =
                  receipt.added_metric_ids::jsonb
              AND event.payload_json->'changed_metric_ids' =
                  receipt.changed_metric_ids::jsonb
              AND event.payload_json->'removed_metric_ids' =
                  receipt.removed_metric_ids::jsonb
              AND event.payload_json->>'actor_id' = NEW.adopted_by
              AND event.payload_json->>'actor_type' = event.actor_type
              AND (
                  event.payload_json->>'occurred_at'
              )::timestamptz = NEW.adopted_at
              AND event.payload_json::jsonb = jsonb_build_object(
                  'event_schema_version', 'guideline-impact/v2',
                  'event_id', NEW.event_id,
                  'operation', 'adopt',
                  'board_id', NEW.board_id,
                  'guideline_id', NEW.guideline_id,
                  'binding_id', NEW.binding_id,
                  'previous_binding_revision',
                      receipt.expected_binding_revision,
                  'binding_revision', NEW.binding_revision,
                  'from_revision_id', receipt.from_revision_id,
                  'from_semantic_version',
                      receipt.from_semantic_version,
                  'from_revision_digest', receipt.from_revision_digest,
                  'to_revision_id', receipt.to_revision_id,
                  'to_semantic_version', receipt.to_semantic_version,
                  'to_revision_digest', receipt.to_revision_digest,
                  'impact_receipt_id', NEW.impact_receipt_id,
                  'impact_digest', NEW.impact_digest,
                  'binding_digest_before', receipt.binding_digest,
                  'binding_head_digest_before',
                      receipt.binding_head_digest_before,
                  'binding_head_digest_after',
                      receipt.binding_head_digest_after,
                  'policy_set_digest_before',
                      receipt.policy_set_digest_before,
                  'policy_set_digest_after',
                      receipt.policy_set_digest_after,
                  'policy_set_digest', receipt.policy_set_digest_after,
                  'added_metric_ids', receipt.added_metric_ids::jsonb,
                  'changed_metric_ids', receipt.changed_metric_ids::jsonb,
                  'removed_metric_ids', receipt.removed_metric_ids::jsonb,
                  'actor_id', NEW.adopted_by,
                  'actor_type', event.actor_type,
                  'occurred_at', event.payload_json->'occurred_at'
              )
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'guideline_impact_adoption_evidence_invalid';
    END IF;

    IF TG_TABLE_NAME = '{unlink_table}' AND TG_OP = 'INSERT' THEN
        IF EXISTS (
            SELECT 1
            FROM "{binding_table}" AS binding
            JOIN "{binding_table}" AS previous
              ON previous.binding_id = NEW.binding_id
             AND previous.binding_revision =
                 NEW.previous_binding_revision
             AND previous.board_id = NEW.board_id
             AND previous.guideline_id = NEW.guideline_id
             AND previous.state = 'active'
            JOIN semantic_guideline_binding_configurations AS configuration
              ON configuration.binding_id = binding.binding_id
             AND configuration.binding_revision =
                 binding.binding_revision
            JOIN semantic_guideline_binding_configurations
                 AS previous_configuration
              ON previous_configuration.binding_id =
                 previous.binding_id
             AND previous_configuration.binding_revision =
                 previous.binding_revision
             AND previous_configuration.revision_digest =
                 configuration.revision_digest
             AND previous_configuration.enforcement =
                 configuration.enforcement
             AND previous_configuration.minimum_confidence =
                 configuration.minimum_confidence
             AND previous_configuration.metric_threshold_overrides::jsonb =
                 configuration.metric_threshold_overrides::jsonb
             AND previous_configuration.configuration_digest =
                 configuration.configuration_digest
            JOIN domain_events AS event
              ON event.id = NEW.event_id
             AND event.event_type = 'board.semantic_guideline_adoption_changed.v2'
             AND event.board_id = NEW.board_id
             AND event.actor_id = NEW.unlinked_by
             AND event.actor_type = NEW.actor_type
             AND event.occurred_at = NEW.unlinked_at
            JOIN activity_logs AS activity
              ON activity.id = NEW.activity_id
             AND activity.board_id = NEW.board_id
             AND activity.card_id IS NULL
             AND activity.action = 'guideline_unlinked'
             AND activity.actor_id = NEW.unlinked_by
             AND activity.actor_type = NEW.actor_type
             AND activity.created_at = NEW.unlinked_at
             AND activity.details::jsonb = event.payload_json::jsonb
            WHERE binding.binding_id = NEW.binding_id
              AND binding.binding_revision = NEW.binding_revision
              AND binding.board_id = NEW.board_id
              AND binding.guideline_id = NEW.guideline_id
              AND binding.state = 'unlinked'
              AND binding.impact_receipt_id IS NULL
              AND binding.impact_adoption_id IS NULL
              AND binding.impact_unlink_id = NEW.unlink_id
              AND binding.binding_revision =
                  previous.binding_revision + 1
              AND binding.revision_id = previous.revision_id
              AND binding.semantic_version = previous.semantic_version
              AND binding.revision_digest = previous.revision_digest
              AND binding.priority = previous.priority
              AND binding.enforcement =
                  previous.enforcement
              AND binding.source_kind = previous.source_kind
              AND binding.binding_origin = previous.binding_origin
              AND binding.legacy_source_id
                  IS NOT DISTINCT FROM previous.legacy_source_id
              AND binding.legacy_guideline_version
                  IS NOT DISTINCT FROM previous.legacy_guideline_version
              AND binding.legacy_template_id
                  IS NOT DISTINCT FROM previous.legacy_template_id
              AND binding.legacy_template_version
                  IS NOT DISTINCT FROM previous.legacy_template_version
              AND binding.legacy_version_unresolvable =
                  previous.legacy_version_unresolvable
              AND binding.adopted_by = NEW.unlinked_by
              AND binding.adopted_at = NEW.unlinked_at
              AND binding.idempotency_key = NEW.idempotency_key
              AND binding.request_digest = NEW.request_digest
              AND length(NEW.unlink_digest) = 64
              AND event.actor_type = activity.actor_type
              AND event.actor_type IN ('agent', 'user', 'system')
              AND activity.actor_name = NEW.unlinked_by
              AND event.payload_json->>'event_schema_version' =
                  'guideline-impact/v2'
              AND event.payload_json->>'event_id' = NEW.event_id
              AND event.payload_json->>'operation' = 'unlink'
              AND event.payload_json->>'board_id' = NEW.board_id
              AND event.payload_json->>'guideline_id' = NEW.guideline_id
              AND event.payload_json->>'binding_id' = NEW.binding_id
              AND (
                  event.payload_json->>'previous_binding_revision'
              )::integer = NEW.previous_binding_revision
              AND (event.payload_json->>'binding_revision')::integer =
                  NEW.binding_revision
              AND event.payload_json->>'from_revision_id' =
                  previous.revision_id
              AND event.payload_json->>'from_semantic_version' =
                  previous.semantic_version
              AND event.payload_json->>'from_revision_digest' =
                  previous_configuration.revision_digest
              AND event.payload_json ? 'to_revision_id'
              AND event.payload_json->'to_revision_id' = 'null'::jsonb
              AND event.payload_json ? 'to_semantic_version'
              AND event.payload_json->'to_semantic_version' =
                  'null'::jsonb
              AND event.payload_json ? 'to_revision_digest'
              AND event.payload_json->'to_revision_digest' =
                  'null'::jsonb
              AND event.payload_json ? 'impact_receipt_id'
              AND event.payload_json->'impact_receipt_id' =
                  'null'::jsonb
              AND event.payload_json ? 'impact_digest'
              AND event.payload_json->'impact_digest' = 'null'::jsonb
              AND event.payload_json->>'binding_digest_before' =
                  NEW.binding_digest_before
              AND event.payload_json->>'binding_head_digest_before' =
                  NEW.binding_head_digest_before
              AND event.payload_json->>'binding_head_digest_after' =
                  NEW.binding_head_digest_after
              AND event.payload_json->>'policy_set_digest_before' =
                  NEW.policy_set_digest_before
              AND event.payload_json->>'policy_set_digest_after' =
                  NEW.policy_set_digest_after
              AND event.payload_json->>'policy_set_digest' =
                  NEW.policy_set_digest_after
              AND event.payload_json->'added_metric_ids' = '[]'::jsonb
              AND event.payload_json->'changed_metric_ids' = '[]'::jsonb
              AND event.payload_json->'removed_metric_ids' =
                  NEW.removed_metric_ids::jsonb
              AND event.payload_json->>'actor_id' = NEW.unlinked_by
              AND event.payload_json->>'actor_type' = NEW.actor_type
              AND (
                  event.payload_json->>'occurred_at'
              )::timestamptz = NEW.unlinked_at
              AND event.payload_json::jsonb = jsonb_build_object(
                  'event_schema_version', 'guideline-impact/v2',
                  'event_id', NEW.event_id,
                  'operation', 'unlink',
                  'board_id', NEW.board_id,
                  'guideline_id', NEW.guideline_id,
                  'binding_id', NEW.binding_id,
                  'previous_binding_revision',
                      NEW.previous_binding_revision,
                  'binding_revision', NEW.binding_revision,
                  'from_revision_id', previous.revision_id,
                  'from_semantic_version',
                      previous.semantic_version,
                  'from_revision_digest',
                      previous_configuration.revision_digest,
                  'to_revision_id', NULL,
                  'to_semantic_version', NULL,
                  'to_revision_digest', NULL,
                  'impact_receipt_id', NULL,
                  'impact_digest', NULL,
                  'binding_digest_before',
                      NEW.binding_digest_before,
                  'binding_head_digest_before',
                      NEW.binding_head_digest_before,
                  'binding_head_digest_after',
                      NEW.binding_head_digest_after,
                  'policy_set_digest_before',
                      NEW.policy_set_digest_before,
                  'policy_set_digest_after',
                      NEW.policy_set_digest_after,
                  'policy_set_digest',
                      NEW.policy_set_digest_after,
                  'added_metric_ids', '[]'::jsonb,
                  'changed_metric_ids', '[]'::jsonb,
                  'removed_metric_ids', NEW.removed_metric_ids::jsonb,
                  'actor_id', NEW.unlinked_by,
                  'actor_type', NEW.actor_type,
                  'occurred_at', event.payload_json->'occurred_at'
              )
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'guideline_impact_unlink_evidence_invalid';
    END IF;

    IF TG_TABLE_NAME = '{retirement_impact_table}'
       AND TG_OP = 'INSERT'
    THEN
        IF EXISTS (
            SELECT 1
            FROM guideline_retirements AS retirement
            JOIN "{binding_table}" AS binding
              ON binding.binding_id = NEW.binding_id
             AND binding.binding_revision = NEW.binding_revision
             AND binding.board_id = NEW.board_id
             AND binding.guideline_id = NEW.guideline_id
             AND binding.state = 'active'
             AND binding.revision_id = NEW.revision_id
             AND binding.semantic_version = NEW.semantic_version
            JOIN semantic_guideline_binding_configurations AS configuration
              ON configuration.binding_id = binding.binding_id
             AND configuration.binding_revision =
                 binding.binding_revision
             AND configuration.revision_id = NEW.revision_id
             AND configuration.revision_digest = NEW.revision_digest
            JOIN domain_events AS event
              ON event.id = NEW.event_id
             AND event.event_type = 'board.semantic_guideline_retirement_changed.v2'
             AND event.board_id = NEW.board_id
             AND event.actor_id = NEW.retired_by
             AND event.actor_type = NEW.actor_type
             AND event.occurred_at = NEW.retired_at
            JOIN activity_logs AS activity
              ON activity.id = NEW.activity_id
             AND activity.board_id = NEW.board_id
             AND activity.card_id IS NULL
             AND activity.action = 'guideline_retired'
             AND activity.actor_id = NEW.retired_by
             AND activity.actor_type = NEW.actor_type
             AND activity.created_at = NEW.retired_at
             AND activity.details::jsonb = event.payload_json::jsonb
            WHERE retirement.retirement_id = NEW.retirement_id
              AND retirement.guideline_id = NEW.guideline_id
              AND retirement.status = NEW.retirement_status
              AND retirement.superseded_by_guideline_id
                  IS NOT DISTINCT FROM NEW.superseded_by_guideline_id
              AND retirement.retired_by = NEW.retired_by
              AND retirement.retired_at = NEW.retired_at
              AND retirement.request_digest = NEW.request_digest
              AND event.actor_type = activity.actor_type
              AND event.actor_type IN ('agent', 'user', 'system')
              AND activity.actor_name = NEW.retired_by
              AND NOT EXISTS (
                  SELECT 1
                  FROM "{binding_table}" AS later
                  WHERE later.board_id = binding.board_id
                    AND later.guideline_id = binding.guideline_id
                    AND later.binding_revision > binding.binding_revision
              )
              AND event.payload_json->>'event_schema_version' =
                  'guideline-impact/v2'
              AND event.payload_json->>'event_id' = NEW.event_id
              AND event.payload_json->>'operation' = 'retire'
              AND event.payload_json->>'board_id' = NEW.board_id
              AND event.payload_json->>'guideline_id' = NEW.guideline_id
              AND event.payload_json->>'retirement_id' =
                  NEW.retirement_id
              AND event.payload_json->>'retirement_status' =
                  NEW.retirement_status
              AND (
                  (
                      NEW.superseded_by_guideline_id IS NULL
                      AND event.payload_json ?
                          'superseded_by_guideline_id'
                      AND event.payload_json
                          ->'superseded_by_guideline_id' = 'null'::jsonb
                  )
                  OR event.payload_json
                      ->>'superseded_by_guideline_id' =
                      NEW.superseded_by_guideline_id
              )
              AND event.payload_json->>'binding_id' = NEW.binding_id
              AND (event.payload_json->>'binding_revision')::integer =
                  NEW.binding_revision
              AND event.payload_json->>'revision_id' = NEW.revision_id
              AND (event.payload_json->>'revision_number')::integer =
                  NEW.revision_number
              AND event.payload_json->>'semantic_version' =
                  NEW.semantic_version
              AND event.payload_json->>'revision_digest' =
                  NEW.revision_digest
              AND event.payload_json->>'binding_digest_before' =
                  NEW.binding_digest_before
              AND event.payload_json->>'binding_head_digest_before' =
                  NEW.binding_head_digest_before
              AND event.payload_json->>'binding_head_digest_after' =
                  NEW.binding_head_digest_after
              AND event.payload_json->>'policy_set_digest_before' =
                  NEW.policy_set_digest_before
              AND event.payload_json->>'policy_set_digest_after' =
                  NEW.policy_set_digest_after
              AND event.payload_json->>'policy_set_digest' =
                  NEW.policy_set_digest_after
              AND event.payload_json->'removed_metric_ids' =
                  NEW.removed_metric_ids::jsonb
              AND event.payload_json->>'actor_id' = NEW.retired_by
              AND event.payload_json->>'actor_type' = NEW.actor_type
              AND (
                  event.payload_json->>'occurred_at'
              )::timestamptz = NEW.retired_at
              AND event.payload_json->>'request_digest' =
                  NEW.request_digest
              AND event.payload_json::jsonb = jsonb_build_object(
                  'event_schema_version', 'guideline-impact/v2',
                  'event_id', NEW.event_id,
                  'operation', 'retire',
                  'board_id', NEW.board_id,
                  'guideline_id', NEW.guideline_id,
                  'retirement_id', NEW.retirement_id,
                  'retirement_status', NEW.retirement_status,
                  'superseded_by_guideline_id',
                      NEW.superseded_by_guideline_id,
                  'binding_id', NEW.binding_id,
                  'binding_revision', NEW.binding_revision,
                  'revision_id', NEW.revision_id,
                  'revision_number', NEW.revision_number,
                  'semantic_version', NEW.semantic_version,
                  'revision_digest', NEW.revision_digest,
                  'binding_digest_before',
                      NEW.binding_digest_before,
                  'binding_head_digest_before',
                      NEW.binding_head_digest_before,
                  'binding_head_digest_after',
                      NEW.binding_head_digest_after,
                  'policy_set_digest_before',
                      NEW.policy_set_digest_before,
                  'policy_set_digest_after',
                      NEW.policy_set_digest_after,
                  'policy_set_digest',
                      NEW.policy_set_digest_after,
                  'removed_metric_ids', NEW.removed_metric_ids::jsonb,
                  'actor_id', NEW.retired_by,
                  'actor_type', NEW.actor_type,
                  'occurred_at', event.payload_json->'occurred_at',
                  'request_digest', NEW.request_digest
              )
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'guideline_retirement_impact_evidence_invalid';
    END IF;

    RAISE EXCEPTION 'guideline_impact_evidence_immutable';
END;
$$
'''
        await conn.execute(sa_text(function_ddl))
        contracts = {
            (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{receipt_table}_guard"): (
                receipt_table,
                "INSERT OR UPDATE OR DELETE",
            ),
            (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{item_table}_guard"): (
                item_table,
                "INSERT OR UPDATE OR DELETE",
            ),
            (
                f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{adoption_table}_guard"
            ): (adoption_table, "INSERT OR UPDATE OR DELETE"),
            (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_{unlink_table}_guard"): (
                unlink_table,
                "INSERT OR UPDATE OR DELETE",
            ),
            (
                f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{retirement_impact_table}_guard"
            ): (
                retirement_impact_table,
                "INSERT OR UPDATE OR DELETE",
            ),
            (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_binding_guard"): (
                binding_table,
                "INSERT",
            ),
            (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_event_guard"): (
                "domain_events",
                "UPDATE OR DELETE",
            ),
            (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_activity_guard"): (
                "activity_logs",
                "UPDATE OR DELETE",
            ),
            (
                f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}_"
                f"{execution_table}_policy_constraint_insert"
            ): (
                execution_table,
                "INSERT",
            ),
        }
        rows = (
            (
                await conn.execute(
                    sa_text(
                        "SELECT trigger.tgname AS name, "
                        "relation.relname AS table_name, "
                        "procedure.proname AS function_name "
                        ", trigger.tgenabled AS enabled "
                        ", trigger.tgtype::integer AS trigger_type "
                        ", trigger.tgqual AS predicate "
                        "FROM pg_trigger AS trigger "
                        "JOIN pg_class AS relation "
                        "ON relation.oid = trigger.tgrelid "
                        "JOIN pg_proc AS procedure "
                        "ON procedure.oid = trigger.tgfoid "
                        "WHERE NOT trigger.tgisinternal "
                        "AND trigger.tgname LIKE :prefix"
                    ),
                    {"prefix": (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .mappings()
            .all()
        )
        existing = {str(row["name"]): row for row in rows}
        unexpected = set(existing) - set(contracts)
        if unexpected:
            raise RuntimeError(
                "guideline impact has unexpected PostgreSQL triggers: "
                + ", ".join(sorted(unexpected))
            )
        changed = False
        operation_types = {
            "UPDATE OR DELETE": 27,
            "INSERT OR UPDATE OR DELETE": 31,
            "INSERT": 7,
        }
        for trigger_name, (table_name, operations) in contracts.items():
            row = existing.get(trigger_name)
            if row is not None:
                if (
                    str(row["table_name"]) != table_name
                    or str(row["function_name"]) != function_name
                    or str(row["enabled"]) != "O"
                    or int(row["trigger_type"]) != operation_types[operations]
                    or row["predicate"] is not None
                ):
                    raise RuntimeError(
                        "guideline impact PostgreSQL trigger drift: " + trigger_name
                    )
                continue
            await conn.execute(
                sa_text(
                    f'CREATE TRIGGER "{trigger_name}" '
                    f"BEFORE {operations} "
                    f'ON "{table_name}" FOR EACH ROW '
                    f'EXECUTE FUNCTION "{function_name}"()'
                )
            )
            changed = True
        return changed

    engine = get_engine()
    changed = False
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "guideline impact migration supports only SQLite and PostgreSQL"
            )
        if dialect == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")

        table_names = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        required_predecessors = {
            binding_table,
            "boards",
            "guidelines",
            "guideline_revisions",
            "guideline_retirements",
            "domain_events",
            "activity_logs",
            "default_board_configurations",
            permit_table,
            *(table.name for table in tables),
        }
        missing = required_predecessors - table_names
        if missing:
            raise RuntimeError(
                "guideline impact migration requires create_all; missing tables: "
                + ", ".join(sorted(missing))
            )
        binding_columns = await conn.run_sync(
            lambda sync_conn: {
                str(column["name"])
                for column in sa_inspect(sync_conn).get_columns(binding_table)
            }
        )
        missing_binding_columns = {
            "impact_receipt_id",
            "binding_origin",
            "impact_adoption_id",
            "impact_unlink_id",
        } - binding_columns
        if missing_binding_columns:
            raise RuntimeError(
                "guideline impact substrate did not add binding evidence: "
                + ", ".join(sorted(missing_binding_columns))
            )

        for table in tables:
            if dialect == "sqlite":
                contract = await conn.run_sync(
                    lambda sync_conn, owned=table: _sqlite_owned_table_contract(
                        sync_conn, owned
                    )
                )
            else:
                contract = await conn.run_sync(
                    lambda sync_conn, owned=table: _postgresql_owned_table_contract(
                        sync_conn, owned
                    )
                )
            if contract["observed"] != contract["expected"]:
                raise RuntimeError(
                    "guideline impact table has a non-canonical contract: " + table.name
                )
        if dialect == "sqlite":
            binding_contract = await conn.run_sync(
                lambda sync_conn: _sqlite_owned_table_contract(
                    sync_conn,
                    GuidelineBoardBindingRow.__table__,
                )
            )
            if binding_contract["observed"] != binding_contract["expected"]:
                raise RuntimeError(
                    "guideline impact binding substrate is non-canonical"
                )

        if dialect == "sqlite":
            adoption_payload_audit = """
              AND json(activity."details") =
                  json(event."payload_json")
              AND json_type(event."payload_json") = 'object'
              AND (
                  SELECT COUNT(*)
                  FROM json_each(event."payload_json")
              ) = 28
              AND json_extract(
                  event."payload_json", '$.event_schema_version'
              ) = 'guideline-impact/v2'
              AND json_extract(event."payload_json", '$.event_id') =
                  adoption."event_id"
              AND json_extract(event."payload_json", '$.operation') =
                  'adopt'
              AND json_extract(event."payload_json", '$.board_id') =
                  adoption."board_id"
              AND json_extract(event."payload_json", '$.guideline_id') =
                  adoption."guideline_id"
              AND json_extract(event."payload_json", '$.binding_id') =
                  adoption."binding_id"
              AND json_extract(
                  event."payload_json", '$.binding_revision'
              ) = adoption."binding_revision"
              AND (
                  (
                      receipt."expected_binding_revision" IS NULL
                      AND json_type(
                          event."payload_json",
                          '$.previous_binding_revision'
                      ) = 'null'
                  )
                  OR json_extract(
                      event."payload_json",
                      '$.previous_binding_revision'
                  ) = receipt."expected_binding_revision"
              )
              AND (
                  (
                      receipt."from_revision_id" IS NULL
                      AND json_type(
                          event."payload_json", '$.from_revision_id'
                      ) = 'null'
                  )
                  OR json_extract(
                      event."payload_json", '$.from_revision_id'
                  ) = receipt."from_revision_id"
              )
              AND (
                  (
                      receipt."from_semantic_version" IS NULL
                      AND json_type(
                          event."payload_json", '$.from_semantic_version'
                      ) = 'null'
                  )
                  OR json_extract(
                      event."payload_json", '$.from_semantic_version'
                  ) = receipt."from_semantic_version"
              )
              AND (
                  (
                      receipt."from_revision_digest" IS NULL
                      AND json_type(
                          event."payload_json", '$.from_revision_digest'
                      ) = 'null'
                  )
                  OR json_extract(
                      event."payload_json", '$.from_revision_digest'
                  ) = receipt."from_revision_digest"
              )
              AND json_extract(
                  event."payload_json", '$.to_revision_id'
              ) = receipt."to_revision_id"
              AND json_extract(
                  event."payload_json", '$.to_semantic_version'
              ) = receipt."to_semantic_version"
              AND json_extract(
                  event."payload_json", '$.to_revision_digest'
              ) = receipt."to_revision_digest"
              AND json_extract(
                  event."payload_json", '$.impact_receipt_id'
              ) = adoption."impact_receipt_id"
              AND json_extract(
                  event."payload_json", '$.impact_digest'
              ) = adoption."impact_digest"
              AND json_extract(
                  event."payload_json", '$.binding_digest_before'
              ) = receipt."binding_digest"
              AND json_extract(
                  event."payload_json", '$.binding_head_digest_before'
              ) = receipt."binding_head_digest_before"
              AND json_extract(
                  event."payload_json", '$.binding_head_digest_after'
              ) = receipt."binding_head_digest_after"
              AND json_extract(
                  event."payload_json", '$.policy_set_digest_before'
              ) = receipt."policy_set_digest_before"
              AND json_extract(
                  event."payload_json", '$.policy_set_digest_after'
              ) = receipt."policy_set_digest_after"
              AND json_extract(
                  event."payload_json", '$.policy_set_digest'
              ) = receipt."policy_set_digest_after"
              AND json(json_extract(
                  event."payload_json", '$.added_metric_ids'
              )) = json(receipt."added_metric_ids")
              AND json(json_extract(
                  event."payload_json", '$.changed_metric_ids'
              )) = json(receipt."changed_metric_ids")
              AND json(json_extract(
                  event."payload_json", '$.removed_metric_ids'
              )) = json(receipt."removed_metric_ids")
              AND json_extract(event."payload_json", '$.actor_id') =
                  adoption."adopted_by"
              AND json_extract(event."payload_json", '$.actor_type') =
                  event."actor_type"
              AND julianday(json_extract(
                  event."payload_json", '$.occurred_at'
              )) = julianday(adoption."adopted_at")"""
        else:
            adoption_payload_audit = """
              AND activity."details"::jsonb =
                  event."payload_json"::jsonb
              AND event."payload_json"->>'event_schema_version' =
                  'guideline-impact/v2'
              AND event."payload_json"->>'event_id' =
                  adoption."event_id"
              AND event."payload_json"->>'operation' = 'adopt'
              AND event."payload_json"->>'board_id' =
                  adoption."board_id"
              AND event."payload_json"->>'guideline_id' =
                  adoption."guideline_id"
              AND event."payload_json"->>'binding_id' =
                  adoption."binding_id"
              AND (
                  event."payload_json"->>'binding_revision'
              )::integer = adoption."binding_revision"
              AND (
                  (
                      receipt."expected_binding_revision" IS NULL
                      AND event."payload_json" ?
                          'previous_binding_revision'
                      AND event."payload_json"
                          ->'previous_binding_revision' = 'null'::jsonb
                  )
                  OR (
                      event."payload_json"
                          ->>'previous_binding_revision'
                  )::integer = receipt."expected_binding_revision"
              )
              AND (
                  (
                      receipt."from_revision_id" IS NULL
                      AND event."payload_json" ? 'from_revision_id'
                      AND event."payload_json"->'from_revision_id' =
                          'null'::jsonb
                  )
                  OR event."payload_json"->>'from_revision_id' =
                      receipt."from_revision_id"
              )
              AND (
                  (
                      receipt."from_semantic_version" IS NULL
                      AND event."payload_json" ? 'from_semantic_version'
                      AND event."payload_json"->'from_semantic_version' =
                          'null'::jsonb
                  )
                  OR event."payload_json"->>'from_semantic_version' =
                      receipt."from_semantic_version"
              )
              AND (
                  (
                      receipt."from_revision_digest" IS NULL
                      AND event."payload_json" ? 'from_revision_digest'
                      AND event."payload_json"->'from_revision_digest' =
                          'null'::jsonb
                  )
                  OR event."payload_json"->>'from_revision_digest' =
                      receipt."from_revision_digest"
              )
              AND event."payload_json"->>'to_revision_id' =
                  receipt."to_revision_id"
              AND event."payload_json"->>'to_semantic_version' =
                  receipt."to_semantic_version"
              AND event."payload_json"->>'to_revision_digest' =
                  receipt."to_revision_digest"
              AND event."payload_json"->>'impact_receipt_id' =
                  adoption."impact_receipt_id"
              AND event."payload_json"->>'impact_digest' =
                  adoption."impact_digest"
              AND event."payload_json"->>'binding_digest_before' =
                  receipt."binding_digest"
              AND event."payload_json"->>'binding_head_digest_before' =
                  receipt."binding_head_digest_before"
              AND event."payload_json"->>'binding_head_digest_after' =
                  receipt."binding_head_digest_after"
              AND event."payload_json"->>'policy_set_digest_before' =
                  receipt."policy_set_digest_before"
              AND event."payload_json"->>'policy_set_digest_after' =
                  receipt."policy_set_digest_after"
              AND event."payload_json"->>'policy_set_digest' =
                  receipt."policy_set_digest_after"
              AND event."payload_json"->'added_metric_ids' =
                  receipt."added_metric_ids"::jsonb
              AND event."payload_json"->'changed_metric_ids' =
                  receipt."changed_metric_ids"::jsonb
              AND event."payload_json"->'removed_metric_ids' =
                  receipt."removed_metric_ids"::jsonb
              AND event."payload_json"->>'actor_id' =
                  adoption."adopted_by"
              AND event."payload_json"->>'actor_type' =
                  event."actor_type"
              AND (
                  event."payload_json"->>'occurred_at'
              )::timestamptz = adoption."adopted_at"
              AND event."payload_json"::jsonb = jsonb_build_object(
                  'event_schema_version', 'guideline-impact/v2',
                  'event_id', adoption."event_id",
                  'operation', 'adopt',
                  'board_id', adoption."board_id",
                  'guideline_id', adoption."guideline_id",
                  'binding_id', adoption."binding_id",
                  'previous_binding_revision',
                      receipt."expected_binding_revision",
                  'binding_revision', adoption."binding_revision",
                  'from_revision_id', receipt."from_revision_id",
                  'from_semantic_version',
                      receipt."from_semantic_version",
                  'from_revision_digest',
                      receipt."from_revision_digest",
                  'to_revision_id', receipt."to_revision_id",
                  'to_semantic_version',
                      receipt."to_semantic_version",
                  'to_revision_digest', receipt."to_revision_digest",
                  'impact_receipt_id', adoption."impact_receipt_id",
                  'impact_digest', adoption."impact_digest",
                  'binding_digest_before', receipt."binding_digest",
                  'binding_head_digest_before',
                      receipt."binding_head_digest_before",
                  'binding_head_digest_after',
                      receipt."binding_head_digest_after",
                  'policy_set_digest_before',
                      receipt."policy_set_digest_before",
                  'policy_set_digest_after',
                      receipt."policy_set_digest_after",
                  'policy_set_digest',
                      receipt."policy_set_digest_after",
                  'added_metric_ids', receipt."added_metric_ids"::jsonb,
                  'changed_metric_ids',
                      receipt."changed_metric_ids"::jsonb,
                  'removed_metric_ids',
                      receipt."removed_metric_ids"::jsonb,
                  'actor_id', adoption."adopted_by",
                  'actor_type', event."actor_type",
                  'occurred_at',
                      event."payload_json"->'occurred_at'
              )"""

        adoption_lineage_audit = f'''
SELECT COUNT(*)
FROM "{adoption_table}" AS adoption
WHERE NOT EXISTS (
    SELECT 1
    FROM "{receipt_table}" AS receipt
    JOIN "{binding_table}" AS binding
      ON binding."binding_id" = adoption."binding_id"
     AND binding."binding_revision" = adoption."binding_revision"
     AND binding."board_id" = adoption."board_id"
     AND binding."guideline_id" = adoption."guideline_id"
     AND binding."impact_receipt_id" = adoption."impact_receipt_id"
     AND binding."impact_adoption_id" = adoption."adoption_id"
     AND binding."impact_unlink_id" IS NULL
     AND binding."state" = 'active'
     AND binding."revision_id" = receipt."to_revision_id"
     AND binding."semantic_version" = receipt."to_semantic_version"
     AND binding."revision_digest" = receipt."to_revision_digest"
     AND binding."priority" = receipt."proposed_priority"
     AND binding."enforcement" =
         receipt."proposed_enforcement"
     AND binding."binding_revision" =
         COALESCE(receipt."expected_binding_revision", 0) + 1
     AND binding."adopted_by" = adoption."adopted_by"
     AND binding."adopted_at" = adoption."adopted_at"
     AND binding."idempotency_key" = adoption."idempotency_key"
     AND binding."request_digest" = adoption."request_digest"
    JOIN "domain_events" AS event
      ON event."id" = adoption."event_id"
     AND event."event_type" = 'board.semantic_guideline_adoption_changed.v2'
     AND event."board_id" = adoption."board_id"
     AND event."actor_id" = adoption."adopted_by"
     AND event."occurred_at" = adoption."adopted_at"
    JOIN "activity_logs" AS activity
      ON activity."id" = adoption."activity_id"
     AND activity."board_id" = adoption."board_id"
     AND activity."card_id" IS NULL
     AND activity."action" = 'guideline_revision_adopted'
     AND activity."actor_id" = adoption."adopted_by"
     AND activity."actor_type" = event."actor_type"
     AND activity."actor_name" = adoption."adopted_by"
     AND activity."created_at" = adoption."adopted_at"
    WHERE receipt."impact_receipt_id" = adoption."impact_receipt_id"
      AND receipt."board_id" = adoption."board_id"
      AND receipt."guideline_id" = adoption."guideline_id"
      AND receipt."binding_id" = adoption."binding_id"
      AND receipt."expected_binding_revision"
          IS NOT DISTINCT FROM adoption."expected_binding_revision"
      AND receipt."impact_digest" = adoption."impact_digest"
      AND receipt."binding_digest" = adoption."binding_digest"
      AND receipt."sealed" = true
      AND receipt."requires_explicit_adoption" = true
      AND length(adoption."adoption_digest") = 64
      AND event."actor_type" = activity."actor_type"
      AND event."actor_type" IN ('agent', 'user', 'system')
      AND (
          (
              receipt."expected_binding_revision" IS NULL
              AND receipt."expected_binding_state" IS NULL
              AND receipt."from_revision_id" IS NULL
              AND receipt."from_semantic_version" IS NULL
              AND receipt."from_revision_digest" IS NULL
          )
          OR EXISTS (
              SELECT 1
              FROM "{binding_table}" AS previous
              WHERE previous."binding_id" = adoption."binding_id"
                AND previous."binding_revision" =
                    receipt."expected_binding_revision"
                AND previous."board_id" = adoption."board_id"
                AND previous."guideline_id" = adoption."guideline_id"
                AND previous."revision_id" =
                    receipt."from_revision_id"
                AND previous."semantic_version" =
                    receipt."from_semantic_version"
                AND previous."revision_digest" =
                    receipt."from_revision_digest"
                AND previous."state" =
                    receipt."expected_binding_state"
          )
      ){adoption_payload_audit}
)
'''

        row_audits = {
            "unsealed_receipts": (
                f'SELECT COUNT(*) FROM "{receipt_table}" WHERE "sealed" = false'
            ),
            "receipt_item_count": (
                f'SELECT COUNT(*) FROM "{receipt_table}" AS receipt '
                'WHERE receipt."item_count" < 1 OR receipt."item_count" <> ('
                f'SELECT COUNT(*) FROM "{item_table}" AS item '
                'WHERE item."impact_receipt_id" = '
                'receipt."impact_receipt_id")'
            ),
            "item_scope": (
                f'SELECT COUNT(*) FROM "{item_table}" AS item '
                f'LEFT JOIN "{receipt_table}" AS receipt '
                'ON receipt."impact_receipt_id" = item."impact_receipt_id" '
                'AND receipt."board_id" = item."board_id" '
                'AND receipt."guideline_id" = item."guideline_id" '
                'WHERE receipt."impact_receipt_id" IS NULL'
            ),
            "adoption_lineage": adoption_lineage_audit,
            "unlink_lineage": (
                f'SELECT COUNT(*) FROM "{unlink_table}" AS unlink '
                f'LEFT JOIN "{binding_table}" AS binding '
                'ON binding."binding_id" = unlink."binding_id" '
                'AND binding."binding_revision" = '
                'unlink."binding_revision" '
                'AND binding."board_id" = unlink."board_id" '
                'AND binding."guideline_id" = unlink."guideline_id" '
                "AND binding.\"state\" = 'unlinked' "
                'AND binding."impact_receipt_id" IS NULL '
                'AND binding."impact_adoption_id" IS NULL '
                'AND binding."impact_unlink_id" = unlink."unlink_id" '
                f'LEFT JOIN "{binding_table}" AS previous '
                'ON previous."binding_id" = unlink."binding_id" '
                'AND previous."binding_revision" = '
                'unlink."previous_binding_revision" '
                'AND previous."board_id" = unlink."board_id" '
                'AND previous."guideline_id" = unlink."guideline_id" '
                "AND previous.\"state\" = 'active' "
                'LEFT JOIN "domain_events" AS event '
                'ON event."id" = unlink."event_id" '
                'AND event."event_type" = '
                "'board.semantic_guideline_adoption_changed.v2' "
                'AND event."board_id" = unlink."board_id" '
                'AND event."actor_id" = unlink."unlinked_by" '
                'AND event."actor_type" = unlink."actor_type" '
                'LEFT JOIN "activity_logs" AS activity '
                'ON activity."id" = unlink."activity_id" '
                'AND activity."board_id" = unlink."board_id" '
                'AND activity."card_id" IS NULL '
                "AND activity.\"action\" = 'guideline_unlinked' "
                'AND activity."actor_id" = unlink."unlinked_by" '
                'AND activity."actor_type" = unlink."actor_type" '
                'WHERE binding."binding_id" IS NULL '
                'OR previous."binding_id" IS NULL '
                'OR unlink."binding_revision" <> '
                'unlink."previous_binding_revision" + 1 '
                'OR binding."revision_id" <> previous."revision_id" '
                'OR binding."semantic_version" <> '
                'previous."semantic_version" '
                'OR binding."revision_digest" <> '
                'previous."revision_digest" '
                'OR binding."priority" <> previous."priority" '
                'OR binding."enforcement" <> '
                'previous."enforcement" '
                'OR binding."source_kind" <> previous."source_kind" '
                'OR binding."binding_origin" <> '
                'previous."binding_origin" '
                'OR event."id" IS NULL OR activity."id" IS NULL'
            ),
            "retirement_lineage": (
                f'SELECT COUNT(*) FROM "{retirement_impact_table}" AS impact '
                'LEFT JOIN "guideline_retirements" AS retirement '
                'ON retirement."retirement_id" = impact."retirement_id" '
                'AND retirement."guideline_id" = impact."guideline_id" '
                'AND retirement."status" = impact."retirement_status" '
                'AND retirement."retired_by" = impact."retired_by" '
                'AND retirement."retired_at" = impact."retired_at" '
                f'LEFT JOIN "{binding_table}" AS binding '
                'ON binding."binding_id" = impact."binding_id" '
                'AND binding."binding_revision" = '
                'impact."binding_revision" '
                'AND binding."board_id" = impact."board_id" '
                'AND binding."guideline_id" = impact."guideline_id" '
                "AND binding.\"state\" = 'active' "
                'AND binding."revision_id" = impact."revision_id" '
                'AND binding."semantic_version" = '
                'impact."semantic_version" '
                'AND binding."revision_digest" = '
                'impact."revision_digest" '
                'LEFT JOIN "domain_events" AS event '
                'ON event."id" = impact."event_id" '
                'AND event."event_type" = '
                "'board.semantic_guideline_retirement_changed.v2' "
                'AND event."board_id" = impact."board_id" '
                'AND event."actor_id" = impact."retired_by" '
                'AND event."actor_type" = impact."actor_type" '
                'AND event."occurred_at" = impact."retired_at" '
                'LEFT JOIN "activity_logs" AS activity '
                'ON activity."id" = impact."activity_id" '
                'AND activity."board_id" = impact."board_id" '
                'AND activity."card_id" IS NULL '
                "AND activity.\"action\" = 'guideline_retired' "
                'AND activity."actor_id" = impact."retired_by" '
                'AND activity."actor_type" = impact."actor_type" '
                'AND activity."created_at" = impact."retired_at" '
                'WHERE retirement."retirement_id" IS NULL '
                'OR binding."binding_id" IS NULL '
                'OR event."id" IS NULL OR activity."id" IS NULL'
            ),
        }
        for audit_name, statement in row_audits.items():
            invalid = int((await conn.execute(sa_text(statement))).scalar_one())
            if invalid:
                raise RuntimeError(
                    f"guideline impact {audit_name} audit failed: {invalid}"
                )

        adoption_digest_rows = (
            (
                await conn.execute(
                    select(
                        GuidelineImpactAdoptionRow.adoption_id,
                        GuidelineImpactAdoptionRow.impact_receipt_id,
                        GuidelineImpactAdoptionRow.impact_digest,
                        GuidelineImpactAdoptionRow.binding_id,
                        GuidelineImpactAdoptionRow.binding_revision,
                        GuidelineImpactAdoptionRow.event_id,
                        GuidelineImpactAdoptionRow.activity_id,
                        GuidelineImpactAdoptionRow.adopted_by,
                        GuidelineImpactAdoptionRow.adopted_at,
                        GuidelineImpactAdoptionRow.request_digest,
                        GuidelineImpactAdoptionRow.adoption_digest,
                        DomainEventRow.actor_type,
                    ).join(
                        DomainEventRow,
                        DomainEventRow.id == GuidelineImpactAdoptionRow.event_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        invalid_adoption_digests = 0
        for row in adoption_digest_rows:
            adopted_at = row["adopted_at"]
            if adopted_at.tzinfo is None or adopted_at.utcoffset() is None:
                adopted_at = adopted_at.replace(tzinfo=timezone.utc)
            else:
                adopted_at = adopted_at.astimezone(timezone.utc)
            expected_request_digest = canonical_sha256(
                {
                    "contract": "guideline-impact/v2",
                    "operation": "adopt",
                    "receipt_id": row["impact_receipt_id"],
                    "impact_digest": row["impact_digest"],
                    "binding_id": row["binding_id"],
                    "binding_revision": row["binding_revision"],
                    "actor_id": row["adopted_by"],
                    "actor_type": row["actor_type"],
                }
            )
            expected_adoption_digest = canonical_sha256(
                {
                    "contract": "guideline-impact-adoption/v1",
                    "adoption_id": row["adoption_id"],
                    "receipt_id": row["impact_receipt_id"],
                    "impact_digest": row["impact_digest"],
                    "binding_id": row["binding_id"],
                    "binding_revision": row["binding_revision"],
                    "event_id": row["event_id"],
                    "activity_id": row["activity_id"],
                    "actor_id": row["adopted_by"],
                    "adopted_at": adopted_at.isoformat(),
                }
            )
            if (
                row["request_digest"] != expected_request_digest
                or row["adoption_digest"] != expected_adoption_digest
            ):
                invalid_adoption_digests += 1
        if invalid_adoption_digests:
            raise RuntimeError(
                "guideline impact adoption digest audit failed: "
                f"{invalid_adoption_digests}"
            )

        def _utc_datetime(value: object) -> object:
            if value.tzinfo is None or value.utcoffset() is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        def _canonical_metric_ids(value: object) -> list[str] | None:
            if not isinstance(value, list | tuple) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                return None
            normalized = sorted(item.strip() for item in value)
            if len(set(normalized)) != len(normalized):
                return None
            return normalized

        def _same_canonical_payload(
            actual: object,
            expected: object,
        ) -> bool:
            try:
                return canonical_sha256(actual) == canonical_sha256(expected)
            except (TypeError, ValueError):
                return False

        unlink_current = aliased(GuidelineBoardBindingRow)
        unlink_previous = aliased(GuidelineBoardBindingRow)
        unlink_previous_configuration = aliased(
            SemanticGuidelineBindingConfigurationRow
        )
        unlink_digest_rows = (
            (
                await conn.execute(
                    select(
                        GuidelineImpactUnlinkRow.unlink_id,
                        GuidelineImpactUnlinkRow.board_id,
                        GuidelineImpactUnlinkRow.guideline_id,
                        GuidelineImpactUnlinkRow.binding_id,
                        GuidelineImpactUnlinkRow.binding_revision,
                        GuidelineImpactUnlinkRow.previous_binding_revision,
                        GuidelineImpactUnlinkRow.binding_digest_before,
                        GuidelineImpactUnlinkRow.binding_head_digest_before,
                        GuidelineImpactUnlinkRow.binding_head_digest_after,
                        GuidelineImpactUnlinkRow.policy_set_digest_before,
                        GuidelineImpactUnlinkRow.policy_set_digest_after,
                        GuidelineImpactUnlinkRow.removed_metric_ids,
                        GuidelineImpactUnlinkRow.unlinked_by,
                        GuidelineImpactUnlinkRow.actor_type,
                        GuidelineImpactUnlinkRow.unlinked_at,
                        GuidelineImpactUnlinkRow.event_id,
                        GuidelineImpactUnlinkRow.activity_id,
                        GuidelineImpactUnlinkRow.idempotency_key,
                        GuidelineImpactUnlinkRow.request_digest,
                        GuidelineImpactUnlinkRow.unlink_digest,
                        unlink_previous.revision_id.label("previous_revision_id"),
                        unlink_previous.semantic_version.label(
                            "previous_semantic_version"
                        ),
                        unlink_previous_configuration.revision_digest.label(
                            "previous_revision_digest"
                        ),
                        unlink_previous.priority.label("previous_priority"),
                        unlink_previous_configuration.enforcement.label(
                            "previous_enforcement"
                        ),
                        unlink_previous.state.label("previous_state"),
                        unlink_previous.binding_origin.label(
                            "previous_source_kind"
                        ),
                        unlink_previous_configuration.minimum_confidence.label(
                            "previous_minimum_confidence"
                        ),
                        unlink_previous_configuration.metric_threshold_overrides.label(
                            "previous_metric_threshold_overrides"
                        ),
                        unlink_previous_configuration.configuration_digest.label(
                            "previous_configuration_digest"
                        ),
                        unlink_current.idempotency_key.label("binding_idempotency_key"),
                        unlink_current.request_digest.label("binding_request_digest"),
                        unlink_current.adopted_by.label("binding_actor_id"),
                        unlink_current.adopted_at.label("binding_occurred_at"),
                        DomainEventRow.event_type,
                        DomainEventRow.board_id.label("event_board_id"),
                        DomainEventRow.actor_id.label("event_actor_id"),
                        DomainEventRow.actor_type.label("event_actor_type"),
                        DomainEventRow.payload_json,
                        DomainEventRow.occurred_at.label("event_occurred_at"),
                        ActivityLog.board_id.label("activity_board_id"),
                        ActivityLog.card_id,
                        ActivityLog.action,
                        ActivityLog.actor_id.label("activity_actor_id"),
                        ActivityLog.actor_type.label("activity_actor_type"),
                        ActivityLog.actor_name,
                        ActivityLog.details,
                        ActivityLog.created_at.label("activity_created_at"),
                    )
                    .join(
                        unlink_current,
                        (
                            unlink_current.binding_id
                            == GuidelineImpactUnlinkRow.binding_id
                        )
                        & (
                            unlink_current.binding_revision
                            == GuidelineImpactUnlinkRow.binding_revision
                        ),
                    )
                    .join(
                        unlink_previous,
                        (
                            unlink_previous.binding_id
                            == GuidelineImpactUnlinkRow.binding_id
                        )
                        & (
                            unlink_previous.binding_revision
                            == GuidelineImpactUnlinkRow.previous_binding_revision
                        ),
                    )
                    .join(
                        unlink_previous_configuration,
                        (
                            unlink_previous_configuration.binding_id
                            == unlink_previous.binding_id
                        )
                        & (
                            unlink_previous_configuration.binding_revision
                            == unlink_previous.binding_revision
                        ),
                    )
                    .join(
                        DomainEventRow,
                        DomainEventRow.id == GuidelineImpactUnlinkRow.event_id,
                    )
                    .join(
                        ActivityLog,
                        ActivityLog.id == GuidelineImpactUnlinkRow.activity_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        invalid_unlink_digests = 0
        for row in unlink_digest_rows:
            removed_metric_ids = _canonical_metric_ids(row["removed_metric_ids"])
            if removed_metric_ids is None:
                invalid_unlink_digests += 1
                continue
            unlinked_at = _utc_datetime(row["unlinked_at"])
            expected_binding_digest = _guideline_binding_fence_digest_v2(
                board_id=row["board_id"],
                guideline_id=row["guideline_id"],
                binding_id=row["binding_id"],
                binding_revision=row["previous_binding_revision"],
                revision_id=row["previous_revision_id"],
                semantic_version=row["previous_semantic_version"],
                revision_digest=row["previous_revision_digest"],
                priority=row["previous_priority"],
                enforcement=row["previous_enforcement"],
                minimum_confidence=row["previous_minimum_confidence"],
                metric_threshold_overrides=(
                    row["previous_metric_threshold_overrides"]
                ),
                configuration_digest=row["previous_configuration_digest"],
                state=row["previous_state"],
                source_kind=row["previous_source_kind"],
            )
            expected_payload = {
                "event_schema_version": "guideline-impact/v2",
                "event_id": row["event_id"],
                "operation": "unlink",
                "board_id": row["board_id"],
                "guideline_id": row["guideline_id"],
                "binding_id": row["binding_id"],
                "previous_binding_revision": row["previous_binding_revision"],
                "binding_revision": row["binding_revision"],
                "from_revision_id": row["previous_revision_id"],
                "from_semantic_version": row["previous_semantic_version"],
                "from_revision_digest": row["previous_revision_digest"],
                "to_revision_id": None,
                "to_semantic_version": None,
                "to_revision_digest": None,
                "impact_receipt_id": None,
                "impact_digest": None,
                "binding_digest_before": expected_binding_digest,
                "binding_head_digest_before": row["binding_head_digest_before"],
                "binding_head_digest_after": row["binding_head_digest_after"],
                "policy_set_digest_before": row["policy_set_digest_before"],
                "policy_set_digest_after": row["policy_set_digest_after"],
                "policy_set_digest": row["policy_set_digest_after"],
                "added_metric_ids": [],
                "changed_metric_ids": [],
                "removed_metric_ids": removed_metric_ids,
                "actor_id": row["unlinked_by"],
                "actor_type": row["actor_type"],
                "occurred_at": unlinked_at.isoformat(),
            }
            expected_request_digest = canonical_sha256(
                {
                    "contract": "guideline-impact/v2",
                    "operation": "unlink",
                    "binding_digest_before": expected_binding_digest,
                    "binding_id": row["binding_id"],
                    "binding_revision": row["binding_revision"],
                    "binding_head_digest_before": row["binding_head_digest_before"],
                    "binding_head_digest_after": row["binding_head_digest_after"],
                    "policy_set_digest_before": row["policy_set_digest_before"],
                    "policy_set_digest_after": row["policy_set_digest_after"],
                    "removed_metric_ids": removed_metric_ids,
                    "actor_id": row["unlinked_by"],
                    "actor_type": row["actor_type"],
                }
            )
            expected_unlink_digest = canonical_sha256(
                {
                    "contract": "guideline-impact-unlink/v1",
                    "unlink_id": row["unlink_id"],
                    "binding_id": row["binding_id"],
                    "binding_revision": row["binding_revision"],
                    "previous_binding_revision": row["previous_binding_revision"],
                    "binding_digest_before": expected_binding_digest,
                    "event_id": row["event_id"],
                    "activity_id": row["activity_id"],
                    "actor_id": row["unlinked_by"],
                    "actor_type": row["actor_type"],
                    "unlinked_at": unlinked_at.isoformat(),
                }
            )
            if (
                row["binding_digest_before"] != expected_binding_digest
                or row["request_digest"] != expected_request_digest
                or row["unlink_digest"] != expected_unlink_digest
                or row["binding_idempotency_key"] != row["idempotency_key"]
                or row["binding_request_digest"] != row["request_digest"]
                or row["binding_actor_id"] != row["unlinked_by"]
                or _utc_datetime(row["binding_occurred_at"]) != unlinked_at
                or row["event_type"] != "board.semantic_guideline_adoption_changed.v2"
                or row["event_board_id"] != row["board_id"]
                or row["event_actor_id"] != row["unlinked_by"]
                or row["event_actor_type"] != row["actor_type"]
                or _utc_datetime(row["event_occurred_at"]) != unlinked_at
                or row["activity_board_id"] != row["board_id"]
                or row["card_id"] is not None
                or row["action"] != "guideline_unlinked"
                or row["activity_actor_id"] != row["unlinked_by"]
                or row["activity_actor_type"] != row["actor_type"]
                or row["actor_name"] != row["unlinked_by"]
                or _utc_datetime(row["activity_created_at"]) != unlinked_at
                or not _same_canonical_payload(
                    row["payload_json"],
                    expected_payload,
                )
                or not _same_canonical_payload(
                    row["details"],
                    expected_payload,
                )
            ):
                invalid_unlink_digests += 1
        if invalid_unlink_digests:
            raise RuntimeError(
                f"guideline impact unlink digest audit failed: {invalid_unlink_digests}"
            )

        retirement_binding = aliased(GuidelineBoardBindingRow)
        retirement_configuration = aliased(
            SemanticGuidelineBindingConfigurationRow
        )
        retirement_digest_rows = (
            (
                await conn.execute(
                    select(
                        GuidelineRetirementImpactRow.impact_id,
                        GuidelineRetirementImpactRow.retirement_id,
                        GuidelineRetirementImpactRow.board_id,
                        GuidelineRetirementImpactRow.guideline_id,
                        GuidelineRetirementImpactRow.retirement_status,
                        GuidelineRetirementImpactRow.superseded_by_guideline_id,
                        GuidelineRetirementImpactRow.binding_id,
                        GuidelineRetirementImpactRow.binding_revision,
                        GuidelineRetirementImpactRow.revision_id,
                        GuidelineRetirementImpactRow.revision_number,
                        GuidelineRetirementImpactRow.semantic_version,
                        GuidelineRetirementImpactRow.revision_digest,
                        GuidelineRetirementImpactRow.binding_digest_before,
                        GuidelineRetirementImpactRow.binding_head_digest_before,
                        GuidelineRetirementImpactRow.binding_head_digest_after,
                        GuidelineRetirementImpactRow.policy_set_digest_before,
                        GuidelineRetirementImpactRow.policy_set_digest_after,
                        GuidelineRetirementImpactRow.removed_metric_ids,
                        GuidelineRetirementImpactRow.retired_by,
                        GuidelineRetirementImpactRow.actor_type,
                        GuidelineRetirementImpactRow.retired_at,
                        GuidelineRetirementImpactRow.event_id,
                        GuidelineRetirementImpactRow.activity_id,
                        GuidelineRetirementImpactRow.request_digest,
                        GuidelineRetirementImpactRow.impact_digest,
                        retirement_binding.priority.label("binding_priority"),
                        retirement_configuration.enforcement.label(
                            "binding_enforcement"
                        ),
                        retirement_binding.state.label("binding_state"),
                        retirement_binding.binding_origin.label(
                            "binding_source_kind"
                        ),
                        retirement_configuration.minimum_confidence.label(
                            "binding_minimum_confidence"
                        ),
                        retirement_configuration.metric_threshold_overrides.label(
                            "binding_metric_threshold_overrides"
                        ),
                        retirement_configuration.configuration_digest.label(
                            "binding_configuration_digest"
                        ),
                        GuidelineRetirementRow.retired_revision_id.label(
                            "terminal_revision_id"
                        ),
                        GuidelineRetirementRow.retired_revision_number.label(
                            "terminal_revision_number"
                        ),
                        GuidelineRetirementRow.retired_semantic_version.label(
                            "terminal_semantic_version"
                        ),
                        GuidelineRetirementRow.retired_revision_digest.label(
                            "terminal_revision_digest"
                        ),
                        GuidelineRetirementRow.retired_head_revision.label(
                            "terminal_head_revision"
                        ),
                        GuidelineRetirementRow.reason.label("terminal_reason"),
                        GuidelineRetirementRow.request_digest.label(
                            "terminal_request_digest"
                        ),
                        DomainEventRow.event_type,
                        DomainEventRow.board_id.label("event_board_id"),
                        DomainEventRow.actor_id.label("event_actor_id"),
                        DomainEventRow.actor_type.label("event_actor_type"),
                        DomainEventRow.payload_json,
                        DomainEventRow.occurred_at.label("event_occurred_at"),
                        ActivityLog.board_id.label("activity_board_id"),
                        ActivityLog.card_id,
                        ActivityLog.action,
                        ActivityLog.actor_id.label("activity_actor_id"),
                        ActivityLog.actor_type.label("activity_actor_type"),
                        ActivityLog.actor_name,
                        ActivityLog.details,
                        ActivityLog.created_at.label("activity_created_at"),
                    )
                    .join(
                        retirement_binding,
                        (
                            retirement_binding.binding_id
                            == GuidelineRetirementImpactRow.binding_id
                        )
                        & (
                            retirement_binding.binding_revision
                            == GuidelineRetirementImpactRow.binding_revision
                        ),
                    )
                    .join(
                        retirement_configuration,
                        (
                            retirement_configuration.binding_id
                            == retirement_binding.binding_id
                        )
                        & (
                            retirement_configuration.binding_revision
                            == retirement_binding.binding_revision
                        ),
                    )
                    .join(
                        GuidelineRetirementRow,
                        GuidelineRetirementRow.retirement_id
                        == GuidelineRetirementImpactRow.retirement_id,
                    )
                    .join(
                        DomainEventRow,
                        DomainEventRow.id == GuidelineRetirementImpactRow.event_id,
                    )
                    .join(
                        ActivityLog,
                        ActivityLog.id == GuidelineRetirementImpactRow.activity_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        invalid_retirement_digests = 0
        for row in retirement_digest_rows:
            removed_metric_ids = _canonical_metric_ids(row["removed_metric_ids"])
            if removed_metric_ids is None:
                invalid_retirement_digests += 1
                continue
            retired_at = _utc_datetime(row["retired_at"])
            expected_binding_digest = _guideline_binding_fence_digest_v2(
                board_id=row["board_id"],
                guideline_id=row["guideline_id"],
                binding_id=row["binding_id"],
                binding_revision=row["binding_revision"],
                revision_id=row["revision_id"],
                semantic_version=row["semantic_version"],
                revision_digest=row["revision_digest"],
                priority=row["binding_priority"],
                enforcement=row["binding_enforcement"],
                minimum_confidence=row["binding_minimum_confidence"],
                metric_threshold_overrides=(
                    row["binding_metric_threshold_overrides"]
                ),
                configuration_digest=row["binding_configuration_digest"],
                state=row["binding_state"],
                source_kind=row["binding_source_kind"],
            )
            expected_payload = {
                "event_schema_version": "guideline-impact/v2",
                "event_id": row["event_id"],
                "operation": "retire",
                "board_id": row["board_id"],
                "guideline_id": row["guideline_id"],
                "retirement_id": row["retirement_id"],
                "retirement_status": row["retirement_status"],
                "superseded_by_guideline_id": row["superseded_by_guideline_id"],
                "binding_id": row["binding_id"],
                "binding_revision": row["binding_revision"],
                "revision_id": row["revision_id"],
                "revision_number": row["revision_number"],
                "semantic_version": row["semantic_version"],
                "revision_digest": row["revision_digest"],
                "binding_digest_before": expected_binding_digest,
                "binding_head_digest_before": row["binding_head_digest_before"],
                "binding_head_digest_after": row["binding_head_digest_after"],
                "policy_set_digest_before": row["policy_set_digest_before"],
                "policy_set_digest_after": row["policy_set_digest_after"],
                "policy_set_digest": row["policy_set_digest_after"],
                "removed_metric_ids": removed_metric_ids,
                "actor_id": row["retired_by"],
                "actor_type": row["actor_type"],
                "occurred_at": retired_at.isoformat(),
                "request_digest": row["request_digest"],
            }
            expected_request_digest = guideline_request_digest_v1(
                operation="retire",
                scope_id=row["guideline_id"],
                payload={
                    "guideline_id": row["guideline_id"],
                    "expected_head_revision": row["terminal_head_revision"],
                    "retired_revision_id": row["terminal_revision_id"],
                    "retired_revision_number": row["terminal_revision_number"],
                    "retired_semantic_version": row["terminal_semantic_version"],
                    "retired_revision_digest": row["terminal_revision_digest"],
                    "status": row["retirement_status"],
                    "reason": row["terminal_reason"],
                    "superseded_by_guideline_id": row["superseded_by_guideline_id"],
                    "actor_id": row["retired_by"],
                },
            )
            expected_impact_digest = canonical_sha256(
                {
                    "contract": "guideline-impact/v2",
                    "operation": "retire",
                    "event": expected_payload,
                }
            )
            if (
                row["binding_digest_before"] != expected_binding_digest
                or row["request_digest"] != expected_request_digest
                or row["terminal_request_digest"] != expected_request_digest
                or row["impact_digest"] != expected_impact_digest
                or row["event_type"] != "board.semantic_guideline_retirement_changed.v2"
                or row["event_board_id"] != row["board_id"]
                or row["event_actor_id"] != row["retired_by"]
                or row["event_actor_type"] != row["actor_type"]
                or _utc_datetime(row["event_occurred_at"]) != retired_at
                or row["activity_board_id"] != row["board_id"]
                or row["card_id"] is not None
                or row["action"] != "guideline_retired"
                or row["activity_actor_id"] != row["retired_by"]
                or row["activity_actor_type"] != row["actor_type"]
                or row["actor_name"] != row["retired_by"]
                or _utc_datetime(row["activity_created_at"]) != retired_at
                or not _same_canonical_payload(
                    row["payload_json"],
                    expected_payload,
                )
                or not _same_canonical_payload(
                    row["details"],
                    expected_payload,
                )
            ):
                invalid_retirement_digests += 1
        if invalid_retirement_digests:
            raise RuntimeError(
                "guideline impact retirement digest audit failed: "
                f"{invalid_retirement_digests}"
            )

        if dialect == "sqlite":
            changed = await _install_sqlite_triggers(conn) or changed
            violations = list(
                (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            )
            if violations:
                raise RuntimeError(
                    "guideline impact migration left foreign-key violations: "
                    + repr(violations[:10])
                )
        else:
            changed = await _install_postgresql_triggers(conn) or changed

    return None if changed else "skipped"


async def _migrate_policy_compliance_v1_schema() -> str | None:
    """Converge B07 subject tokens and immutable compliance evidence."""

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        Card,
        PolicyComplianceAdoptedRevisionRow,
        PolicyComplianceFindingRow,
        PolicyComplianceReceiptRow,
        Spec,
    )

    engine = get_engine()
    changed = False
    tables = (
        PolicyComplianceReceiptRow.__table__,
        PolicyComplianceAdoptedRevisionRow.__table__,
        PolicyComplianceFindingRow.__table__,
    )

    def _table_names(sync_conn: object) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    def _columns(sync_conn: object, table_name: str) -> set[str]:
        return {
            str(column["name"])
            for column in sa_inspect(sync_conn).get_columns(table_name)
        }

    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "policy compliance migration supports only SQLite and PostgreSQL"
            )
        if dialect == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")

        table_names = await conn.run_sync(_table_names)
        for model, column_name in (
            (Spec, "test_scenario_policy_epoch"),
            (Card, "policy_version"),
        ):
            if model.__tablename__ not in table_names:
                raise RuntimeError(
                    "policy compliance migration missing subject table: "
                    + model.__tablename__
                )
            columns = await conn.run_sync(
                lambda sync_conn, name=model.__tablename__: _columns(
                    sync_conn,
                    name,
                )
            )
            if column_name not in columns:
                await conn.execute(
                    sa_text(
                        f'ALTER TABLE "{model.__tablename__}" '
                        f'ADD COLUMN "{column_name}" '
                        "INTEGER NOT NULL DEFAULT 1"
                    )
                )
                changed = True
            invalid = int(
                (
                    await conn.execute(
                        sa_text(
                            f'SELECT COUNT(*) FROM "{model.__tablename__}" '
                            f'WHERE "{column_name}" IS NULL '
                            f'OR "{column_name}" < 1'
                        )
                    )
                ).scalar_one()
            )
            if invalid:
                raise RuntimeError(
                    f"policy compliance invalid {column_name}: {invalid}"
                )

        table_names = await conn.run_sync(_table_names)
        receipt_table_name = PolicyComplianceReceiptRow.__tablename__
        if receipt_table_name in table_names:
            receipt_columns = await conn.run_sync(
                lambda sync_conn: _columns(sync_conn, receipt_table_name)
            )
            if "sealed" not in receipt_columns:
                sealed_type = (
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                    if dialect == "postgresql"
                    else "BOOLEAN NOT NULL DEFAULT false"
                )
                await conn.execute(
                    sa_text(
                        f'ALTER TABLE "{receipt_table_name}" '
                        f'ADD COLUMN "sealed" {sealed_type}'
                    )
                )
                changed = True

        for table in tables:
            if table.name not in table_names:
                await conn.run_sync(
                    lambda sync_conn, owned=table: owned.create(
                        sync_conn,
                        checkfirst=True,
                    )
                )
                table_names.add(table.name)
                changed = True
            if dialect == "sqlite":
                contract = await conn.run_sync(
                    lambda sync_conn, owned=table: _sqlite_owned_table_contract(
                        sync_conn,
                        owned,
                    )
                )
                if contract["observed"] != contract["expected"]:
                    raise RuntimeError(
                        "policy compliance table has a non-canonical contract: "
                        + table.name
                    )

        if dialect == "sqlite":
            expected = policy_compliance_immutability_trigger_manifest()
            predecessors = (
                policy_compliance_immutability_trigger_manifest(
                    allow_board_erasure=False,
                ),
                policy_compliance_immutability_trigger_manifest(
                    allow_aggregate_sealing=False,
                ),
                policy_compliance_immutability_trigger_manifest(
                    allow_board_erasure=False,
                    allow_aggregate_sealing=False,
                ),
            )
            rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {
                            "prefix": (
                                f"{POLICY_COMPLIANCE_IMMUTABILITY_TRIGGER_PREFIX}%"
                            )
                        },
                    )
                )
                .mappings()
                .all()
            )
            existing = {str(row["name"]): row for row in rows}
            unexpected = set(existing) - set(expected)
            if unexpected:
                raise RuntimeError(
                    "policy compliance has unexpected owned triggers: "
                    + ", ".join(sorted(unexpected))
                )
            for trigger_name, (table_name, trigger_sql) in expected.items():
                row = existing.get(trigger_name)
                if row is None:
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
                observed = normalize_global_discovery_source_revision_trigger_sql(
                    row["sql"]
                )
                canonical = normalize_global_discovery_source_revision_trigger_sql(
                    trigger_sql
                )
                if str(row["tbl_name"]) == table_name and observed == canonical:
                    continue
                recognized_predecessor = any(
                    trigger_name in predecessor
                    and str(row["tbl_name"]) == predecessor[trigger_name][0]
                    and observed
                    == normalize_global_discovery_source_revision_trigger_sql(
                        predecessor[trigger_name][1]
                    )
                    for predecessor in predecessors
                )
                if recognized_predecessor:
                    await conn.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
                raise RuntimeError(
                    "policy compliance immutable trigger is corrupt: " + trigger_name
                )
            final_rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {
                            "prefix": (
                                f"{POLICY_COMPLIANCE_IMMUTABILITY_TRIGGER_PREFIX}%"
                            )
                        },
                    )
                )
                .mappings()
                .all()
            )
            final = {str(row["name"]): row for row in final_rows}
            if set(final) != set(expected):
                raise RuntimeError(
                    "policy compliance trigger installation is incomplete"
                )
            incomplete_receipts = int(
                (
                    await conn.execute(
                        sa_text(
                            f'SELECT COUNT(*) FROM "{receipt_table_name}" AS receipt '
                            "WHERE receipt.finding_count <> ("
                            "SELECT COUNT(*) "
                            'FROM "policy_compliance_findings" AS finding '
                            "WHERE finding.receipt_id = receipt.receipt_id"
                            ")"
                        )
                    )
                ).scalar_one()
            )
            if incomplete_receipts:
                raise RuntimeError(
                    "policy compliance migration found incomplete receipt "
                    f"aggregates: {incomplete_receipts}"
                )
            sealed_rows = await conn.execute(
                sa_text(
                    f'UPDATE "{receipt_table_name}" SET "sealed" = 1 WHERE "sealed" = 0'
                )
            )
            if int(sealed_rows.rowcount or 0):
                changed = True
            violations = list(
                (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            )
            if violations:
                raise RuntimeError(
                    "policy compliance migration left foreign-key violations: "
                    + repr(violations[:10])
                )
        else:
            # Community is SQLite-first, but keep the append-only authority
            # correct for PostgreSQL deployments using the same metadata.
            function_name = "policy_compliance_immutable_guard_v1"
            await conn.execute(
                sa_text(
                    f"""
CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND TG_TABLE_NAME = 'policy_compliance_receipts'
       AND OLD.sealed = FALSE
       AND NEW.sealed = TRUE
       AND (to_jsonb(NEW) - 'sealed')
           IS NOT DISTINCT FROM (to_jsonb(OLD) - 'sealed')
    THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT'
       AND TG_TABLE_NAME IN (
           'policy_compliance_adopted_revisions',
           'policy_compliance_findings'
       )
    THEN
        IF EXISTS (
            SELECT 1 FROM policy_compliance_receipts AS receipt
            WHERE receipt.receipt_id = NEW.receipt_id
              AND receipt.sealed = TRUE
        ) THEN
            RAISE EXCEPTION 'policy_compliance_evidence_sealed';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF TG_TABLE_NAME = 'policy_compliance_adopted_revisions' THEN
            IF EXISTS (
                SELECT 1 FROM kg_board_erasure_permits AS permit
                JOIN policy_compliance_receipts AS receipt
                  ON receipt.board_id = permit.board_id
                WHERE receipt.receipt_id = OLD.receipt_id
            ) THEN RETURN OLD; END IF;
        ELSIF EXISTS (
            SELECT 1 FROM kg_board_erasure_permits AS permit
            WHERE permit.board_id = OLD.board_id
        ) THEN RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'policy_compliance_evidence_immutable';
END;
$$ LANGUAGE plpgsql
"""
                )
            )
            postgres_trigger_prefix = "trg_pc_immutable_"
            trigger_names = {
                PolicyComplianceReceiptRow.__tablename__: (
                    f"{postgres_trigger_prefix}receipt"
                ),
                PolicyComplianceAdoptedRevisionRow.__tablename__: (
                    f"{postgres_trigger_prefix}adopted"
                ),
                PolicyComplianceFindingRow.__tablename__: (
                    f"{postgres_trigger_prefix}finding"
                ),
            }
            if any(len(name) > 63 for name in trigger_names.values()):
                raise RuntimeError(
                    "policy compliance PostgreSQL trigger name exceeds 63 bytes"
                )
            trigger_rows = {
                str(row["trigger_name"]): row
                for row in (
                    (
                        await conn.execute(
                            sa_text(
                                """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       function.proname AS function_name,
       trigger.tgtype AS trigger_type
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
WHERE NOT trigger.tgisinternal
  AND trigger.tgname LIKE :prefix
"""
                            ),
                            {"prefix": f"{postgres_trigger_prefix}%"},
                        )
                    )
                    .mappings()
                    .all()
                )
            }
            expected_names = set(trigger_names.values())
            unexpected = set(trigger_rows) - expected_names
            if unexpected:
                raise RuntimeError(
                    "policy compliance has unexpected PostgreSQL triggers: "
                    + ", ".join(sorted(unexpected))
                )
            for table in tables:
                trigger_name = trigger_names[table.name]
                trigger_row = trigger_rows.get(trigger_name)
                includes_insert = table.name != PolicyComplianceReceiptRow.__tablename__
                operation_clause = (
                    "INSERT OR UPDATE OR DELETE"
                    if includes_insert
                    else "UPDATE OR DELETE"
                )
                expected_trigger_type = 31 if includes_insert else 27
                if trigger_row is None:
                    await conn.execute(
                        sa_text(
                            f'CREATE TRIGGER "{trigger_name}" '
                            f'BEFORE {operation_clause} ON "{table.name}" '
                            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
                        )
                    )
                    changed = True
                    continue
                if (
                    str(trigger_row["table_name"]) != table.name
                    or str(trigger_row["function_name"]) != function_name
                ):
                    raise RuntimeError(
                        "policy compliance PostgreSQL trigger is corrupt: "
                        + trigger_name
                    )
                observed_trigger_type = int(trigger_row["trigger_type"])
                if observed_trigger_type != expected_trigger_type:
                    if includes_insert and observed_trigger_type == 27:
                        await conn.execute(
                            sa_text(f'DROP TRIGGER "{trigger_name}" ON "{table.name}"')
                        )
                        await conn.execute(
                            sa_text(
                                f'CREATE TRIGGER "{trigger_name}" '
                                f'BEFORE {operation_clause} ON "{table.name}" '
                                f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
                            )
                        )
                        changed = True
                        continue
                    raise RuntimeError(
                        "policy compliance PostgreSQL trigger is corrupt: "
                        + trigger_name
                    )

            incomplete_receipts = int(
                (
                    await conn.execute(
                        sa_text(
                            f'SELECT COUNT(*) FROM "{receipt_table_name}" AS receipt '
                            "WHERE receipt.finding_count <> ("
                            "SELECT COUNT(*) "
                            'FROM "policy_compliance_findings" AS finding '
                            "WHERE finding.receipt_id = receipt.receipt_id"
                            ")"
                        )
                    )
                ).scalar_one()
            )
            if incomplete_receipts:
                raise RuntimeError(
                    "policy compliance migration found incomplete receipt "
                    f"aggregates: {incomplete_receipts}"
                )
            sealed_rows = await conn.execute(
                sa_text(
                    f'UPDATE "{receipt_table_name}" '
                    'SET "sealed" = TRUE WHERE "sealed" = FALSE'
                )
            )
            if int(sealed_rows.rowcount or 0):
                changed = True

    return None if changed else "skipped"


async def _migrate_policy_waiver_v1_schema() -> str | None:
    """Converge the retired policy/v1 waiver audit substrate.

    The legacy rows remain physically auditable and immutable so existing
    databases upgrade safely, but they are no longer reconstructed through the
    retired policy/v1 runtime.  The following semantic-governance migration
    records every legacy head as an ineffective waiver without granting it gate
    authority.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        PolicyComplianceFindingRow,
        PolicyComplianceReceiptRow,
        PolicyWaiverEventRow,
        PolicyWaiverRow,
    )

    engine = get_engine()
    changed = False
    tables = (
        PolicyWaiverRow.__table__,
        PolicyWaiverEventRow.__table__,
    )

    def _table_names(sync_conn: object) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "policy waiver migration supports only SQLite and PostgreSQL"
            )
        if dialect == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        table_names = await conn.run_sync(_table_names)
        for required in (
            PolicyComplianceReceiptRow.__tablename__,
            PolicyComplianceFindingRow.__tablename__,
        ):
            if required not in table_names:
                raise RuntimeError(
                    "policy waiver migration missing B07 authority: " + required
                )
        for table in tables:
            if table.name not in table_names:
                await conn.run_sync(
                    lambda sync_conn, owned=table: owned.create(
                        sync_conn,
                        checkfirst=True,
                    )
                )
                table_names.add(table.name)
                changed = True
            if dialect == "sqlite":
                contract = await conn.run_sync(
                    lambda sync_conn, owned=table: _sqlite_owned_table_contract(
                        sync_conn,
                        owned,
                    )
                )
                if contract["observed"] != contract["expected"]:
                    raise RuntimeError(
                        "policy waiver table has a non-canonical contract: "
                        + table.name
                    )

        inconsistent_heads = int(
            (
                await conn.execute(
                    sa_text(
                        """
SELECT COUNT(*)
FROM policy_waivers AS head
LEFT JOIN policy_waiver_events AS event
  ON event.event_id = head.last_event_id
 AND event.waiver_id = head.waiver_id
 AND event.board_id = head.board_id
WHERE event.event_id IS NULL
   OR event.waiver_revision <> head.waiver_revision
   OR event.event_type <> head.last_event_type
   OR event.to_status <> head.status
   OR event.occurred_at <> head.last_event_at
   OR event.expires_at <> head.expires_at
   OR event.scope_digest <> head.scope_digest
   OR event.waiver_digest <> head.head_digest
   OR head.waiver_revision <> (
       SELECT COUNT(*)
       FROM policy_waiver_events AS lineage
       WHERE lineage.waiver_id = head.waiver_id
         AND lineage.board_id = head.board_id
   )
"""
                    )
                )
            ).scalar_one()
        )
        if inconsistent_heads:
            raise RuntimeError(
                "policy waiver migration found inconsistent head/event "
                f"lineages: {inconsistent_heads}"
            )

        if dialect == "sqlite":
            predecessor_rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {"prefix": (f"{POLICY_WAIVER_PREDECESSOR_TRIGGER_PREFIX}%")},
                    )
                )
                .mappings()
                .all()
            )
            allowed_predecessors = {
                (f"{POLICY_WAIVER_PREDECESSOR_TRIGGER_PREFIX}_{suffix}")
                for suffix in (
                    "head_insert",
                    "head_update",
                    "head_delete",
                    "event_insert",
                    "event_update",
                    "event_delete",
                )
            }
            unexpected_predecessors = {
                str(row["name"]) for row in predecessor_rows
            } - allowed_predecessors
            if unexpected_predecessors:
                raise RuntimeError(
                    "policy waiver has unexpected predecessor triggers: "
                    + ", ".join(sorted(unexpected_predecessors))
                )
            for row in predecessor_rows:
                trigger_name = str(row["name"])
                await conn.execute(sa_text(f'DROP TRIGGER "{trigger_name}"'))
                changed = True
            expected = policy_waiver_immutability_trigger_manifest()
            rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {"prefix": f"{POLICY_WAIVER_TRIGGER_PREFIX}%"},
                    )
                )
                .mappings()
                .all()
            )
            existing = {str(row["name"]): row for row in rows}
            unexpected = set(existing) - set(expected)
            if unexpected:
                raise RuntimeError(
                    "policy waiver has unexpected owned triggers: "
                    + ", ".join(sorted(unexpected))
                )
            for trigger_name, (table_name, trigger_sql) in expected.items():
                row = existing.get(trigger_name)
                if row is None:
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
                if (
                    str(row["tbl_name"]) != table_name
                    or normalize_global_discovery_source_revision_trigger_sql(
                        row["sql"]
                    )
                    != normalize_global_discovery_source_revision_trigger_sql(
                        trigger_sql
                    )
                ):
                    raise RuntimeError(
                        "policy waiver immutable trigger is corrupt: " + trigger_name
                    )
            violations = list(
                (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            )
            if violations:
                raise RuntimeError(
                    "policy waiver migration left foreign-key violations: "
                    + repr(violations[:10])
                )
        else:
            ddl = policy_waiver_postgresql_immutability_ddl()
            predecessor_names = (
                f"{POLICY_WAIVER_PREDECESSOR_TRIGGER_PREFIX}_head",
                f"{POLICY_WAIVER_PREDECESSOR_TRIGGER_PREFIX}_event",
            )
            predecessor_rows = (
                (
                    await conn.execute(
                        sa_text(
                            """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
WHERE NOT trigger.tgisinternal
  AND trigger.tgname LIKE :prefix
"""
                        ),
                        {"prefix": (f"{POLICY_WAIVER_PREDECESSOR_TRIGGER_PREFIX}%")},
                    )
                )
                .mappings()
                .all()
            )
            allowed_predecessors = {
                predecessor_names[0]: PolicyWaiverRow.__tablename__,
                predecessor_names[1]: PolicyWaiverEventRow.__tablename__,
            }
            for row in predecessor_rows:
                trigger_name = str(row["trigger_name"])
                table_name = str(row["table_name"])
                if allowed_predecessors.get(trigger_name) != table_name:
                    raise RuntimeError(
                        "policy waiver PostgreSQL predecessor trigger is "
                        "corrupt: " + trigger_name
                    )
                await conn.execute(
                    sa_text(f'DROP TRIGGER "{trigger_name}" ON "{table_name}"')
                )
                changed = True
            if predecessor_rows:
                await conn.execute(
                    sa_text("DROP FUNCTION IF EXISTS policy_waiver_guard_v1()")
                )
            await conn.execute(sa_text(ddl[0]))
            expected = {
                f"{POLICY_WAIVER_TRIGGER_PREFIX}_head": (
                    PolicyWaiverRow.__tablename__,
                    31,
                ),
                f"{POLICY_WAIVER_TRIGGER_PREFIX}_event": (
                    PolicyWaiverEventRow.__tablename__,
                    31,
                ),
            }
            if any(len(name) > 63 for name in expected):
                raise RuntimeError(
                    "policy waiver PostgreSQL trigger name exceeds 63 bytes"
                )
            rows = (
                (
                    await conn.execute(
                        sa_text(
                            """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       function.proname AS function_name,
       trigger.tgtype AS trigger_type
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
WHERE NOT trigger.tgisinternal
  AND trigger.tgname LIKE :prefix
"""
                        ),
                        {"prefix": f"{POLICY_WAIVER_TRIGGER_PREFIX}%"},
                    )
                )
                .mappings()
                .all()
            )
            existing = {str(row["trigger_name"]): row for row in rows}
            unexpected = set(existing) - set(expected)
            if unexpected:
                raise RuntimeError(
                    "policy waiver has unexpected PostgreSQL triggers: "
                    + ", ".join(sorted(unexpected))
                )
            for index, (trigger_name, (table_name, trigger_type)) in enumerate(
                expected.items(),
                start=1,
            ):
                row = existing.get(trigger_name)
                if row is None:
                    await conn.execute(sa_text(ddl[index]))
                    changed = True
                    continue
                if (
                    str(row["table_name"]) != table_name
                    or str(row["function_name"]) != "policy_waiver_guard_v2"
                    or int(row["trigger_type"]) != trigger_type
                ):
                    raise RuntimeError(
                        "policy waiver PostgreSQL trigger is corrupt: " + trigger_name
                    )

    return None if changed else "skipped"


def semantic_guideline_owned_tables() -> tuple[object, ...]:
    """Return the single ORM-owned manifest for all semantic v2 tables."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineAssessmentReceiptRow,
        SemanticGuidelineBindingConfigurationRow,
        SemanticGuidelineFindingRow,
        SemanticGuidelineLegacyMigrationRow,
        SemanticGuidelineMetricResultRow,
        SemanticGuidelineRevisionRow,
        SemanticGuidelineSkipRow,
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
        SemanticSubjectVersionEventRow,
        SemanticSubjectVersionRow,
    )

    return (
        SemanticGuidelineRevisionRow.__table__,
        SemanticGuidelineBindingConfigurationRow.__table__,
        SemanticSubjectVersionEventRow.__table__,
        SemanticSubjectVersionRow.__table__,
        SemanticGuidelineAssessmentReceiptRow.__table__,
        SemanticGuidelineMetricResultRow.__table__,
        SemanticGuidelineFindingRow.__table__,
        SemanticGuidelineWaiverRow.__table__,
        SemanticGuidelineWaiverEventRow.__table__,
        SemanticGuidelineSkipRow.__table__,
        SemanticGuidelineLegacyMigrationRow.__table__,
    )


def _postgresql_catalog_char(value: object) -> str:
    """Normalize PostgreSQL's internal ``\"char\"`` across DBAPI drivers."""

    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)


def audit_semantic_guideline_postgresql_trigger_rows(
    rows: list[dict[str, object]],
    *,
    expected_schema: str,
    trigger_specs: dict[str, tuple[str, str, int]] | None = None,
) -> tuple[str, ...]:
    """Audit exact non-internal PostgreSQL trigger identity and enabled state.

    Missing triggers are returned so the migration can install them. Any
    unexpected, disabled, misbound, or wrong-operation trigger is corruption
    and fails closed.
    """

    if not isinstance(expected_schema, str) or not expected_schema.strip():
        raise RuntimeError(
            "semantic guideline PostgreSQL trigger schema is invalid"
        )
    expected = trigger_specs
    if expected is None:
        _function_sql, expected = semantic_guideline_postgresql_ddl()
    existing = {
        str(row["trigger_name"]): row
        for row in rows
    }
    if len(existing) != len(rows):
        seen: set[str] = set()
        duplicates: set[str] = set()
        for row in rows:
            trigger_name = str(row["trigger_name"])
            if trigger_name in seen:
                duplicates.add(trigger_name)
            seen.add(trigger_name)
        raise RuntimeError(
            "semantic guideline has duplicate PostgreSQL trigger names: "
            + ", ".join(sorted(duplicates))
        )
    unexpected = set(existing) - set(expected)
    if unexpected:
        raise RuntimeError(
            "semantic guideline has unexpected PostgreSQL triggers: "
            + ", ".join(sorted(unexpected))
        )
    missing: list[str] = []
    for trigger_name, (
        table_name,
        _operation_clause,
        expected_type,
    ) in expected.items():
        observed = existing.get(trigger_name)
        if observed is None:
            missing.append(trigger_name)
            continue
        if (
            str(observed["table_name"]) != table_name
            or str(observed["table_schema"]) != expected_schema
            or str(observed["function_name"]) != "semantic_guideline_guard_v3"
            or str(observed["function_schema"]) != expected_schema
            or int(observed["trigger_type"]) != expected_type
            or _postgresql_catalog_char(observed["trigger_enabled"]) != "O"
            or bool(observed["has_when_clause"])
            or int(observed["argument_count"]) != 0
            or str(observed["update_columns"]).strip()
        ):
            raise RuntimeError(
                "semantic guideline PostgreSQL trigger is corrupt: "
                + trigger_name
            )
    return tuple(missing)


async def _backfill_semantic_guideline_waiver_digests(conn: object) -> bool:
    """Backfill the permanent assessor fence and every derived waiver digest."""

    from sqlalchemy import select as sa_select

    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineAssessmentReceiptRow,
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
    )
    from okto_pulse.core.domain.guideline_policy import (
        PolicyEntityType,
        PolicySubjectRef,
    )
    from okto_pulse.core.domain.guideline_semantic_currentness import (
        SemanticAssessmentCurrentnessReason,
    )
    from okto_pulse.core.domain.guideline_semantic_exceptions import (
        SemanticMetricWaiverAnchor,
        SemanticMetricWaiverEventType,
        SemanticMetricWaiverExpireReason,
        SemanticMetricWaiverRevalidationReason,
        SemanticMetricWaiverRevalidationStatus,
        request_semantic_metric_waiver,
        revalidate_semantic_metric_waiver,
        transition_semantic_metric_waiver,
    )
    from okto_pulse.core.domain.quality_assessment import EvidenceRef

    waiver_table = SemanticGuidelineWaiverRow.__table__
    event_table = SemanticGuidelineWaiverEventRow.__table__
    receipt_table = SemanticGuidelineAssessmentReceiptRow.__table__
    heads = (
        await conn.execute(
            sa_select(waiver_table).order_by(waiver_table.c.waiver_id)
        )
    ).mappings().all()
    if not heads:
        return False

    for head in heads:
        assessor_id = (
            await conn.execute(
                sa_select(receipt_table.c.assessor_agent_id).where(
                    receipt_table.c.receipt_id == head["receipt_id"],
                    receipt_table.c.board_id == head["board_id"],
                    receipt_table.c.receipt_digest
                    == head["receipt_digest"],
                    receipt_table.c.sealed.is_(True),
                )
            )
        ).scalar_one_or_none()
        if assessor_id is None:
            raise RuntimeError(
                "semantic waiver assessor backfill cannot resolve its "
                f"sealed receipt: {head['waiver_id']}"
            )
        anchor = SemanticMetricWaiverAnchor(
            metric_result_id=head["metric_result_id"],
            metric_result_digest=head["metric_result_digest"],
            finding_id=head["finding_id"],
            finding_digest=head["finding_digest"],
            receipt_id=head["receipt_id"],
            receipt_digest=head["receipt_digest"],
            subject=PolicySubjectRef(
                board_id=head["board_id"],
                entity_type=PolicyEntityType(head["subject_type"]),
                subject_id=head["subject_id"],
                subject_version=head["subject_version"],
            ),
            subject_content_digest=head["subject_content_digest"],
            guideline_id=head["guideline_id"],
            guideline_revision_id=head["revision_id"],
            guideline_revision_digest=head["revision_digest"],
            binding_id=head["binding_id"],
            binding_revision=head["binding_revision"],
            binding_configuration_digest=head["configuration_digest"],
            metric_id=head["metric_id"],
            metric_code=head["metric_code"],
            assessment_assessor_id=assessor_id,
        )
        events = (
            await conn.execute(
                sa_select(event_table)
                .where(event_table.c.waiver_id == head["waiver_id"])
                .order_by(event_table.c.waiver_revision)
            )
        ).mappings().all()
        if not events or events[0]["event_type"] != "request":
            raise RuntimeError(
                "semantic waiver assessor backfill found an invalid event "
                f"lineage: {head['waiver_id']}"
            )

        def evidence(raw: object) -> tuple[EvidenceRef, ...]:
            return tuple(EvidenceRef(**item) for item in (raw or ()))

        first = events[0]
        mutation = request_semantic_metric_waiver(
            waiver_id=head["waiver_id"],
            event_id=first["event_id"],
            anchor=anchor,
            justification=head["justification"],
            evidence_refs=evidence(first["evidence_refs"]),
            requested_by=head["requested_by"],
            requested_at=head["requested_at"],
            expires_at=head["original_expires_at"],
            idempotency_key=first["idempotency_key"],
        )
        rebuilt = [mutation]
        for raw in events[1:]:
            event_type = SemanticMetricWaiverEventType(raw["event_type"])
            if event_type is SemanticMetricWaiverEventType.REVALIDATE:
                if (
                    raw["evaluated_at"] is None
                    or raw["revalidation_status"] is None
                    or raw["revalidation_current"] is None
                    or raw["revalidation_reason_code"] is None
                ):
                    raise RuntimeError(
                        "legacy semantic waiver revalidation cannot be "
                        "silently upgraded; recreate the development waiver: "
                        f"{head['waiver_id']}"
                    )
                mutation = revalidate_semantic_metric_waiver(
                    mutation.waiver,
                    event_id=raw["event_id"],
                    expected_waiver_revision=(
                        mutation.waiver.waiver_revision
                    ),
                    actor_id=raw["actor_id"],
                    occurred_at=raw["occurred_at"],
                    evaluated_at=raw["evaluated_at"],
                    status=SemanticMetricWaiverRevalidationStatus(
                        raw["revalidation_status"]
                    ),
                    reason_code=SemanticMetricWaiverRevalidationReason(
                        raw["revalidation_reason_code"]
                    ),
                    currentness_reasons=tuple(
                        SemanticAssessmentCurrentnessReason(item)
                        for item in raw["currentness_reasons"]
                    ),
                    scheduled_expiry_observed=bool(
                        raw["scheduled_expiry_observed"]
                    ),
                    evidence_refs=evidence(raw["evidence_refs"]),
                    idempotency_key=raw["idempotency_key"],
                )
            else:
                transition_kwargs: dict[str, object] = {}
                if event_type is SemanticMetricWaiverEventType.APPROVE:
                    transition_kwargs["expires_at"] = raw["expires_at"]
                if event_type is SemanticMetricWaiverEventType.EXPIRE:
                    transition_kwargs["expire_reason"] = (
                        SemanticMetricWaiverExpireReason(
                            raw["expire_reason_code"]
                        )
                    )
                mutation = transition_semantic_metric_waiver(
                    mutation.waiver,
                    event_id=raw["event_id"],
                    expected_waiver_revision=(
                        mutation.waiver.waiver_revision
                    ),
                    event_type=event_type,
                    actor_id=raw["actor_id"],
                    occurred_at=raw["occurred_at"],
                    reason=raw["reason"],
                    evidence_refs=evidence(raw["evidence_refs"]),
                    idempotency_key=raw["idempotency_key"],
                    **transition_kwargs,
                )
            if (
                mutation.waiver.waiver_revision
                != raw["waiver_revision"]
                or mutation.waiver.status.value != raw["to_status"]
            ):
                raise RuntimeError(
                    "semantic waiver assessor backfill would rewrite its "
                    f"lifecycle: {head['waiver_id']}"
                )
            rebuilt.append(mutation)

        anchor_fields = {
            "metric_result_id": anchor.metric_result_id,
            "metric_result_digest": anchor.metric_result_digest,
            "finding_id": anchor.finding_id,
            "finding_digest": anchor.finding_digest,
            "receipt_id": anchor.receipt_id,
            "receipt_digest": anchor.receipt_digest,
            "board_id": anchor.subject.board_id,
            "subject_type": anchor.subject.entity_type.value,
            "subject_id": anchor.subject.subject_id,
            "subject_version": anchor.subject.subject_version,
            "subject_content_digest": anchor.subject_content_digest,
            "guideline_id": anchor.guideline_id,
            "revision_id": anchor.guideline_revision_id,
            "revision_digest": anchor.guideline_revision_digest,
            "binding_id": anchor.binding_id,
            "binding_revision": anchor.binding_revision,
            "configuration_digest": (
                anchor.binding_configuration_digest
            ),
            "metric_id": anchor.metric_id,
            "metric_code": anchor.metric_code,
        }
        final = rebuilt[-1].waiver
        expected_head = {
            **anchor_fields,
            "waiver_id": final.waiver_id,
            "justification": final.justification,
            "requested_by": final.requested_by,
            "requested_at": final.requested_at,
            "original_expires_at": final.original_expires_at,
            "status": final.status.value,
            "waiver_revision": final.waiver_revision,
            "expires_at": final.expires_at,
            "last_event_id": final.last_event_id,
            "last_event_type": final.last_event_type.value,
            "last_event_at": final.last_event_at,
            "reviewed_by": final.reviewed_by,
            "reviewed_at": final.reviewed_at,
            "review_reason": final.review_reason,
            "revoked_by": final.revoked_by,
            "revoked_at": final.revoked_at,
            "expire_reason_code": (
                final.expire_reason.value
                if final.expire_reason is not None
                else None
            ),
            "idempotency_key": rebuilt[0].event.idempotency_key,
        }
        for field_name, expected_value in expected_head.items():
            if head[field_name] != expected_value:
                raise RuntimeError(
                    "semantic waiver assessor backfill found corrupted "
                    f"head field {field_name}: {head['waiver_id']}"
                )
        if evidence(head["evidence_refs"]) != final.evidence_refs:
            raise RuntimeError(
                "semantic waiver assessor backfill found corrupted head "
                f"evidence: {head['waiver_id']}"
            )

        for raw, item in zip(events, rebuilt, strict=True):
            event = item.event
            waiver = item.waiver
            expected_event = {
                "event_id": event.event_id,
                "predecessor_event_id": event.predecessor_event_id,
                "waiver_id": event.waiver_id,
                "board_id": waiver.anchor.subject.board_id,
                "waiver_revision": event.waiver_revision,
                "event_type": event.event_type.value,
                "from_status": (
                    event.from_status.value
                    if event.from_status is not None
                    else None
                ),
                "to_status": event.to_status.value,
                "actor_id": event.actor_id,
                "occurred_at": event.occurred_at,
                "reason": event.reason,
                "expires_at": event.expires_at,
                "reviewed_by": waiver.reviewed_by,
                "reviewed_at": waiver.reviewed_at,
                "review_reason": waiver.review_reason,
                "revoked_by": waiver.revoked_by,
                "revoked_at": waiver.revoked_at,
                "expire_reason_code": (
                    event.expire_reason.value
                    if event.expire_reason is not None
                    else None
                ),
                "evaluated_at": event.evaluated_at,
                "revalidation_status": (
                    event.revalidation_status.value
                    if event.revalidation_status is not None
                    else None
                ),
                "revalidation_current": event.revalidation_current,
                "revalidation_reason_code": (
                    event.revalidation_reason_code.value
                    if event.revalidation_reason_code is not None
                    else None
                ),
                "currentness_reasons": [
                    reason.value
                    for reason in event.currentness_reasons
                ],
                "scheduled_expiry_observed": (
                    event.scheduled_expiry_observed
                ),
                "idempotency_key": event.idempotency_key,
            }
            for field_name, expected_value in expected_event.items():
                if raw[field_name] != expected_value:
                    raise RuntimeError(
                        "semantic waiver assessor backfill found corrupted "
                        f"event field {field_name}: {event.event_id}"
                    )
            if evidence(raw["evidence_refs"]) != event.evidence_refs:
                raise RuntimeError(
                    "semantic waiver assessor backfill found corrupted "
                    f"event evidence: {event.event_id}"
                )
            await conn.execute(
                event_table.update()
                .where(event_table.c.event_id == raw["event_id"])
                .values(
                    reason=event.reason,
                    expires_at=event.expires_at,
                    scope_digest=event.scope_digest,
                    waiver_digest=event.waiver_digest,
                    reviewed_by=waiver.reviewed_by,
                    reviewed_at=waiver.reviewed_at,
                    review_reason=waiver.review_reason,
                    revoked_by=waiver.revoked_by,
                    revoked_at=waiver.revoked_at,
                    expire_reason_code=(
                        event.expire_reason.value
                        if event.expire_reason is not None
                        else None
                    ),
                    evaluated_at=event.evaluated_at,
                    revalidation_status=(
                        event.revalidation_status.value
                        if event.revalidation_status is not None
                        else None
                    ),
                    revalidation_current=event.revalidation_current,
                    revalidation_reason_code=(
                        event.revalidation_reason_code.value
                        if event.revalidation_reason_code is not None
                        else None
                    ),
                    currentness_reasons=[
                        reason.value
                        for reason in event.currentness_reasons
                    ],
                    scheduled_expiry_observed=(
                        event.scheduled_expiry_observed
                    ),
                    request_digest=event.request_digest,
                )
            )

        await conn.execute(
            waiver_table.update()
            .where(waiver_table.c.waiver_id == head["waiver_id"])
            .values(
                assessment_assessor_id=assessor_id,
                scope_digest=final.scope_digest,
                status=final.status.value,
                waiver_revision=final.waiver_revision,
                expires_at=final.expires_at,
                last_event_id=final.last_event_id,
                last_event_type=final.last_event_type.value,
                last_event_at=final.last_event_at,
                last_event_idempotency_key=(
                    final.last_event_idempotency_key
                ),
                reviewed_by=final.reviewed_by,
                reviewed_at=final.reviewed_at,
                review_reason=final.review_reason,
                revoked_by=final.revoked_by,
                revoked_at=final.revoked_at,
                expire_reason_code=(
                    final.expire_reason.value
                    if final.expire_reason is not None
                    else None
                ),
                last_revalidation_status=(
                    final.last_revalidation_status.value
                    if final.last_revalidation_status is not None
                    else None
                ),
                last_revalidation_current=(
                    final.last_revalidation_current
                ),
                last_revalidation_reason_code=(
                    final.last_revalidation_reason_code.value
                    if final.last_revalidation_reason_code is not None
                    else None
                ),
                last_revalidation_evaluated_at=(
                    final.last_revalidation_evaluated_at
                ),
                last_revalidation_currentness_reasons=[
                    reason.value
                    for reason in (
                        final.last_revalidation_currentness_reasons
                    )
                ],
                last_revalidation_scheduled_expiry_observed=(
                    final
                    .last_revalidation_scheduled_expiry_observed
                ),
                head_digest=final.head_digest,
                request_digest=rebuilt[0].event.request_digest,
            )
        )
    return True


def _semantic_waiver_upgrade_column_definitions(
    dialect: object,
) -> dict[str, dict[str, str]]:
    """Return append-only predecessor columns using canonical ORM SQL types."""

    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
    )

    waiver_table = SemanticGuidelineWaiverRow.__table__
    event_table = SemanticGuidelineWaiverEventRow.__table__

    def sql_type(table: object, column_name: str) -> str:
        column = table.c[column_name]
        return str(column.type.compile(dialect=dialect))

    return {
        waiver_table.name: {
            "assessment_assessor_id": sql_type(
                waiver_table,
                "assessment_assessor_id",
            ),
            "last_event_idempotency_key": sql_type(
                waiver_table,
                "last_event_idempotency_key",
            ),
            "last_revalidation_status": sql_type(
                waiver_table,
                "last_revalidation_status",
            ),
            "last_revalidation_current": sql_type(
                waiver_table,
                "last_revalidation_current",
            ),
            "last_revalidation_reason_code": sql_type(
                waiver_table,
                "last_revalidation_reason_code",
            ),
            "last_revalidation_evaluated_at": sql_type(
                waiver_table,
                "last_revalidation_evaluated_at",
            ),
            "last_revalidation_currentness_reasons": (
                f"{sql_type(waiver_table, 'last_revalidation_currentness_reasons')} "
                "NOT NULL DEFAULT '[]'"
            ),
            "last_revalidation_scheduled_expiry_observed": (
                f"{sql_type(waiver_table, 'last_revalidation_scheduled_expiry_observed')} "
                "NOT NULL DEFAULT false"
            ),
        },
        event_table.name: {
            "evaluated_at": sql_type(event_table, "evaluated_at"),
            "revalidation_status": sql_type(
                event_table,
                "revalidation_status",
            ),
            "revalidation_current": sql_type(
                event_table,
                "revalidation_current",
            ),
            "revalidation_reason_code": sql_type(
                event_table,
                "revalidation_reason_code",
            ),
            "currentness_reasons": (
                f"{sql_type(event_table, 'currentness_reasons')} "
                "NOT NULL DEFAULT '[]'"
            ),
            "scheduled_expiry_observed": (
                f"{sql_type(event_table, 'scheduled_expiry_observed')} "
                "NOT NULL DEFAULT false"
            ),
        },
    }


async def _converge_semantic_guideline_waiver_contract() -> bool:
    """Upgrade the unreleased semantic waiver schema without dropping evidence."""

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import AddConstraint

    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
    )

    engine = get_engine()
    waiver_table = SemanticGuidelineWaiverRow.__table__
    event_table = SemanticGuidelineWaiverEventRow.__table__
    changed = False

    predecessor_waiver_columns = {
        "assessment_assessor_id",
        "last_event_idempotency_key",
        "last_revalidation_status",
        "last_revalidation_current",
        "last_revalidation_reason_code",
        "last_revalidation_evaluated_at",
        "last_revalidation_currentness_reasons",
        "last_revalidation_scheduled_expiry_observed",
    }
    predecessor_event_columns = {
        "evaluated_at",
        "revalidation_status",
        "revalidation_current",
        "revalidation_reason_code",
        "currentness_reasons",
        "scheduled_expiry_observed",
    }
    new_waiver_checks = {
        "ck_sg_waiver_revalidation_shape",
        "ck_sg_waiver_revalidation_decision",
    }
    new_event_checks = {
        "ck_sg_waiver_event_revalidation_shape",
        "ck_sg_waiver_event_revalidation_decision",
    }
    predecessor_event_checks = {
        "ck_sg_waiver_event_transition": (
            "(event_type = 'request' AND from_status IS NULL "
            "AND to_status = 'requested' AND waiver_revision = 1) "
            "OR (event_type = 'approve' AND from_status = 'requested' "
            "AND to_status = 'approved' AND waiver_revision > 1) "
            "OR (event_type = 'reject' AND from_status = 'requested' "
            "AND to_status = 'rejected' AND waiver_revision > 1) "
            "OR (event_type = 'revoke' AND from_status = 'approved' "
            "AND to_status = 'revoked' AND waiver_revision > 1) "
            "OR (event_type = 'expire' AND from_status = 'approved' "
            "AND to_status = 'expired' AND waiver_revision > 1) "
            "OR (event_type = 'revalidate' "
            "AND from_status IN ('approved', 'expired') "
            "AND to_status = 'approved' AND waiver_revision > 1)"
        ),
        "ck_sg_waiver_event_expire": (
            "(event_type = 'expire' AND expire_reason_code IN "
            "('scheduled_expiry', 'subject_scope_changed', "
            "'guideline_revision_changed', "
            "'binding_configuration_changed', 'metric_result_changed')) "
            "OR (event_type <> 'expire' AND expire_reason_code IS NULL)"
        ),
    }

    def _sql_tokens(value: object) -> tuple[str, ...]:
        return tuple(
            token.lower()
            for token in re.findall(
                r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z0-9_]*|"
                r"<>|<=|>=|=|<|>|\d+",
                str(value),
            )
        )

    def predecessor_contract(
        contract: dict[str, dict[str, object]],
        *,
        table_kind: str,
        dialect: str,
    ) -> dict[str, object]:
        expected = dict(contract["expected"])
        removed_columns = (
            predecessor_waiver_columns
            if table_kind == "waiver"
            else predecessor_event_columns
        )
        expected["columns"] = tuple(
            column
            for column in expected["columns"]
            if column[0] not in removed_columns
        )
        removed_checks = (
            new_waiver_checks
            if table_kind == "waiver"
            else new_event_checks
        )
        checks = {
            name: expression
            for name, expression in expected["checks"]
            if name not in removed_checks
        }
        if table_kind == "event":
            for name, expression in predecessor_event_checks.items():
                checks[name] = (
                    _normalize_sqlite_contract_ddl(expression)
                    if dialect == "sqlite"
                    else _sql_tokens(expression)
                )
        expected["checks"] = tuple(sorted(checks.items(), key=repr))
        return expected

    def is_predecessor(
        contract: dict[str, dict[str, object]],
        *,
        table_kind: str,
        dialect: str,
    ) -> bool:
        observed = dict(contract["observed"])
        expected = predecessor_contract(
            contract,
            table_kind=table_kind,
            dialect=dialect,
        )
        if dialect == "postgresql" and table_kind == "event":
            observed_checks = {
                name: expression
                for name, expression in observed["checks"]
            }
            expected_checks = dict(expected["checks"])
            for name in predecessor_event_checks:
                expression = observed_checks.get(name)
                if expression is None or _sql_tokens(expression) != (
                    expected_checks[name]
                ):
                    return False
                observed_checks[name] = expected_checks[name]
            observed["checks"] = tuple(
                sorted(observed_checks.items(), key=repr)
            )
        return observed == expected

    def classify_contracts(sync_conn: object) -> str:
        inspector = sa_inspect(sync_conn)
        names = set(inspector.get_table_names())
        if waiver_table.name not in names or event_table.name not in names:
            return "missing"
        waiver_contract = (
            _sqlite_owned_table_contract(sync_conn, waiver_table)
            if sync_conn.dialect.name == "sqlite"
            else _postgresql_owned_table_contract(sync_conn, waiver_table)
        )
        event_contract = (
            _sqlite_owned_table_contract(sync_conn, event_table)
            if sync_conn.dialect.name == "sqlite"
            else _postgresql_owned_table_contract(sync_conn, event_table)
        )
        waiver_current = (
            waiver_contract["observed"] == waiver_contract["expected"]
        )
        event_current = (
            event_contract["observed"] == event_contract["expected"]
        )
        if waiver_current and event_current:
            return "current"
        dialect = sync_conn.dialect.name
        if is_predecessor(
            waiver_contract,
            table_kind="waiver",
            dialect=dialect,
        ) and is_predecessor(
            event_contract,
            table_kind="event",
            dialect=dialect,
        ):
            return "predecessor"
        raise RuntimeError(
            "semantic waiver convergence found unrecognized schema drift"
        )

    async with engine.connect() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "semantic waiver convergence supports only SQLite and "
                "PostgreSQL"
            )
        contract_state = await conn.run_sync(classify_contracts)
        if contract_state in {"missing", "current"}:
            return False
        if dialect == "sqlite":
            observed_triggers = set(
                (
                    await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND name LIKE "
                        f"'{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}%'"
                    )
                ).scalars()
            )
            current_trigger_names = set(
                semantic_guideline_sqlite_trigger_manifest()
            )
            predecessor_trigger_names = (
                current_trigger_names
                - {
                    f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}"
                    "_waiver_scope_insert"
                }
            ) | {
                f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}"
                "_waiver_revalidate_scope"
            }
            if observed_triggers not in (
                set(),
                predecessor_trigger_names,
            ):
                raise RuntimeError(
                    "semantic waiver convergence found unrecognized "
                    "SQLite trigger drift"
                )
        else:
            _function_sql, trigger_specs = (
                semantic_guideline_postgresql_ddl()
            )
            expected_schema = str(
                (
                    await conn.exec_driver_sql(
                        "SELECT current_schema()"
                    )
                ).scalar_one()
            )
            trigger_rows = list(
                (
                    await conn.exec_driver_sql(
                        """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       relation_namespace.nspname AS table_schema,
       function.proname AS function_name,
       function_namespace.nspname AS function_schema,
       trigger.tgtype AS trigger_type,
       trigger.tgenabled AS trigger_enabled,
       trigger.tgqual IS NOT NULL AS has_when_clause,
       trigger.tgnargs AS argument_count,
       trigger.tgattr::text AS update_columns
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_namespace AS relation_namespace
  ON relation_namespace.oid = relation.relnamespace
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
JOIN pg_namespace AS function_namespace
  ON function_namespace.oid = function.pronamespace
WHERE NOT trigger.tgisinternal
  AND relation_namespace.nspname = current_schema()
  AND trigger.tgname LIKE 'trg_sgv3_%'
"""
                    )
                )
                .mappings()
                .all()
            )
            audit_semantic_guideline_postgresql_trigger_rows(
                trigger_rows,
                expected_schema=expected_schema,
                trigger_specs=trigger_specs,
            )
        await conn.rollback()
        if dialect == "sqlite":
            original_foreign_keys = int(
                (
                    await conn.exec_driver_sql("PRAGMA foreign_keys")
                ).scalar_one()
            )
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            await conn.begin()
        try:
            if dialect == "sqlite":
                trigger_names = (
                    await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND name LIKE "
                        f"'{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}%'"
                    )
                ).scalars().all()
                for trigger_name in trigger_names:
                    await conn.exec_driver_sql(
                        f'DROP TRIGGER "{trigger_name}"'
                    )
            else:
                _function_sql, trigger_specs = (
                    semantic_guideline_postgresql_ddl()
                )
                for trigger_name, (table_name, _ops, _type) in (
                    trigger_specs.items()
                ):
                    await conn.exec_driver_sql(
                        f'DROP TRIGGER IF EXISTS "{trigger_name}" '
                        f'ON "{table_name}"'
                    )

            def column_names(sync_conn: object, table_name: str) -> set[str]:
                return {
                    str(column["name"])
                    for column in sa_inspect(sync_conn).get_columns(
                        table_name
                    )
                }

            waiver_columns = await conn.run_sync(
                lambda sync_conn: column_names(
                    sync_conn,
                    waiver_table.name,
                )
            )
            event_columns = await conn.run_sync(
                lambda sync_conn: column_names(
                    sync_conn,
                    event_table.name,
                )
            )
            definitions = _semantic_waiver_upgrade_column_definitions(
                conn.dialect
            )
            observed_by_table = {
                waiver_table.name: waiver_columns,
                event_table.name: event_columns,
            }
            for table_name, columns in definitions.items():
                for column_name, definition in columns.items():
                    if column_name in observed_by_table[table_name]:
                        continue
                    await conn.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {definition}'
                    )
                    changed = True

            changed = (
                await _backfill_semantic_guideline_waiver_digests(conn)
                or changed
            )

            if dialect == "sqlite":
                def rebuild_tables(sync_conn: object) -> None:
                    waiver_backup_name = (
                        f"{waiver_table.name}__skb3_contract_upgrade"
                    )
                    event_backup_name = (
                        f"{event_table.name}__skb3_contract_upgrade"
                    )
                    inspector = sa_inspect(sync_conn)
                    existing = set(inspector.get_table_names())
                    if {
                        waiver_backup_name,
                        event_backup_name,
                    } & existing:
                        raise RuntimeError(
                            "semantic waiver convergence found stale "
                            "temporary tables"
                        )
                    for table in (waiver_table, event_table):
                        for index in inspector.get_indexes(table.name):
                            index_name = str(index.get("name") or "")
                            if (
                                index_name
                                and not index_name.startswith(
                                    "sqlite_autoindex_"
                                )
                            ):
                                sync_conn.exec_driver_sql(
                                    f'DROP INDEX "{index_name}"'
                                )
                    sync_conn.exec_driver_sql(
                        f'ALTER TABLE "{event_table.name}" RENAME TO '
                        f'"{event_backup_name}"'
                    )
                    sync_conn.exec_driver_sql(
                        f'ALTER TABLE "{waiver_table.name}" RENAME TO '
                        f'"{waiver_backup_name}"'
                    )
                    waiver_table.create(sync_conn, checkfirst=False)
                    event_table.create(sync_conn, checkfirst=False)
                    for table, backup_name in (
                        (waiver_table, waiver_backup_name),
                        (event_table, event_backup_name),
                    ):
                        columns = ", ".join(
                            f'"{column.name}"'
                            for column in table.columns
                        )
                        sync_conn.exec_driver_sql(
                            f'INSERT INTO "{table.name}" ({columns}) '
                            f'SELECT {columns} FROM "{backup_name}"'
                        )
                    sync_conn.exec_driver_sql(
                        f'DROP TABLE "{event_backup_name}"'
                    )
                    sync_conn.exec_driver_sql(
                        f'DROP TABLE "{waiver_backup_name}"'
                    )

                await conn.run_sync(rebuild_tables)
                changed = True
            else:
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{waiver_table.name}" '
                    'ALTER COLUMN "assessment_assessor_id" SET NOT NULL'
                )
                await conn.exec_driver_sql(
                    f'ALTER TABLE "{waiver_table.name}" '
                    'ALTER COLUMN "last_event_idempotency_key" SET NOT NULL'
                )
                changed_constraints = {
                    "ck_sg_waiver_revalidation_shape",
                    "ck_sg_waiver_revalidation_decision",
                    "ck_sg_waiver_event_transition",
                    "ck_sg_waiver_event_expire",
                    "ck_sg_waiver_event_revalidation_shape",
                    "ck_sg_waiver_event_revalidation_decision",
                }
                for table in (waiver_table, event_table):
                    constraints = {
                        str(item.name): item
                        for item in table.constraints
                        if item.name in changed_constraints
                    }
                    for name, constraint in constraints.items():
                        await conn.exec_driver_sql(
                            f'ALTER TABLE "{table.name}" '
                            f'DROP CONSTRAINT IF EXISTS "{name}"'
                        )
                        await conn.execute(AddConstraint(constraint))
                changed = True

            def audit_current_waiver_contracts(
                sync_conn: object,
            ) -> None:
                contract_reader = (
                    _sqlite_owned_table_contract
                    if sync_conn.dialect.name == "sqlite"
                    else _postgresql_owned_table_contract
                )
                for table in (waiver_table, event_table):
                    contract = contract_reader(sync_conn, table)
                    if contract["observed"] != contract["expected"]:
                        raise RuntimeError(
                            "semantic waiver convergence produced a "
                            "non-canonical table contract: " + table.name
                        )

            await conn.run_sync(audit_current_waiver_contracts)

            if dialect == "sqlite":
                manifest = semantic_guideline_sqlite_trigger_manifest()
                for _trigger_name, (
                    _table_name,
                    trigger_sql,
                ) in manifest.items():
                    await conn.exec_driver_sql(trigger_sql)
                rows = (
                    (
                        await conn.exec_driver_sql(
                            "SELECT name, tbl_name, sql "
                            "FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE "
                            f"'{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}%'"
                        )
                    )
                    .mappings()
                    .all()
                )
                observed = {
                    str(row["name"]): row
                    for row in rows
                }
                if set(observed) != set(manifest):
                    raise RuntimeError(
                        "semantic waiver convergence produced a "
                        "non-canonical SQLite trigger manifest"
                    )
                for trigger_name, (
                    table_name,
                    trigger_sql,
                ) in manifest.items():
                    row = observed[trigger_name]
                    if (
                        str(row["tbl_name"]) != table_name
                        or normalize_global_discovery_source_revision_trigger_sql(
                            row["sql"]
                        )
                        != normalize_global_discovery_source_revision_trigger_sql(
                            trigger_sql
                        )
                    ):
                        raise RuntimeError(
                            "semantic waiver convergence produced a corrupt "
                            "SQLite trigger: " + trigger_name
                        )
                violations = list(
                    (
                        await conn.exec_driver_sql(
                            "PRAGMA foreign_key_check"
                        )
                    ).all()
                )
                if violations:
                    raise RuntimeError(
                        "semantic waiver convergence left foreign-key "
                        "violations: " + repr(violations[:10])
                    )
            else:
                function_sql, trigger_specs = (
                    semantic_guideline_postgresql_ddl()
                )
                expected_schema = str(
                    (
                        await conn.exec_driver_sql(
                            "SELECT current_schema()"
                        )
                    ).scalar_one()
                )
                await conn.exec_driver_sql(function_sql)
                for trigger_name, (
                    table_name,
                    operation_clause,
                    _expected_type,
                ) in trigger_specs.items():
                    await conn.exec_driver_sql(
                        f'CREATE TRIGGER "{trigger_name}" '
                        f"BEFORE {operation_clause} "
                        f'ON "{table_name}" FOR EACH ROW '
                        "EXECUTE FUNCTION "
                        "semantic_guideline_guard_v3()"
                    )
                trigger_rows = list(
                    (
                        await conn.exec_driver_sql(
                            """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       relation_namespace.nspname AS table_schema,
       function.proname AS function_name,
       function_namespace.nspname AS function_schema,
       trigger.tgtype AS trigger_type,
       trigger.tgenabled AS trigger_enabled,
       trigger.tgqual IS NOT NULL AS has_when_clause,
       trigger.tgnargs AS argument_count,
       trigger.tgattr::text AS update_columns
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_namespace AS relation_namespace
  ON relation_namespace.oid = relation.relnamespace
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
JOIN pg_namespace AS function_namespace
  ON function_namespace.oid = function.pronamespace
WHERE NOT trigger.tgisinternal
  AND relation_namespace.nspname = current_schema()
  AND trigger.tgname LIKE 'trg_sgv3_%'
"""
                        )
                    )
                    .mappings()
                    .all()
                )
                missing = audit_semantic_guideline_postgresql_trigger_rows(
                    trigger_rows,
                    expected_schema=expected_schema,
                    trigger_specs=trigger_specs,
                )
                if missing:
                    raise RuntimeError(
                        "semantic waiver convergence failed to install "
                        "PostgreSQL triggers: " + ", ".join(missing)
                    )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            if dialect == "sqlite":
                await conn.exec_driver_sql(
                    f"PRAGMA foreign_keys={original_foreign_keys}"
                )
    return changed


async def _migrate_semantic_guideline_governance_schema() -> str | None:
    """Install semantic authority and retire unreleased policy/v1 evidence.

    Legacy predicates are never translated to metrics.  Every historical
    revision receives an explicit context-only or incompatible semantic
    authority row with ``metrics=[]`` and a newly computed v2 revision digest.
    Existing bindings, receipts and waivers remain physically auditable but
    receive only inert/stale migration records; none is copied into the new
    executable binding or assessment authority.
    """

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select as sa_select
    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        Guideline,
        GuidelineBoardBindingRow,
        GuidelineRevisionRow,
        PolicyComplianceReceiptRow,
        PolicyWaiverRow,
        SemanticGuidelineBindingConfigurationRow,
        SemanticGuidelineLegacyMigrationRow,
        SemanticGuidelineRevisionRow,
    )
    from okto_pulse.core.domain.guideline_policy import (
        GUIDELINE_REVISION_DIGEST_CONTRACT_VERSION,
        guideline_revision_digest_v2,
    )
    from okto_pulse.core.domain.quality_canonicalization import canonical_sha256

    engine = get_engine()
    changed = await _converge_semantic_guideline_waiver_contract()
    tables = semantic_guideline_owned_tables()

    def _table_names(sync_conn: object) -> set[str]:
        return set(sa_inspect(sync_conn).get_table_names())

    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise RuntimeError(
                "semantic guideline migration supports only SQLite and PostgreSQL"
            )
        if dialect == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")

        table_names = await conn.run_sync(_table_names)
        if GuidelineBoardBindingRow.__tablename__ in table_names:
            await conn.exec_driver_sql(
                'CREATE UNIQUE INDEX IF NOT EXISTS '
                '"uq_guideline_binding_exact_authority" ON '
                '"guideline_board_bindings" '
                '("binding_id", "binding_revision", "board_id", '
                '"guideline_id", "revision_id")'
            )
        for table in tables:
            if table.name not in table_names:
                await conn.run_sync(
                    lambda sync_conn, owned=table: owned.create(
                        sync_conn,
                        checkfirst=True,
                    )
                )
                table_names.add(table.name)
                changed = True
            contract = await conn.run_sync(
                (
                    lambda sync_conn, owned=table: (
                        _sqlite_owned_table_contract(sync_conn, owned)
                    )
                )
                if dialect == "sqlite"
                else (
                    lambda sync_conn, owned=table: (
                        _postgresql_owned_table_contract(sync_conn, owned)
                    )
                )
            )
            if contract["observed"] != contract["expected"]:
                raise RuntimeError(
                    "semantic guideline table has a non-canonical contract: "
                    + table.name
                )

        revision_source = (
            GuidelineRevisionRow.__table__.join(
                Guideline.__table__,
                Guideline.__table__.c.id
                == GuidelineRevisionRow.__table__.c.guideline_id,
            )
        )
        revision_rows = (
            (
                await conn.execute(
                    sa_select(
                        GuidelineRevisionRow.__table__,
                        Guideline.__table__.c.board_id.label(
                            "source_board_id"
                        ),
                    )
                    .select_from(revision_source)
                    .order_by(
                        GuidelineRevisionRow.__table__.c.guideline_id.asc(),
                        GuidelineRevisionRow.__table__.c.revision_number.asc(),
                        GuidelineRevisionRow.__table__.c.revision_id.asc(),
                    )
                )
            )
            .mappings()
            .all()
        )

        async def _insert_migration_audit(
            *,
            source_type: str,
            source_id: str,
            board_id: str | None,
            guideline_id: str | None,
            migration_state: str,
            source_digest: str,
            details: dict[str, object],
            migrated_at: object,
        ) -> bool:
            audit_table = SemanticGuidelineLegacyMigrationRow.__table__
            migration_id = canonical_sha256(
                {
                    "contract": "semantic-guideline-legacy-migration/v1",
                    "source_type": source_type,
                    "source_id": source_id,
                }
            )
            expected = {
                "migration_id": migration_id,
                "source_type": source_type,
                "source_id": source_id,
                "board_id": board_id,
                "guideline_id": guideline_id,
                "migration_state": migration_state,
                "source_digest": source_digest,
                "details": details,
                "migrated_at": migrated_at,
            }
            existing = (
                (
                    await conn.execute(
                        sa_select(audit_table).where(
                            audit_table.c.source_type == source_type,
                            audit_table.c.source_id == source_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                await conn.execute(audit_table.insert().values(**expected))
                return True
            for key, value in expected.items():
                observed = existing[key]
                if key == "details":
                    matches = canonical_sha256(observed) == canonical_sha256(value)
                else:
                    matches = observed == value
                if not matches:
                    raise RuntimeError(
                        "semantic guideline legacy migration audit conflict: "
                        f"{source_type}:{source_id}:{key}"
                    )
            return False

        semantic_revision_table = SemanticGuidelineRevisionRow.__table__
        for row in revision_rows:
            existing = (
                (
                    await conn.execute(
                        sa_select(semantic_revision_table).where(
                            semantic_revision_table.c.revision_id
                            == row["revision_id"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["guideline_id"] != row["guideline_id"]
                    or existing["source_revision_digest"]
                    != row["content_digest"]
                ):
                    raise RuntimeError(
                        "semantic guideline revision source fence conflict: "
                        + str(row["revision_id"])
                    )
                # Native revisions were written atomically by the semantic
                # adapter and are not legacy migration candidates.
                if existing["authority_state"] == "native":
                    continue
            else:
                rules = row["rules"]
                rules_are_empty = isinstance(rules, list) and not rules
                legacy_rules_payload = rules if isinstance(rules, list) else {
                    "invalid_legacy_rules_payload": repr(rules)
                }
                authority_state = (
                    "legacy_context_only"
                    if rules_are_empty
                    else "legacy_incompatible"
                )
                try:
                    revision_digest = guideline_revision_digest_v2(
                        semantic_version=row["semantic_version"],
                        title=row["title"],
                        content=row["content"],
                        metrics=(),
                        tags=tuple(row["tags"] or ()),
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "semantic guideline legacy revision cannot be "
                        "canonicalized: " + str(row["revision_id"])
                    ) from exc
                expected_revision = {
                    "revision_id": row["revision_id"],
                    "guideline_id": row["guideline_id"],
                    "contract_version": (
                        GUIDELINE_REVISION_DIGEST_CONTRACT_VERSION
                    ),
                    "metrics": [],
                    "revision_digest": revision_digest,
                    "source_revision_digest": row["content_digest"],
                    "authority_state": authority_state,
                    "legacy_rules_digest": canonical_sha256(
                        legacy_rules_payload
                    ),
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                await conn.execute(
                    semantic_revision_table.insert().values(
                        **expected_revision
                    )
                )
                existing = expected_revision
                changed = True

            migration_state = (
                "context_only"
                if existing["authority_state"] == "legacy_context_only"
                else "legacy_incompatible"
            )
            if await _insert_migration_audit(
                source_type="revision",
                source_id=str(row["revision_id"]),
                board_id=row["source_board_id"],
                guideline_id=row["guideline_id"],
                migration_state=migration_state,
                source_digest=row["content_digest"],
                details={
                    "semantic_revision_digest": existing["revision_digest"],
                    "legacy_rules_digest": existing["legacy_rules_digest"],
                    "metrics": [],
                    "executable": migration_state == "context_only",
                    "remediation": (
                        "author_semantic_metrics_and_create_a_new_revision"
                        if migration_state == "legacy_incompatible"
                        else "adopt_the_semantic_context_only_revision"
                    ),
                },
                migrated_at=row["created_at"],
            ):
                changed = True

        semantic_binding_table = (
            SemanticGuidelineBindingConfigurationRow.__table__
        )
        binding_rows = (
            (
                await conn.execute(
                    sa_select(GuidelineBoardBindingRow.__table__).order_by(
                        GuidelineBoardBindingRow.__table__.c.board_id.asc(),
                        GuidelineBoardBindingRow.__table__.c.guideline_id.asc(),
                        GuidelineBoardBindingRow.__table__.c.binding_revision.asc(),
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in binding_rows:
            semantic_binding_exists = (
                await conn.execute(
                    sa_select(semantic_binding_table.c.binding_id).where(
                        semantic_binding_table.c.binding_id == row["binding_id"],
                        semantic_binding_table.c.binding_revision
                        == row["binding_revision"],
                    )
                )
            ).first()
            if semantic_binding_exists is not None:
                continue
            source_id = (
                f'{row["binding_id"]}:{int(row["binding_revision"])}'
            )
            source_digest = canonical_sha256(
                {
                    "binding_id": row["binding_id"],
                    "binding_revision": row["binding_revision"],
                    "board_id": row["board_id"],
                    "guideline_id": row["guideline_id"],
                    "revision_id": row["revision_id"],
                    "legacy_revision_digest": row["revision_digest"],
                    "priority": row["priority"],
                    "enforcement": row["enforcement"],
                    "state": row["state"],
                }
            )
            if await _insert_migration_audit(
                source_type="binding",
                source_id=source_id,
                board_id=row["board_id"],
                guideline_id=row["guideline_id"],
                migration_state="inert_binding",
                source_digest=source_digest,
                details={
                    "legacy_binding_revision": row["binding_revision"],
                    "semantic_configuration_created": False,
                    "executable": False,
                    "remediation": "preview_and_adopt_semantic_configuration",
                },
                migrated_at=row["adopted_at"],
            ):
                changed = True

        receipt_rows = (
            (
                await conn.execute(
                    sa_select(PolicyComplianceReceiptRow.__table__)
                )
            )
            .mappings()
            .all()
        )
        for row in receipt_rows:
            if await _insert_migration_audit(
                source_type="receipt",
                source_id=row["receipt_id"],
                board_id=row["board_id"],
                guideline_id=None,
                migration_state="stale_receipt",
                source_digest=row["receipt_digest"],
                details={
                    "legacy_contract": "policy-compliance/v1",
                    "semantic_currentness": "stale",
                    "gate_authority": False,
                    "remediation": "run_semantic_guideline_assessment",
                },
                migrated_at=row["evaluated_at"],
            ):
                changed = True

        waiver_rows = (
            (
                await conn.execute(sa_select(PolicyWaiverRow.__table__))
            )
            .mappings()
            .all()
        )
        for row in waiver_rows:
            if await _insert_migration_audit(
                source_type="waiver",
                source_id=row["waiver_id"],
                board_id=row["board_id"],
                guideline_id=row["guideline_id"],
                migration_state="ineffective_waiver",
                source_digest=row["head_digest"],
                details={
                    "legacy_contract": "waiver-event/v1",
                    "semantic_effective": False,
                    "gate_authority": False,
                    "remediation": (
                        "request_a_waiver_for_a_current_semantic_metric_result"
                    ),
                },
                migrated_at=row["last_event_at"],
            ):
                changed = True

        if dialect == "sqlite":
            expected = semantic_guideline_sqlite_trigger_manifest()
            rows = (
                (
                    await conn.execute(
                        sa_text(
                            "SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE :prefix"
                        ),
                        {
                            "prefix": (
                                f"{SEMANTIC_GUIDELINE_TRIGGER_PREFIX}%"
                            )
                        },
                    )
                )
                .mappings()
                .all()
            )
            existing = {str(row["name"]): row for row in rows}
            unexpected = set(existing) - set(expected)
            if unexpected:
                raise RuntimeError(
                    "semantic guideline has unexpected owned triggers: "
                    + ", ".join(sorted(unexpected))
                )
            for trigger_name, (table_name, trigger_sql) in expected.items():
                row = existing.get(trigger_name)
                if row is None:
                    await conn.execute(sa_text(trigger_sql))
                    changed = True
                    continue
                if (
                    str(row["tbl_name"]) != table_name
                    or normalize_global_discovery_source_revision_trigger_sql(
                        row["sql"]
                    )
                    != normalize_global_discovery_source_revision_trigger_sql(
                        trigger_sql
                    )
                ):
                    raise RuntimeError(
                        "semantic guideline owned trigger is corrupt: "
                        + trigger_name
                    )
            violations = list(
                (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
            )
            if violations:
                raise RuntimeError(
                    "semantic guideline migration left foreign-key violations: "
                    + repr(violations[:10])
                )
        else:
            function_sql, trigger_specs = (
                semantic_guideline_postgresql_ddl()
            )
            function_name = "semantic_guideline_guard_v3"
            expected_schema = str(
                (
                    await conn.exec_driver_sql(
                        "SELECT current_schema()"
                    )
                ).scalar_one()
            )
            await conn.execute(sa_text(function_sql))
            trigger_rows = list(
                (
                    await conn.execute(
                        sa_text(
                            """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       relation_namespace.nspname AS table_schema,
       function.proname AS function_name,
       function_namespace.nspname AS function_schema,
       trigger.tgtype AS trigger_type,
       trigger.tgenabled AS trigger_enabled,
       trigger.tgqual IS NOT NULL AS has_when_clause,
       trigger.tgnargs AS argument_count,
       trigger.tgattr::text AS update_columns
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_namespace AS relation_namespace
  ON relation_namespace.oid = relation.relnamespace
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
JOIN pg_namespace AS function_namespace
  ON function_namespace.oid = function.pronamespace
WHERE NOT trigger.tgisinternal
  AND relation_namespace.nspname = current_schema()
  AND trigger.tgname LIKE :prefix
"""
                        ),
                        {"prefix": "trg_sgv3_%"},
                    )
                )
                .mappings()
                .all()
            )
            missing_triggers = set(
                audit_semantic_guideline_postgresql_trigger_rows(
                    trigger_rows,
                    expected_schema=expected_schema,
                    trigger_specs=trigger_specs,
                )
            )
            for trigger_name, (
                table_name,
                operation_clause,
                _expected_type,
            ) in trigger_specs.items():
                if trigger_name in missing_triggers:
                    await conn.execute(
                        sa_text(
                            f'CREATE TRIGGER "{trigger_name}" '
                            f"BEFORE {operation_clause} "
                            f'ON "{table_name}" FOR EACH ROW '
                            f"EXECUTE FUNCTION {function_name}()"
                        )
                    )
                    changed = True

    return None if changed else "skipped"


async def _migrate_add_cancellation_columns() -> None:
    """Add cancellation-justification columns to the 5 lifecycle tables (ITEM 17).

    ``cancellation_reason`` / ``cancelled_at`` / ``cancelled_by`` are required
    when an ideation/refinement/spec/sprint/card moves to 'cancelled' and are
    cleared on reopen. All nullable — existing rows read as NULL (legacy-safe).
    Idempotent via SQLite duplicate-column handling.
    """
    from sqlalchemy import text as sa_text

    tables = ["ideations", "refinements", "specs", "sprints", "cards"]
    columns = [
        ("cancellation_reason", "TEXT"),
        ("cancelled_at", "TIMESTAMP"),
        ("cancelled_by", "VARCHAR(255)"),
    ]
    async with get_engine().begin() as conn:
        for table in tables:
            for col_name, col_type in columns:
                try:
                    await conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                    )
                except Exception:
                    pass


async def _migrate_pagination_indices_and_positions() -> None:
    """Pagination support (spec 8b33f9a8): covering indices + dense-position backfill.

    1) Covering indices for the paginated read paths (CREATE INDEX IF NOT
       EXISTS — idempotent):
       ``cards(board_id, status, position, id)`` for Kanban column scans and
       the resequencer's deterministic order, plus
       ``<entity>(board_id, updated_at, id)`` on the six board-wide lists so
       the stable ``(updated_at DESC, id DESC)`` pagination never needs a
       table-level TEMP B-TREE.
    2) Dense-position backfill for cards, per ``(board_id, status)``: active
       cards get ``0..n-1`` and archived cards ``n..m``, derived from the
       deterministic order ``(archived ASC, position ASC, id DESC)`` — the
       same tie-break as ``CardService.resequence_columns`` (refinement v17,
       item 7). Normalizes legacy defects (literal ``-1`` sentinels, gaps,
       collisions, interleaved archived rows). Only rows whose position
       differs are rewritten, so a second run updates zero rows
       (idempotency oracle ts_dfbe2715).
    """
    from sqlalchemy import text as sa_text

    list_entities = (
        "stories",
        "ideations",
        "refinements",
        "specs",
        "sprints",
        "cards",
    )
    # FULL TR3 matrix (tr_8b519755) — every canonical read-path shape,
    # including the ARCHIVED-FREE variants (include_archived=true), the facet
    # batch with archived BEFORE status, the sprint-by-spec list, the
    # board+spec EXISTS probe and the open-QA partial indexes.
    ddl_statements = [
        # Kanban column page + resequencer canonical query:
        #   WHERE board_id=? AND status=? [AND archived=?]
        #   ORDER BY position ASC, id DESC
        # The mixed direction requires an explicit ``id DESC`` index column
        # or SQLite emits USE TEMP B-TREE FOR RIGHT PART OF ORDER BY.
        "CREATE INDEX IF NOT EXISTS ix_cards_board_status_archived_position_iddesc "
        "ON cards(board_id, status, archived, position, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_status_position_iddesc "
        "ON cards(board_id, status, position, id DESC)",
        # card_type facets: batch (archived BEFORE status — serves both the
        # per-column and the GROUP BY status,card_type batch walk), the
        # board-wide roll-up, and the two ARCHIVED-FREE variants.
        "CREATE INDEX IF NOT EXISTS ix_cards_board_archived_status_card_type "
        "ON cards(board_id, archived, status, card_type)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_archived_card_type "
        "ON cards(board_id, archived, card_type)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_status_card_type "
        "ON cards(board_id, status, card_type)",
        # assignee facets: board-wide archived-aware + ARCHIVED-FREE.
        "CREATE INDEX IF NOT EXISTS ix_cards_board_archived_assignee "
        "ON cards(board_id, archived, assignee_id)",
        "CREATE INDEX IF NOT EXISTS ix_cards_board_assignee "
        "ON cards(board_id, assignee_id)",
        # EXISTS probe for lookup options (linked_to_cards universe).
        "CREATE INDEX IF NOT EXISTS ix_cards_board_spec ON cards(board_id, spec_id)",
        # Topic summaries aggregate both active and archived Story counts in
        # one board-scoped GROUP BY without hydrating Story rows.
        "CREATE INDEX IF NOT EXISTS ix_stories_board_topic_archived "
        "ON stories(board_id, topic_id, archived)",
        # Sprint lists scoped by spec (TR3 literal ASC form + the
        # status-filtered DESC/DESC variant from the round-3 addendum).
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_archived_updated_id "
        "ON sprints(spec_id, archived, updated_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_status_archived_updated_id "
        "ON sprints(spec_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_updated_id "
        "ON sprints(spec_id, updated_at, id)",
        # MCP list_by_board preserves its legacy sprint order
        # (created_at ASC, id DESC), independently from the REST list order.
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_archived_created_iddesc "
        "ON sprints(board_id, spec_id, archived, created_at ASC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sprints_spec_status_archived_created_iddesc "
        "ON sprints(board_id, spec_id, status, archived, created_at ASC, id DESC)",
        # Lookup/typeahead canonical order (title ASC, id ASC) — with or
        # without a status eligibility filter and with the linked_to_cards
        # EXISTS probe (AC13 covers the lookups too).
        "CREATE INDEX IF NOT EXISTS ix_specs_board_title_id "
        "ON specs(board_id, title, id)",
        "CREATE INDEX IF NOT EXISTS ix_ideations_board_title_id "
        "ON ideations(board_id, title, id)",
        # Refinement lists scoped by ideation (the real caller scope): the
        # archived-filtered and status-filtered DESC/DESC variants plus the
        # include_archived variant.
        "CREATE INDEX IF NOT EXISTS ix_refinements_ideation_archived_updated_id "
        "ON refinements(ideation_id, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_ideation_status_archived_updated_id "
        "ON refinements(ideation_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_ideation_updated_id "
        "ON refinements(ideation_id, updated_at, id)",
        # Nested refinement routes carry BOTH the board and ideation anchors.
        # Without the composite prefix SQLite may choose the board-wide index
        # and scan the entire board instead of the handful of rows belonging
        # to the selected ideation (DR6 @10k repro).
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_ideation_archived_updated_iddesc "
        "ON refinements(board_id, ideation_id, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_ideation_status_archived_updated_iddesc "
        "ON refinements(board_id, ideation_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_ideation_updated_iddesc "
        "ON refinements(board_id, ideation_id, updated_at DESC, id DESC)",
        # C8 board-wide refinement list: active/all/status-filtered forms keep
        # the canonical updated_at DESC, id DESC order without a table sort.
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_archived_updated_iddesc "
        "ON refinements(board_id, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_status_archived_updated_iddesc "
        "ON refinements(board_id, status, archived, updated_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_refinements_board_updated_iddesc "
        "ON refinements(board_id, updated_at DESC, id DESC)",
        # Open-QA partial indexes (open_qa_count derived fields).
        "CREATE INDEX IF NOT EXISTS ix_qa_items_card_open "
        "ON qa_items(card_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_ideation_qa_items_parent_open "
        "ON ideation_qa_items(ideation_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_refinement_qa_items_parent_open "
        "ON refinement_qa_items(refinement_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_spec_qa_items_parent_open "
        "ON spec_qa_items(spec_id) WHERE answered_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_sprint_qa_items_parent_open "
        "ON sprint_qa_items(sprint_id) WHERE answered_at IS NULL",
    ]
    for table in list_entities:
        # Board-wide list, archived-filtered variant — TR3 literally requires
        # PHYSICAL DESC/DESC on (updated_at, id):
        #   (scope, archived, updated_at DESC, id DESC).
        ddl_statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_board_archived_updated_id "
            f"ON {table}(board_id, archived, updated_at DESC, id DESC)"
        )
        # include_archived=true variant (no archived predicate; TR3 keeps the
        # plain ASC form here — a backward scan serves the DESC order).
        ddl_statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_board_updated_id "
            f"ON {table}(board_id, updated_at, id)"
        )
        # Status-filtered list variant — DESC/DESC per TR3.
        ddl_statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_board_status_archived_updated_id "
            f"ON {table}(board_id, status, archived, updated_at DESC, id DESC)"
        )

    async with get_engine().begin() as conn:
        for ddl in ddl_statements:
            await conn.execute(sa_text(ddl))

        await conn.execute(
            sa_text(
                """
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY board_id, status
                               ORDER BY COALESCE(archived, 0) ASC,
                                        COALESCE(position, 0) ASC,
                                        id DESC
                           ) - 1 AS dense_position
                    FROM cards
                )
                UPDATE cards
                SET position = (
                    SELECT dense_position FROM ranked WHERE ranked.id = cards.id
                )
                WHERE position IS NULL
                   OR position <> (
                       SELECT dense_position FROM ranked WHERE ranked.id = cards.id
                   )
                """
            )
        )


async def _migrate_agent_permissions() -> None:
    """Migrate agents from legacy flat permissions to granular permission_flags."""
    import logging

    logger = logging.getLogger("okto_pulse.migrations")

    import json as _json
    from sqlalchemy import JSON as sa_JSON
    from sqlalchemy import bindparam, text as sa_text

    async with get_session_factory()() as session:
        try:
            from okto_pulse.core.ports.permission_policy import (
                legacy_permissions_to_flags,
                registered_permission_flags,
            )

            result = await session.execute(
                sa_text(
                    "SELECT id, permissions FROM agents WHERE permission_flags IS NULL"
                )
            )
            agents = list(result.mappings().all())
            if not agents:
                return

            for agent in agents:
                old_perms = agent["permissions"]
                if isinstance(old_perms, str):
                    decoded_perms = _json.loads(old_perms)
                else:
                    decoded_perms = old_perms
                if decoded_perms is None:
                    new_flags = registered_permission_flags()
                else:
                    # Duplicate strings are valid: legacy mapping only sets
                    # boolean leaves, so replaying one permission is idempotent.
                    if not isinstance(decoded_perms, list) or not all(
                        isinstance(permission, str) for permission in decoded_perms
                    ):
                        raise ValueError(
                            f"Agent {agent['id']!r} legacy permissions must be "
                            "a JSON array of strings"
                        )
                    new_flags = legacy_permissions_to_flags(decoded_perms)
                await session.execute(
                    sa_text(
                        "UPDATE agents SET permission_flags = :permission_flags "
                        "WHERE id = :id"
                    ).bindparams(bindparam("permission_flags", type_=sa_JSON)),
                    {
                        "id": agent["id"],
                        "permission_flags": new_flags,
                    },
                )
                logger.info(f"Migrated agent {agent['id'][:8]} permissions")
            await session.commit()
            logger.info(f"Permission migration complete: {len(agents)} agent(s)")
        except Exception as e:
            logger.error(f"Permission migration failed: {e}")
            await session.rollback()
            raise


_RKG04_FIXTURE_BOARD_RE = re.compile(r"^(?:rkg04-[0-9a-f]{10}|rkg04mcp-[0-9a-f]{8})$")
_FIXTURE_POLLUTION_FIRST_DAY = "2026-06-27"
_FIXTURE_POLLUTION_LAST_DAY = "2026-07-02"


async def _migrate_ensure_guideline_binding_exact_authority_index() -> str | None:
    """Backfill the 5-column unique authority index on migrated databases.

    Fresh ``create_all`` schemas declare
    ``uq_guideline_binding_exact_authority`` (unique over binding_id,
    binding_revision, board_id, guideline_id, revision_id) on
    guideline_board_bindings, and the SK-B3 child table
    semantic_guideline_binding_configurations carries a composite FK
    referencing exactly those five columns. A database migrated from an
    older shape has the table but NOT the unique index, which makes the FK
    structurally invalid — SQLite raises ``foreign key mismatch`` the moment
    ``PRAGMA foreign_key_check`` runs (the fixture FK-orphan repair step
    below) and startup fails closed. The index is safe to backfill
    unconditionally: (binding_id, binding_revision) is already the table's
    primary key, so uniqueness over the five-column superset can never
    conflict on existing rows.
    """

    engine = get_engine()
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_guideline_binding_exact_authority "
            "ON guideline_board_bindings "
            "(binding_id, binding_revision, board_id, guideline_id, revision_id)"
        )
    return None


async def _migrate_rebuild_guideline_import_candidates_semantic_shape() -> str | None:
    """Rebuild the legacy import-candidate table into the semantic shape.

    The SK-B3 semantic migration renamed ``source_default_enforcement`` to
    ``source_enforcement`` and repointed the exact-revision FK from the
    legacy ``guideline_revisions`` quadruple to
    ``semantic_guideline_revisions`` (guideline_id, revision_id,
    revision_digest) on ``guideline_import_binding_candidates``. Fresh
    ``create_all`` schemas already have the canonical shape, but a migrated
    database keeps the legacy table, and the strict B03 substrate audit then
    fails closed with "non-canonical contract" at startup. SQLite cannot
    ALTER a column rename plus an FK repoint in place, so the legacy table is
    rebuilt: copy rows with the column renamed, drop the legacy table (its
    owned triggers drop with it; the substrate step recreates the canonical
    ones), and recreate from the ORM DDL. Deferred FK enforcement validates
    every copied row against ``semantic_guideline_revisions`` at commit, so a
    row whose resolved revision never reached the semantic ledger still fails
    closed instead of being silently rewired.
    """

    from sqlalchemy import text as sa_text

    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineImportBindingCandidateRow,
    )

    engine = get_engine()
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"
        columns = {
            str(row[1])
            for row in (
                await conn.exec_driver_sql(
                    "PRAGMA table_info(guideline_import_binding_candidates)"
                )
            ).all()
        }
        if not columns or "source_enforcement" in columns:
            return "skipped"
        if "source_default_enforcement" not in columns:
            raise RuntimeError(
                "guideline import candidate table has an unrecognized legacy "
                "shape; refusing to rebuild"
            )
        legacy_rows = (
            await conn.exec_driver_sql(
                "SELECT * FROM guideline_import_binding_candidates"
            )
        ).mappings().all()
        await conn.exec_driver_sql(
            "DROP TABLE guideline_import_binding_candidates"
        )
        await conn.run_sync(
            lambda sync_conn: GuidelineImportBindingCandidateRow.__table__.create(
                sync_conn
            )
        )
        if legacy_rows:
            canonical_columns = [
                str(column.name)
                for column in GuidelineImportBindingCandidateRow.__table__.columns
            ]
            for row in legacy_rows:
                values = dict(row)
                values["source_enforcement"] = values.pop(
                    "source_default_enforcement"
                )
                missing = set(canonical_columns) - set(values)
                if missing:
                    raise RuntimeError(
                        "guideline import candidate rebuild cannot map legacy "
                        "row; missing canonical columns: "
                        + ", ".join(sorted(missing))
                    )
                placeholders = ", ".join(f":{name}" for name in canonical_columns)
                await conn.execute(
                    sa_text(
                        "INSERT INTO guideline_import_binding_candidates ("
                        + ", ".join(canonical_columns)
                        + f") VALUES ({placeholders})"
                    ),
                    {name: values[name] for name in canonical_columns},
                )
    return None


async def _migrate_rebuild_guideline_policy_v1_semantic_alignment() -> str | None:
    """Rebuild the legacy guideline v1 family into the SK-B3 semantic shape.

    The SK-B3 closure renamed the enforcement/rule columns
    (``default_enforcement`` -> ``enforcement``, ``*_rule_ids`` ->
    ``*_metric_ids``, ``proposed_default_enforcement`` ->
    ``proposed_enforcement``), added the semantic proposal columns
    (``proposed_minimum_confidence``, ``proposed_metric_threshold_overrides``)
    and repointed the impact/retirement exact-revision FKs from the legacy
    ``guideline_revisions`` quadruple to ``semantic_guideline_revisions``.
    Fresh ``create_all`` schemas are canonical, but a migrated database keeps
    the legacy tables and the strict B03/B08 audits then fail closed at
    startup ("non-canonical contract"). SQLite cannot express these changes
    as ALTERs, so each legacy table is rebuilt from the ORM DDL with an
    explicit row mapping. Semantic revision targets for surviving impact
    receipts are seeded first using EXACTLY the legacy-bridge construction of
    ``_migrate_semantic_guideline_governance_schema`` (same digest function,
    same authority-state classification, same source fence), so when that
    later step enumerates the same legacy revisions it passes its fences
    instead of conflicting. ``proposed_minimum_confidence`` is backfilled
    with 70 — the product default used by the semantic adoption paths — and
    the overrides with an empty object; historical ``*_rule_ids`` values are
    carried into the renamed columns as immutable ledger history. FK
    enforcement is re-enabled and a scoped ``foreign_key_check`` over the
    rebuilt tables fails closed on any violation.
    """

    from sqlalchemy import select as sa_select, text as sa_text

    from okto_pulse.core.domain.guideline_policy import (
        GUIDELINE_REVISION_DIGEST_CONTRACT_VERSION,
        guideline_revision_digest_v2,
    )
    from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineBoardBindingRow,
        GuidelineImpactReceiptRow,
        GuidelineImpactUnlinkRow,
        GuidelineRetirementImpactRow,
        GuidelineRetirementRow,
        GuidelineRevisionRow,
        SemanticGuidelineRevisionRow,
    )

    rename_by_table = {
        "guideline_board_bindings": (
            GuidelineBoardBindingRow,
            {"default_enforcement": "enforcement"},
            "default_enforcement",
            {},
        ),
        "guideline_impact_receipts": (
            GuidelineImpactReceiptRow,
            {
                "added_rule_ids": "added_metric_ids",
                "changed_rule_ids": "changed_metric_ids",
                "removed_rule_ids": "removed_metric_ids",
                "proposed_default_enforcement": "proposed_enforcement",
            },
            "added_rule_ids",
            {
                # Product default used by the semantic adoption paths
                # (core services adopt with minimum_confidence=70).
                "proposed_minimum_confidence": 70,
                "proposed_metric_threshold_overrides": "{}",
            },
        ),
        "guideline_impact_unlinks": (
            GuidelineImpactUnlinkRow,
            {"removed_rule_ids": "removed_metric_ids"},
            "removed_rule_ids",
            {},
        ),
        "guideline_retirement_impacts": (
            GuidelineRetirementImpactRow,
            {"removed_rule_ids": "removed_metric_ids"},
            "removed_rule_ids",
            {},
        ),
        "guideline_retirements": (
            GuidelineRetirementRow,
            {},
            None,
            {},
        ),
    }

    engine = get_engine()
    rebuilt: list[str] = []
    digest_rewrites = 0
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"

        # The driver defers BEGIN until the first DML statement, so this
        # PRAGMA executes outside the transaction and enforcement is
        # genuinely off for the digest rewrite and the rebuilds below (the
        # family holds RESTRICT FKs in both directions, so no in-place order
        # satisfies them). A scoped foreign_key_check with enforcement
        # restored runs at the end and fails closed.
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")

        async def _columns(table_name: str) -> set[str]:
            return {
                str(row[1])
                for row in (
                    await conn.exec_driver_sql(
                        f"PRAGMA table_info({table_name})"
                    )
                ).all()
            }

        legacy_tables: list[str] = []
        for table_name, (_, _, marker, _) in rename_by_table.items():
            columns = await _columns(table_name)
            if not columns:
                continue
            if marker is not None:
                if marker in columns:
                    legacy_tables.append(table_name)
                continue
            # guideline_retirements keeps its columns; the legacy variant is
            # detected by its FK still targeting guideline_revisions.
            ddl = (
                await conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE name = "
                    f"'{table_name}'"
                )
            ).scalar_one_or_none() or ""
            if (
                "retired_revision_id" in ddl
                and "semantic_guideline_revisions" not in ddl
            ):
                legacy_tables.append(table_name)
        # Phase 0 — digest realignment. Pre-I9 databases store revision
        # content digests produced by the RETIRED policy/v1 algorithm; the
        # closure recomputes every digest with guideline-revision-digest/v2
        # from the stored canonical fields (content is the authority — the
        # old algorithm no longer exists to verify against) and the strict
        # baseline audit in _migrate_guideline_policy_v1_schema requires the
        # v2 value. Bindings carry the digest in their exact-revision FK and
        # are updated in lockstep; for migrated-legacy baselines the audited
        # request_digest is recomputed with the SAME payload construction the
        # audit itself uses.
        import hashlib as _hashlib
        import json as _json

        def _aligned_request_digest(payload: object) -> str:
            encoded = _json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            return _hashlib.sha256(encoded).hexdigest()

        revision_table = GuidelineRevisionRow.__table__
        revision_rows = (
            (await conn.execute(sa_select(revision_table))).mappings().all()
        )
        # The revision/binding ledgers are guarded by immutability triggers;
        # the digest realignment is the sanctioned rewrite point, so the
        # exact observed triggers are captured, dropped, and recreated
        # byte-identically after the updates.
        guard_triggers = (
            await conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('guideline_revisions', "
                "'guideline_board_bindings') AND sql IS NOT NULL"
            )
        ).all()
        for trigger_name, _trigger_sql in guard_triggers:
            await conn.exec_driver_sql(f"DROP TRIGGER {trigger_name}")
        semantic_ledger_table = SemanticGuidelineRevisionRow.__table__
        for revision_row in revision_rows:
            semantic_row = (
                (
                    await conn.execute(
                        sa_select(
                            semantic_ledger_table.c.authority_state
                        ).where(
                            semantic_ledger_table.c.revision_id
                            == revision_row["revision_id"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                semantic_row is not None
                and semantic_row["authority_state"] == "native"
            ):
                # Native semantic revisions carry METRICS in their digest;
                # recomputing here with metrics=() would be a false rewrite.
                # The semantic adapter wrote their digest atomically — it is
                # the authority, never a legacy realignment candidate.
                continue
            recomputed = guideline_revision_digest_v2(
                semantic_version=revision_row["semantic_version"],
                title=revision_row["title"],
                content=revision_row["content"],
                metrics=(),
                tags=tuple(revision_row["tags"] or ()),
            )
            if recomputed == revision_row["content_digest"]:
                continue
            updates: dict[str, object] = {"content_digest": recomputed}
            if (
                revision_row["revision_number"] == 1
                and revision_row["legacy_version"] is not None
            ):
                guideline_row = (
                    (
                        await conn.exec_driver_sql(
                            "SELECT title, content, version, tags "
                            "FROM guidelines WHERE id = "
                            f"'{revision_row['guideline_id']}'"
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if guideline_row is None:
                    raise RuntimeError(
                        "guideline v1 semantic alignment: baseline revision "
                        "without its guideline: "
                        + str(revision_row["guideline_id"])
                    )
                updates["request_digest"] = _aligned_request_digest(
                    {
                        "guideline_id": revision_row["guideline_id"],
                        "revision_id": revision_row["revision_id"],
                        "title": str(guideline_row["title"]).strip(),
                        "content": str(guideline_row["content"]).strip(),
                        "legacy_title": guideline_row["title"],
                        "legacy_content": guideline_row["content"],
                        "content_digest": recomputed,
                        "legacy_version": guideline_row["version"],
                        "legacy_tags": _json.loads(guideline_row["tags"])
                        if isinstance(guideline_row["tags"], str)
                        else guideline_row["tags"],
                    }
                )
            await conn.execute(
                revision_table.update()
                .where(
                    revision_table.c.revision_id
                    == revision_row["revision_id"]
                )
                .values(**updates)
            )
            digest_rewrites += 1

        if digest_rewrites:
            # Migrated-legacy bindings embed the revision digest in their
            # audited request_digest, which cannot be recomputed here without
            # replicating the whole legacy transcription. They are fully
            # deterministic from board_guidelines/guidelines/revision rows and
            # carry no dependents (adoptions/configs reference native
            # bindings only, and their impact columns are NULL), so the
            # sanctioned path is deletion: the strict v1 step re-creates each
            # one canonically when it finds no existing row. Fail closed if a
            # dependent exists after all.
            dependents = (
                await conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM guideline_board_bindings b WHERE "
                    "b.source_kind != 'native' AND ("
                    "b.impact_receipt_id IS NOT NULL OR "
                    "b.impact_adoption_id IS NOT NULL OR "
                    "b.impact_unlink_id IS NOT NULL OR EXISTS ("
                    "SELECT 1 FROM semantic_guideline_binding_configurations "
                    "c WHERE c.binding_id = b.binding_id) OR EXISTS ("
                    "SELECT 1 FROM guideline_impact_adoptions a WHERE "
                    "a.binding_id = b.binding_id))"
                )
            ).scalar_one()
            if int(dependents):
                raise RuntimeError(
                    "guideline v1 semantic alignment: legacy bindings still "
                    "have dependents; refusing to realign by deletion"
                )
            await conn.exec_driver_sql(
                "DELETE FROM guideline_board_bindings "
                "WHERE source_kind != 'native'"
            )

        for _trigger_name, trigger_sql in guard_triggers:
            # guideline_board_bindings triggers die with the table rebuild
            # below when its legacy shape is detected; recreating them here
            # first keeps the digest-only path guarded, and DROP TABLE
            # removes them again cleanly before the canonical rebuild.
            await conn.exec_driver_sql(trigger_sql)

        if not legacy_tables and not digest_rewrites:
            return "skipped"

        # 1. Seed semantic revision targets for surviving impact receipts.
        semantic_digest_by_revision: dict[str, str] = {}
        if "guideline_impact_receipts" in legacy_tables:
            referenced = (
                await conn.exec_driver_sql(
                    "SELECT DISTINCT guideline_id, revision_id FROM ("
                    "SELECT guideline_id, from_revision_id AS revision_id "
                    "FROM guideline_impact_receipts "
                    "WHERE from_revision_id IS NOT NULL "
                    "UNION SELECT guideline_id, to_revision_id "
                    "FROM guideline_impact_receipts "
                    "WHERE to_revision_id IS NOT NULL)"
                )
            ).all()
            revision_table = GuidelineRevisionRow.__table__
            semantic_table = SemanticGuidelineRevisionRow.__table__
            for guideline_id, revision_id in referenced:
                legacy_row = (
                    (
                        await conn.execute(
                            sa_select(revision_table).where(
                                revision_table.c.guideline_id == guideline_id,
                                revision_table.c.revision_id == revision_id,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if legacy_row is None:
                    raise RuntimeError(
                        "guideline v1 semantic alignment: impact receipt "
                        "references a missing legacy revision: "
                        f"{revision_id}"
                    )
                existing = (
                    (
                        await conn.execute(
                            sa_select(semantic_table).where(
                                semantic_table.c.revision_id == revision_id
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if (
                        existing["guideline_id"] != legacy_row["guideline_id"]
                        or existing["source_revision_digest"]
                        != legacy_row["content_digest"]
                    ):
                        raise RuntimeError(
                            "guideline v1 semantic alignment: semantic "
                            "revision source fence conflict: "
                            + str(revision_id)
                        )
                    semantic_digest_by_revision[str(revision_id)] = str(
                        existing["revision_digest"]
                    )
                    continue
                rules = legacy_row["rules"]
                rules_are_empty = isinstance(rules, list) and not rules
                legacy_rules_payload = (
                    rules
                    if isinstance(rules, list)
                    else {"invalid_legacy_rules_payload": repr(rules)}
                )
                revision_digest = guideline_revision_digest_v2(
                    semantic_version=legacy_row["semantic_version"],
                    title=legacy_row["title"],
                    content=legacy_row["content"],
                    metrics=(),
                    tags=tuple(legacy_row["tags"] or ()),
                )
                await conn.execute(
                    semantic_table.insert().values(
                        revision_id=legacy_row["revision_id"],
                        guideline_id=legacy_row["guideline_id"],
                        contract_version=(
                            GUIDELINE_REVISION_DIGEST_CONTRACT_VERSION
                        ),
                        metrics=[],
                        revision_digest=revision_digest,
                        source_revision_digest=legacy_row["content_digest"],
                        authority_state=(
                            "legacy_context_only"
                            if rules_are_empty
                            else "legacy_incompatible"
                        ),
                        legacy_rules_digest=canonical_sha256(
                            legacy_rules_payload
                        ),
                        created_by=legacy_row["created_by"],
                        created_at=legacy_row["created_at"],
                    )
                )
                semantic_digest_by_revision[str(revision_id)] = revision_digest

        # 2. Rebuild each legacy table from the canonical ORM DDL.
        for table_name in legacy_tables:
            row_class, renames, _, backfills = rename_by_table[table_name]
            table = row_class.__table__
            legacy_rows = (
                (await conn.exec_driver_sql(f"SELECT * FROM {table_name}"))
                .mappings()
                .all()
            )
            await conn.exec_driver_sql(f"DROP TABLE {table_name}")
            await conn.run_sync(
                lambda sync_conn, owned=table: owned.create(sync_conn)
            )
            canonical_columns = [str(column.name) for column in table.columns]
            for legacy_row in legacy_rows:
                values = dict(legacy_row)
                for old_name, new_name in renames.items():
                    values[new_name] = values.pop(old_name)
                for column_name, backfill in backfills.items():
                    values.setdefault(column_name, backfill)
                if table_name == "guideline_impact_receipts":
                    for prefix in ("from", "to"):
                        revision_id = values.get(f"{prefix}_revision_id")
                        if revision_id is None:
                            continue
                        digest = semantic_digest_by_revision.get(
                            str(revision_id)
                        )
                        if digest is None:
                            raise RuntimeError(
                                "guideline v1 semantic alignment: no "
                                "semantic digest for receipt revision "
                                + str(revision_id)
                            )
                        values[f"{prefix}_revision_digest"] = digest
                missing = set(canonical_columns) - set(values)
                if missing:
                    raise RuntimeError(
                        f"guideline v1 semantic alignment: {table_name} "
                        "legacy row lacks canonical columns: "
                        + ", ".join(sorted(missing))
                    )
                placeholders = ", ".join(
                    f":{name}" for name in canonical_columns
                )
                await conn.execute(
                    sa_text(
                        f"INSERT INTO {table_name} ("
                        + ", ".join(canonical_columns)
                        + f") VALUES ({placeholders})"
                    ),
                    {name: values[name] for name in canonical_columns},
                )
            rebuilt.append(table_name)

    # New transaction: the PRAGMA executes before the deferred BEGIN, so
    # enforcement is restored on the pooled connection, then the rebuilt
    # tables must pass a scoped integrity check or startup fails closed.
    checked_tables = sorted(
        set(rebuilt)
        | (
            {"guideline_revisions", "guideline_board_bindings"}
            if digest_rewrites
            else set()
        )
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        for table_name in checked_tables:
            violations = (
                await conn.exec_driver_sql(
                    f"PRAGMA foreign_key_check({table_name})"
                )
            ).all()
            if violations:
                raise RuntimeError(
                    "guideline v1 semantic alignment left FK violations in "
                    f"{table_name}: {violations[:5]!r}"
                )
    return None


async def _migrate_drop_retired_guideline_impact_v1_triggers() -> str | None:
    """Drop the retired trg_guideline_impact_v1_* guard family.

    The SK-B3 closure replaced the guideline-impact/v1 event contract with
    guideline-impact/v2 semantic events, and the v2 trigger manifest guards
    every surface the v1 family guarded (activity logs, domain events,
    handler executions, adoptions, items) with the v2 shapes. A migrated
    database keeps BOTH families, and the v1 policy-constraint execution
    guard then rejects every v2 semantic adoption/retirement event
    (policy_constraint_execution_event_invalid raised on the handler
    execution insert), making semantic adoption impossible at runtime.
    Fresh schemas never install the v1 family; dropping it on upgraded
    databases converges both worlds.
    """

    engine = get_engine()
    dropped = 0
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"
        names = [
            str(row[0])
            for row in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND name LIKE 'trg_guideline_impact_v1_%'"
                )
            ).all()
        ]
        for name in names:
            await conn.exec_driver_sql(f'DROP TRIGGER "{name}"')
            dropped += 1
    return None if dropped else "skipped"


async def _migrate_seed_semantic_configurations_for_legacy_bindings() -> str | None:
    """Give every binding the semantic configuration the read path requires.

    The semantic governance migration deliberately leaves migrated legacy
    bindings "inert" (audit remediation: preview_and_adopt), but the SK-B3
    binding hydration fails closed whenever ANY binding lacks its
    semantic_guideline_binding_configurations row
    (guideline_semantic_binding_configuration_inventory_incomplete) — so on
    an upgraded database every board-guidelines listing 500s permanently.
    This convergence seeds the missing configuration with the semantically
    inert equivalent: the binding's own enforcement (advisory for legacy
    rows), the product-default minimum confidence of 70, no overrides, and
    the guideline's seeded context-only semantic revision. A context-only
    revision carries zero metrics, so the seeded configuration grants no
    executable authority the operator never adopted — compliance still
    resolves not_applicable until a real preview+adopt — while the closed
    read contract becomes satisfiable. Fails closed when a binding's
    guideline has no semantic revision to pin.
    """

    from sqlalchemy import select as sa_select

    from okto_pulse.core.domain.guideline_policy import (
        GuidelineEnforcement,
        guideline_binding_configuration_digest_v1,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        GuidelineBoardBindingRow,
        SemanticGuidelineBindingConfigurationRow,
        SemanticGuidelineRevisionRow,
    )

    engine = get_engine()
    seeded = 0
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"
        binding_table = GuidelineBoardBindingRow.__table__
        config_table = SemanticGuidelineBindingConfigurationRow.__table__
        semantic_table = SemanticGuidelineRevisionRow.__table__
        binding_rows = (
            (await conn.execute(sa_select(binding_table))).mappings().all()
        )
        for binding in binding_rows:
            existing = (
                await conn.execute(
                    sa_select(config_table.c.binding_id).where(
                        config_table.c.binding_id == binding["binding_id"],
                        config_table.c.binding_revision
                        == binding["binding_revision"],
                    )
                )
            ).first()
            if existing is not None:
                continue
            semantic_revision = (
                (
                    await conn.execute(
                        sa_select(semantic_table).where(
                            semantic_table.c.guideline_id
                            == binding["guideline_id"],
                            semantic_table.c.revision_id
                            == binding["revision_id"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if semantic_revision is None:
                raise RuntimeError(
                    "semantic configuration seeding: binding references a "
                    "revision with no semantic ledger row: "
                    + str(binding["binding_id"])
                )
            enforcement = GuidelineEnforcement(binding["enforcement"])
            configuration_digest = guideline_binding_configuration_digest_v1(
                binding_id=binding["binding_id"],
                board_id=binding["board_id"],
                guideline_id=binding["guideline_id"],
                revision_id=binding["revision_id"],
                revision_digest=semantic_revision["revision_digest"],
                priority=binding["priority"],
                enforcement=enforcement,
                minimum_confidence=70,
                metric_threshold_overrides={},
            )
            await conn.execute(
                config_table.insert().values(
                    binding_id=binding["binding_id"],
                    binding_revision=binding["binding_revision"],
                    board_id=binding["board_id"],
                    guideline_id=binding["guideline_id"],
                    revision_id=binding["revision_id"],
                    revision_digest=semantic_revision["revision_digest"],
                    enforcement=binding["enforcement"],
                    minimum_confidence=70,
                    metric_threshold_overrides={},
                    configuration_digest=configuration_digest,
                    configured_by=binding["adopted_by"],
                    configured_at=binding["adopted_at"],
                )
            )
            seeded += 1
    return None if seeded else "skipped"


async def _migrate_recompute_cognitive_source_fingerprints_v2() -> str | None:
    """Recompute the durable cognitive-source ledger under fingerprint v2.

    Fingerprint v1 hashed the FULL node payload while ``source_revision``
    derives from ``attestation_count`` — so read-side usage drift
    (query_hits/last_queried_at/relevance_score change on every KG query)
    made an identical knowledge replay diverge from its own stored revision
    and permanently poisoned consolidation with
    ``cognitive_source_replay_conflict`` (observed live on
    decision_059d5828). Fingerprint v2 excludes the volatile usage fields
    from the IDENTITY hash (stored payloads keep them for literal rebuild
    restoration). This convergence rewrites every stored
    ``record_fingerprint`` under v2 so replays of drifted-but-identical
    knowledge resolve idempotently against the migrated ledger.
    """

    import json as _json

    from sqlalchemy import select as sa_select

    from okto_pulse.core.ports.kg_cognitive_source import (
        canonical_cognitive_source_fingerprint,
    )
    from okto_pulse.community.adapters.sqlalchemy_models import (
        KGCognitiveSource,
        KGCognitiveSourceRevision,
    )

    def _mapping(value: object) -> dict:
        if isinstance(value, str):
            value = _json.loads(value)
        return dict(value or {})

    def _refs(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            parsed = _json.loads(value)
            value = parsed if isinstance(parsed, list) else (parsed,)
        return tuple(str(ref) for ref in (value or ()))

    _IMMUTABLE_UPDATE_TRIGGER = (
        "trg_kg_cognitive_source_immutable_"
        "kg_cognitive_source_revisions_update"
    )

    rewritten = 0
    async with get_engine().begin() as conn:
        # The ledger is guarded by an immutability trigger that (correctly)
        # aborts every UPDATE. This convergence is the one governed writer
        # allowed to touch record_fingerprint: capture the trigger DDL,
        # drop it for the scope of this transaction, and recreate it
        # byte-identically afterwards (same pattern as the guideline
        # revision digest realignment).
        trigger_sql = (
            await conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name=?",
                (_IMMUTABLE_UPDATE_TRIGGER,),
            )
        ).scalar()
        if trigger_sql:
            await conn.exec_driver_sql(
                f'DROP TRIGGER "{_IMMUTABLE_UPDATE_TRIGGER}"'
            )
        bases = {
            str(row.id): row
            for row in (
                await conn.execute(
                    sa_select(
                        KGCognitiveSource.id,
                        KGCognitiveSource.board_id,
                        KGCognitiveSource.node_id,
                        KGCognitiveSource.node_type,
                        KGCognitiveSource.generation,
                    )
                )
            ).all()
        }
        revisions = (
            await conn.execute(
                sa_select(
                    KGCognitiveSourceRevision.id,
                    KGCognitiveSourceRevision.cognitive_source_id,
                    KGCognitiveSourceRevision.record_fingerprint,
                    KGCognitiveSourceRevision.payload,
                    KGCognitiveSourceRevision.evidence_refs,
                )
            )
        ).all()
        for revision in revisions:
            base = bases.get(str(revision.cognitive_source_id))
            if base is None:
                # Orphan revision: leave untouched; the FK repair step and
                # ledger integrity checks own that failure mode.
                continue
            fingerprint = canonical_cognitive_source_fingerprint(
                board_id=str(base.board_id),
                node_id=str(base.node_id),
                node_type=str(base.node_type),
                generation=int(base.generation),
                payload=_mapping(revision.payload),
                evidence_refs=_refs(revision.evidence_refs),
            )
            if fingerprint == str(revision.record_fingerprint):
                continue
            await conn.execute(
                KGCognitiveSourceRevision.__table__.update()
                .where(KGCognitiveSourceRevision.id == revision.id)
                .values(record_fingerprint=fingerprint)
            )
            rewritten += 1
        if trigger_sql:
            await conn.exec_driver_sql(str(trigger_sql))
    return None if rewritten else "skipped"


async def _migrate_repair_known_fixture_fk_orphans() -> str | None:
    """Remove only historical data written by pre-isolation test fixtures.

    Older Core test fixtures accidentally resolved the default Community
    SQLite home.  One synthetic sprint CRUD board (and its graph directory)
    survived alongside RKG-04 orphan rows.  This migration is deliberately
    narrower than a generic scrubber: every violating row and the surviving
    board must match the known fixture identity/date window, or startup fails
    closed without committing any mutation.
    """

    from sqlalchemy import text as sa_text

    engine = get_engine()
    relational_changed = False
    async with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return "skipped"
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        if int((await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()) != 1:
            raise RuntimeError("fixture FK repair requires foreign-key enforcement")

        table_names = {
            str(row[0])
            for row in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ).all()
        }
        violations = list(
            (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
        )

        card_rowids: list[int] = []
        history_rowids: list[int] = []
        dlq_rowids: list[int] = []

        for table, rowid, parent, _fkid in violations:
            if not isinstance(rowid, int):
                raise RuntimeError("fixture FK repair encountered a row without rowid")

            if (table, parent) == ("cards", "sprints"):
                row = (
                    await conn.execute(
                        sa_text(
                            "SELECT id, board_id, created_by, created_at "
                            "FROM cards WHERE rowid = :rowid"
                        ),
                        {"rowid": rowid},
                    )
                ).first()
                if (
                    row is None
                    or row.board_id != "sprint-crud-board-001"
                    or row.created_by != "sprint-crud-agent-001"
                    or not str(row.id).startswith("sprint-crud-")
                    or not _fixture_pollution_day_allowed(row.created_at)
                ):
                    raise RuntimeError(
                        "fixture FK repair rejected an unknown card orphan"
                    )
                card_rowids.append(rowid)
                continue

            if (table, parent) == ("sprint_history", "sprints"):
                row = (
                    await conn.execute(
                        sa_text(
                            "SELECT actor_id, created_at FROM sprint_history "
                            "WHERE rowid = :rowid"
                        ),
                        {"rowid": rowid},
                    )
                ).first()
                if (
                    row is None
                    or row.actor_id != "sprint-crud-agent-001"
                    or not _fixture_pollution_day_allowed(row.created_at)
                ):
                    raise RuntimeError(
                        "fixture FK repair rejected an unknown sprint-history orphan"
                    )
                history_rowids.append(rowid)
                continue

            if (table, parent) == ("consolidation_dead_letter", "boards"):
                row = (
                    await conn.execute(
                        sa_text(
                            "SELECT board_id, artifact_type, created_at "
                            "FROM consolidation_dead_letter WHERE rowid = :rowid"
                        ),
                        {"rowid": rowid},
                    )
                ).first()
                if (
                    row is None
                    or row.artifact_type != "spec"
                    or _RKG04_FIXTURE_BOARD_RE.fullmatch(str(row.board_id)) is None
                    or not _fixture_pollution_day_allowed(row.created_at)
                ):
                    raise RuntimeError(
                        "fixture FK repair rejected an unknown DLQ orphan"
                    )
                dlq_rowids.append(rowid)
                continue

            raise RuntimeError("fixture FK repair encountered an unknown FK violation")

        fixture_board_present = False
        if "boards" in table_names:
            fixture_board = (
                await conn.execute(
                    sa_text(
                        "SELECT name, owner_id, realm_id, created_at FROM boards "
                        "WHERE id = 'sprint-crud-board-001'"
                    )
                )
            ).first()
            if fixture_board is not None:
                if (
                    fixture_board.name != "Sprint CRUD Board"
                    or fixture_board.owner_id != "sprint-crud-agent-001"
                    or fixture_board.realm_id != "local"
                    or not _fixture_pollution_day_allowed(fixture_board.created_at)
                ):
                    raise RuntimeError("fixture FK repair rejected the synthetic board")
                fixture_board_present = True

        if fixture_board_present:
            known_scoped_tables = {
                "activity_logs",
                "cards",
                "consolidation_audit",
                "consolidation_dead_letter",
                "domain_events",
                "global_update_outbox",
                "kuzu_node_refs",
                "specs",
                "sprints",
            }
            for table_name in sorted(table_names):
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
                    raise RuntimeError(
                        "fixture FK repair rejected an unsafe table name"
                    )
                columns = {
                    str(row[1])
                    for row in (
                        await conn.exec_driver_sql(f'PRAGMA table_info("{table_name}")')
                    ).all()
                }
                if "board_id" not in columns:
                    continue
                count = int(
                    (
                        await conn.execute(
                            sa_text(
                                f'SELECT COUNT(*) FROM "{table_name}" '
                                "WHERE board_id = 'sprint-crud-board-001'"
                            )
                        )
                    ).scalar_one()
                )
                if count and table_name not in known_scoped_tables:
                    raise RuntimeError(
                        "fixture FK repair found an unknown synthetic-board table"
                    )

            for table_name in ("activity_logs", "global_update_outbox"):
                if table_name in table_names:
                    await conn.execute(
                        sa_text(
                            f'DELETE FROM "{table_name}" '
                            "WHERE board_id = 'sprint-crud-board-001'"
                        )
                    )
            await conn.execute(
                sa_text("DELETE FROM boards WHERE id = 'sprint-crud-board-001'")
            )
            for table_name in known_scoped_tables & table_names:
                remaining_scoped = int(
                    (
                        await conn.execute(
                            sa_text(
                                f'SELECT COUNT(*) FROM "{table_name}" '
                                "WHERE board_id = 'sprint-crud-board-001'"
                            )
                        )
                    ).scalar_one()
                )
                if remaining_scoped:
                    raise RuntimeError(
                        "fixture FK repair left synthetic board-scoped rows"
                    )
            relational_changed = True
        elif card_rowids:
            await conn.execute(
                sa_text("UPDATE cards SET sprint_id = NULL WHERE rowid = :rowid"),
                [{"rowid": rowid} for rowid in card_rowids],
            )
            relational_changed = True
        if history_rowids:
            await conn.execute(
                sa_text("DELETE FROM sprint_history WHERE rowid = :rowid"),
                [{"rowid": rowid} for rowid in history_rowids],
            )
            relational_changed = True
        if dlq_rowids:
            await conn.execute(
                sa_text("DELETE FROM consolidation_dead_letter WHERE rowid = :rowid"),
                [{"rowid": rowid} for rowid in dlq_rowids],
            )
            relational_changed = True

        remaining = list((await conn.exec_driver_sql("PRAGMA foreign_key_check")).all())
        if remaining:
            raise RuntimeError("fixture FK repair did not converge to a clean database")

    graph_removed = _remove_known_fixture_graph_if_present(engine)
    return None if relational_changed or graph_removed else "skipped"


def _fixture_pollution_day_allowed(value: object) -> bool:
    day = str(value)[:10]
    return _FIXTURE_POLLUTION_FIRST_DAY <= day <= _FIXTURE_POLLUTION_LAST_DAY


def _remove_known_fixture_graph_if_present(engine: object) -> bool:
    """Remove the exact synthetic board graph only for a canonical Pulse home."""

    database = getattr(getattr(engine, "url", None), "database", None)
    if not database:
        return False
    database_path = Path(str(database)).expanduser().resolve()
    if database_path.name != "pulse.db" or database_path.parent.name != "data":
        return False

    boards_root = (database_path.parent.parent / "boards").resolve()
    fixture_dir = boards_root / "sprint-crud-board-001"
    if not fixture_dir.exists():
        return False
    if (
        fixture_dir.parent.resolve() != boards_root
        or fixture_dir.is_symlink()
        or (hasattr(os.path, "isjunction") and os.path.isjunction(fixture_dir))
        or not fixture_dir.is_dir()
    ):
        raise RuntimeError("fixture graph cleanup rejected an unsafe path")
    shutil.rmtree(fixture_dir)
    if fixture_dir.exists():
        raise RuntimeError("fixture graph cleanup did not remove the synthetic graph")
    return True


def _quality_c7_sqlite_trigger_manifest() -> dict[str, tuple[str, str]]:
    """Return permit-aware append-only guards installed after create_all."""

    board_permit = "kg_board_erasure_permits"
    subject_permit = "quality_assessment_subject_erasure_permits"

    def board_allowed(board_sql: str) -> str:
        return (
            "EXISTS (SELECT 1 "
            f'FROM "{board_permit}" AS board_permit '
            f"WHERE board_permit.board_id = {board_sql})"
        )

    def subject_allowed(
        board_sql: str,
        subject_type_sql: str,
        subject_id_sql: str,
    ) -> str:
        return (
            "EXISTS (SELECT 1 "
            f'FROM "{subject_permit}" AS subject_permit '
            f"WHERE subject_permit.board_id = {board_sql} "
            f"AND subject_permit.subject_type = {subject_type_sql} "
            f"AND subject_permit.subject_id = {subject_id_sql})"
        )

    direct_subject_allowed = (
        f"{board_allowed('OLD.board_id')} OR "
        f"{subject_allowed('OLD.board_id', 'OLD.subject_type', 'OLD.subject_id')}"
    )
    refinement_allowed = (
        board_allowed("OLD.board_id")
        + " OR "
        + subject_allowed(
            "OLD.board_id",
            "'refinement'",
            "OLD.refinement_id",
        )
    )
    derivation_allowed = (
        board_allowed("OLD.board_id")
        + " OR "
        + subject_allowed("OLD.board_id", "'spec'", "OLD.spec_id")
        + " OR "
        + subject_allowed(
            "OLD.board_id",
            "'refinement'",
            "OLD.source_refinement_id",
        )
    )
    board_only_allowed = board_allowed("OLD.board_id")
    manifest: dict[str, tuple[str, str]] = {}

    def add_guard(
        *,
        table: str,
        operation: str,
        allowed_delete_sql: str | None = None,
        trigger_name: str | None = None,
        message: str = "quality_c7_row_immutable",
    ) -> None:
        name = trigger_name or f"trg_quality_c7_{table}_immutable_{operation}"
        when = ""
        if operation == "delete" and allowed_delete_sql is not None:
            when = f"\nWHEN NOT ({allowed_delete_sql})"
        sql = (
            f'CREATE TRIGGER "{name}"\n'
            f'BEFORE {operation.upper()} ON "{table}"{when}\n'
            "BEGIN\n"
            f"    SELECT RAISE(ABORT, '{message}');\n"
            "END"
        )
        manifest[name] = (table, sql)

    for table in (
        "quality_assessment_receipts",
        "quality_findings",
        "quality_assessment_lifecycle_transitions",
        "quality_assessment_lifecycle_stale_transitions",
    ):
        add_guard(table=table, operation="update")
        add_guard(
            table=table,
            operation="delete",
            allowed_delete_sql=direct_subject_allowed,
        )

    receipt_join_allowed = (
        "EXISTS (SELECT 1 FROM quality_assessment_receipts AS receipt "
        "WHERE receipt.id = OLD.receipt_id AND ("
        f"{board_allowed('receipt.board_id')} OR "
        f"{subject_allowed('receipt.board_id', 'receipt.subject_type', 'receipt.subject_id')}"
        "))"
    )
    add_guard(table="quality_assessment_outbox", operation="update")
    add_guard(
        table="quality_assessment_outbox",
        operation="delete",
        allowed_delete_sql=receipt_join_allowed,
    )
    add_guard(table="quality_proposed_questions", operation="update")
    add_guard(
        table="quality_proposed_questions",
        operation="delete",
        allowed_delete_sql=receipt_join_allowed,
    )
    finding_join_allowed = (
        "EXISTS (SELECT 1 FROM quality_findings AS finding "
        "WHERE finding.id = OLD.finding_id "
        "AND finding.receipt_id = OLD.receipt_id AND ("
        f"{board_allowed('finding.board_id')} OR "
        f"{subject_allowed('finding.board_id', 'finding.subject_type', 'finding.subject_id')}"
        "))"
    )
    add_guard(table="quality_finding_qa_links", operation="update")
    add_guard(
        table="quality_finding_qa_links",
        operation="delete",
        allowed_delete_sql=finding_join_allowed,
    )

    for table in (
        "quality_assessment_legacy_import_runs",
        "quality_assessment_legacy_import_candidates",
        "quality_assessment_legacy_import_resolutions",
        "quality_assessment_legacy_import_completions",
    ):
        add_guard(table=table, operation="update")
        add_guard(
            table=table,
            operation="delete",
            allowed_delete_sql=board_only_allowed,
        )
    # The checkpoint is the sole mutable epoch row: progress advances through
    # guarded CAS updates, but deletion is still a board-erasure-only action.
    add_guard(
        table="quality_assessment_legacy_import_checkpoints",
        operation="delete",
        allowed_delete_sql=board_only_allowed,
    )

    # Replace the unconditional RDL DELETE triggers emitted when a table is
    # first created. UPDATE remains unconditionally blocked.
    for table in (
        "research_decision_entries",
        "research_decision_history",
        "research_decision_snapshots",
    ):
        add_guard(
            table=table,
            operation="delete",
            allowed_delete_sql=refinement_allowed,
            trigger_name=f"trg_{table}_immutable_delete",
            message="research_decision_entry_immutable",
        )
    add_guard(
        table="research_decision_derivations",
        operation="delete",
        allowed_delete_sql=derivation_allowed,
        trigger_name="trg_research_decision_derivations_immutable_delete",
        message="research_decision_entry_immutable",
    )

    add_guard(
        table="checklist_template_versions",
        operation="delete",
        message="checklist_row_immutable",
        trigger_name="trg_checklist_template_versions_immutable_delete",
    )
    add_guard(
        table="checklist_bindings",
        operation="delete",
        allowed_delete_sql=board_only_allowed,
        message="checklist_row_immutable",
        trigger_name="trg_checklist_bindings_immutable_delete",
    )
    add_guard(
        table="checklist_receipts",
        operation="delete",
        allowed_delete_sql=(
            board_allowed("OLD.board_id")
            + " OR "
            + subject_allowed("OLD.board_id", "'spec'", "OLD.spec_id")
        ),
        message="checklist_row_immutable",
        trigger_name="trg_checklist_receipts_immutable_delete",
    )
    checklist_item_allowed = (
        "EXISTS (SELECT 1 FROM checklist_receipts AS receipt "
        "WHERE receipt.id = OLD.receipt_id AND ("
        + board_allowed("receipt.board_id")
        + " OR "
        + subject_allowed(
            "receipt.board_id",
            "'spec'",
            "receipt.spec_id",
        )
        + "))"
    )
    add_guard(
        table="checklist_item_results",
        operation="delete",
        allowed_delete_sql=checklist_item_allowed,
        message="checklist_row_immutable",
        trigger_name="trg_checklist_item_results_immutable_delete",
    )
    return manifest


def research_decision_postgresql_ddl(
) -> tuple[str, dict[str, tuple[str, str, int]]]:
    """Return permit-aware PostgreSQL append-only guards for the RDL.

    Entry, history, snapshot, and derivation rows are immutable on every
    supported database. DELETE remains available only inside the explicit
    board/subject erasure protocols, matching the SQLite C7 contract.
    """

    function_name = "pulse_research_decision_immutable_guard"
    function_sql = f'''CREATE OR REPLACE FUNCTION "{function_name}"()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
            FROM "kg_board_erasure_permits" AS permit
            WHERE permit."board_id" = OLD."board_id"
        ) THEN
            RETURN OLD;
        END IF;
        IF TG_TABLE_NAME = 'research_decision_derivations' THEN
            IF EXISTS (
                SELECT 1
                FROM "quality_assessment_subject_erasure_permits" AS permit
                WHERE permit."board_id" = OLD."board_id"
                  AND (
                      (
                          permit."subject_type" = 'spec'
                          AND permit."subject_id" =
                              (to_jsonb(OLD) ->> 'spec_id')
                      )
                      OR (
                          permit."subject_type" = 'refinement'
                          AND permit."subject_id" =
                              (to_jsonb(OLD) ->> 'source_refinement_id')
                      )
                  )
            ) THEN
                RETURN OLD;
            END IF;
        ELSIF EXISTS (
            SELECT 1
            FROM "quality_assessment_subject_erasure_permits" AS permit
            WHERE permit."board_id" = OLD."board_id"
              AND permit."subject_type" = 'refinement'
              AND permit."subject_id" =
                  (to_jsonb(OLD) ->> 'refinement_id')
        ) THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'research_decision_entry_immutable';
END;
$$ LANGUAGE plpgsql'''
    tables = (
        ("entries", "research_decision_entries"),
        ("history", "research_decision_history"),
        ("snapshots", "research_decision_snapshots"),
        ("derivations", "research_decision_derivations"),
    )
    return function_sql, {
        f"trg_rdl_{suffix}_immutable": (
            table_name,
            "UPDATE OR DELETE",
            27,
        )
        for suffix, table_name in tables
    }


def audit_research_decision_postgresql_trigger_rows(
    rows: list[dict[str, object]],
    *,
    trigger_specs: dict[str, tuple[str, str, int]] | None = None,
) -> tuple[str, ...]:
    """Audit exact RDL trigger identity, operations, function and state."""

    expected = trigger_specs
    if expected is None:
        _function_sql, expected = research_decision_postgresql_ddl()
    existing = {str(row["trigger_name"]): row for row in rows}
    unexpected = set(existing) - set(expected)
    if unexpected:
        raise RuntimeError(
            "research decision ledger has unexpected PostgreSQL triggers: "
            + ", ".join(sorted(unexpected))
        )
    missing: list[str] = []
    for trigger_name, (
        table_name,
        _operation_clause,
        expected_type,
    ) in expected.items():
        observed = existing.get(trigger_name)
        if observed is None:
            missing.append(trigger_name)
            continue
        if (
            str(observed["table_name"]) != table_name
            or str(observed["function_name"])
            != "pulse_research_decision_immutable_guard"
            or int(observed["trigger_type"]) != expected_type
            or _postgresql_catalog_char(observed["trigger_enabled"]) != "O"
        ):
            raise RuntimeError(
                "research decision ledger PostgreSQL trigger is corrupt: "
                + trigger_name
            )
    return tuple(missing)


async def _migrate_quality_assessment_c7_schema() -> None:
    """Converge additive Q&A fields and permit-aware immutable ledgers."""

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    engine = get_engine()
    async with engine.begin() as conn:
        for table_name in (
            "ideation_qa_items",
            "refinement_qa_items",
            "spec_qa_items",
        ):
            columns = await conn.run_sync(
                lambda sync_conn, name=table_name: {
                    str(column["name"])
                    for column in sa_inspect(sync_conn).get_columns(name)
                }
            )
            for column_name, ddl in (
                ("revision", "INTEGER NOT NULL DEFAULT 1"),
                ("lifecycle", "VARCHAR(20) NOT NULL DEFAULT 'active'"),
                ("tombstoned", "BOOLEAN NOT NULL DEFAULT false"),
            ):
                if column_name not in columns:
                    await conn.execute(
                        sa_text(
                            f'ALTER TABLE "{table_name}" '
                            f'ADD COLUMN "{column_name}" {ddl}'
                        )
                    )
            await conn.execute(
                sa_text(
                    f'UPDATE "{table_name}" '
                    "SET revision = COALESCE(revision, 1), "
                    "lifecycle = CASE "
                    "WHEN COALESCE(tombstoned, false) "
                    "THEN 'tombstoned' ELSE COALESCE(lifecycle, 'active') END, "
                    "tombstoned = COALESCE(tombstoned, false)"
                )
            )

        if conn.dialect.name == "postgresql":
            function_sql, trigger_specs = research_decision_postgresql_ddl()
            await conn.execute(sa_text(function_sql))
            trigger_rows = list(
                (
                    await conn.execute(
                        sa_text(
                            """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       function.proname AS function_name,
       trigger.tgtype AS trigger_type,
       trigger.tgenabled AS trigger_enabled
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
WHERE NOT trigger.tgisinternal
  AND trigger.tgname LIKE :prefix
"""
                        ),
                        {"prefix": "trg_rdl_%"},
                    )
                )
                .mappings()
                .all()
            )
            missing = set(
                audit_research_decision_postgresql_trigger_rows(
                    trigger_rows,
                    trigger_specs=trigger_specs,
                )
            )
            for trigger_name, (
                table_name,
                operation_clause,
                _trigger_type,
            ) in trigger_specs.items():
                if trigger_name not in missing:
                    continue
                await conn.execute(
                    sa_text(
                        f'CREATE TRIGGER "{trigger_name}" '
                        f"BEFORE {operation_clause} "
                        f'ON "{table_name}" FOR EACH ROW '
                        "EXECUTE FUNCTION "
                        '"pulse_research_decision_immutable_guard"()'
                    )
                )
            return
        if conn.dialect.name != "sqlite":
            return

        manifest = _quality_c7_sqlite_trigger_manifest()
        for trigger_name, (_table_name, trigger_sql) in manifest.items():
            await conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
            await conn.exec_driver_sql(trigger_sql)

        installed = {
            str(row[0])
            for row in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            ).all()
        }
        missing = sorted(set(manifest) - installed)
        if missing:
            raise RuntimeError(f"quality C7 trigger convergence incomplete: {missing}")


SCHEMA_STEP_CALLABLES: dict[str, StepCallable] = {
    "_migrate_card_statuses": _migrate_card_statuses,
    "_migrate_add_priority_column": _migrate_add_priority_column,
    "_migrate_add_realm_id": _migrate_add_realm_id,
    "_migrate_add_comment_choice_columns": _migrate_add_comment_choice_columns,
    "_migrate_add_bug_card_columns": _migrate_add_bug_card_columns,
    "_migrate_add_task_requirement_gate_card_column": _migrate_add_task_requirement_gate_card_column,
    "_migrate_add_skip_rules_coverage": _migrate_add_skip_rules_coverage,
    "_migrate_add_skip_trs_coverage": _migrate_add_skip_trs_coverage,
    "_migrate_add_decisions_columns": _migrate_add_decisions_columns,
    "_migrate_decisions_default_false": _migrate_decisions_default_false,
    "_migrate_add_archive_columns": _migrate_add_archive_columns,
    "_migrate_add_spec_edition": _migrate_add_spec_edition,
    "_migrate_add_spec_validation_columns": _migrate_add_spec_validation_columns,
    "_migrate_add_ir_or_columns": _migrate_add_ir_or_columns,
    "_migrate_add_spec_validation_gate_columns": _migrate_add_spec_validation_gate_columns,
    "_migrate_add_ideation_skip_ambiguity_gate": _migrate_add_ideation_skip_ambiguity_gate,
    "_migrate_add_refinement_skip_ambiguity_gate": _migrate_add_refinement_skip_ambiguity_gate,
    "_migrate_heal_task_validation_field_names": _migrate_heal_task_validation_field_names,
    "_migrate_status_renames": _migrate_status_renames,
    "_migrate_add_permission_columns": _migrate_add_permission_columns,
    "_migrate_add_event_tables": _migrate_add_event_tables,
    "_migrate_add_consolidation_work_kinds": _migrate_add_consolidation_work_kinds,
    "_migrate_global_discovery_delivery_contract": (
        _migrate_global_discovery_delivery_contract
    ),
    "_migrate_cognitive_source_revision_ledger": (
        _migrate_cognitive_source_revision_ledger
    ),
    "_migrate_global_discovery_recovery_control_plane": (
        _migrate_global_discovery_recovery_control_plane
    ),
    "_migrate_story_ideation_single_link": _migrate_story_ideation_single_link,
    "_migrate_add_card_sprint_id": _migrate_add_card_sprint_id,
    "_migrate_add_card_knowledge_bases": _migrate_add_card_knowledge_bases,
    "_migrate_add_knowledge_source_columns": _migrate_add_knowledge_source_columns,
    "_migrate_add_kb_lineage_columns": _migrate_add_kb_lineage_columns,
    "_migrate_add_kb_governance_metadata": _migrate_add_kb_governance_metadata,
    "_migrate_knowledge_propagation_v2_schema": (
        _migrate_knowledge_propagation_v2_schema
    ),
    "_migrate_add_sprint_scope_fields": _migrate_add_sprint_scope_fields,
    "_migrate_add_sprint_lane_fields": _migrate_add_sprint_lane_fields,
    "_migrate_agent_boards": _migrate_agent_boards,
    "_migrate_add_task_validation_columns": _migrate_add_task_validation_columns,
    "_migrate_add_consolidation_resilience_columns": _migrate_add_consolidation_resilience_columns,
    "_migrate_add_kg_tick_boards_failed": _migrate_add_kg_tick_boards_failed,
    "_migrate_drop_spec_skills": _migrate_drop_spec_skills,
    "_migrate_add_default_config_snapshot": _migrate_add_default_config_snapshot,
    "_migrate_add_default_config_spec_checklist_mode": _migrate_add_default_config_spec_checklist_mode,
    "_migrate_add_agent_seen_board_id": _migrate_add_agent_seen_board_id,
    "_migrate_add_board_guideline_provenance": _migrate_add_board_guideline_provenance,
    "_migrate_add_cancellation_columns": _migrate_add_cancellation_columns,
    "_migrate_pagination_indices_and_positions": _migrate_pagination_indices_and_positions,
    "_migrate_ensure_guideline_binding_exact_authority_index": (
        _migrate_ensure_guideline_binding_exact_authority_index
    ),
    "_migrate_rebuild_guideline_import_candidates_semantic_shape": (
        _migrate_rebuild_guideline_import_candidates_semantic_shape
    ),
    "_migrate_rebuild_guideline_policy_v1_semantic_alignment": (
        _migrate_rebuild_guideline_policy_v1_semantic_alignment
    ),
    "_migrate_drop_retired_guideline_impact_v1_triggers": (
        _migrate_drop_retired_guideline_impact_v1_triggers
    ),
    "_migrate_repair_known_fixture_fk_orphans": _migrate_repair_known_fixture_fk_orphans,
    "_migrate_guideline_policy_lifecycle_substrate": (
        _migrate_guideline_policy_lifecycle_substrate
    ),
    "_migrate_guideline_impact_substrate": (_migrate_guideline_impact_substrate),
    "_migrate_guideline_policy_v1_schema": _migrate_guideline_policy_v1_schema,
    "_migrate_guideline_impact_v1_schema": (_migrate_guideline_impact_v1_schema),
    "_migrate_policy_compliance_v1_schema": (_migrate_policy_compliance_v1_schema),
    "_migrate_policy_waiver_v1_schema": _migrate_policy_waiver_v1_schema,
    "_migrate_semantic_guideline_governance_schema": (
        _migrate_semantic_guideline_governance_schema
    ),
    "_migrate_seed_semantic_configurations_for_legacy_bindings": (
        _migrate_seed_semantic_configurations_for_legacy_bindings
    ),
    "_migrate_recompute_cognitive_source_fingerprints_v2": (
        _migrate_recompute_cognitive_source_fingerprints_v2
    ),
    "_migrate_quality_assessment_c7_schema": _migrate_quality_assessment_c7_schema,
    "_migrate_agent_permissions": _migrate_agent_permissions,
}
