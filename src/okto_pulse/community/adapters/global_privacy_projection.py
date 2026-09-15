"""Engine-neutral canonical privacy-survivor projection and replay."""

from __future__ import annotations
from datetime import datetime, timezone
import json
import hashlib
from typing import Any, Callable

_PRIVACY_SNAPSHOT_VERSION = 3
_PRIVACY_ROW_WIDTHS = {
    "boards": 8,
    "topics": 1,
    "entities": 1,
    "digests": 10,
    "has_topic": 2,
    "mentions_entity": 2,
    "contains_decision": 2,
    "decision_mentions_entity": 2,
    "decision_derives_from": 2,
}


class GlobalPrivacyProjection:
    @staticmethod
    def _privacy_snapshot_value(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [
                GlobalPrivacyProjection._privacy_snapshot_value(item) for item in value
            ]
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return GlobalPrivacyProjection._privacy_snapshot_value(tolist())
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return isoformat()
        return value

    @staticmethod
    def _privacy_row_sort_key(row: list[Any]) -> str:
        return json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _canonical_privacy_rows(
        cls,
        rows: dict[str, Any],
        *,
        survivor_board_ids: set[str],
    ) -> dict[str, list[list[Any]]]:
        """Validate, fence and canonicalize journal rows.

        Topic and Entity aggregates intentionally retain only their stable IDs.
        Their names, aliases, embeddings and counts are cross-board aggregates,
        so copying those values could preserve a deleted board's contribution.
        The topology is retained with redacted placeholders until normal
        clustering rematerializes aggregate properties from survivor sources.
        """

        normalized: dict[str, list[list[Any]]] = {
            key: [] for key in _PRIVACY_ROW_WIDTHS
        }
        for key, width in _PRIVACY_ROW_WIDTHS.items():
            raw_rows = rows.get(key, [])
            if not isinstance(raw_rows, list):
                raise RuntimeError(
                    f"global_discovery_privacy_survivor_snapshot_invalid rows={key}"
                )
            seen: set[str] = set()
            for raw_row in raw_rows:
                if not isinstance(raw_row, (list, tuple)):
                    raise RuntimeError(
                        f"global_discovery_privacy_survivor_snapshot_invalid row={key}"
                    )
                # Version-1 journals stored full Topic/Entity aggregates.
                # Only the stable identity is safe to carry forward.
                row = list(raw_row[:1] if key in {"topics", "entities"} else raw_row)
                if len(row) != width:
                    raise RuntimeError(
                        "global_discovery_privacy_survivor_snapshot_invalid "
                        f"shape={key}:{len(row)}"
                    )
                row = [cls._privacy_snapshot_value(value) for value in row]
                identity = cls._privacy_row_sort_key(row)
                if identity in seen:
                    continue
                seen.add(identity)
                normalized[key].append(row)

        board_rows: dict[str, list[Any]] = {}
        for row in normalized["boards"]:
            identity = str(row[0])
            if identity not in survivor_board_ids:
                continue
            if identity in board_rows:
                raise RuntimeError(
                    "global_discovery_privacy_survivor_snapshot_invalid "
                    f"duplicate_board={identity}"
                )
            board_rows[identity] = row
        live_board_ids = set(board_rows)

        digest_rows: dict[str, list[Any]] = {}
        digest_owners: dict[str, str] = {}
        for row in normalized["digests"]:
            digest_id = str(row[0])
            owner = str(row[1])
            if owner not in live_board_ids:
                continue
            if digest_id in digest_rows:
                raise RuntimeError(
                    "global_discovery_privacy_survivor_snapshot_invalid "
                    f"duplicate_digest={digest_id}"
                )
            digest_rows[digest_id] = row
            digest_owners[digest_id] = owner
        live_digest_ids = set(digest_rows)

        def _relation_rows(
            key: str,
            predicate: Callable[[str, str], bool],
        ) -> list[list[Any]]:
            return [
                row for row in normalized[key] if predicate(str(row[0]), str(row[1]))
            ]

        has_topic = _relation_rows(
            "has_topic",
            lambda board, _topic: board in live_board_ids,
        )
        mentions_entity = _relation_rows(
            "mentions_entity",
            lambda board, _entity: board in live_board_ids,
        )
        contains_decision = _relation_rows(
            "contains_decision",
            lambda board, digest: (
                board in live_board_ids
                and digest in live_digest_ids
                and digest_owners[digest] == board
            ),
        )
        decision_mentions_entity = _relation_rows(
            "decision_mentions_entity",
            lambda digest, _entity: digest in live_digest_ids,
        )
        decision_derives_from = _relation_rows(
            "decision_derives_from",
            lambda source, target: (
                source in live_digest_ids and target in live_digest_ids
            ),
        )

        topic_ids = {str(row[1]) for row in has_topic}
        entity_ids = {
            str(row[1]) for row in (*mentions_entity, *decision_mentions_entity)
        }
        declared_topic_ids = {str(row[0]) for row in normalized["topics"]}
        declared_entity_ids = {str(row[0]) for row in normalized["entities"]}
        if not topic_ids.issubset(declared_topic_ids):
            raise RuntimeError(
                "global_discovery_privacy_survivor_snapshot_invalid missing_topic"
            )
        if not entity_ids.issubset(declared_entity_ids):
            raise RuntimeError(
                "global_discovery_privacy_survivor_snapshot_invalid missing_entity"
            )

        topic_counts = {
            board_id: len({str(row[1]) for row in has_topic if str(row[0]) == board_id})
            for board_id in live_board_ids
        }
        entity_counts = {
            board_id: len(
                {str(row[1]) for row in mentions_entity if str(row[0]) == board_id}
            )
            for board_id in live_board_ids
        }
        decision_counts = {
            board_id: sum(1 for owner in digest_owners.values() if owner == board_id)
            for board_id in live_board_ids
        }
        for board_id, row in board_rows.items():
            row[4] = topic_counts[board_id]
            row[5] = entity_counts[board_id]
            row[6] = decision_counts[board_id]

        canonical = {
            "boards": list(board_rows.values()),
            "topics": [[identity] for identity in topic_ids],
            "entities": [[identity] for identity in entity_ids],
            "digests": list(digest_rows.values()),
            "has_topic": has_topic,
            "mentions_entity": mentions_entity,
            "contains_decision": contains_decision,
            "decision_mentions_entity": decision_mentions_entity,
            "decision_derives_from": decision_derives_from,
        }
        for key, values in canonical.items():
            canonical[key] = sorted(
                values,
                key=cls._privacy_row_sort_key,
            )
        return canonical

    @classmethod
    def _build_privacy_survivor_snapshot(
        cls,
        *,
        board_id: str,
        rows: dict[str, Any],
        survivor_board_ids: set[str],
    ) -> dict[str, Any]:
        canonical = cls._canonical_privacy_rows(
            rows,
            survivor_board_ids=survivor_board_ids,
        )
        encoded_rows = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            "version": _PRIVACY_SNAPSHOT_VERSION,
            "target_board_id": board_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "survivor_board_ids": sorted(survivor_board_ids),
            "rows": canonical,
            "manifest": {
                "counts": {key: len(value) for key, value in canonical.items()},
                "sha256": hashlib.sha256(encoded_rows).hexdigest(),
            },
        }

    @classmethod
    def _validate_privacy_survivor_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        board_id: str,
    ) -> None:
        if (
            snapshot.get("version") != _PRIVACY_SNAPSHOT_VERSION
            or snapshot.get("target_board_id") != board_id
            or not isinstance(snapshot.get("rows"), dict)
            or not isinstance(snapshot.get("manifest"), dict)
        ):
            raise RuntimeError(
                f"global_discovery_privacy_survivor_snapshot_invalid board={board_id}"
            )
        declared_survivors = snapshot.get("survivor_board_ids")
        if not isinstance(declared_survivors, list) or not all(
            isinstance(value, str) for value in declared_survivors
        ):
            raise RuntimeError(
                "global_discovery_privacy_survivor_snapshot_invalid "
                f"authority={board_id}"
            )
        canonical = cls._canonical_privacy_rows(
            snapshot["rows"],
            survivor_board_ids=set(declared_survivors),
        )
        if canonical != snapshot["rows"]:
            raise RuntimeError(
                "global_discovery_privacy_survivor_snapshot_invalid "
                f"canonical={board_id}"
            )
        encoded_rows = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_manifest = {
            "counts": {key: len(value) for key, value in canonical.items()},
            "sha256": hashlib.sha256(encoded_rows).hexdigest(),
        }
        if snapshot["manifest"] != expected_manifest:
            raise RuntimeError(
                "global_discovery_privacy_survivor_snapshot_invalid "
                f"manifest={board_id}"
            )

    @classmethod
    def _merge_privacy_survivor_rows(
        cls,
        *,
        journal_rows: dict[str, list[list[Any]]],
        current_rows: dict[str, list[list[Any]]],
    ) -> dict[str, list[list[Any]]]:
        """Prefer current rows for materialized boards, retain crash survivors."""

        current_board_ids = {str(row[0]) for row in current_rows["boards"]}
        journal_digest_owners = {
            str(row[0]): str(row[1]) for row in journal_rows["digests"]
        }

        def _merge_owned(
            key: str,
            owner: Callable[[list[Any]], str | None],
        ) -> list[list[Any]]:
            retained = [
                row for row in journal_rows[key] if owner(row) not in current_board_ids
            ]
            return [*retained, *current_rows[key]]

        merged = {
            "boards": _merge_owned("boards", lambda row: str(row[0])),
            "digests": _merge_owned("digests", lambda row: str(row[1])),
            "has_topic": _merge_owned("has_topic", lambda row: str(row[0])),
            "mentions_entity": _merge_owned(
                "mentions_entity",
                lambda row: str(row[0]),
            ),
            "contains_decision": _merge_owned(
                "contains_decision",
                lambda row: str(row[0]),
            ),
            "decision_mentions_entity": _merge_owned(
                "decision_mentions_entity",
                lambda row: journal_digest_owners.get(str(row[0])),
            ),
            "decision_derives_from": [
                row
                for row in journal_rows["decision_derives_from"]
                if (
                    journal_digest_owners.get(str(row[0])) not in current_board_ids
                    and journal_digest_owners.get(str(row[1])) not in current_board_ids
                )
            ]
            + current_rows["decision_derives_from"],
            # Aggregate identities are recomputed below from retained topology.
            "topics": [
                *journal_rows["topics"],
                *current_rows["topics"],
            ],
            "entities": [
                *journal_rows["entities"],
                *current_rows["entities"],
            ],
        }
        return merged

    @staticmethod
    def _timestamp_expression(value: Any, parameter_name: str) -> str:
        return f"timestamp(${parameter_name})" if value not in (None, "") else "NULL"

    def _restore_privacy_survivor_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> dict[str, int]:
        rows = snapshot["rows"]

        for row in rows["boards"]:
            params = {
                "board_id": row[0],
                "name": row[1],
                "summary": row[2],
                "summary_embedding": row[3],
                "topic_count": row[4],
                "entity_count": row[5],
                "decision_count": row[6],
                "last_sync_at": row[7],
            }
            last_sync = self._timestamp_expression(row[7], "last_sync_at")
            self.execute(
                "CREATE (n:Board {board_id: $board_id, name: $name, "
                "summary: $summary, summary_embedding: $summary_embedding, "
                "topic_count: $topic_count, entity_count: $entity_count, "
                "decision_count: $decision_count, "
                f"last_sync_at: {last_sync}}})",
                params,
            )
        for row in rows["digests"]:
            params = {
                "id": row[0],
                "board_id": row[1],
                "original_node_id": row[2],
                "title": row[3],
                "summary": row[4],
                "node_type": row[5],
                "graph_layer": row[6],
                "source_revoked": bool(row[7]),
                "embedding": row[8],
                "created_at": row[9],
            }
            created = self._timestamp_expression(row[9], "created_at")
            self.execute(
                "CREATE (n:DecisionDigest {id: $id, board_id: $board_id, "
                "original_node_id: $original_node_id, title: $title, "
                "one_line_summary: $summary, node_type: $node_type, "
                "graph_layer: $graph_layer, source_revoked: $source_revoked, "
                f"embedding: $embedding, created_at: {created}}})",
                params,
            )
        for row in rows["topics"]:
            # Aggregate properties are intentionally redacted.  The stable
            # identity keeps survivor topology connected until clustering
            # rematerializes names, centroids and counts from live sources.
            self.execute(
                "CREATE (n:Topic {id: $id})",
                {"id": row[0]},
            )
        for row in rows["entities"]:
            self.execute(
                "CREATE (n:Entity {id: $id})",
                {"id": row[0]},
            )

        relation_specs = (
            (
                "has_topic",
                "Board",
                "board_id",
                "HAS_TOPIC",
                "Topic",
                "id",
                False,
            ),
            (
                "mentions_entity",
                "Board",
                "board_id",
                "MENTIONS_ENTITY",
                "Entity",
                "id",
                False,
            ),
            (
                "contains_decision",
                "Board",
                "board_id",
                "CONTAINS_DECISION",
                "DecisionDigest",
                "id",
                False,
            ),
            (
                "decision_mentions_entity",
                "DecisionDigest",
                "id",
                "DECISION_MENTIONS_ENTITY",
                "Entity",
                "id",
                False,
            ),
            (
                "decision_derives_from",
                "DecisionDigest",
                "id",
                "DECISION_DERIVES_FROM",
                "DecisionDigest",
                "id",
                False,
            ),
        )
        for (
            row_key,
            from_type,
            from_key,
            relation_type,
            to_type,
            to_key,
            weighted,
        ) in relation_specs:
            for row in rows[row_key]:
                relationship = (
                    f"[:{relation_type} {{weight: $weight}}]"
                    if weighted
                    else f"[:{relation_type}]"
                )
                params = {"from_id": row[0], "to_id": row[1]}
                if weighted:
                    params["weight"] = row[2]
                self.execute(
                    f"MATCH (a:{from_type} {{{from_key}: $from_id}}), "
                    f"(b:{to_type} {{{to_key}: $to_id}}) "
                    f"CREATE (a)-{relationship}->(b)",
                    params,
                )

        return {
            "boards": len(rows["boards"]),
            "topics": len(rows["topics"]),
            "entities": len(rows["entities"]),
            "digests": len(rows["digests"]),
            "relationships": sum(
                len(rows[key])
                for key in (
                    "has_topic",
                    "mentions_entity",
                    "contains_decision",
                    "decision_mentions_entity",
                    "decision_derives_from",
                )
            ),
        }
