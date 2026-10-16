"""Run BASE T39/T40 against installed packages, without pytest checkout hooks.

Usage: python -I tests/installed_completion_runner.py NEW_DISPOSABLE_DIRECTORY
The directory must not exist. No product services or existing data are touched.
"""
import asyncio
import json
from pathlib import Path
import site
import sys

# The qualification venv inherits third-party dependencies. Append them after
# its installed wheels; verify every loaded Pulse module's origin below.
sys.path.append(site.getusersitepackages())
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_completion_authenticated_delivery as scenario  # noqa: E402
from okto_pulse.core.runtime_context import RuntimeValueRegistry, runtime_value_scope  # noqa: E402


async def run(root):
    results = []
    for substantive in (False, True):
        directory = root / ('substantive' if substantive else 'projection_only')
        directory.mkdir(parents=True)
        with runtime_value_scope(registry=RuntimeValueRegistry()):
            fixture = scenario.ledger.__wrapped__(directory)
            value = await anext(fixture)
            try:
                with pytest.MonkeyPatch.context() as patch:
                    await scenario.test_signed_test_closeout_separates_product_proof_from_projection(
                        value, directory, patch, substantive)
            finally:
                await fixture.aclose()
        results.append({'substantive': substantive, 'passed': True})
    origins = {}
    for name, module in tuple(sys.modules.items()):
        if not name.startswith('okto_pulse.') or not getattr(module, '__file__', None):
            continue
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix).resolve()), (name, str(path))
        origins[name] = str(path)
    return {'cases': results, 'python': sys.executable, 'isolated': sys.flags.isolated,
            'installed_module_origins': origins}


if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=False)
    report = asyncio.run(run(root))
    print(json.dumps(report, indent=2))
