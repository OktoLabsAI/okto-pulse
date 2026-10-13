# Delivery readiness implementation report — community

## Scope and baseline (2026-10-13)

Normative plan: `../PLANO_CODEX_PULSE_ENTREGA_VERIFICACAO_READINESS_v1.md` in the shared workspace.
Plan SHA-256: `124196e6e43a9495dac164d46c3e9a0f8ce05970d289c55ee5ce078d9a0b0b53`.
Initial HEAD: `5eeceeb83795a3b694c9467a51bb5667ef032569`. Plan base: `5eeceeb83795a3b694c9467a51bb5667ef032569`.
Committed diff against plan base: none.
Branch: `feature/v0.4.0`. Final HEAD: PENDING (implementation not complete).

This report is the implementation ledger for this plan. No release/deployment is authorized by the plan.
Core remains infrastructure-free; concrete authentication, storage and consistent reads belong to Community adapters.
The new metrics are independent proof counts. Legacy `delivery_completeness` keeps its explicitly deprecated contract.
Historical evidence must not be rewritten. Gates and diagnostics must share factual predicates.

## Milestones

| Phase | Required work | Status |
|---|---|---|
| P00 | Fixar a base e construir regressões | COMPLETE (expected RED) |
| P01 | Contratos e modelo factual compartilhado | PENDING |
| P02 | Inventário, aplicabilidade e métricas | PENDING |
| P03 | Resultados de verificação e bases | PENDING |
| P04 | Diagnóstico e ações | PENDING |
| P05 | Adapter, snapshot e paginação | PENDING |
| P06 | Gates, transições e autorização de override | PENDING |
| P07 | REST/MCP e erros | PENDING |
| P08 | Mostradores e face do card | PENDING |
| P09 | Painel de pendências e atualização cruzada | PENDING |
| P10 | Documentação, compatibilidade e pacote | PENDING |
| P11 | Verificação integrada e relatório final | PENDING |

## Consumer inventory (P00.2)

Search: `rg -n "calculate_delivery_completeness|delivery_completeness|evaluate_delivery_coverage|require_card_delivery|require_spec_delivery|GetDeliveryEvidenceUseCase|getDeliveryEvidence|skip_delivery_evidence|DeliveryCompletenessView" src tests frontend/src --glob '*.py' --glob '*.ts' --glob '*.tsx' --glob '*.md'`.
Generated frontend bundles are excluded from source inventory and will be regenerated at P10.

| Consumer | Symbols |
|---|---|
| `frontend/src/components/code-traceability/CardDeliveryDoDPanel.tsx` | `DeliveryCompletenessView`, `delivery_completeness`, `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/DeliveryCompletenessView.tsx` | `DeliveryCompletenessView` |
| `frontend/src/components/code-traceability/DeliveryEvidencePanel.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/DeliverySelectionEditor.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/CardDeliveryDoDPanel.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/CardProgressPanel.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/DeliveryCompleteness.test.tsx` | `DeliveryCompletenessView`, `delivery_completeness`, `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/DeliveryEvidencePanel.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/DeliveryReadinessDoD.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/DeliveryReportBatchEditor.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/__tests__/DeliverySelectionEditor.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/code-traceability/useTaskDeliveryCompleteness.ts` | `DeliveryCompletenessView`, `delivery_completeness`, `getDeliveryEvidence` |
| `frontend/src/components/kanban/KanbanCard.tsx` | `DeliveryCompletenessView` |
| `frontend/src/components/kanban/KanbanColumn.tsx` | `DeliveryCompletenessView` |
| `frontend/src/components/kanban/__tests__/CardModal.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/specs/SpecCoveragePanel.test.tsx` | `evaluate_delivery_coverage` |
| `frontend/src/components/specs/SpecModal.tsx` | `skip_delivery_evidence` |
| `frontend/src/components/specs/__tests__/NativeVerificationContext.test.tsx` | `getDeliveryEvidence` |
| `frontend/src/components/specs/specCoverageTypes.ts` | `evaluate_delivery_coverage` |
| `frontend/src/services/api.ts` | `getDeliveryEvidence` |
| `frontend/src/types/delivery-evidence.ts` | `delivery_completeness` |
| `frontend/src/types/index.ts` | `skip_delivery_evidence` |
| `src/okto_pulse/community/adapters/delivery_resume_reader.py` | `evaluate_delivery_coverage` |
| `src/okto_pulse/community/adapters/sqlalchemy_decision_reviews.py` | `evaluate_delivery_coverage` |
| `src/okto_pulse/community/adapters/sqlalchemy_delivery_evidence.py` | `calculate_delivery_completeness`, `delivery_completeness`, `evaluate_delivery_coverage` |
| `src/okto_pulse/community/adapters/sqlalchemy_models.py` | `skip_delivery_evidence` |
| `src/okto_pulse/community/adapters/sqlalchemy_spec_coverage.py` | `require_spec_delivery` |
| `src/okto_pulse/community/api/code_traceability.py` | `GetDeliveryEvidenceUseCase` |
| `tests/test_architecture_acceptance_witnesses.py` | `evaluate_delivery_coverage`, `require_spec_delivery`, `skip_delivery_evidence` |
| `tests/test_decision_reviews.py` | `evaluate_delivery_coverage` |
| `tests/test_delivery_contributions.py` | `delivery_completeness`, `evaluate_delivery_coverage`, `require_card_delivery` |
| `tests/test_delivery_evidence_integration.py` | `delivery_completeness`, `evaluate_delivery_coverage`, `require_card_delivery`, `require_spec_delivery`, `skip_delivery_evidence` |
| `tests/test_delivery_execution_sets.py` | `evaluate_delivery_coverage` |
| `tests/test_delivery_progress.py` | `evaluate_delivery_coverage` |
| `tests/test_delivery_progress_currentness.py` | `require_card_delivery` |
| `tests/test_delivery_selection.py` | `require_card_delivery` |
| `tests/test_learning_submission_writer.py` | `require_card_delivery` |
| `tests/test_multicard_delivery_integration.py` | `require_spec_delivery` |
| `tests/test_native_delivery_envelope_parity.py` | `evaluate_delivery_coverage` |
| `tests/test_native_delivery_skip_authority.py` | `skip_delivery_evidence` |
| `tests/test_native_operational_partial_failure.py` | `require_spec_delivery`, `skip_delivery_evidence` |
| `tests/test_native_shared_checkout_resume.py` | `GetDeliveryEvidenceUseCase` |
| `tests/test_spec_coverage_read.py` | `evaluate_delivery_coverage` |
| `tests/test_technical_report_delivery.py` | `require_spec_delivery` |

