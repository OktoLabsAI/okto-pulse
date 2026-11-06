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

    def _pytest(self, role, *selectors, timeout=300):
        report = self.root / f'{role}-pytest.xml'
        report.unlink(missing_ok=True)
        # Both repositories' conftest fixtures configure the paired source roots
        # and isolated persistence. Integration selectors depend on those fixtures.
        self._run([str(self.python), '-m', 'pytest', *selectors,
                   '-q', '--tb=short', f'--junitxml={report}'], self.repos[role], timeout=timeout)
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
            'tests/test_f14_distribution_packaging.py::test_community_declares_every_runtime_dependency_directly')
        self._pytest('community',
            'tests/test_readiness_projection_worker.py::test_backfill_after_v401_upgrade_completes_all_scopes_and_three_restarts',
            'tests/test_readiness_projection_worker.py::test_real_community_lifespan_serves_pending_then_current_on_upgraded_copy')


class ProtocolAcceptance(PairedAcceptance):
    def test_invalid_entry_rolls_back_the_whole_handoff(self):
        self._pytest('community', 'tests/test_delivery_batch.py',
            'tests/test_delivery_inline_execution.py::test_later_failure_rolls_back_receipt_binding_event_and_handler',
            'tests/test_delivery_report.py::test_report_rejection_cannot_leave_batch_or_outbox_after_outer_commit')

    def test_exact_replay_keeps_original_acceptance_and_changed_payload_conflicts(self):
        self._pytest('community',
            'tests/test_delivery_report.py::test_atomic_report_manifest_replay_and_later_rework_do_not_resubmit',
            'tests/test_delivery_batch.py::test_atomic_batch_replay_manifest_and_revision',
            'tests/test_delivery_inline_execution.py::test_inline_real_origin_commits_once_and_replay_has_same_ids')

    def test_material_progress_only_invalidates_affected_proof(self):
        self._pytest('community', 'tests/test_delivery_progress_currentness.py')

    def test_fixed_trajectory_preserves_audit_and_meets_call_and_payload_budget(self):
        self._pytest('core', 'tests/test_delivery_protocol_guidance.py',
            'tests/test_mcp_resources.py::test_cards_workflow_defers_to_typed_transition_read_model')
        self._pytest('community', 'tests/test_delivery_protocol_budget.py')

    def test_progress_help_is_central_contextual_and_keyboard_accessible(self):
        self._react('src/components/help/__tests__/HelpPanel.taskProgress.test.tsx',
            'src/components/help/__tests__/ContextualHelpLink.test.tsx',
            'src/components/code-traceability/__tests__/CardProgressPanel.test.tsx',
            'src/components/code-traceability/__tests__/CardProgressHistory.test.tsx')

    def test_protocol_keeps_native_authorization_limits_and_transaction_boundaries(self):
        self._pytest('core', 'tests/test_delivery_protocol_guidance.py',
            'tests/test_delivery_selection.py::test_manifest_pins_exact_records_and_rework_preserves_historical_report',
            'tests/test_delivery_selection.py::test_prospective_report_checks_selection_without_changing_history',
            'tests/test_delivery_selection.py::test_manifest_corruption_cannot_fall_back_to_all_records',
            'tests/test_delivery_selection.py::test_selection_is_closed_and_bounded')
        self._pytest('community', 'tests/test_delivery_report.py', 'tests/test_delivery_inline_execution.py',
            'tests/test_delivery_progress_read.py')


class ValidationAcceptance(PairedAcceptance):
    def test_quality_success_cannot_hide_failed_decomposition(self):
        self._pytest('core',
            'tests/test_spec_planning_readiness.py::test_quality_success_never_hides_failed_decomposition',
            'tests/test_spec_planning_readiness.py::test_native_failure_cannot_be_compensated_by_another_review')
        self._react('src/components/specs/__tests__/SpecValidationSummary.test.tsx')

    def test_unavailable_component_preserves_denied_information(self):
        self._pytest('core',
            'tests/test_spec_planning_readiness.py::test_unreadable_component_does_not_invent_failure_counts_or_disclose_ids',
            'tests/test_spec_planning_readiness.py::test_technical_failure_is_unavailable_without_protected_exception_text')
        self._react('src/components/specs/__tests__/SpecValidationSummary.test.tsx')

    def test_previous_edition_is_history_without_current_validation(self):
        self._pytest('core',
            'tests/test_spec_planning_readiness.py::test_previous_missing_or_failed_result_cannot_validate')
        self._react('src/components/specs/__tests__/SpecValidationSummary.test.tsx',
            'src/components/specs/__tests__/SpecModal.validationTabs.test.tsx')

    def test_details_are_lazy_and_preserve_drafts_and_history(self):
        self._react('src/components/specs/__tests__/SpecModal.validationTabs.test.tsx')

    def test_validation_help_is_central_contextual_and_accessible(self):
        self._react('src/components/help/__tests__/HelpPanel.specValidation.test.tsx',
            'src/components/help/__tests__/ContextualHelpLink.test.tsx',
            'src/components/specs/__tests__/SpecModal.validationTabs.test.tsx')

    def test_canonical_census_matches_native_gates_and_authority(self):
        self._pytest('core', 'tests/test_spec_planning_readiness.py')
        self._pytest('community', 'tests/test_spec_planning_code_evidence_parity.py',
            'tests/test_spec_planning_critical_context_parity.py', 'tests/test_spec_planning_dependency_parity.py',
            'tests/test_spec_planning_lint_parity.py', 'tests/test_spec_planning_resource_parity.py',
            'tests/test_spec_planning_policy_parity.py', 'tests/test_spec_planning_manifest.py')


