"""Native evidence entry points; execute tests from paired immutable snapshots.

The host runner supplies the Core/Community roots. JavaScript packages are the
local toolchain, shared read-only; application and test sources come from the
snapshot. Direct My Board browser observations complement these automated tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


DEPENDENCIES = Path(r"D:\Projetos\Techridy\okto-pulse\frontend\node_modules")
FRONT = "src/components/code-traceability/__tests__/"
KANBAN = "src/components/kanban/__tests__/"


class TaskDeliveryScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roots = {role: Path(os.environ[f"OKTO_PULSE_{role.upper()}_REPO"]).resolve()
                     for role in ("core", "community")}
        if cls.roots['community'] != Path(__file__).resolve().parents[1]:
            raise AssertionError('Community tests must come from the declared snapshot')

    def _pytest(self, role, *selectors):
        roots = [str(self.roots[r] / 'src') for r in ('core', 'community')]
        roots.append(str(self.roots[role] / 'tests'))
        bootstrap = 'import sys,pytest; sys.path[:0]=' + repr(roots) + '; raise SystemExit(pytest.main(sys.argv[1:]))'
        result = subprocess.run([sys.executable, '-E', '-P', '-B', '-c', bootstrap,
            '-q', '-p', 'no:cacheprovider', *selectors], cwd=self.roots[role],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-5000:])
        self.assertRegex(result.stdout, r'\d+ passed')

    def _vitest(self, files, pattern=None):
        frontend = self.roots['community'] / 'frontend'
        link = frontend / 'node_modules'
        self.assertTrue((DEPENDENCIES / 'vitest/vitest.mjs').is_file())
        # Only a disposable snapshot may mount dependencies. Never replace an
        # existing directory, junction, or a user's development installation.
        self.assertFalse(link.exists())
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(DEPENDENCIES)],
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, 'Could not mount the local test toolchain')
        try:
            with tempfile.TemporaryDirectory(prefix='pulse-vitest-result-') as temporary:
                report = Path(temporary) / 'result.json'
                command = ['node', str(DEPENDENCIES / 'vitest/vitest.mjs'), 'run', *files,
                           '--reporter=json', '--outputFile=' + str(report)]
                if pattern:
                    command += ['-t', pattern]
                result = subprocess.run(command, cwd=frontend, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=240)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])
                data = json.loads(report.read_text(encoding='utf-8'))
                self.assertGreater(data['numPassedTests'], 0)
                self.assertEqual(data['numFailedTests'], 0)
        finally:
            # Removing this junction does not remove its dependency target.
            self.assertEqual(link.parent.resolve(), frontend.resolve())
            os.rmdir(link)

    def test_s01_human_identity(self):
        self._pytest('community', 'tests/test_human_task_submissions.py::test_progress_receipt_server_identity_audit_and_exact_replay',
                     'tests/test_human_task_submission_route.py')

    def test_s02_authority_separation(self):
        self._pytest('core', 'tests/test_human_task_submission.py')
        self._pytest('community', 'tests/test_human_task_submissions.py::test_human_history_does_not_change_technical_facts_or_impact')

    def test_s03_atomic_rollback(self):
        self._pytest('community', 'tests/test_human_task_submissions.py::test_event_failure_rolls_back_receipt_even_if_caller_catches_and_commits',
                     'tests/test_human_task_submissions.py::test_report_writer_failure_reverts_transition_content_and_audit')
        self._pytest('core', 'tests/test_human_task_submission.py', '-k', 'failure or commit')

    def test_s04_concurrent_retry(self):
        self._pytest('community', 'tests/test_human_task_submissions.py::test_concurrent_same_key_and_lost_response_retry_share_receipt',
                     'tests/test_human_task_submissions.py::test_exact_retry_after_terminal_state_and_edition_change_returns_original_result')
        self._vitest([FRONT + 'HumanTaskSubmissionForm.test.tsx'], 'uncertain|double submission')

    def test_s05_append_only_correction(self):
        self._pytest('community', 'tests/test_human_task_submissions.py::test_changed_payload_under_same_key_is_conflict_and_correction_is_append_only')

    def test_s06_canonical_states(self):
        self._pytest('core', 'tests/test_task_delivery_summary.py')

    def test_s07_transport_parity(self):
        self._pytest('community', 'tests/test_task_delivery_projection.py')

    def test_s08_task_and_test_ui(self):
        self._vitest([FRONT + 'CardDeliveryDoDPanel.test.tsx', KANBAN + 'CardModal.test.tsx',
                      KANBAN + 'KanbanBoard.impactEvidence.test.tsx'], 'verdict|state|human|single|scoped')

    def test_s09_no_fabricated_rejection(self):
        self._pytest('core', 'tests/test_task_delivery_summary.py')
        self._vitest([FRONT + 'DeliveryReadinessDoD.test.tsx'])

    def test_s10_human_report(self):
        self._pytest('community', 'tests/test_human_task_delivery_report.py')
        self._vitest([FRONT + 'HumanTaskSubmissionForm.test.tsx', KANBAN + 'CardModal.test.tsx'], 'human|submits human')

    def test_s11_blocked_report(self):
        self._pytest('community', 'tests/test_human_task_submissions.py::test_missing_authoritative_assessment_blocks_report_without_creating_receipt')
        self._vitest([FRONT + 'HumanTaskSubmissionForm.test.tsx', KANBAN + 'CardModal.test.tsx'], 'rejection|409|blocked')

    def test_s12_fences_and_reauthorization(self):
        self._pytest('core', 'tests/test_human_task_submission.py', '-k', 'permission or reauthorizes or nonhuman')
        self._pytest('community', 'tests/test_human_task_submissions.py', '-k', 'fences or agent or changed_payload')

    def test_s13_lazy_bounded_reads(self):
        self._vitest([FRONT + 'CardDeliveryDoDPanel.test.tsx', KANBAN + 'KanbanBoard.impactEvidence.test.tsx'],
                     'scoped projection|coalesces|suspends background')

    def test_s14_preserved_draft(self):
        self._vitest([FRONT + 'CardDeliveryDoDPanel.test.tsx', FRONT + 'HumanTaskSubmissionForm.test.tsx'], 'draft|selection|preserves text')

    def test_s15_contextual_help(self):
        self._vitest(['src/components/help/__tests__/HelpPanel.deliveryStatus.test.tsx'])

    def test_s16_state_examples(self):
        self._pytest('core', 'tests/test_task_delivery_summary.py')
        self._vitest([FRONT + 'CardDeliveryDoDPanel.test.tsx'])

    def test_s17_single_human_command(self):
        self._pytest('community', 'tests/test_human_task_submission_route.py',
                     'tests/test_human_task_delivery_report.py')
        self._vitest([FRONT + 'HumanTaskSubmissionForm.test.tsx'])

    def test_s18_agent_composed_handoff(self):
        self._pytest('community', 'tests/test_delivery_report.py', 'tests/test_task_delivery_operation_cost.py')
        self._vitest([FRONT + 'HumanTaskSubmissionForm.test.tsx'])

