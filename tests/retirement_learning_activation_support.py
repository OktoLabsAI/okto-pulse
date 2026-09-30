"""Disposable installed-runtime verification for the Learning coordinator test."""

import asyncio
import subprocess
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import retirement_activation_installation as installation
from okto_pulse.community.adapters.retirement_runtime_admission import require_retirement_runtime_admission


async def assert_learning_candidate_activation(candidate, arguments, *, completion_arguments,
        result, tmp_path, monkeypatch):
    destination = tmp_path / 'learning-installation'
    with monkeypatch.context() as scoped:
        def interrupt_publication(*_):
            raise RuntimeError('interrupted before Learning activation publication')
        scoped.setattr(installation, '_publish', interrupt_publication)
        with pytest.raises(RuntimeError, match='interrupted before Learning activation'):
            await candidate.activate_retirement_graph_candidate(*arguments, destination, **completion_arguments)
    assert not destination.exists()
    assert not list(tmp_path.glob('.learning-installation.*.activation'))
    activated = await candidate.activate_retirement_graph_candidate(*arguments, destination, **completion_arguments)
    assert activated['state'] == 'activated'
    assert await installation.resume_retirement_activation(destination,
        expected_candidate_receipt_sha256=result['receipt_sha256'], confirm_installation_offline=True) == activated
    engine = create_async_engine(f'sqlite+aiosqlite:///{activated["database"]}')
    try:
        await require_retirement_runtime_admission(engine)
        # A new isolated process must import the installed pair, not a checkout.
        script = '''
import asyncio, site, sys, sysconfig
from pathlib import Path
sys.path.append(site.getusersitepackages())
import okto_pulse.core, okto_pulse.community
installed = Path(sysconfig.get_path('purelib')).resolve()
assert Path(okto_pulse.core.__file__).resolve().is_relative_to(installed)
assert Path(okto_pulse.community.__file__).resolve().is_relative_to(installed)
from okto_pulse.core import configure_settings, configure_storage
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
async def main():
    root = Path(sys.argv[1])
    settings = CommunitySettings(database_url=f'sqlite+aiosqlite:///{root / "database.sqlite3"}',
        data_dir=str(root), kg_base_dir=str(root / 'kg-artifacts'), upload_dir=str(root / 'uploads'),
        kg_embedding_mode='stub', kg_embedding_dim=384)
    configure_settings(settings)
    configure_storage(CommunityFileSystemStorage(settings.upload_dir))
    runtime = db.configure_community_database(settings.database_url)
    register_community_relational_schema_lifecycle()
    try:
        await db.init_db()
        print('Learning installation ready')
    finally:
        await runtime.close()
asyncio.run(main())
'''
        booted = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-I', '-c', script, str(destination)], capture_output=True, text=True, timeout=180)
        assert booted.returncode == 0, booted.stderr
        assert booted.stdout.strip().endswith('Learning installation ready')
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE cards SET title=title || ' after activation'")
        await require_retirement_runtime_admission(engine)
        proof = destination / 'retirement-activation/run.json'
        original = proof.read_bytes()
        try:
            proof.write_bytes(original + b' ')
            with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                await require_retirement_runtime_admission(engine)
        finally:
            proof.write_bytes(original)
    finally:
        await engine.dispose()
