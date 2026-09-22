"""Observe private, offline candidate records against authenticated history.

The caller holds the retirement fences and seals the returned evidence with the
candidate checkpoint. Observations never waive source, active-set or orphan
reconciliation, and do not authorize historical removal or runtime admission.
"""

from contextlib import closing
import hashlib

from okto_grafx import connect

from .logical_transfer_factories import make_grafx_logical_source
from .recovery_graph_inventory import read_recovery_graph_inventory
from .relational_recovery_snapshot import _check_time, _deadline, _readonly, _sidecars_absent
from .retirement_historical_graph_census import (
    compare_graph_record_censuses, read_graph_record_census,
    read_retirement_historical_graph_census,
)
from .sprint_retirement_archive import _encode

_LIMIT = 64 * 1024 * 1024


def observe_candidate_history(target, snapshot, *, max_seconds=180):
    """Read every routed scope, including Global Discovery and unchanged nodes."""
    deadline = _deadline(max_seconds)
    previous, previous_digest = read_retirement_historical_graph_census(snapshot, max_seconds=max_seconds)
    sql = target / 'database.sqlite3'
    kg = target / 'kg-artifacts'
    _sidecars_absent(sql)
    graphs, budget = [], [0]
    with closing(_readonly(sql, immutable=True)) as connection:
        connection.execute('BEGIN')
        inventory = read_recovery_graph_inventory(connection, kg)
        inventory.require_resolved_routes()
        for route in inventory.routes:
            _check_time(deadline)
            if route.state != 'bound':
                continue
            with connect(route.physical_path, page_size=route.page_size, read_only=True) as database:
                reader = make_grafx_logical_source(database, scope=route.scope).open_snapshot()
                try:
                    _, records = read_graph_record_census(reader, deadline=deadline, budget=budget)
                finally:
                    reader.close()
                graphs.append({'scope': route.scope, 'board_id': route.board_id,
                    'generation': route.generation, 'binding_sha256': route.binding_sha256,
                    'database_uuid': database.identity.database_uuid.hex(), **records})
        if read_recovery_graph_inventory(connection, kg) != inventory:
            raise ValueError('retirement_candidate_history_routes_changed')
        connection.execute('ROLLBACK')
    _sidecars_absent(sql)
    current = {'format': 'retirement-candidate-record-census/v1', 'graphs': graphs}
    encoded = _encode(current)
    if len(encoded) > _LIMIT:
        raise ValueError('retirement_candidate_history_limit')
    observations = compare_graph_record_censuses(previous['graphs'], graphs, deadline=deadline)
    result = {'format': 'retirement-candidate-history-observations/v2',
        'state': 'observed_not_classified', 'snapshot_sha256': snapshot.manifest_sha256,
        'before_census_sha256': previous_digest,
        'candidate_census_sha256': hashlib.sha256(encoded).hexdigest(), 'graphs': observations}
    if len(_encode(result)) > _LIMIT:
        raise ValueError('retirement_candidate_history_limit')
    _check_time(deadline)
    return result