class VerificationAuthorityAcceptance(PairedAcceptance):
    """R5 scenario entry points; actual Core/Community sources are paired by the host."""

    def test_reserved_context_and_native_issuance(self):
        self._pytest('core', 'tests/test_evidence_v2_contract.py',
            'tests/test_scenario_verification_method.py', 'tests/test_verification_report.py')
        self._pytest('community', 'tests/test_verification_currentness_integration.py',
            'tests/test_external_test_runner.py', 'tests/test_verification_report_admission.py')

    def test_independent_bases_ordering_and_atomic_publication(self):
        self._pytest('core', 'tests/test_verification_currentness.py')
        self._pytest('community', 'tests/test_verification_currentness_store.py', '-k',
            'independent_bases or delayed or same_basis or concurrent or owner_rollback '
            'or audit_failure or shared_owner_new_failure or owner_commits')

    def test_reset_semantics_tombstones_and_no_resurrection(self):
        self._pytest('community', 'tests/test_verification_currentness_invalidation.py')

    def test_verified_copy_preservation_and_format_admission(self):
        self._pytest('community', 'tests/test_verification_storage_upgrade.py',
            'tests/test_storage_upgrade_v043.py', 'tests/test_readiness_schema_upgrade.py')

    def test_real_capacity_history_and_rollback_resilience(self):
        self._pytest('community', 'tests/test_verification_currentness_store.py', '-k',
            'history or real_5000 or real_20000 or bounds_and_missing or counter_exhaustion '
            'or audit_failure or missing_generation or rechecks_actual_storage or tampering '
            'or current_heads_and_revision')

    def test_legacy_seed_replay_and_transport_authorization(self):
        # Exact-format copy/replay tests perform real SQLite persistence. Keep
        # every assertion while allowing this suite a bounded 10-minute budget
        # within its separately authorized 15-minute native runner deadline.
        self._pytest('community', 'tests/test_verification_storage_upgrade.py',
            'tests/test_verification_report_admission.py',
            'tests/test_verification_currentness_store.py::test_result_publication_retains_state_permissions_authentication_and_locked_spec_guard',
            timeout=600)


class RequirementVerificationAcceptance(PairedAcceptance):
    """R6 paired-source checks; installed My Board acceptance remains separate.

    These selectors run final Delivery/readiness evaluation as well as native
    association and persistence. A passing component suite cannot stand in for
    the separately observed package installation, browser journey or signed
    evidence required by the installed-acceptance scenario.
    """

    def test_independent_bases_keep_exact_requirement_credit(self):
        self._pytest('community',
            'tests/test_delivery_readiness_acceptance.py::test_R6_independent_bases_remain_verified_in_one_final_projection',
            'tests/test_external_test_delivery_binding.py')

    def test_same_basis_failure_and_fresh_binding(self):
        self._pytest('community',
            'tests/test_delivery_readiness_acceptance.py::test_R6_same_basis_failure_only_impacts_its_requirement_and_recovery_requires_binding',
            'tests/test_multicard_delivery_integration.py::test_one_signed_report_preserves_distinct_criterion_verdicts')

    def test_reset_and_bounded_coherent_projection(self):
        self._pytest('community',
            'tests/test_delivery_readiness_acceptance.py::test_R6_reset_never_revives_old_bases_and_fresh_bindings_restore_each_requirement',
            'tests/test_delivery_readiness_batch.py',
            'tests/test_spec_coverage_read.py', 'tests/test_spec_coverage_transport.py')
        self._pytest('core', 'tests/test_persisted_delivery_readiness.py')

    def test_english_requirement_diagnosis_and_help(self):
        self._react('src/components/code-traceability/__tests__/DeliveryReadiness.test.tsx',
            'src/components/code-traceability/__tests__/DeliveryEvidencePanel.test.tsx',
            'src/components/code-traceability/__tests__/CardDeliveryDoDPanel.test.tsx',
            'src/components/code-traceability/__tests__/TaskDeliveryDiagnosis.test.tsx',
            'src/components/code-traceability/__tests__/TaskRequirementControl.test.tsx',
            'src/components/kanban/__tests__/CardModal.test.tsx',
            'src/components/specs/SpecCoveragePanel.test.tsx',
            'src/components/specs/__tests__/SpecCoveragePanel.persisted.test.tsx',
            'src/components/help/__tests__/HelpPanel.deliveryStatus.test.tsx',
            'src/components/help/__tests__/HelpPanel.taskRequirements.test.tsx',
            'src/components/help/__tests__/HelpPanel.taskProgress.test.tsx')

    def test_human_submission_and_copy_upgrade_preconditions(self):
        # This method deliberately does not claim installed My Board acceptance.
        # It exercises native human submission and isolated format conversion;
        # installation, restart and direct browser evidence are additional work.
        self._pytest('community', 'tests/test_human_task_delivery_report.py',
            'tests/test_verification_storage_upgrade.py', timeout=600)

    def test_economical_agent_protocol_preserves_exact_proof(self):
        self._pytest('core', 'tests/test_delivery_protocol_guidance.py')
        self._pytest('community', 'tests/test_delivery_report.py',
            'tests/test_external_test_delivery_binding.py',
            'tests/test_delivery_readiness_batch.py::test_query_count_does_not_grow_per_card',
            'tests/test_delivery_readiness_batch.py::test_near_native_card_limit_has_fixed_query_count')


