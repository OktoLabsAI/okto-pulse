"""Paired, immutable-source acceptance selectors for Pulse's unittest observer.

This file is deliberately outside pytest's automatic ``test_*.py`` discovery.
The host registers one explicit method per Spec scenario. Each method runs real
pytest/React suites in a disposable, lock-resolved environment; an empty,
skipped, failed or unreported suite cannot produce a successful outer test.
No installed Pulse package, live database or lifecycle status is modified.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


class PairedAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        roots = {role: Path(os.environ[f'OKTO_PULSE_{role.upper()}_REPO']).resolve(strict=True)
                 for role in ('core', 'community')}
        cls.sandbox = tempfile.TemporaryDirectory(prefix='pulse-v043-acceptance-')
        cls.addClassCleanup(cls.sandbox.cleanup)
        cls.root = Path(cls.sandbox.name)
        cls.repos = {}
        for role, source in roots.items():
            target = cls.root / ('okto-pulse-core' if role == 'core' else 'okto-pulse')
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(
                '.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.vite'))
            cls.repos[role] = target
        cls.environment = dict(os.environ)
        cls.environment.pop('PYTHONPATH', None)
        cls.environment.pop('VIRTUAL_ENV', None)
        cls.environment['UV_PROJECT_ENVIRONMENT'] = str(cls.root / 'venv')
        cls.environment['UV_PYTHON_DOWNLOADS'] = 'never'
        for role, target in cls.repos.items():
            cls.environment[f'OKTO_PULSE_{role.upper()}_REPO'] = str(target)
        cls._run(['uv', 'sync', '--frozen', '--extra', 'dev', '--python', sys.executable],
                 cls.repos['community'], timeout=600)
        cls.python = cls.root / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        cls.frontend_ready = False

    @classmethod
    def _run(cls, arguments, cwd, *, timeout=300):
        executable = shutil.which(arguments[0])
        if not executable:
            raise AssertionError(f'Required acceptance tool is unavailable: {arguments[0]}')
        result = subprocess.run([executable, *map(str, arguments[1:])], cwd=cwd,
                                env=cls.environment, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
        if result.returncode:
            raise AssertionError(f'Acceptance command failed ({arguments[0]}):\n{result.stdout[-6000:]}')
        return result

    def _pytest(self, role, *selectors):
        report = self.root / f'{role}-pytest.xml'
        report.unlink(missing_ok=True)
        options = ['--noconftest'] if role == 'core' else []
        self._run([str(self.python), '-m', 'pytest', *selectors, *options,
                   '-q', '--tb=short', f'--junitxml={report}'], self.repos[role])
        suites = ET.parse(report).getroot()
        cases = list(suites.iter('testcase'))
        self.assertGreater(len(cases), 0, 'The selected acceptance suite must execute tests')
        for case in cases:
            for rejected in ('failure', 'error', 'skipped'):
                self.assertIsNone(case.find(rejected), f'{case.attrib} contains {rejected}')

    def _react(self, *selectors):
        frontend = self.repos['community'] / 'frontend'
        if not self.frontend_ready:
            self._run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], frontend)
            type(self).frontend_ready = True
        report = self.root / 'react-results.json'
        report.unlink(missing_ok=True)
        self._run(['node', 'node_modules/vitest/vitest.mjs', 'run', *selectors,
                   '--maxWorkers=1', '--reporter=json', f'--outputFile={report}'], frontend)
        results = json.loads(report.read_text(encoding='utf-8'))
        self.assertTrue(results['success'])
        self.assertGreater(results['numTotalTests'], 0)
        self.assertEqual(results['numPassedTests'], results['numTotalTests'])
        self.assertEqual(results.get('numPendingTests', 0), 0)
        self.assertEqual(results.get('numTodoTests', 0), 0)
        self.assertEqual(results['numFailedTests'], 0)


class TaskRequirementAcceptance(PairedAcceptance):
    def test_shared_requirement_keeps_other_owner_required(self):
        self._pytest('core', 'tests/test_task_requirement_decision.py')
        self._pytest('community', 'tests/test_task_requirement_effects.py')

    def test_human_mode_separates_requirement_from_proof(self):
        self._pytest('community', 'tests/test_task_requirement_routes.py')
        self._react('src/components/code-traceability/__tests__/TaskRequirementControl.test.tsx',
                    'src/components/code-traceability/__tests__/CardDeliveryDoDPanel.test.tsx')

    def test_blank_note_keeps_authenticated_audit(self):
        self._pytest('community', 'tests/test_task_requirement_commands.py',
                     'tests/test_task_requirement_decision_store.py')

    def test_atomic_replay_and_failure(self):
        self._pytest('community', 'tests/test_task_requirement_commands.py',
                     'tests/test_task_requirement_routes.py')
        self._react('src/components/code-traceability/__tests__/TaskRequirementControl.test.tsx')

    def test_restore_never_recovers_revoked_proof(self):
        self._pytest('community', 'tests/test_task_requirement_effects.py',
                     'tests/test_task_requirement_commands.py')

    def test_central_and_contextual_help(self):
        self._react('src/components/help/__tests__/HelpPanel.taskRequirements.test.tsx',
                    'src/components/help/__tests__/ContextualHelpLink.test.tsx',
                    'src/components/code-traceability/__tests__/TaskRequirementControl.test.tsx',
                    'src/components/code-traceability/__tests__/TaskRequirementHistory.test.tsx')

    def test_authorization_storage_upgrade_and_rollback(self):
        self._pytest('community', 'tests/test_task_requirement_commands.py',
                     'tests/test_task_requirement_routes.py', 'tests/test_task_requirement_decision_store.py',
                     'tests/test_task_requirement_erasure.py', 'tests/test_storage_upgrade_v043.py')


class DeliveryAcceptance(PairedAcceptance):
    def test_shared_obligation_has_one_global_denominator(self):
        self._pytest('core',
            'tests/test_delivery_summary.py::test_shared_obligation_denominator_is_never_a_sum_of_card_metrics',
            'tests/test_delivery_summary.py::test_pages_keep_full_counts_and_load_only_the_requested_visible_items')
        self._pytest('community',
            'tests/test_readiness_projection_access.py::test_delivery_summary_reads_one_generation_and_live_metadata_without_proof_scans')

    def test_validation_stays_pending_and_default_view_is_simple(self):
        self._pytest('core',
            'tests/test_delivery_summary.py::test_lifecycle_does_not_turn_one_hundred_percent_into_delivered')
        self._react('src/components/code-traceability/__tests__/DeliverySummaryPanel.test.tsx')

    def test_historical_done_preserves_history_without_current_delivery_credit(self):
        self._pytest('core',
            'tests/test_delivery_summary.py::test_historical_done_with_insufficient_or_unknown_proof_stays_pending',
            'tests/test_delivery_summary.py::test_legacy_completion_with_exception_remains_pending_confirmation_without_proof_credit',
            'tests/test_delivery_summary.py::test_noncurrent_generation_never_confirms_done_or_current_exceptions',
            'tests/test_delivery_summary.py::test_card_version_or_status_drift_cannot_reuse_a_done_assessment')
        self._pytest('community', 'tests/test_delivery_readiness_historical.py')

    def test_optional_waived_and_denied_reads_never_become_delivered_or_empty_success(self):
        self._pytest('core',
            'tests/test_delivery_summary.py::test_current_task_exception_has_its_own_collapsed_group_and_no_delivery_count',
            'tests/test_delivery_summary.py::test_stale_task_exception_does_not_excuse_a_changed_scope',
            'tests/test_delivery_summary.py::test_missing_or_partial_population_is_unknown_rather_than_successful_empty')
        self._pytest('community',
            'tests/test_readiness_projection_access.py::test_delivery_summary_rechecks_revoked_authority_instead_of_returning_an_empty_list',
            'tests/test_readiness_projection_access.py::test_summary_captures_human_requirement_and_restores_it_with_the_exact_generation_manifest',
            'tests/test_readiness_projection_access.py::test_delivery_summary_metadata_capacity_is_unknown_and_never_changes_the_denominator')

    def test_delivery_help_is_central_contextual_and_keeps_focus(self):
        self._react('src/components/help/__tests__/HelpPanel.deliveryStatus.test.tsx',
            'src/components/help/__tests__/ContextualHelpLink.test.tsx',
            'src/components/code-traceability/__tests__/DeliverySummaryPanel.test.tsx')

    def test_delivery_contract_authority_cursor_and_legacy_copy_upgrade(self):
        self._pytest('core', 'tests/test_delivery_summary_read.py', 'tests/test_delivery_readiness_contracts.py')
        self._pytest('community', 'tests/test_delivery_summary_cursor.py',
            'tests/test_delivery_summary_route.py',
            'tests/test_readiness_projection_access.py::test_summary_signed_pages_reauthorize_and_refuse_live_metadata_changes_within_one_generation')
        self._pytest('community', 'tests/test_readiness_schema_upgrade.py', 'tests/test_storage_upgrade_v043.py')
        self._pytest('community',
            'tests/test_readiness_projection_worker.py::test_backfill_after_v401_upgrade_completes_all_scopes_and_three_restarts',
            'tests/test_readiness_projection_worker.py::test_real_community_lifespan_serves_pending_then_current_on_upgraded_copy')
