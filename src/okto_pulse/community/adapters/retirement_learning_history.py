"""Bounded private history views for ordered candidate Learning executions.

The coordinator supplies an authenticated complete baseline and exact SQL
append batches. This adapter implements reads only; it does not issue evidence,
write source history, authenticate sessions or certify candidate completion.
"""
from copy import deepcopy
from dataclasses import asdict
import json

from okto_pulse.core.ports.kg_cognitive_source import CognitiveSourceRecord, latest_cognitive_source_records
from okto_pulse.core.ports.learning_reconciliation import require_learning_reconciliation_source_append


class CandidateLearningHistory:
    def __init__(self, records):
        if (type(records) is not tuple or len(records) > 100_000
                or any(type(row) is not CognitiveSourceRecord for row in records)):
            raise ValueError('retirement_learning_history_invalid')
        size, revisions = 0, set()
        for row in records:
            identity = row.board_id, row.node_id, row.generation, row.source_revision
            if identity in revisions:
                raise ValueError('retirement_learning_history_revision_duplicate')
            revisions.add(identity)
            size += len(json.dumps(asdict(row), ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if size > 64 * 1024 * 1024:
                raise ValueError('retirement_learning_history_limit')
        latest_cognitive_source_records(records)
        self._records = deepcopy(records)
        self._histories = {}
        for row in self._records:
            key = row.board_id, row.node_id, row.generation
            self._histories.setdefault(key, []).append(row)
        for key, rows in self._histories.items():
            self._histories[key] = tuple(sorted(rows, key=lambda row: row.source_revision))

    async def read_history_in_context(self, context, *, board_id, node_id, generation):
        return deepcopy(self._histories.get((board_id, node_id, generation), ()))

    async def read_fingerprint_in_context(self, context, *, board_id, node_id, generation, fingerprint):
        rows = self._histories.get((board_id, node_id, generation), ())
        matches = [row for row in rows if row.record_fingerprint == fingerprint]
        if len(matches) > 1:
            raise ValueError('retirement_learning_history_fingerprint_ambiguous')
        return deepcopy(matches[0]) if matches else None

    async def verify_append(self, *, appended, execution):
        """Return the next prefix only after validating the entire atomic batch."""
        if (type(appended) is not tuple or not 1 <= len(appended) <= 2
                or any(type(row) is not CognitiveSourceRecord for row in appended)):
            raise ValueError('retirement_learning_history_append_invalid')
        identities = set()
        for row in appended:
            key = row.board_id, row.node_id, row.generation
            history = self._histories.get(key, ())
            if (key in identities or not history or row.source_revision != history[-1].source_revision + 1):
                raise ValueError('retirement_learning_history_append_not_next')
            identities.add(key)
        following = CandidateLearningHistory((*self._records, *appended))
        await require_learning_reconciliation_source_append(None, following,
            appended=appended, execution=execution)
        return following
