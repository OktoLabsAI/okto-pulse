"""Installer-owned terminal proof, separate from mutable runtime contents.

The frozen candidate remains the proof of the initial graph/data state. An
activation checkpoint binds its receipts to the immutable migration journal.
Startup checks that chain, not an eternal hash of user data or graph pages.
"""

from dataclasses import dataclass
from pathlib import Path
import re

from .joint_recovery_snapshot import _explicit_path
from .retirement_candidate_checkpoint import _read_sealed
from .retirement_candidate_completion import require_candidate_projection_completion
from .retirement_data_journal import RetirementDataRun, _digest, read_retirement_data_journal


_FORMAT = 'retirement-activation/v1'
_PATHS = {'database': 'database.sqlite3', 'kg': 'kg-artifacts', 'storage': 'uploads'}
_KEYS = {'format', 'migration_id', 'input_sha256', 'journal_prefix_sha256',
    'candidate_receipt_sha256', 'seed_sha256', 'projection_inputs', 'builds',
    'original_backup', 'generation', 'installation_id', 'paths'}


def require_retirement_activation_roots(settings, *, upload_dir=None):
    """Prevent an activated SQL database from composing unrelated graph/storage roots."""
    from sqlalchemy.engine import make_url

    database_url = getattr(settings, 'database_url', None)
    if not database_url:
        return  # Graph-only composition has no SQL installation to adopt.
    url = make_url(database_url)
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        return
    database = Path(url.database).expanduser().resolve()
    if not (database.parent / 'retirement-activation').exists():
        return
    wanted = {'data_dir': database.parent, 'kg_base_dir': database.parent / _PATHS['kg'],
        'upload_dir': database.parent / _PATHS['storage']}
    if database.name != _PATHS['database'] or any(
            Path(getattr(settings, key)).expanduser().resolve() != path for key, path in wanted.items()):
        raise ValueError('retirement_activation_runtime_roots_mismatch')
    if upload_dir is not None and Path(upload_dir).expanduser().resolve() != wanted['upload_dir']:
        raise ValueError('retirement_activation_runtime_roots_mismatch')


@dataclass(frozen=True, slots=True)
class RetirementActivationCheckpoint:
    migration_id: str
    manifest_sha256: str

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or type(self.manifest_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', self.manifest_sha256) is None):
            raise ValueError('retirement_activation_checkpoint_invalid')


async def verify_retirement_activation(connection, database_path):
    """Read immutable installation evidence; permit subsequent domain writes.

    The caller has already checked journal structure and begun a read transaction.
    This validates SQL admission only; runtime composition must use these roots.
    """
    path = _explicit_path(Path(database_path))
    if path.name != _PATHS['database']:
        raise ValueError('retirement_activation_database_path_invalid')
    migrations = (await connection.exec_driver_sql(
        'SELECT DISTINCT migration_id FROM retirement_data_checkpoints LIMIT 2')).all()
    if len(migrations) != 1:
        raise ValueError('retirement_activation_scope_invalid')
    migration_id = migrations[0][0]
    first = (await connection.exec_driver_sql(
        'SELECT sha256 FROM retirement_data_checkpoints WHERE migration_id=? AND ordinal=0',
        (migration_id,))).scalar_one()
    run = RetirementDataRun(migration_id, first)
    records = await read_retirement_data_journal(connection, run)
    if len(records) != 10:
        raise ValueError('retirement_activation_incomplete')
    terminal = RetirementActivationCheckpoint(**records[9]['payload'])
    manifest = _read_sealed(_explicit_path(path.parent / 'retirement-activation'), terminal.manifest_sha256)
    if (set(manifest) != _KEYS or manifest['format'] != _FORMAT
            or manifest['migration_id'] != run.migration_id or manifest['input_sha256'] != run.input_sha256
            or manifest['journal_prefix_sha256'] != _digest(records[8]) or manifest['paths'] != _PATHS
            or type(manifest['installation_id']) is not str
            or re.fullmatch('[0-9a-f]{24}', manifest['installation_id']) is None):
        raise ValueError('retirement_activation_manifest_invalid')
    candidate = _read_sealed(_explicit_path(path.parent / 'candidate-receipt'), manifest['candidate_receipt_sha256'])
    if (candidate['format'] != 'retirement-native-candidate/v2'
            or candidate['seed_sha256'] != manifest['seed_sha256']
            or candidate['state'] != 'projected_not_reconciled'
            or any(route['generation'] != manifest['generation'] for route in candidate['routes'])):
        raise ValueError('retirement_activation_candidate_invalid')
    checkpoint = _read_sealed(_explicit_path(path.parent / 'candidate-checkpoint'), candidate['checkpoint_sha256'])
    if checkpoint['seed_sha256'] != manifest['seed_sha256']:
        raise ValueError('retirement_activation_candidate_invalid')
    projected = _read_sealed(_explicit_path(path.parent / 'projection-receipt'), candidate['projection_receipt_sha256'])
    if projected['seed_sha256'] != manifest['seed_sha256'] or projected['format'] != 'retirement-candidate-projection/v6':
        raise ValueError('retirement_activation_projection_invalid')
    # The complete plan was authenticated during installation. Keep its exact
    # bytes in the activated set rather than depending on an external temp path.
    projection = _read_sealed(_explicit_path(path.parent / 'activation-projection-inputs'), manifest['projection_inputs'])
    if (projection['migration_builds'] != manifest['builds']
            or projection['original_backup'] != manifest['original_backup']):
        raise ValueError('retirement_activation_source_invalid')
    require_candidate_projection_completion(projection, projected['graph_reconciliation'])
    return manifest