## Mandatory test matrix

M01, M02, M03, M04, M05, M06, M07, M08, M09, M10, M11, M12, M13, M14, M15, M16, M17, M18, M19, M20, M21, M22, M23, M24, V01, V02, V03, V04, V05, V06, V07, V08, V09, V10, V11, V12, V13, V14, V15, V16, V17, V18, V19, V20, V21, V22, V23, V24, D01, D02, D03, D04, D05, D06, D07, D08, D09, D10, G01, G02, G03, G04, G05, G06, G07, G08, G09, G10, G11, G12, S01, S02, S03, S04, S05, S06, S07, S08, S09, S10, S11, S12, U01, U02, U03, U04, U05, U06, U07, U08, U09, U10, R01, R02, R03, R04: NOT_RUN. Individual execution results below supersede this initial status only for the named cases.
Full matrix and INV-01 through INV-15 remain required; an expected failing baseline is not a completed feature.

## Changes and execution log

- P00: base inspection and consumer inventory executed; both repositories match the normative commits.
- P00: regression tests added for required complementary failures, specialized report base mismatch, override authority and pre-Done DoD.
- Behavioral results and import parity: recorded in P00 result below.
- Product code: unchanged. P01–P11 implementation, integrated verification, performance and UI inspection: PENDING.

## Resume

Run the P00 regressions with isolated paired installation byte parity. Repair fixture problems if any; preserve assertions of the intended behavior. Record expected RED before shared factual refactoring. Follow P00–P11 dependencies and retain the complete matrix.

## P00 result — 2026-10-13

Installed/source parity: 851 Core and 320 Community Python files, byte-for-byte in isolated dv-venv. Import probes resolved both local src trees via paired PYTHONPATH. Production installation was not changed.

Commands executed from workspace (each prefixed with rtk proxy):
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_verification.py --tb=short: 8 FAILED, expected RED in 33.44s. V09/V10/V11 source and revision mismatch were admitted; V01/V05 and V03 required complementary failure was masked, with and without Delivery binding. Real signed issuers verified every fixture result before the failing product assertion.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_readiness_authorization.py --tb=short: 4 FAILED, expected RED in 8.50s. G07 both directions lacked coverage authority; unauthorized requests reached the writer.
- npm run test -- src/components/code-traceability/__tests__/DeliveryReadinessDoD.test.tsx (Community/frontend): 1 FAILED, expected RED; U05 shows accepted proof but 1 of 1 unproven.

P00 completed: intended failures reproduced, not waived or xfailed. These cases remain unresolved feature tests until their implementation phases. P01 is next; P02-P11 and the remainder of the 96-case matrix remain PENDING/NOT_RUN. No product code changed in this milestone.