class CoverageAcceptance(PairedAcceptance):
    """R7 correctness checks; installed UI and latency acceptance stay separate."""

    def test_transport_permissions_cursor_and_source_fences(self):
        self._pytest('core',
            'tests/test_spec_coverage_query.py',
            'tests/test_spec_coverage_authorization.py',
            'tests/test_spec_coverage_mcp.py',
            'tests/test_spec_coverage_graph.py',
            'tests/test_spec_coverage_degraded_graph.py')
        self._pytest('community',
            'tests/test_spec_coverage_transport.py',
            'tests/test_spec_coverage_read.py::test_restricted_proof_is_not_even_read_or_counted',
            'tests/test_spec_coverage_read.py::test_card_change_without_spec_version_changes_source_identity',
            'tests/test_spec_coverage_read.py::test_mandatory_final_fence_detects_source_change_and_drains_timeout',
            'tests/test_spec_coverage_graph.py::test_persisted_native_generation_change_invalidates_next_page',
            timeout=600)

    def test_reader_admission_cancellation_and_lifecycle(self):
        self._pytest('community',
            'tests/test_coverage_responsiveness_acceptance.py',
            'tests/test_grafx_query_execution_scope.py::test_nested_scope_cannot_extend_or_change_board_and_restores_context',
            'tests/test_grafx_query_execution_scope.py::test_native_shared_deadline_ends_read_and_releases_writer',
            'tests/test_grafx_query_execution_scope.py::test_absolute_deadline_and_existing_policy_cannot_be_relaxed',
            'tests/test_grafx_database_pool.py::test_existing_lease_deadline_refuses_before_pin_or_open',
            'tests/test_grafx_database_pool.py::test_existing_lease_lock_contention_is_bounded_without_late_acquisition',
            'tests/test_grafx_database_pool.py::test_observation_acquire_never_opens_or_finalizes_a_terminal_handle',
            'tests/test_grafx_database_pool.py::test_observation_acquire_pins_existing_identity_without_another_open',
            'tests/test_grafx_database_pool.py::test_observation_acquire_refuses_geometry_mismatch_without_pinning',
            'tests/test_grafx_database_pool.py::test_read_only_pool_remains_available_while_writer_participant_is_busy',
            'tests/test_grafx_database_pool.py::TestReleaseIsExactlyOnceUnderContention',
            'tests/test_routed_board_graph_composition.py::test_coverage_cold_readers_never_open_checkpoint_or_prepare',
            'tests/test_routed_board_graph_composition.py::test_prepared_readers_pin_exact_generation_and_release_on_failure',
            'tests/test_routed_board_graph_composition.py::test_lifecycle_prepares_after_durability_without_removing_any_step',
            'tests/test_routed_board_graph_composition.py::test_optional_reader_preparation_failure_does_not_rewrite_durability',
            'tests/test_grafx_read_lanes.py::test_scope_covers_query_and_cleanup_and_releases_before_retry',
            'tests/test_grafx_query_deadline.py::test_native_deadline_finishes_and_releases_read_resources',
            'tests/test_spec_coverage_read.py::test_expired_absolute_deadline_never_reads_or_starts_savepoint',
            'tests/test_spec_coverage_read.py::test_timeout_drains_read_scope_before_returning',
            'tests/test_spec_coverage_graph.py::test_closing_graph_preserves_coverage_and_source_fence',
            'tests/test_spec_coverage_graph.py::test_close_fallback_still_obeys_graph_read_deadline',
            timeout=600)

    def test_coverage_modal_refresh_scope_and_help(self):
        self._react('src/components/specs/SpecCoveragePanel.test.tsx',
            'src/components/specs/__tests__/SpecCoveragePanel.persisted.test.tsx',
            'src/components/specs/__tests__/SpecModal.validationTabs.test.tsx')
