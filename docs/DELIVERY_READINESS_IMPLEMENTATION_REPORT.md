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
| P01 | Contratos e modelo factual compartilhado | COMPLETE |
| P02 | Inventário, aplicabilidade e métricas | IN_PROGRESS |
| P03 | Resultados de verificação e bases | IN_PROGRESS |
| P04 | Diagnóstico e ações | IN_PROGRESS |
| P05 | Adapter, snapshot e paginação | IN_PROGRESS |
| P06 | Gates, transições e autorização de override | IN_PROGRESS |
| P07 | REST/MCP e erros | IN_PROGRESS (contract tests passed; prerequisite acceptance reconciliation pending) |
| P08 | Mostradores e face do card | IMPLEMENTED (focused tests and visual checks passed; full acceptance P11) |
| P09 | Painel de pendências e atualização cruzada | IMPLEMENTED (native links, invalidation and focused browser tests passed; full acceptance P11) |
| P10 | Documentação, compatibilidade e pacote | IMPLEMENTED (docs/manifests, paired locks, built/installed asset parity and bundle browser test passed) |
| P11 | Verificação integrada e relatório final | IN_PROGRESS (full named-case/invariant reconciliation and final acceptance pending) |

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

Current resume point: P02/P03 IN_PROGRESS. Read the latest checkpoint; do not restart P00. Factual producers cover Spec/normal/bug obligations and native Test Card scenarios; exact proof associations now share verified credit without duplicating work. Reconcile M01-M24/V01-V24 evidence and residual typed rejection causes, then implement P04 diagnostics/actions from the shared assessments. P05-P11 and the complete acceptance matrix remain required.

## P00 result — 2026-10-13

Installed/source parity: 851 Core and 320 Community Python files, byte-for-byte in isolated dv-venv. Import probes resolved both local src trees via paired PYTHONPATH. Production installation was not changed.

Commands executed from workspace (each prefixed with rtk proxy):
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_verification.py --tb=short: 8 FAILED, expected RED in 33.44s. V09/V10/V11 source and revision mismatch were admitted; V01/V05 and V03 required complementary failure was masked, with and without Delivery binding. Real signed issuers verified every fixture result before the failing product assertion.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_readiness_authorization.py --tb=short: 4 FAILED, expected RED in 8.50s. G07 both directions lacked coverage authority; unauthorized requests reached the writer.
- npm run test -- src/components/code-traceability/__tests__/DeliveryReadinessDoD.test.tsx (Community/frontend): 1 FAILED, expected RED; U05 shows accepted proof but 1 of 1 unproven.

P00 completed: intended failures reproduced, not waived or xfailed. These cases remain unresolved feature tests until their implementation phases. P01 is next; P02-P11 and the remainder of the 96-case matrix remain PENDING/NOT_RUN. No product code changed in this milestone.

## P01 verification checkpoint — 2026-10-13

Added Core domain/delivery_assessment.py (closed reason/state vocabulary and frozen phase/unit/completeness results) and models/delivery_readiness.py (closed response DTOs, count equations, bounded issues/actions, versioned pinpoints, explicit nulls). Shared coverage now takes a lifecycle requirement independently of authentic proof. The deprecated maturity projection reuses that evaluator without manufacturing Done facts or its own implementation sufficiency check. Default gate behavior is preserved. New REST/MCP/UI consumers are not yet connected.

Validation commands (rtk proxy prefix):
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_readiness_contracts.py tests/test_delivery_completeness.py tests/test_effective_delivery_coverage.py tests/test_delivery_evidence_domain.py --tb=short --show-capture=no: 131 PASSED in 10.95s.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_evidence_integration.py tests/test_technical_report_delivery.py tests/test_native_operational_partial_failure.py --tb=short --show-capture=no: 41 PASSED in 80.10s.
- Both runs proved isolated installed/source parity (853 Core / 320 Community) and fresh process imports from both local src trees.
- Ruff found one unused CardStatus import after predicate extraction; cleanup and final architecture audit pending.

P00 regression failures are intentionally still open until P03/P06/P08. Passing compatibility suites do not resolve those defects. Final wheel, broad suites, performance and rendered UI checks remain required.

P01 encerrado: ruff sem findings apos remover import nao utilizado; a auditoria oficial de fonte (audit_readiness.py, 2026-10-13) passou com 8.840 linhas de propriedade, zero findings, READMEs exatos e os oito budgets em ZERO. Paridade isolada final 853/320. Nenhuma alteracao de comportamento apos os 131+41 testes, apenas remocao de import nao utilizado. Auditoria explicita dos wheels finais permanece no P11.

Arquivos funcionais P01 (Core): domain/delivery_assessment.py, models/delivery_readiness.py, domain/delivery_evidence.py, domain/effective_delivery_coverage.py, domain/delivery_completeness.py; testes test_delivery_readiness_contracts.py. READMEs pareados regenerados pela projecao oficial. Esta fase entrega tipos e predicados compartilhados; agregacao/utilizacao desses tipos, fatos negativos completos, diagnostico e superficies seguem P02-P11. Proximo: P02, com testes de agregacao escritos e ainda NOT_RUN.

## P02 in progress — 2026-10-13

Pure aggregation now counts unique canonical obligations, or assigned scenarios for Test cards; Test implementation is structurally N/A. Counts use floor division, preserve unknown denominators and keep waivers applicable without proof credit. Contradictory duplicate units produce unknown instead of selecting the first result. Native human waivers accept optional disposition; omission retains the original request fingerprint and unchanged payload. Projections expose legacy_disposition_unspecified. Shared waiver authority validates exact phase, scope, digest and current authorization. Proof later satisfied makes an old risk waiver unused.

Validation (rtk proxy prefix):
- First pure aggregation run: 62 PASSED (metrics, contracts and legacy completeness).
- Expanded Core run exposed order-dependent partial counts for conflicting duplicates: 1 FAILED / 156 PASSED. Corrected by treating conflicting duplicate units as unknown.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_readiness_metrics.py tests/test_delivery_readiness_contracts.py tests/test_delivery_completeness.py tests/test_effective_delivery_coverage.py tests/test_delivery_evidence_domain.py --tb=short --show-capture=no: 157 PASSED in 13.60s.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_waivers.py tests/test_delivery_evidence_integration.py --tb=short --show-capture=no: 41 PASSED in 87.23s. This includes actual SQLite waiver persistence, exact old-format replay, read-only history and changed-disposition conflict.
- Both prove 853/320 byte parity and paired source imports. Focused ruff checks passed. Counts overlap prior runs; they are not a total of distinct tests.

P02 NOT COMPLETE: connect normalized factual evaluation to per-unit assessments, including bounded/inherited contributions, fallback, Decisions and complete scenario population. Current metric tests prove aggregation on controlled assessed units; they do not claim that runtime readiness or the complete M01-M24 matrix is delivered. The native fact loader, diagnostics, REST/MCP and frontend remain pending. Source closure rerun is live; record its terminal result before claiming it for these changes. No production reinstall/deploy.

Resume: start from current WIP and this ledger, not from P00. P01 commits are Core 0b896202 / Community cb2fff93, pushed. P03 may proceed alongside P02 per the plan after P01: source matching must compare verification_report.sources to every implementation receipt source/revision; positive native report fixtures currently use unrelated generic repo:component and must be corrected to authentic expected bases, retaining negative cases. Required complementary scenarios and unbound failures are still the P00 RED reproductions. G07 and U05 remain RED until P06/P08.

Auditoria P02 parcial terminou com sucesso: 8.841 linhas de propriedade, zero findings, READMEs exatos, oito budgets ZERO (audit_readiness.py). Nao ha processos de teste/auditoria desta rodada pendentes. Este checkpoint preserva trabalho testado; P02 continua IN_PROGRESS, com as lacunas e proximo passo descritos acima.

## P03 base verification checkpoint — 2026-10-13

P02 checkpoint pushed: Core 9d6e539d / Community 3033a0f7.
Base correction pushed: Core 580defc7 / Community 8ea89fcb.
P03 may proceed alongside P02 after P01, as explicitly allowed by the plan.

Core now compares every expected canonical source/revision with the authenticated report through verification_report_source_issue. Missing expected identities remain unresolved; timestamps cannot make incompatible bases equivalent. Community calls this public predicate in _test, shared by admission and reads, retaining receipt, temporal, Card and scope validation. Positive fixtures name actual mock execution bases. The shared report fixture now returns deep copies, avoiding cross-test mutation of SOURCE.

Commands executed (each prefixed with rtk proxy):

- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_readiness_report_bases.py tests/test_verification_report.py tests/test_delivery_readiness_metrics.py --tb=short --show-capture=no: **66 PASSED in 7.60s**.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_verification.py tests/test_technical_report_delivery.py tests/test_native_operational_partial_failure.py tests/test_native_br_verification_reuse.py tests/test_native_mixed_verification.py tests/test_native_verification_applicability.py tests/test_multicard_delivery_integration.py -k "not complementary_failure" --tb=short --show-capture=no: **33 PASSED / 2 DESELECTED in 78.66s**. The excluded cases are the still-open P00 complementary-failure reproductions; this is not a full P03 pass. Six source/revision regressions now pass, plus three real signed exact-base cases.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_native_verification_context.py --tb=short --show-capture=no: **1 PASSED in 15.48s**, including native qualified BR/IR, persisted proof and REST/MCP/resume parity. Its optional browser branch was **not enabled**.
- Focused ruff passed. Paired byte parity: 853 Core / 320 Community; fresh local source imports.
- Official source closure passed: **8,841 ownership rows, zero findings, exact README projections, all eight budgets ZERO**. Explicit final wheel audit remains P11.

P03 remains **IN_PROGRESS**: required complementary scenarios/unbound negative results, retry/conflict normalization, diagnostic reasons for rejected facts and the full V01–V24 matrix are incomplete. P02 remains IN_PROGRESS. No readiness endpoint or UI is published; P04–P11 remain pending. No test/audit process from this checkpoint remains live.

Resume from these commits. Reuse domain/verification_plan.py and SpecExecutionPlan: all authored scenarios linked to required active criteria count. Normalize required scenario heads and all native outcomes, including missing Delivery bindings, into immutable Core facts. Extend the shared evaluator to require every applicable scenario per contribution/criterion, preserving mixed per-criterion verdicts, exact base, native retry and conflict. Do not use snapshot.tests alone as the scenario population: it contains selected ledger bindings. Project these factual results into P02 UnitPhaseAssessment; do not create artificial scenarios or Test Cards.

The first shell command intended to append this checkpoint failed from quoting before touching either report; product commits/pushes succeeded. This documentation follow-up records the actual results and commit identities.

## P03 mandatory scenario topology — 2026-10-13

The shared evaluator now requires every native scenario for each applicable contribution/criterion, including supplemental ACs outside requirement paths. The canonical planning resolver exposes the full topology independently of selected delivery records; missing or ambiguous topology stays unavailable. Community consumes that public contract. A passing criterion union cannot hide a failed, unsigned, unexecuted or unbound complementary scenario. Rows retain missing (implementation, criterion, scenario) triples for the upcoming diagnostics.

Distinct authenticated current receipts for the same scenario are a conflict unless the native currentness resolver has already invalidated the previous attempt. Multiple bindings to the same receipt are not conflicts. Tests preserve mixed per-criterion report outcomes and stale retry history. Scenario expansion is bounded before evaluating the join.

The implementation-only Card gate ignores the new verification-specific blockers. An expanded regression caught this coupling (1 FAILED / 247 PASSED); the correction keeps structural implementation failures blocking while missing test topology does not prevent completion of otherwise proven implementation.

Validation so far (commands prefixed with rtk proxy):
- Core first run: 7 FAILED / 181 PASSED because older controlled fixtures omitted explicit scenario topology. Fixtures now declare their native plan; no production fallback to bound records was introduced. Next run: 190 PASSED.
- Final expanded Core command: python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_readiness_scenarios.py tests/test_verification_plan.py tests/test_effective_delivery_coverage.py tests/test_delivery_completeness.py tests/test_delivery_card_readiness.py tests/test_delivery_test_admission.py tests/test_execution_contract.py tests/test_spec_coverage_query.py tests/test_delivery_readiness_contracts.py tests/test_delivery_readiness_metrics.py tests/test_delivery_readiness_report_bases.py --tb=short --show-capture=no: **251 PASSED in 16.89s**.
- Community command before the final conflict/phase-isolation refinement: python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_verification.py tests/test_technical_report_delivery.py tests/test_native_operational_partial_failure.py tests/test_native_br_verification_reuse.py tests/test_native_mixed_verification.py tests/test_native_verification_applicability.py tests/test_multicard_delivery_integration.py tests/test_delivery_evidence_integration.py --tb=short --show-capture=no: **72 PASSED in 121.01s**. Includes both previously RED complementary failure reproductions; neither was deselected.
- Frontend: npm test -- src/components/code-traceability/__tests__/DeliveryCompleteness.test.tsx src/components/code-traceability/__tests__/DeliveryEvidencePanel.test.tsx src/components/specs/SpecCoveragePanel.test.tsx: **51 PASSED, 3 files, 6.51s**. Existing rendering regressions only; no new UI or browser inspection claimed.
- Focused Ruff passed after cleaning pre-existing E701 statements in the touched completeness fixture. Isolated installed/source parity is 853 Core / 320 Community. Production was not reinstalled.
- Final Community rerun and source closure audit are still running at this append; terminal results will be recorded below. The earlier source audit passed 8,845 ownership rows, zero findings and eight zero budgets before the final refinement.

P03 remains IN_PROGRESS. Requiring all scenarios fixes the gate masking, but an unbound failure is still represented as a missing required scenario, not a fully normalized authenticated negative result. Next: load immutable native current results including unbound negatives/inconclusives and original rejection reasons; preserve exact source/revision/receipt/observation identity, classify these facts and connect them to P02 UnitPhaseAssessment. Full V01–V24 proof, diagnostic causes/actions, batch read-only loading, API/MCP/UI, final wheels and performance remain required. G07 and U05 are still outstanding until P06/P08. No claim of total readiness delivery.

Terminal results for this checkpoint:
- Final Core command above, after preserving authorized waiver behavior for conflicts: **252 PASSED in 17.87s**. A risk waiver may allow the existing gate with explicit exception IDs; conflicting receipts remain in detail and receive no proof credit.
- Final refinement rerun: python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_verification.py tests/test_native_mixed_verification.py tests/test_native_operational_partial_failure.py tests/test_multicard_delivery_integration.py --tb=short --show-capture=no: **31 PASSED in 72.84s**.
- Added real signed retry/history and valid-unbound-proof reuse tests. Their first run had one assertion-shape error (tuple versus list) after the retry gate had passed: 1 FAILED / 12 PASSED. Fixed the assertion without changing product behavior; then **13 PASSED in 24.70s**.
- Final Community command: python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_waivers.py tests/test_delivery_readiness_verification.py tests/test_native_verification_context.py --tb=short --show-capture=no: **18 PASSED in 40.98s**, including real SQLite, signed receipts, unchanged failed history, replay, and REST/MCP/resume parity. Optional browser branch was not enabled.
- Final official source audit: **8,845 ownership rows, zero findings, exact README projections, all eight budgets ZERO**. Explicit final wheel audit remains P11. Ruff passed for all touched product/test files; git diff whitespace checks passed.
- All test/audit processes from this checkpoint are terminal. Test counts overlap and must not be added as distinct coverage. Commit the paired changes and these ledgers on feature/v0.4.0; production remains untouched.

Resume at P03 native current-result normalization described above, then P02 factual assessment integration. P02/P03 remain IN_PROGRESS; P04–P11 are not delivered.

## P03 native current-result normalization — 2026-10-13

Previous paired checkpoint is Core 08bf654c / Community a153ae13, pushed.

Added Core domain/delivery_verification.py: immutable native run/source/observation facts, explicit population completeness and typed scenario findings. A run retains scope, scenario digest, receipt, evidence identity, outcome, canonical sources/revisions/digests, observation time and native rejection codes. Exact source comparison is now shared by verification reports and automated execution attestations. The shared coverage evaluator uses the native current receipt when present and explains missing positive joins without granting credit to unbound results.

Community adapters/delivery_verification.py authenticates the Spec's native current evidence pointer through the existing public verifier. It includes failed, inconclusive, aborted and unavailable reports, and distinguishes a never-executed scenario from malformed/unsigned evidence. Rollup authenticates each native head once and reuses the immutable population across Card snapshots; no extra per-scenario SQL queries were added. The cached authentication also checks the current semantic digest before accepting a binding.

The public projection now exposes typed scenario_findings for failed, inconclusive, unexecuted, invalid-receipt, incompatible-base and association-missing cases. An old-base failure is not called a current product failure. Applicable mixed-report criteria are preserved. Both admission and historical reads now refuse automated delivery proof with missing source/revision, rather than approving by omission. Rejected TestDeliveryFacts retain rejection_reasons. Native result fields and historical payloads are not rewritten.

Implementation proof observation time is internal factual input. Its public JSON projection uses one ISO string so REST/MCP do not diverge on Z versus +00:00. This was caught by the existing actual transport comparison and corrected in Community serialization.

Validation (commands prefixed with rtk proxy):
- Core initial focused run: 153 PASSED in 9.93s.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py core -q tests/test_delivery_verification_facts.py tests/test_delivery_readiness_scenarios.py tests/test_delivery_readiness_report_bases.py tests/test_verification_report.py tests/test_effective_delivery_coverage.py tests/test_delivery_completeness.py tests/test_delivery_card_readiness.py tests/test_delivery_test_admission.py tests/test_spec_coverage_query.py tests/test_delivery_readiness_contracts.py tests/test_delivery_readiness_metrics.py --tb=short --show-capture=no: **247 PASSED in 17.27s**.
- Initial Community normalization suite: 84 PASSED in 119.59s. Enforcing canonical bases for automated proof then exposed 11 old multicard HTTP fixtures with no basis (11 FAILED / 76 PASSED). The fixtures now name their actual controlled source/execution revision and retain real HTTP execution plus native manifest/attestation/HMAC verification; no authentication flag is fabricated.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_delivery_readiness_verification.py tests/test_technical_report_delivery.py tests/test_native_operational_partial_failure.py tests/test_native_br_verification_reuse.py tests/test_native_mixed_verification.py tests/test_native_verification_applicability.py tests/test_multicard_delivery_integration.py tests/test_delivery_evidence_integration.py --tb=short --show-capture=no: **87 PASSED in 148.29s** after fixture correction.
- python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_external_test_runner.py tests/test_native_verification_context.py tests/test_delivery_readiness_waivers.py tests/test_native_delivery_currentness.py --tb=short --show-capture=no: **32 PASSED / 1 FAILED in 53.78s**. The single failure was timestamp serialization parity between actual REST/MCP, not an evidence assertion. External test execution, tamper rejection, failed runs, scope, timeout, historical currentness and waivers passed.
- After the serialization correction: python -X utf8 PULSE_REFACTOR/.validation-v040/run_readiness_tests.py community -q tests/test_native_verification_context.py tests/test_delivery_readiness_verification.py tests/test_delivery_readiness_waivers.py --tb=short --show-capture=no: **31 PASSED in 42.01s**, including actual REST/MCP/resume parity. Optional browser branch was not enabled.
- Frontend: npm test -- src/components/code-traceability/__tests__/DeliveryCompleteness.test.tsx src/components/code-traceability/__tests__/DeliveryEvidencePanel.test.tsx src/components/specs/SpecCoveragePanel.test.tsx: **51 PASSED / 3 files in 5.32s**. No new UI was implemented or visually inspected in this backend checkpoint.
- Fresh paired import/installed byte parity: **854 Core / 321 Community**. Ruff passed for all modified Python product/test files. Whitespace checks passed.
- Final official source closure: **8,860 ownership rows; zero findings; exact paired README projections; eight budgets ZERO**. Explicit final wheel audit stays P11.
- All processes from this checkpoint are terminal. Counts overlap and are not an aggregate of distinct requirements. No production reinstall/deployment/release.

P02 and P03 remain IN_PROGRESS. P03 current native outcome/base normalization is implemented and exercised; complete the V01–V24 audit and preserve typed reasons for selected ledger records that still return None or fail before a fact is constructed (e.g. missing Card/scenario or malformed contribution payload). Then connect the shared factual evaluations/findings to P02 UnitPhaseAssessment for obligation/Card/Test/Spec scopes, keeping phase-specific unknown distinct from population/allocation completeness. Do not add another proof engine or infer a percentage from status. P04–P11, G07 override authority and U05 DoD rendering remain pending. Whole readiness endpoint, two new metric bars, full diagnostics/actions, consistent batch snapshots/cursors, performance and final artifacts are not delivered yet.

## Checkpoint solicitado de commit/push — 2026-10-13

Base anterior: Core e67b3c7c / Community dbde1552. Preservadas as alteracoes pendentes de P03 para motivos de rejeicao: contrato DeliveryRecordRejection no Core, retencao dos registros selecionados rejeitados, motivos das provas de execucao e projecao/rollup no adapter Community. Registros invalidos nao recebem credito; sua presenca isolada nao substitui a avaliacao das provas validas.

Este checkpoint atende ao pedido de versionar e enviar o trabalho atual; nao conclui P02/P03 nem a entrega total. Retomar pelos testes adversariais especificos dos novos motivos (payload malformado, escopo, revogacao, cenario ausente/ambiguo e prova valida coexistente), concluir a auditoria V01-V24 e integrar UnitPhaseAssessment. P04-P11 e G07/U05 continuam pendentes. Nao houve alteracao de frontend, reinstalacao de producao ou deploy. Dumps locais de navegador em .playwright-cli/ e output/ permanecem fora do versionamento.

Validacao deste checkpoint: paridade byte-a-byte isolada de 854 arquivos Core e 321 Community; Ruff dos quatro arquivos alterados aprovado. Core: tests/test_delivery_verification_facts.py, tests/test_delivery_readiness_scenarios.py, tests/test_effective_delivery_coverage.py, tests/test_delivery_completeness.py, tests/test_delivery_card_readiness.py e tests/test_spec_coverage_query.py: 142 passed in 15.48s, via run_readiness_tests.py core -q, --tb=short --show-capture=no. Auditoria oficial de fonte: 8.860 linhas, zero findings, READMEs exatos, oito budgets ZERO; auditoria final de wheels segue em P11. Resultado Community registrado abaixo.

Community: tests/test_delivery_readiness_verification.py, tests/test_delivery_evidence_integration.py, tests/test_delivery_selection.py e tests/test_native_verification_context.py: 72 passed in 154.69s, via run_readiness_tests.py community -q, --tb=short --show-capture=no. Inclui regressao de selecao e comparacao REST/MCP; inspecao de browser opcional nao habilitada. Todos os processos de teste/auditoria deste checkpoint terminaram.

## P02 factual obligation assessment / P03 rejected selected inputs — 2026-10-13

Previous paired checkpoint: Core 7c6e4239 / Community dd52b589, pushed. This turn made concrete progress; no blocker or goal pause.

The new assess_obligation_delivery entry point delegates to the shared effective evaluator and constructs UnitPhaseAssessment for Spec and normal/bug contributions. Proof and lifecycle use the same predicates with explicit lifecycle mode; real Card statuses are never rewritten. Independent metrics now consume actual evaluated units rather than only hand-built test assessments. Local contribution completion does not complete a split global obligation. Other Cards' authenticated tests remain available. Native fallback card:<id> is calculable; Decisions retain structural implementation N/A and native inspection outcomes. Known missing allocation stays in the denominator. Exceptions are applied after proof evaluation: risk grants no credit, authorized N/A removes applicability, sufficient proof makes old risk unused, and accepted risk cannot make unavailable verification known.

Snapshot facts_complete distinguishes successful ledger/selection loading from the legacy complete flag that also incorporates planning. Internal phase completeness keeps implementation measurable when a required verification population/method is unavailable. This does not change the existing legacy projection or gates. New readiness REST/MCP/UI integration is still pending. Test Card scenario-unit assessment is not implemented by this obligation-only producer and remains the next P02 task; do not route Test Cards through the obligation denominator.

Added actual SQLite/current-schema adversarial tests for selected malformed payloads, bindings, contribution scope, foreign Card payload, missing/ambiguous scenario, revocation, Target revision, frozen selection, unchanged history and coexistence with valid proof. They found a real selection-summary AttributeError for a non-object payload; fixed in Community without changing stored history. Invalid scenario identifiers now retain a rejection instead of raising an unhashable-key error. Revoked selected bindings retain only relevant current-scope bindings for stale-proof diagnosis. Core foreign-scope native outcome classification no longer exposes receipt IDs, including a mixed same/foreign-scope population.

Validation commands use rtk proxy, paired source paths and an isolated byte-matched install (854 Core / 321 Community); production installation/data were untouched:
- run_readiness_tests.py community -q tests/test_delivery_readiness_rejections.py --tb=short --show-capture=no initially: 2 FAILED / 11 PASSED in 26.38s. One product failure in selection summary; one fixture attempted to bypass the native Target CAS guard. Fixed the summary and changed the fixture to the actual native Target update method. Rerun: 13 PASSED in 39.01s.
- run_readiness_tests.py core -q tests/test_delivery_obligation_assessment.py tests/test_delivery_readiness_metrics.py tests/test_delivery_verification_facts.py --tb=short --show-capture=no: 56 PASSED in 6.97s.
- run_readiness_tests.py core -q tests/test_delivery_obligation_assessment.py tests/test_delivery_readiness_metrics.py tests/test_delivery_verification_facts.py tests/test_effective_delivery_coverage.py tests/test_delivery_readiness_scenarios.py tests/test_delivery_completeness.py tests/test_delivery_card_readiness.py tests/test_spec_coverage_query.py --tb=short --show-capture=no: 191 PASSED in 14.48s.
- run_readiness_tests.py community -q tests/test_delivery_readiness_rejections.py tests/test_delivery_readiness_verification.py tests/test_native_delivery_currentness.py tests/test_delivery_selection.py tests/test_native_verification_context.py --tb=short --show-capture=no: 64 PASSED in 157.10s.
- Final targeted Core command: run_readiness_tests.py core -q tests/test_delivery_obligation_assessment.py tests/test_delivery_readiness_metrics.py tests/test_delivery_readiness_contracts.py tests/test_delivery_verification_facts.py --tb=short --show-capture=no: 87 PASSED in 10.35s. Previous attempt: 86 PASSED / 1 FAILED because the P01 import allowlist did not include composition of the existing pure effective evaluator. The test now explicitly allows that domain module and additionally checks plain import statements; no infrastructure exception or budget increase.
- Final targeted Community command: run_readiness_tests.py community -q tests/test_delivery_readiness_rejections.py tests/test_native_verification_context.py --tb=short --show-capture=no: 19 PASSED in 53.48s, including malformed scenario identities and actual REST/MCP parity.
- Frontend: npm test -- src/components/code-traceability/__tests__/DeliveryCompleteness.test.tsx src/components/code-traceability/__tests__/DeliveryEvidencePanel.test.tsx src/components/specs/SpecCoveragePanel.test.tsx: 51 PASSED, 3 files, 7.92s. Existing consumer regression only; no new UI/browser inspection claimed.
- Ruff passed for changed product/test Python files. Source audit passed 8,862 ownership rows, zero findings, exact README projections and eight budgets ZERO before final small guards; final source audit result recorded below. Final wheel closure remains P11. Counts overlap and are not distinct requirement totals.

Resume: P02/P03 remain IN_PROGRESS. Next implement Test Card scenario-unit assessment using the shared evaluated scenario joins and native scenario identity; do not invent automatic credit or an obligation average. Complete the V01-V24 requirement audit and remaining typed rejection gaps, scope/allocation edge cases and phase uncertainty handling. Then P04 diagnostics/actions, P05 consistent batch loading/snapshots/cursors, P06 gates/override, P07 REST/MCP, P08-P09 UI, P10 docs/artifacts, P11 full 96-case/15-invariant audit and performance/wheels. G07/U05 remain pending. This checkpoint is not whole-plan completion and does not authorize production deploy/reinstall.

Final source audit: 8,862 ownership rows, zero findings, exact paired README projections, eight budgets ZERO. All test/audit processes in this checkpoint terminated. No production writes or deployment.

## P02 native Test Card scenarios and equivalent proof associations — 2026-10-13

Previous pushed pair: Core 30267ebd / Community a98fbc56. This turn is progress; no external blocker or goal pause.

Implemented assess_test_delivery using the authorized Card's native scenario assignments. Implementation is structurally N/A; verification counts unique scenarios, never obligations, receipts or an average of Cards. The shared effective evaluator now returns immutable ScenarioDeliveryCheck joins (binding, contribution, criterion, scenario, implementation and accepted test IDs/reasons). Legacy coverage and obligation/scenario projections consume the same predicates. The scenario projection groups evaluated joins without re-authenticating evidence or copying status to Done. Missing implementation remains a required dependency; unbound passes remain association work; failed/inconclusive/not-run/current conflicts and exact-base rejection are preserved. Unknown subject identity has no fabricated digest. Scope-mismatched native receipts do not leak their IDs.

Scenario exceptions are inherited only from valid human Spec-phase exceptions on the required bindings. A waiver of one join cannot waive another unmet obligation. Current proof makes old risk unused; all applicable joins must be proved before a scenario earns credit. The returned full assessment remains internal until the planned P07 route integration; no new MCP tool or frontend readiness view is claimed.

M23 reproduction found a real remaining gap: adding another complete association of the exact same admitted execution changed verification from 100 to 0, falsely requiring another test. The regression failed before correction (1 FAILED / 24 deselected, 4.73s). The evaluator now groups only exact same-scope/Card/binding/contribution-digest/complete declarations with identical ordered execution IDs and bases/Targets/path. Each named member first passes the existing test predicate, then the observation applies to its exact equivalence group. IDs/authors/history remain intact. Different execution, Target, revision, scope and partial declarations do not borrow credit. No Git ancestry, timestamps or textual similarity establish equivalence.

Validation (all Python commands prefixed rtk proxy, via PULSE_REFACTOR/.validation-v040/run_readiness_tests.py; final isolated byte parity 854 Core / 321 Community):
- Initial core scenario/obligation/effective/scenario-topology/metric suite: 128 PASSED in 11.15s.
- Initial actual-SQL/signed-report scenarios plus verification suite: 33 PASSED in 71.49s.
- Core final new assessment suites: core -q tests/test_delivery_scenario_assessment.py tests/test_delivery_obligation_assessment.py tests/test_effective_delivery_coverage.py tests/test_delivery_readiness_scenarios.py tests/test_delivery_readiness_metrics.py tests/test_delivery_readiness_contracts.py --tb=short --show-capture=no: 160 PASSED in 11.53s.
- Core existing gate/legacy/contribution regression: core -q tests/test_delivery_completeness.py tests/test_delivery_card_readiness.py tests/test_delivery_contributions.py tests/test_delivery_execution_sets.py tests/test_delivery_test_admission.py tests/test_spec_coverage_query.py tests/test_delivery_verification_facts.py --tb=short --show-capture=no: 142 PASSED in 14.69s.
- Community current native regression: community -q tests/test_delivery_readiness_scenarios.py tests/test_native_br_verification_reuse.py tests/test_native_mixed_verification.py tests/test_delivery_readiness_rejections.py tests/test_native_verification_context.py --tb=short --show-capture=no: 31 PASSED in 73.28s, including actual REST/MCP parity and exact-proof duplicate association.
- Additional M15/M18 check: community -q tests/test_delivery_readiness_scenarios.py tests/test_native_br_verification_reuse.py --tb=short --show-capture=no: 10 PASSED / 1 FAILED in 31.80s. The single failure was the new test expecting origin=derived; the native contract uses inherited. All metric/proof assertions had passed. Corrected the assertion to the existing native value; final test_native_br_verification_reuse.py: 2 PASSED in 12.13s. Direct and inherited BR responsibility both show 100/100 from one authenticated Test Card/run; inherited provenance remains in contribution.sources. Cancelling the only contributor retains both normative obligations in the denominator at 0/0, with allocation pending.
- Actual M20 fixture has four native scenarios, three proved and a failed/inconclusive/not-run/unbound-positive fourth: Test V=75 over four while the shared obligation remains unverified. Binding an already signed current result advances 50 to 100 without rerun or Card completion. Specialized wrong-base failures remain stale, not current product failures.
- Frontend existing consumer regressions: npm test -- src/components/code-traceability/__tests__/DeliveryCompleteness.test.tsx src/components/code-traceability/__tests__/DeliveryEvidencePanel.test.tsx src/components/specs/SpecCoveragePanel.test.tsx: 51 PASSED / 3 files in 6.59s. No new UI or browser inspection claimed.
- Ruff passed. Official source closure: 8,863 ownership rows, zero findings, exact paired README projections, all eight budgets ZERO. Explicit final wheel closure remains P11. All test/audit processes are terminal; counts overlap and must not be summed as distinct requirements. No production reinstall, data change, deploy or release.

Resume: factual producers now exist for Spec/normal/bug obligations and Test scenarios. Do not reimplement these or restart P00. P02/P03 remain IN_PROGRESS for the complete named-case audit and residual typed-rejection coverage; full delivery state/gate/diagnostic assertions are not yet proved. Reconcile the M01-M24/V01-V24 evidence against the plan (especially exact native invalidation causes and Decision dependency detail), then implement P04's finite 26-reason/14-action registry, grouping and dependency DAG from these shared assessments. P05-P11, G07 override authority, U05 DoD rendering, batch consistency/cursors/byte limits, all REST/MCP/UI consumers and final 96-case/15-invariant/performance/wheel verification remain mandatory. No completion claim for the full goal.

## User-requested checkpoint: P04 diagnostic draft — 2026-10-13

Official paired source closure: 8,871 ownership rows, zero findings, eight architecture budgets ZERO, exact regenerated README projections. Final wheel closure remains P11.

User requested commit and push of pending work. Previous pushed pair: Core 088c23bb / Community d2777122.
Preserved the new, not-yet-integrated Core domain module delivery_diagnostics.py: closed 26-reason/14-action registry, immutable cause/action data, deterministic grouping and dependency graph draft. It does not execute actions, change authority, update metrics or expose a new API. This is a work-in-progress checkpoint, not completion of P04 or the plan.

Validation: Ruff passed for the new module. Source import and registry structure check passed (26 reasons / 14 actions). Existing readiness contract and metric regression suites passed: 50 tests in 9.21s, using run_readiness_tests.py with isolated installed/source byte parity (855 Core / 321 Community Python files). These regressions do not establish correctness of the new diagnostic graph; dedicated diagnostic tests remain pending. No frontend or production installation was changed.

Resume here: review and test the draft before integrating it. Cover canonical grouping, source/base identity, deterministic ordering, permission-aware availability, missing/null arguments, shared actions across dependencies, exact cycles and downstream impacts, bounded graph expansion, and the three-action limit. Validate the registered tool names against the native MCP registry. Connect the shared factual assessment to causes, evidence, expected/observed facts and pinpoints without a second evaluator. Review intrinsic-state validation and dependency indexing/performance. P02/P03 remain IN_PROGRESS; P04 is now IN_PROGRESS; P05-P11 and all previously documented acceptance gaps remain mandatory.

Generated local browser captures and test logs under Community .playwright-cli/ and output/ remain local, outside the source checkpoint. No data reset, release, deployment or runtime restart was performed.

## P04 factual diagnostic projection and graph regression — 2026-10-13

Previous pushed pair: Core f5605fcb / Community 586cf205. This is concrete progress after the user-requested checkpoint, not a blocker or a pause. P04 remains IN_PROGRESS.

Implemented diagnose_delivery_assessment over the existing immutable Spec/normal/bug obligation and Test scenario assessments. It does not authenticate another receipt, rerun a proof evaluator, infer Done or change metric counts. Every unsatisfied applicable unit/phase must have a mapped cause; absent causes and unrelated/ambiguous native resolutions fail explicitly. Unknown population carries a scope cause without fabricated units. Exact DiagnosticResolution context may enrich an already assessed reason; missing native context is exposed as requires_input/unavailable. An admitted result whose association is missing retains the association action, not a rerun recommendation. Native implementation rejection mappings are now closed: known malformed/context rejections retain assessment_unavailable with the native code, while a new unmapped code raises ReadinessIntegrityError instead of being silently absorbed.

The graph registry retains 26 reasons / 14 action kinds. Its action tool names, argument fields, required arguments and documentation references are tested against the live MCP registry. DTOs now consume the same ActionKind enum. blocks_target_transition permits null when the native transition has not been evaluated; this is not an authorization change. No tool was added/renamed and the generated catalog remains byte-equal.

Reproduced 12 failures / 13 passes in 8.57s before graph corrections: empty arguments reported ready; duplicate JSON keys were silently accepted; an exact shared operation waited on itself; native prerequisites were unenforced; action cycle edges were absent; missing causes/actions and intrinsic waiver states lacked explicit integrity errors. Fixed these cases. The graph deduplicates the same native fact across affected units, reports exact cause/action cycle edges, bounds next actions to three and graph expansion to the existing resolution limit, indexes action predecessors, and preserves IDs/facts when caller permission changes. Prerequisites for associating a compatible result cannot be satisfied merely by knowing the implementation base. All 512 three-node directed graphs check cycle edges against independent reachability; downstream victims are not mislabeled as cycle members.

Validation (rtk proxy; isolated paired byte parity: 855 Core / 321 Community Python files):
- Initial diagnostic/contracts/metrics correction: 75 PASSED in 10.09s.
- Assessment integration and closed native mapping: 131 PASSED in 12.47s.
- Final core diagnostic, obligation/scenario assessment, metric/DTO and live catalog suites: 140 PASSED in 14.72s. Command: run_readiness_tests.py core -q tests/test_delivery_diagnostics.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py tests/test_delivery_readiness_contracts.py tests/test_delivery_readiness_metrics.py tests/test_mcp_tools_catalog_drift.py --tb=short --show-capture=no.
- Actual SQLite/current receipt regression: 28 PASSED in 70.66s. Command: run_readiness_tests.py community -q tests/test_delivery_readiness_rejections.py tests/test_delivery_readiness_scenarios.py tests/test_native_verification_context.py --tb=short --show-capture=no. Includes REST/MCP parity; no browser/new UI claim.
- Ruff passed for all modified Python files. Test counts overlap and are not a count of completed plan requirements.

Resume without restarting P00: finish P04.2 native same-snapshot composition of expected/observed facts, evidence references, versioned pinpoints and owner visibility; resolve exact action payloads/permissions/preconditions against native contracts; derive actual dependency edges from contribution/scenario/Decision relations, including implementation root causes in Test Card views. The pure graph is tested but concrete producer enrichment remains pending. Do not expose the conservative missing-context projection as the final user experience. Native lifecycle checks remain P06; structured integrity error transport belongs to P07. Review remaining native reason families for explicit mapping rather than generic fallbacks. Complete the full P02/P03 named-case audit, P05-P11, G07/U05, all API/MCP/UI consumers, bounded snapshots/cursors, performance and final 96-case/15-invariant/wheel acceptance. No whole-plan completion or P04-complete claim.

No frontend implementation or production runtime/data was changed. Local .playwright-cli/ and output/ captures/logs stay outside commits.

Final checkpoint checks: existing Core gate/legacy/contribution/verification regression passed (142 tests in 15.28s). Official paired source closure: 8,873 ownership rows, zero findings, exact README projections and all eight architecture budgets ZERO. Final wheel audit remains P11. All test/audit handles terminated; no live validation jobs remain.

## P04 native evidence details and shared invalidation causes — 2026-10-13

Previous pushed pair: Core 2eb4737f / Community 9c85aa62. Progress, no external blocker, no goal pause. P04 remains IN_PROGRESS; this checkpoint is not the completed readiness surface.

FactualReason now retains exact implementation IDs and bindings from the already evaluated scenario/contribution joins, including Test scenario projections. Diagnostics no longer need to reconstruct the expected implementation base. Reason sorting includes this provenance for deterministic output. Added services/delivery_readiness_diagnostics.py: pure composition over the authorized immutable snapshot and assessment, without authentication, another proof evaluation, per-unit queries or infrastructure imports. The service indexes native facts once, checks scope/identity/reference consistency, and produces expected/observed facts, typed evidence references, scenario/report observations, native implementation/test Card IDs and portable versioned Target/symbol pinpoints. Missing code locations remain entity-only; malformed absolute/host-local paths are not published as portable locations. Historical locations keep their observed revision and never acquire invented line numbers.

Owner visibility is supplied explicitly by the read composition; evidence authors are not inferred to be owners and restricted identities are not exposed. Permission changes affect action availability without changing causes, evidence or metric counts. Association actions contain only known native arguments: current scenario, exact implementation IDs, obligation refs, edition and Card version when supplied. Justification and idempotency key remain missing inputs. Tests validate these partial arguments against the real CardDeliveryEvidenceInput after providing those observations; production responses do not fabricate them. Signed current unbound results request association, not rerun. Wrong-base observations show both exact bases and the existing verification method. Text from observations remains data, never shell/HTML execution.

Canonical contribution relations now supply implementation predecessors; Test Card diagnostics reuse the provided full Spec assessment to expose relevant implementation root causes without N+1 assessment. Test implementation remains N/A and root-cause score impact remains verification. Decision verification consumes its native inspection/obligation plan and review records; failed/conflicting/revoked/pending inspections suggest native review rather than inventing tests for scope decisions. Rendering uses the closed DTOs, includes explicit null for unevaluated native transition gates, and bounds issue arrays (including expected/observed) to ten entries with exact totals/truncation flags. Full arrays remain internal for P05 detail paging; global byte limits/cursors and API integration are not yet claimed.

D02 reproduction: five record associations of one revoked execution produced five causes (1 FAILED / 14 deselected, 5.24s). Corrected action identity to reuse one execution refresh while preserving all five record references in detail. Generic accepted-execution failure is now retained under the concrete native rejection instead of producing an additional generic stale cause. Diagnostic classification reads only the binding's selected execution set and does not reapply aggregate rejection codes from the same admitted fact. No proof predicate or authority gate was relaxed.

Validation through rtk proxy and run_readiness_tests.py; isolated byte parity 856 Core / 321 Community Python files:
- Initial native composition plus existing factual/graph regressions: 98 PASSED in 10.15s.
- DTO/count/transport-contract regression before final D02 correction: 148 PASSED in 14.66s.
- Native SQLite signed-report diagnostics plus scenario/rejection/REST-MCP regression before D02 correction: 33 PASSED in 76.07s.
- Final Core: core -q tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_contracts.py tests/test_delivery_readiness_metrics.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py tests/test_mcp_tools_catalog_drift.py --tb=short --show-capture=no — 155 PASSED in 10.05s.
- Final Community: community -q tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_rejections.py tests/test_delivery_readiness_scenarios.py tests/test_native_verification_context.py --tb=short --show-capture=no — 33 PASSED in 97.73s. Includes inspection/static-analysis/demonstration signed reports, wrong source/revision, existing native input validation, unchanged ledger rows/session state and REST/MCP parity.
- Existing Core gate/legacy/contribution/verification regression: 142 PASSED in 16.25s.
- Ruff passed. Official paired source closure: 8,881 ownership rows, zero findings, exact README projections and eight architecture budgets ZERO. Generated MCP catalog still matches the live registry. Explicit final wheel closure remains P11.
- Renamed the graph regression suite to tests/test_delivery_readiness_diagnostics.py and placed native suites under the required test_delivery_readiness_*.py pattern. Earlier ledger commands are historical, not current file names. Counts overlap; do not sum them as distinct plan requirements. All validation processes terminated.

Resume: do not restart P00 or rebuild the factual/graph/native-detail producers. Finish the remaining P04 native-context audit, especially Decision obligation dependency cases, exact scope/selection invalidation identity, action procedure/context completeness and explicit mappings for remaining native reason families. P05 must load authorization, visible owners, Card versions, scenario procedures and all facts from one consistent batch snapshot and reuse the Spec assessment; compose_delivery_diagnostics is not yet wired into a public use case. P05 must also provide signed cursors, hard byte budgets, complete detail paging (including cycle edges) and generation-race handling. P06 supplies actual lifecycle gates/overrides and mutation reauthorization; P07 exposes the existing REST/MCP tool view and structured errors. P02/P03 acceptance audit, P08-P11, G07/U05, all consumers and the final 96-case/15-invariant/performance/wheel acceptance remain required. No P04-complete or whole-plan completion claim.

No frontend implementation, production reinstall/restart, runtime data mutation, release or deployment. Local Community .playwright-cli/ and output/ artifacts remain outside commits.

## User-requested checkpoint: P04 Decision outcomes and dependencies — 2026-10-13

Previous pushed pair: Core 2e0f3408 / Community be5a31bf. User requested commit and push of all pending implementation work. P04 remains IN_PROGRESS; this checkpoint does not complete the plan.

Readiness now explicitly maps every registered reason to its intrinsic state; unknown reasons fail with readiness_reason_unmapped instead of silently becoming missing. Decision inspection outcomes use a closed native map and preserve their status and exact obligation binding. Inconclusive and aborted inspections recommend reviewing the native Decision, without introducing a fictitious test. Unknown Decision outcomes fail with readiness_native_reason_unmapped. Action-specific prerequisites remain enforced.

Added a regression for a Decision depending on a functional obligation: an existing current result awaiting association leaves the Decision action waiting on that association. Associating the existing proof resolves the dependency and reaches verification 100 without another execution or inspection. Existing native inspection regression now covers inconclusive and aborted outcomes.

Validation: run_readiness_tests.py core -q tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py --tb=short --show-capture=no — 103 PASSED in 16.99s. Isolated installed/source byte parity: 856 Core / 321 Community Python files. Ruff passed for all four modified Python files. No frontend changes or browser validation claimed.

Resume: remaining P04 native-context audit includes scope/selection invalidation identity and action procedure/context completeness. P05 was investigated but NOT implemented: the current Community rollup performs per-Card queries and its lock_scope writes a fence, so readiness needs a consistent read-only batch adapter reusing existing validators. Decision review material must retain its existing authority predicates when receiving preloaded facts. Signed cursors, injected clock, generation-race handling, byte budgets and public integration remain pending. P02/P03 acceptance audit and P05-P11, including lifecycle/override authority, REST/MCP/UI consumers and final acceptance/performance/wheel checks, remain mandatory. No production reinstall/restart, data mutation, deployment or release. Community .playwright-cli/ and output/ are local generated artifacts and remain outside commits.

Official paired source closure passed with zero findings and all eight architecture budgets ZERO; README projections validated by audit_readiness.py. Final wheel closure remains P11. All test/audit processes completed.

## P05 consistent batch facts and native Decision timestamp regression — 2026-10-13

Previous pushed pair: Core 668ed531 / Community 5f8ed37b. Concrete implementation progress; no external blocker. P05 is now IN_PROGRESS. P04 remains IN_PROGRESS for its previously recorded context audit. This is not the completed readiness API or completed plan.

Added the provider-neutral DeliveryReadinessReadPort and frozen internal metadata for Spec/Card versions, scenario procedures and observed time. Added CommunityDeliveryReadinessReader in Community adapters: dedicated SQLite deferred read transaction, bounded batch loading of Board/Spec/Cards/current Card records, all relevant Spec review records/history, Targets, execution heads, investigation receipts and revocations. It loads signed current scenario heads once, retains negative/unbound outcomes, indexes Card allocation and related rows, and caches native execution validation within that read only. The native store's receipt, selection, contribution, test and waiver predicates are reused. Native Decision review material accepts preloaded rows while preserving authorship, separation, reconciliation and review-basis checks. No SQL or concrete clock entered Core.

The reader never calls lock_scope, commits, updates a ledger or mutates the graph. SQLite BEGIN is explicit because the driver does not start a database read snapshot for SELECT merely on logical Session.begin. data_version is checked on the same connection before the snapshot and after rollback. A committed race retries once; a second race returns readiness_snapshot_changed. Cross-scope/archived/missing requests cannot succeed with an empty population. Population over capacity fails explicitly instead of calculating a truncated percentage; malformed inventory remains unknown. This internal port still requires authorized composition before public use.

The fingerprint is SHA-256 over sorted input identities/content, evaluator/contract versions and effective factual validity. The injected evaluation clock is excluded from identity; the same facts at another time keep the fingerprint. Target revision changes invalidate both identity and proof. Full ORM rows, Board configuration, credential-bearing raw payloads and sessions do not escape. Scenario given/when/then, method, title, notes and criterion references are carried as authored data for later action context. Instrumentation logs duration/query/fact counts without evidence contents.

Reproductions and corrections: the first new suite produced 11 failures / 4 passes in 46.93s because the fingerprint serializer did not handle immutable sets; it now sorts them canonically. Next run: 14 passes / 1 failure in 35.51s; the failing fixture attempted direct Target revision mutation and was correctly refused by the native CAS trigger. Updated the fixture through the native Target operation. Broader run: 53 passes / 1 failure in 146.85s exposed an existing Decision inspection digest regression: ImplementationExecutionProof.observed_at is a datetime, and the inspection digest attempted JSON encoding without normalization. Fixed the pure Decision scope serialization to include UTC observation times, preserving their influence on the inspection basis. Equal instants in different offsets produce the same base; a different instant changes it; a timezone-less timestamp fails explicitly. No review approval or proof predicate was relaxed.

Validation through rtk proxy/run_readiness_tests.py; paired isolated installed/source byte parity: 857 Core / 322 Community Python files:
- Core inventory/Decision/readiness graph/native diagnostics/obligation/scenario regressions: 125 PASSED in 15.44s.
- Final new batch suite: 18 PASSED in 60.38s. Real SQLite concurrency proves old rows remain stable while another connection commits, the first race reloads and the second race errors. Complete signed proof on another Test Card still verifies the normal Card. Negative/inconclusive/aborted current observations and native Decision conflicts match the original snapshots. No new frontend or browser validation claim.
- Queries: 10 batched SELECTs plus BEGIN and two data_version PRAGMAs, unchanged for 2, 32 and 4,502 Cards. A local six-iteration comparison (first warm-up omitted) measured median batch 0.0456276000s versus legacy native rollup 0.0888277000s on the same proved fixture, below the 20% regression threshold. This is local evidence, not a production SLA or the final all-consumer performance acceptance.
- Final native Decision, rejection, diagnostics and delivery integration regression: 74 PASSED in 171.62s.
- Ruff passed. Official paired source closure: 8893 ownership rows, zero findings, eight architecture budgets ZERO and exact regenerated README projections. Explicit final wheel closure remains P11. All validation handles are terminal; test counts overlap and must not be summed as distinct plan cases.

Resume without restarting earlier producers: finish P04 action procedure/context and exact scope/selection cause identity audit; the batch now supplies scenario procedures but diagnostics do not yet consume them. P05 still needs authorization/visibility enrichment in the same authorized composition, the services/delivery_readiness.py orchestration and reuse of one Spec evaluation for Card views, authenticated cursors, summary/detail paging with actual byte limits and cycle edges, and the complete named-case audit. No public use case is wired to the new batch reader yet. Retain current request authorization and reauthorize all writes; readiness is never an authorization token. P02/P03 acceptance audit, P06-P11, G07/U05, all API/MCP/UI consumers and the final 96-case/15-invariant/performance/wheel evidence remain mandatory. No whole-plan or P05-complete claim. No production install/restart/data mutation, deployment or release. Local .playwright-cli/ and output/ artifacts stay outside commits.

## P05 bounded summary/detail and authenticated continuation — 2026-10-14

Previous pushed pair: Core f3176d7a / Community 40e685b4. Concrete progress; no external blocker or goal pause. P05 remains IN_PROGRESS and the public readiness view is not exposed yet.

Added services/delivery_readiness.py with PreparedDeliveryReadiness and exact compact UTF-8 serialization. It consumes an already evaluated, visibility-filtered context plus native diagnostics, never authenticates another proof, recalculates a percentage, queries storage or evaluates/mutates a transition. The caller still has to supply the actual native gate/lifecycle context. Header and diagnostics must have the same Board/Spec/edition/Card; inconsistent graph/detail/action references fail explicitly. NativeDeliveryDiagnostics now retains its scope and Card identity.

The summary pages at most 20 causes and three next actions within 32 KiB; each existing issue array retains its ten-item cap and exact totals. A phase filter slices issues and their related guidance but preserves the complete metrics, gate/lifecycle context, qualification and global summary. The separate closed detail response retains the same context and pages at most 20 typed affected_unit/evidence/pinpoint/action entries within 128 KiB. Full expected/observed facts, evidence, criteria/scenarios, related Cards, native codes, issue dependencies, action payloads and cause/action cycle edges are recoverable through those entries. Entries are stably ordered and identified by their type and canonical content. Pagination does not equate scope/evaluation completeness with response completeness.

Byte limits measure the actual serialized response with significant nulls present. A page is tested with its actual continuation token; the chosen prefix is the largest fitting one. The final page may fit even when a shorter page plus cursor does not, so the fitter does not assume monotonic size. A single unrepresentable item fails explicitly with readiness_limit_exceeded and its already authorized issue identity; mandatory fields are not silently removed. Oversized detail facts remain explicit errors rather than partial facts.

Added the public DeliveryReadinessCursorPort and a Community adapter using the native installation setting guideline_policy_cursor_signing_key, native HMAC-SHA256 envelope rules and a readiness-specific derived-key namespace. No random/fallback secret, filesystem/environment reader or concrete signer entered Core. Tokens bind authenticated actor, Board/Spec/Card, edition, snapshot, view, phase, issue, limit and the visibility-sensitive projection. Scope/filter/actor or signature mismatch is refused; edition/snapshot/projection changes produce readiness_cursor_stale. A fresh codec with the same installation key can resume; another installation cannot. The token carries hashes rather than raw actor/Board/Card identifiers. Canonical JSON/base64, closed field shape, integer offsets and fixed digest shapes are checked even for correctly signed malformed payloads. The projection identity excludes evaluated_at, so merely observing the same facts later does not stale the cursor. A changed owner visibility/action projection does stale it.

ReadinessReadQuery is currently the internal paging contract. P07 still must extend the existing DeliveryEvidenceReadQuery/tool/REST route and route readiness before progress_history; no new MCP tool or public alternate endpoint was added. DeliveryReadinessContext shares the existing closed summary fields with DeliveryReadinessDetail. No transport serializer has yet been wired to serialize_readiness_response; preserving its actual byte budgets and nulls at REST/MCP is still part of P07 acceptance.

Validation via rtk proxy/run_readiness_tests.py, with paired isolated installed/source byte parity: 858 Core / 323 Community Python files:
- Initial pure paging plus contracts/native diagnostics: 55 PASSED in 7.69s.
- Initial real HMAC/closed-envelope/native SQLite + signed-report continuation: 32 PASSED in 17.71s.
- Broader pre-scope-guard Core regression: 172 PASSED in 20.76s.
- Final Core paging, contracts, native diagnostics, graph, metrics, obligation/scenario assessments and live MCP catalog drift: 175 PASSED in 26.47s. Command: core -q tests/test_delivery_readiness_paging.py tests/test_delivery_readiness_contracts.py tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_metrics.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py tests/test_mcp_tools_catalog_drift.py --tb=short --show-capture=no.
- Final Community cursor, batch snapshot and native diagnostic regression: 55 PASSED in 91.30s. Command: community -q tests/test_delivery_readiness_cursor.py tests/test_delivery_readiness_batch.py tests/test_delivery_readiness_diagnostics.py --tb=short --show-capture=no.
- Fixtures recover all 47 summary causes and all 250 affected units plus long evidence lists, enforce actual UTF-8 byte limits, preserve scores across every page, retain cycle edges, refuse cross-scope assembly and refuse changed native snapshots during continuation. A real signed inspection fixture keeps I=100/V=0 while two current results await association; another read/codec resumes the next distinct cause, and changing a Card version stales the old cursor. No fabricated gate authorization is asserted in this test.
- Ruff passed. Official paired source closure: 8902 ownership rows, zero findings, eight architecture budgets ZERO and exact README projections. The MCP catalog remains byte-equal. Explicit final wheel audit remains P11. All validation handles are terminal; overlapping test counts are not distinct plan requirements.

Resume: do not rebuild the graph, native detail, batch loader, paging or signing foundations. P05 still requires authorized composition/visibility against current native authority in the read snapshot, reuse of the full Spec evaluation for Card projections, and capacity failures surfaced as unknown rather than a successful truncated population (S10). PreparedDeliveryReadiness accepts derived context; it does not yet construct native gate/lifecycle flags or authorize callers. P04 still needs the recorded scenario procedure included in actionable external guidance (§7.6), plus the exact scope/selection cause identity audit. The batch already carries procedures, but compose_delivery_diagnostics does not consume them yet. Complete P02/P03 named-case audit, P06 native gates/override authority (G07), P07 transport integration, P08/P09 frontend including U05 and browser inspection, P10 packaging/docs, and P11 complete 96-case/15-invariant/performance/wheel acceptance. No P05-complete or whole-plan completion claim. No production install/restart/data mutation, deployment or release. Local Community .playwright-cli/ and output/ artifacts remain outside commits.

## P04 authored verification procedures and scope mismatch details — 2026-10-14

Starting pushed pair: Core 38be03b8 / Community 3f5e0106, both feature/v0.4.0. The preceding user-requested push verified both remotes already current; this continuation makes concrete implementation progress. P04/P05 remain IN_PROGRESS. This is not a completed public readiness view or whole-plan delivery.

Implemented the missing §7.6 procedure context in native diagnostics. compose_delivery_diagnostics accepts the batch's authorized ReadinessScenarioProcedure metadata. execute_verification now carries known Board/Spec/Card/edition, only the relevant authored scenarios (title, given, when, then, notes, method and criterion IDs) and exact evaluated execution/Target/version/source/revision bases in arguments. The surface remains external_agent with pulse_tool=null. Procedures are data; reading readiness executes nothing and fabricates no command, receipt, outcome, hash, justification or idempotency key. Missing procedures/required steps leave the action requires_input. Conflicting method/criteria or duplicate procedure identities fail explicitly rather than mixing snapshots. An existing valid current result still produces association guidance instead of reexecution. Arbitrary observed report text is not substituted for the procedure. Irrelevant scenario metadata does not enter the action.

Completed another part of the P04 scope-context audit: a scope_contract_changed cause now includes the actual current definition/contribution hashes and the hashes attested by the affected implementation record, with its Card and record identity. These known hashes participate in action context so different scope mismatches are not represented by an indistinguishable remedy. The diagnostic does not revalidate the proof or alter either metric; it consumes the canonical stale classification. The complete scope/selection named-case audit is still pending.

Files changed: Core domain/delivery_diagnostics.py (closed external guidance argument names), services/delivery_readiness_diagnostics.py (procedure and exact scope context), tests/test_delivery_readiness_diagnostics.py and tests/test_delivery_readiness_native_diagnostics.py. Community tests/test_delivery_readiness_diagnostics.py exercises actual batch metadata and signed-report fixtures. No SQL, filesystem, concrete runtime, transport or signer was added to Core. No frontend changed; browser/UI validation is not claimed.

Validation via rtk proxy and run_readiness_tests.py, isolated installed/source byte parity 858 Core / 323 Community Python files:
- Initial Core graph/native/paging run: 4 FAILED / 78 PASSED in 13.49s. The test incorrectly assumed cause identity cannot change when its action payload changes; the specified grouping includes the remedy. Corrected the test to compare factual code/units/origin and unchanged metrics, retaining the changed guidance identity. Rerun: 82 PASSED in 10.55s.
- Initial Community native procedure run: 6 FAILED / 5 PASSED in 34.01s. The new test expected the pure Core fixture's c-revision, but the real native fixture records the a-revision. Corrected only that fixture expectation; product output already named the actual accepted base.
- Final Core: 163 PASSED in 14.81s. Command: core -q tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_paging.py tests/test_delivery_readiness_contracts.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py tests/test_mcp_tools_catalog_drift.py --tb=short --show-capture=no.
- Final Community: 79 PASSED in 115.65s. Command: community -q tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_batch.py tests/test_delivery_readiness_cursor.py tests/test_delivery_readiness_rejections.py --tb=short --show-capture=no. Native inspection/static-analysis/demonstration cases cover missing execution and signed wrong-base results; authored procedures and accepted bases are preserved and ledger rows remain unchanged.
- Ruff and git diff --check passed. Official paired source closure: 8902 ownership rows, zero findings, all eight budgets ZERO and exact README projections. Live MCP catalog drift passed. Final wheel closure remains P11. All test/audit handles are terminal. Counts overlap and are not distinct plan-case totals.

Resume at authorized P05 composition, not at rebuilding existing foundations. Fresh authority and detail visibility must be resolved in the same read snapshot; native candidate paths investigated are CommunityAgentAuthenticationGateway.resolve_agent_permission_context (active Agent + Board binding + preset lineage), CommunitySqlAlchemyApplicationPersistence.resolve_user_permissions (native REST user policy), and the public permission_policy seam. No authorization implementation was added this turn. Decision read requires spec.entity.read and code_traceability.evidence.read; Target details use code_traceability.target.read. Do not rely solely on cached actor permissions, infer a principal from transport, import private Core mechanisms or broaden native authority. Also pending: reuse full Spec evaluation for Card projections, unknown capacity S10, P02/P03/P04 named-case closure, P06 gates/G07, P07 existing REST/MCP view wiring/serialization, P08/P09 frontend/U05/browser validation, P10 documentation/build and P11 all 96 cases/15 invariants/performance/wheel acceptance. Preserve the full original plan. No production reinstall/restart/data mutation, deployment or release. Community .playwright-cli/ and output/ remain local artifacts outside commits.

## P05 current authority and restricted diagnostic details — 2026-10-14

Starting pushed pair: Core 079f0393 / Community 52b20e78 on feature/v0.4.0. The previous goal turn made implementation progress; this turn continues that progress with no external blocker. P05 remains IN_PROGRESS, and the full readiness plan is not complete.

The public read port now requires a ReadinessActor carrying authenticated identity, kind, realm and optional Board binding, without cached permission claims. The Community batch reader resolves current Board membership, active Agent and grant, preset lineage, direct flags and Board ceilings in the same explicit read transaction as the delivery facts. Human owner/share access and the existing REST first-created-Agent policy lookup are retained; an inactive Agent's policy can still apply to its authenticated human creator exactly as in the native user resolver. Agent access, separately, requires an active Agent and live Board grant. Cross-realm or mismatched Board identities fail before reading Spec/evidence content. An empty or malformed permission layer is not converted into full access.

Core's public readiness seam projects fresh policy into immutable allowed operations via the canonical permission policy. evidence.read and Spec read are required; a requested Card additionally requires Card read. Denial raises a typed authorization error rather than a successful empty population. Community returns access facts with the batch; actor, effective capabilities and native Spec-write membership enter the snapshot fingerprint. A concurrent committed grant revocation causes the existing one-retry protocol to reload authority and refuse the read instead of returning its earlier allowed result. The actor-less adapter path remains internal fact-loading compatibility for existing callers/fixtures only; compose_authorized_delivery_diagnostics refuses snapshots without authority. Future public integration MUST call the actor-required port and authorized composition path.

services/delivery_readiness.py now composes native diagnostic details/actions against those same-snapshot access facts. The aggregate assessment is preserved. Restricted Target/receipt/test/Card details are removed together from expected/observed/evidence/pinpoints, action arguments/context references and the display origin. Public Spec/evidence unit references and the factual cause remain available; detail_state=restricted explicitly distinguishes redaction from empty findings, in both summary and paginated detail. Owner IDs remain restricted without the corresponding Card/Agent visibility. Redacted actions become unavailable or retain their native forbidden state. The visibility-sensitive projection hash prevents an old detail continuation from being reused even if the factual snapshot ID is unchanged.

D09 correction: execute_verification guidance now declares the native spec.tests.execute permission. Missing authority does not recommend execution; explicit denial produces forbidden, without changing metrics. Executing/admitting a scenario also honors native writable Spec membership (owner/editor/admin or verified Agent grant). This is kept separate from associating an existing result: the latter's native delivery-record path does not acquire a new share-role gate. Read-only sharing and later promotion to editor change execution availability and snapshot identity while preserving identical proof facts. No mutation, final transition or approval is authorized by readiness.

Changed Core files: ports/delivery_readiness.py, domain/delivery_diagnostics.py, models/delivery_readiness.py, services/delivery_readiness.py, services/delivery_readiness_diagnostics.py; new tests/test_delivery_readiness_access.py plus the existing diagnostic test fixtures. Changed Community files: adapters/sqlalchemy_delivery_readiness.py; new tests/test_delivery_readiness_authorization.py plus native diagnostic fixtures. Official README ownership fragments were regenerated in both repos. SQL, membership/preset loading and transaction mechanics remain in Community; Core adds only typed facts, policy decisions and read-only projection. No frontend changes or browser verification claimed.

Validation via rtk proxy/run_readiness_tests.py, paired isolated installed/source byte parity 858 Core / 323 Community Python files:
- Initial Core access/paging/native diagnostic suite: 62 PASSED in 9.60s.
- Initial Community authority suite: 5 FAILED / 15 PASSED in 50.28s. The fixture used permission_overrides={} as though it meant no ceiling; native policy correctly denies introduced permissions when a materialized ceiling omits them. Corrected the fixture to None, retained a dedicated empty-ceiling denial regression, and used complete native ceilings for selective denials. No native permission rule was relaxed.
- Intermediate Core suite: 123 PASSED in 11.81s. Intermediate Community authorization/native diagnostics: 32 PASSED in 87.83s. Intermediate batch/cursor/native authentication/preset-lineage regressions: 69 PASSED in 104.01s. Additional Core metric/assessment/catalog checks: 81 PASSED in 16.59s.
- Diff review found that the new Core tests initially reused the P00/G07 authorization test filename. Moved the new suite to test_delivery_readiness_access.py and restored the original G07 file byte-for-byte from the clean starting HEAD; git diff for that original file is empty. G07 remains the explicitly pending P06 regression, not silently removed or claimed green.
- Final Core: 205 PASSED in 17.53s. Command: core -q tests/test_delivery_readiness_access.py tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_paging.py tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_readiness_contracts.py tests/test_delivery_readiness_metrics.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py tests/test_mcp_tools_catalog_drift.py --tb=short --show-capture=no. The earlier identical test set before the filename correction passed 205 in 18.42s; the final run confirms the corrected file layout.
- Final Community: 103 PASSED in 141.51s. Command: community -q tests/test_delivery_readiness_authorization.py tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_batch.py tests/test_delivery_readiness_cursor.py tests/test_native_agent_authentication.py tests/test_ska_permission_lineage_adapter.py --tb=short --show-capture=no.
- Real SQL tests cover owner/shared-human/Agent access, inactive/revoked/missing grants, wrong realm/Board/kind, unknown/cyclic presets, owner-review, selective permission denial before facts, native human policy equivalence, share-role changes, concurrent grant revocation, unchanged ledger/Agent rows and secret-free results. Authorized Agent reads use 17 SQL commands for both 2 and 32 Cards (the original 13 plus four authority batch queries), with no per-Card authorization query. This is a query-bound check, not final latency acceptance; Spec evaluation reuse and final P11 performance remain pending.
- Ruff and git diff --check passed. Paired source closure: 8908 ownership rows, zero findings, all eight budgets ZERO, exact README projections. MCP catalog drift passed. The final wheel closure remains P11. All validation/audit handles are terminal. Overlapping test totals are not distinct plan-case counts.

Resume: preserve this authorized batch/projection path, existing procedures, graph, paging and signing. Next P05 work is sharing one full Spec evaluation across Card projections and turning capacity failures into an explicit unknown assessment (S10), then complete its acceptance audit. The service still does not build native gate/lifecycle context; P06 remains necessary, including the existing G07 tests. P07 must wire the existing DeliveryEvidenceReadQuery/REST/MCP readiness view through the authorized reader/composer and exact serializer, map ReadinessAccessDenied through native error envelopes, and route before progress_history. P08/P09 must render detail_state=restricted distinctly and complete two-bar/UI/U05/browser behavior. P02/P03/P04 named-case closure, P10 docs/build and P11 all 96 cases/15 invariants/performance/final-wheel acceptance remain required. No production install/restart/data mutation, deployment or release. Community .playwright-cli/ and output/ stay local outside commits.

## P05 shared evaluation foundation — user-requested checkpoint — 2026-10-14

User requested commit and push of all pending project changes. Starting pushed pair: Core bf04d7c5 / Community ae5f8b74, feature/v0.4.0. This checkpoint preserves the current work; P05 remains IN_PROGRESS and no additional milestone or whole-plan completion is claimed.

Core domain/effective_delivery_coverage.py now derives full-obligation and per-contribution rows from the same admitted proof joins. EvaluatedDeliverySnapshot evaluates proof and lifecycle once each, indexes the results, and supports Spec/Card/scenario projections tied to the exact input snapshot. Local Card sufficiency is derived from that Card's contributions rather than the global result. Existing assessment entry points consume this foundation; scope mismatch rejects reuse. No mechanism, transport or permission relaxation was introduced.

Validation of this checkpoint:
- Paired isolated build/source byte parity: 858 Core and 323 Community Python files; production installation was not changed.
- Core: 167 passed in 14.44s. Command: run_readiness_tests.py core -q tests/test_effective_delivery_coverage.py tests/test_delivery_obligation_assessment.py tests/test_delivery_scenario_assessment.py tests/test_delivery_readiness_access.py tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_readiness_metrics.py --tb=short --show-capture=no.
- Community: 29 passed in 74.97s. Command: run_readiness_tests.py community -q tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_batch.py --tb=short --show-capture=no.
- Ruff and whitespace checks passed. Official paired source closure: 8908 ownership rows, zero findings, all eight budgets ZERO; README projections unchanged. Final wheel acceptance remains P11.
- No frontend change; no browser validation claimed. All test/audit processes completed.

Resume with authorized service-level orchestration that shares one evaluated snapshot across multiple Card reads and dedicated call-count/performance/equivalence acceptance tests; those are not yet implemented. Capacity-to-unknown handling (S10), P05 acceptance, the previously recorded P02/P03/P04 gaps, P06 gates/G07, P07 transport, P08/P09 frontend, P10 packaging and P11 final acceptance remain pending. Preserve the existing plan and authority rules. Community .playwright-cli/ and output/ are local generated inspection artifacts excluded from version control. No production restart/reset, deployment or release.

## P05 shared authorized projections and capacity unknown — 2026-10-14

Starting pushed pair: Core fe0a24f2 / Community dc67db3e, feature/v0.4.0. Previous goal turn classified as progress: it validated and committed the shared evaluation foundation. This continuation implements its authorized service composition and S10 capacity handling. P05 remains IN_PROGRESS; full-plan completion remains unproved.

EvaluatedDeliveryReadiness authorizes Spec/evidence access before evaluating any proof, owns one exact request-local batch, and derives cached Spec, normal/bug Card and Test assessments from two canonical proof/lifecycle passes total. Card projections additionally require Card read and membership in that batch. Diagnostics reuse the full Spec assessment for Test implementation predecessors and retain existing visibility/action policy. No cache crosses read requests, policy changes or snapshots, and no read authorizes a transition. Domain inventory population checks and Card contribution indexing are prepared once rather than reconstructed for every Card.

ReadinessSnapshot now carries explicit unavailable_reason_codes. A Community fact-population overflow discards the incomplete prefix and returns only the validated Spec/optional Card header with empty, incomplete facts and capacity codes. The Core produces an unknown assessment with a capacity diagnostic; it does not run the proof evaluator or calculate a truncated percentage. Test implementation remains structurally N/A; Test verification is unknown. Requested Cards are checked by exact Board/Spec/archive scope even when outside the population cap. Current authority is still resolved before loading facts. Permission/preset population overflow is outside the fallback and remains an error because access cannot be established. Snapshot consistency/retry, injected clock and rollback-only behavior are retained; no concrete mechanism enters Core.

New dedicated Core tests cover current, missing-contribution, missing-association, failed, open-lifecycle, exact-alias, waived, incomplete, missing-topology, partial and stale states. They assert no further proof evaluation during Card/diagnostic projection; known expected cases independently verify local 100/global 0, cross-Card verification, scenario 50%, lifecycle pending and alias evidence identity. A 4500-Card fixture evaluates the proof universe twice total and projects all local denominators without extra canonical passes. Separate tests refuse missing authority, foreign actor/Board/Card and stale input reuse, observe changed policy/facts on a new read, and prove capacity never evaluates the incomplete universe.

Community S10 tests use real native SQLite and authorization: Spec, Task and Test capacity responses, deterministic unknown snapshot, explicit native diagnostic, no writes, rejected foreign Card and revoked grant. An overflowing authority population cannot produce an authorized unknown response. Existing capacity regression now verifies explicit unknown rather than an exception, as required by plan section 9.3. Native authorization rules and budgets are unchanged.

Validation (paired isolated install/source parity: 858 Core / 323 Community Python files):
- Initial dedicated shared-evaluation suite: 20 passed in 6.77s; expanded with near-limit and capacity cases: 22 passed in 8.11s.
- Final Core regression: 270 passed in 25.47s. Files: test_effective_delivery_coverage, test_delivery_obligation_assessment, test_delivery_scenario_assessment, test_delivery_readiness_shared_evaluation, test_delivery_readiness_access, test_delivery_readiness_diagnostics, test_delivery_readiness_native_diagnostics, test_delivery_readiness_paging, test_delivery_readiness_contracts, test_delivery_readiness_metrics and test_mcp_tools_catalog_drift.
- Community native authority/batch regression: 46 passed in 90.38s. Files: test_delivery_readiness_authorization and test_delivery_readiness_batch.
- Ruff and whitespace checks passed. Supplementary diagnostic/cursor regression and official source closure results are recorded below after completion.

Resume: wire this authorized shared composition into existing transport/Board consumers in P07/P08, not a second evaluator. P05 final performance acceptance is still pending: diagnostics currently rebuild their detail index for a new Card projection; measure/reuse where required by section 9.4, and measure many contributions to one obligation as well as many obligations. S10 native evaluator/expansion limits and all named-case acceptance still require the complete audit. Preserve pending P02/P03/P04 closure, P06 gates/G07 and lifecycle context, P07 REST/MCP, P08/P09 frontend/U05/events/browser, P10 docs/build and P11 all 96 cases/15 invariants/performance/final wheels. This checkpoint does not claim the public readiness view or entire plan delivered. No production install/restart/data reset, deployment or release.

Validation completion: supplementary Community diagnostic/cursor suites passed 43 tests in 43.14s. Official paired source closure produced 8912 ownership rows, zero findings and all eight architecture budgets ZERO; exact README projections regenerated in both repositories. All validation/audit processes are terminal. These overlapping test totals do not replace the plan's named-case acceptance audit.

## P06 typed gates and protected override — 2026-10-14

Starting pushed pair: Core 6afdbd5c / Community 603f82a8 on feature/v0.4.0. Previous goal turn classified as progress. P06 is IN_PROGRESS; earlier named-case/performance acceptance remains open.

Core assess_delivery_gate now consumes the same immutable assessment as metrics and diagnostics. Typed blockers carry phase, binding, factual reasons and uncertainty. Normal/bug completion owns implementation only; Spec completion additionally checks verification and native evidence lifecycle; Test completion remains outside this seam with its existing transition controls. Both require_card_delivery and require_spec_delivery use this predicate. Removed the Card gate's exclusion list of verification error strings and independent proof scan. Unknown verification methods or a new verification-only failure cannot accidentally block ready implementation. Structural unknown still fails closed. Risk waiver cannot turn unavailable verification into known gate authority. Advisory and the Spec-only override keep their native scope without creating proof credit; an unused override does not manufacture an exception. DeliveryGateBlocked retains typed assessed deficits for future structured transport errors.

Prospective Card reports still pass through the native snapshot loader before persistence. Spec for_update fences before loading current facts. Inspection confirmed existing real writers retain their Board fences: Card Done before policy/proof, task validation before mutable policy evaluation, and Spec late fence/reload/recheck. No cached read is accepted as mutation authority. Complete real race/rollback acceptance remains pending; this checkpoint does not claim P06.3 complete.

skip_delivery_evidence now requires spec.entity.edit_coverage_flags in both directions. Board access and Draft-only service guards remain unchanged. The Spec modal supplies the override callback only for authorized, unarchived Draft with state permission; otherwise the existing panel displays a passive state. No new permission, concrete mechanism or MCP tool was introduced.

Validation:
- Initial focused Core gates/Card/override: 36 passed in 7.93s. Expanded run: 2 failed / 118 passed in 21.76s because the authorized-write fixture omitted unrelated post-write effective-knowledge seams; stubbed only those read projections. Native permission, writer/commit and Draft guard remain exercised. Rerun: 120 passed in 20.98s.
- Final Core: 121 passed in 35.41s, including risk waiver with unavailable verifier. Files: test_delivery_readiness_gates, test_delivery_card_readiness, test_delivery_readiness_authorization, test_delivery_obligation_assessment, test_delivery_scenario_assessment, test_delivery_readiness_shared_evaluation.
- Community native regression: 90 passed in 289.77s. Files: test_delivery_evidence_integration, test_delivery_contributions, test_delivery_progress_currentness, test_delivery_selection, test_delivery_report, test_technical_report_delivery, test_multicard_delivery_integration. Signed reports, cross-Card joins, selection/currentness and report rollback exercised.
- Frontend initial: 3 failed / 84 passed; negative-permission fixture omitted Coverage read permissions. Added those existing read permissions while withholding edit/state authority. Focused retry: 9 passed. Final full pair: 87 passed in 86.81s (SpecModal.validationTabs.test.tsx and DeliveryEvidencePanel.test.tsx).
- Ruff passed. Isolated installed/source byte parity: 858 Core / 323 Community Python files. Production installation unchanged.
- npm run build passed; 79 packaged files synchronized, tree SHA-256 5c6e02f758cef4502563aa3672d9f329fa8ac99a130853b47769c7b8c7d2a224. Verification/lint/final supplemental checks recorded below.

Actual Playwright CLI/browser check used fresh DATA_DIR=PULSE_REFACTOR/.validation-v040/readiness-gates-home, API/MCP 8128/8129, Vite 5178. The disposable launcher sets VITE_API_URL=/api/v1 and proxy 8128 after detecting that local .env otherwise targets 8100. Created a mock Spec through native REST. Clicked the Draft override in Coverage > Implementation; API readback confirmed true/version=2 while proof gaps remained. Moved to Review through the UI; switch became passive Skipped/version=3. Native PATCH disabling override returned 409 subject_edit_requires_draft and left Review/true/version=3 unchanged. Captured and visually inspected output/playwright/readiness-gate-draft.png and readiness-gate-review.png. Browser/servers closed; no listeners remained at 5178/8128/8129. Isolated KG lifecycle logs contained a graph failure; this UI exercise is not KG acceptance and no workaround was added.

Resume with remaining P06 lifecycle/context composition and native transition reuse without mutation/recursion; finish actual G10/G11 race/transaction acceptance and the remaining named cases. Lifecycle blockers name the affected binding without inventing which Test Card is open; enrich using native evidence in the lifecycle diagnostic. Preserve pending P05 detail-index/performance and capacity-expansion audit, P02/P03/P04 closure, P07 REST/MCP readiness view/errors, P08/P09 official two-bar consumers/U05/events/browser, P10 and all P11 acceptance. No whole-plan completion, production reinstall/restart/reset, deployment, release or historical rewrite. Local browser artifacts stay outside commits.

Checkpoint requested by the user (commit and push all pending Pulse work): final supplementary Community suites passed 21 tests in 117.19s (technical_report_delivery, multicard_delivery_integration, delivery_readiness_waivers). Packaged frontend verification passed for all 79 files with the SHA-256 above. Final source closure artifact reports 8913 ownership rows, zero findings and all eight budgets ZERO. git diff --check passed in both repositories. Frontend lint exited 1: zero errors, 315 warnings; react-refresh/only-export-components exceeds its per-rule budget (34 > 32). This lint failure remains explicit and the budget was not relaxed. Commit includes source, tests, generated distribution, README projections and ledgers; local browser output, disposable environments and runtime data are excluded. P06 and the full plan remain in progress.

## P06 current qualification and snapshot policy — 2026-10-14

Starting pushed pair: Core 9fb99efd / Community db78e44d. Previous turn classified as progress (commits and pushes confirmed). P06 remains IN_PROGRESS.

Implemented pure qualify_delivery composition over the shared assessment and typed gate: explicit proof_complete, current qualification, technical delivery state and historical divergence. Structural uncertainty precedes exceptions; effective exceptions precede verification failures; failed/conflicting verification precedes implementation and verification deficits. Stored Done supplies neither proof nor current native authority. Actual native closure evaluation is required to label a current delivery delivered; an unknown transition check remains explicit. Effective exceptions preserve factual deficits and metrics. Normal/bug implementation can pass its own delivery gate while technical verification remains pending.

EvaluatedDeliveryReadiness.context now composes the closed context from the same authorized batch, cached assessment and diagnostics. It exposes both metrics, scope/version/snapshot/time, gate exceptions, unique unresolved-unit counts and a historical-divergence message without mutating state. Other lifecycle gates are explicitly not_evaluated, with native_transition_context_required and the native Card/Spec context reference. This is an intermediate integration seam, NOT completion of P06.5: it does not yet collect all native blockers or assert transition permission. PreparedDeliveryReadiness consumes this context in tests without a separate proof evaluation.

Community batch and capacity snapshots now carry the Board delivery gate mode and Spec-only override from the same read transaction. Both affect snapshot identity, including capacity-unknown snapshots. The native policy resolver was factored into the domain and exposed through the public readiness port for the adapter; the mutation service delegates to the same resolver. No Community import of a Core service/private implementation, policy duplication or added query.

Validation:
- Initial Core focused run: 116 passed / 1 failed in 14.18s. P01's exact import allowlist lacked standard-library typing (from the prior typed-gate commit) and collections.abc (shared policy resolver). Added only those stdlib contracts; no infrastructure allowance.
- Final Core: 135 passed in 18.74s (readiness_state, readiness_gates, readiness_shared_evaluation, readiness_paging, readiness_contracts, delivery_card_readiness).
- Community: 48 passed in 125.53s (readiness_batch and readiness_authorization). Includes actual SQLite policy/override changes and snapshot identity for normal and capacity-unknown batches; query-count, concurrency, authority and no-write checks remain passing.
- Ruff and git diff --check passed. Paired isolated install/source byte parity proved 858 Core / 323 Community Python files. Official paired source closure: 8915 ownership rows, zero findings and eight budgets ZERO; README projections regenerated.
- No frontend UI modification or production runtime change. Existing lint ratchet failure remains recorded in the prior checkpoint. All test/audit handles are terminal.

Resume: complete native lifecycle composition by sharing the real read-only gate evaluation, not calling move_card/move_spec in dry-run. Investigation: CardService._task_completion_gate_failures currently invokes the fenced Policy Compliance enforcement, so it cannot be blindly reused by a read. GuidelineService.preview_policy_transition already offers a non-locking native seam. move_card requires a conclusion/report and may require Bug learning input; absent transition inputs must remain not_evaluated. Spec Done checks linked normal tasks, cognitive/resource/architecture gates and Policy Compliance before a late delivery fence/reload/recheck. Preserve full G10/G11 transactional acceptance pending. Then finish remaining P02–P05 audits, P07 public REST/MCP, P08/P09 official UI consumers and frontend/browser acceptance, P10 and P11 full named-case/invariant/wheel/performance audit. Do not claim this context alone completes lifecycle diagnosis or the plan.

## P06 shared native Spec completion checks — 2026-10-14

Starting pushed pair: Core 38aa2e54 / Community 2d8c859b. Previous goal turn: progress. P06 remains IN_PROGRESS.

Extracted SpecService.validate_completion_gates from the real move_spec path. The mutation and ListAllowedTransitionsUseCase now share AC/scenario coverage, missing semantic links, authoritative active normal-Card completion query, cognitive and resource/architecture checks. Removed the preview's independent copy (including its dependence on a possibly incomplete spec.cards relationship). The shared method preserves native mutation messages, filters, order and policy; no additional gate, override, history rewrite or changed status eligibility. Delivery and Policy Compliance stay separate, with their existing preview/fenced write paths; the late delivery fence/reload/recheck remains unchanged. Cognitive preview uses its existing read_only_preview path.

Validation:
- Six new native-database cases cover open, Done, cancelled and archived normal Cards plus open Bug/Test Cards. Preview/native shared calls agree; the authoritative query ignores a deliberately empty loaded relationship. SQL capture permits SELECT only; no pending ORM changes, status mutation or proof write.
- First broad run: 63 passed / 4 failed in 33.37s. Three code-evidence fixtures and one AF23 preview fixture omitted current Decision verification populations/plans. Supplied explicit empty native requirement populations and a Spec-scoped inspection plan, preserving the real Decision gate; no gate mock or bypass.
- Rerun: 67 passed in 27.54s (allowed_transitions_mutation_parity_regressions, af23_allowed_transitions, skb_b10_allowed_transition_preview, delivery_readiness_gates).
- Native Spec REST/MCP use-case regressions: 84 passed in 42.94s (r01a_spec_crud_uow and r01a_mcp_spec_uow). Ruff and diff whitespace checks passed. Isolated installed/source parity: 858 Core / 323 Community Python files.
- No frontend source or production runtime change. Architecture source closure result is recorded below when terminal.

Resume: wire the shared native completion checks into readiness's authorized consistent snapshot/context and lifecycle diagnostics; compose Policy Compliance through its preview, never its fenced writer. Card completion still needs corresponding shared native evaluation and explicit missing report/validation/learning inputs. Preserve remaining G10/G11 real race/rollback acceptance, P02–P05 audits, P07 REST/MCP integration/catalog, P08/P09 UI/events/frontend/browser, P10 and P11 full acceptance. The new helper alone does not complete P06.5 or the plan.

Final source closure: 8913 ownership rows, zero findings, all eight budgets ZERO; exact README fragments regenerated in both repositories. All test and audit handles are terminal. Production install, processes and data remain unchanged.

## P06 native lifecycle integration - work-in-progress checkpoint, 2026-10-14

User requested committing and pushing all pending Pulse work. Starting pair: Core e0ac6d5d / Community fd1a6695 on feature/v0.4.0. This checkpoint preserves incomplete work; P06 and full-plan acceptance remain IN_PROGRESS.

Core now exposes typed native lifecycle observations and current completion-state permissions through its public readiness contract. The lifecycle service reads scalar Spec/Board records through public persistence and invokes the shared read-only Spec completion checks. Typed coverage/task blockers feed context and diagnostics without reducing proof scores or revealing private error text. An incomplete native check never grants final transition authority; Card completion composition remains pending. Community integrates this evaluation into the authorized read snapshot and its fingerprint.

Validation:
- Final Core regression: 172 passed in 41.36s (readiness lifecycle/state/shared evaluation/access/paging, allowed-transition parity, AF23, SKB B10 and readiness gates).
- Official paired source closure: 8922 ownership rows, zero findings, all eight architecture budgets ZERO. Exact README projections regenerated. Isolated installed/source parity: 859 Core / 323 Community Python files.
- Community initial run: 7 failed / 42 passed in 178.51s because test setup lacked native persistence registration; fixture registration added.
- Latest focused Community retry: 4 failed / 1 passed / 24 deselected in 32.53s. Current unresolved cause: MissingRealmScope (realm_scope_required) in the fresh read session when native persistence is called. This checkpoint is NOT a passing integration milestone.
- Core regression and architecture audit processes are terminal. No frontend change or production install/restart/reset.

Next action: bind the fresh session's realm scope from verified access.realm_id after Board/actor authorization, following the existing Community unit-of-work behavior; retain the missing-realm guard. Then rerun focused Community authorization tests. Measure native-gate query costs and preserve query-count independence from Card count; do not blindly relax the existing 17-query assertion. Complete the full authorization/batch regression and source audit after fixes.

Remaining scope stays unchanged: full native Card/Spec lifecycle composition, P06 race/rollback acceptance, earlier named-case/performance audits and P07-P11 integration/acceptance. Existing frontend lint budget failure remains open as recorded above. Generated local browser logs/screenshots (.playwright-cli/ and output/) are excluded from the checkpoint.

## P06 authorized realm and native gate transaction regression - 2026-10-14

Starting pushed pair: Core 1ffe1340 / Community 1522eafc. Previous turn classified as progress (checkpoint committed and pushed). P06 remains IN_PROGRESS; this closes the specific realm integration failure recorded above.

Community now binds RealmScope on the dedicated read session from the realm verified by actor/Board access, before public persistence evaluates native completion. Native-schema integration exercises local and tenant realms, wrong-realm refusal, permission revocation and a real coverage gate. No missing-realm guard or authority check was relaxed.

The broader retry exposed cognitive gate construction outside its existing infrastructure-error handler. Core now includes factory resolution inside that handler: unavailable configuration remains a typed CompletionInfrastructureUnavailable and readiness reports not_evaluated, without disclosing the raw infrastructure error or permitting completion. Added a regression for this construction failure.

P05 instrumentation now counts actual SQL executions on the dedicated connection, including native port reads, rather than maintaining incomplete manual increments. The listener is removed in finally; no SQL or parameter content is logged. Tests compare observed SQL with the logged count. Authorized evidence-only reads remain 17 queries; coverage-gate reads use 19, independent of 2 versus 32 Cards. Permission test fixtures preserve existing read flags while revoking only native transition authority; Board overrides are complete effective flags, not partial patches.

Added test_delivery_readiness_transactions.py: G10 reads actual signed complete proof (100/100), changes Target revision through its native CAS adapter or revokes a receipt in a separate committed session, and then invokes the real fenced Spec delivery gate. The gate begins with the Board write fence, reloads changed proof and rejects it; the old readiness snapshot is unchanged and cannot authorize the writer. Status, evidence rows and events remain unchanged by the rejected gate. This proves the delivery-gate transaction boundary; it does not claim all move_spec/move_card interleavings or complete P06.3.

Validation executed through PULSE_REFACTOR/.validation-v040/run_readiness_tests.py (paired isolated install, both source roots and byte parity before behavior tests):
- Core: core -q tests/test_delivery_readiness_lifecycle.py tests/test_cognitive_closeout_service_wiring.py tests/test_allowed_transitions_mutation_parity_regressions.py --tb=short --show-capture=no: 55 passed in 18.88s.
- Community initial realm retry: 4 failed / 2 passed / 24 deselected in 29.91s; missing KG construction classification and incomplete override fixtures corrected as above.
- Community authorization + batch: 50 passed in 165.57s before instrumentation/tenant additions; final run community -q tests/test_delivery_readiness_authorization.py tests/test_delivery_readiness_batch.py --tb=short --show-capture=no: 51 passed in 168.35s.
- Community transaction + existing delivery_report regression: 1 failed / 17 passed in 72.91s. New Target-revision stimulus attempted raw SQL rejected by implementation_target_cas_invalid; replaced it with the native update_target adapter, preserving the trigger. All 16 existing report cases passed, including rejected-report rollback and concurrency.
- Final transaction retry: community -q tests/test_delivery_readiness_transactions.py --tb=short --show-capture=no: 2 passed in 15.45s.
- Ruff passed all changed Python files; git diff --check passed. Installed/source parity: 859 Core / 323 Community Python files.
- Final audit_readiness.py: 8922 ownership rows, zero findings, all eight budgets ZERO; exact README projections valid. Final wheel acceptance remains P11. All test/audit handles are terminal.

Next: finish P06.5 by reusing native Policy Compliance preview and the remaining Spec/Card completion evaluation without fenced writers in reads or duplicate delivery evaluation. Missing report/transition inputs must stay explicit. Complete actual transition race/rollback acceptance beyond the gate seam, P02-P05 outstanding named-case/performance acceptance, then P07 public REST/MCP, P08/P09 UI/events and browser tests, P10 packaging/docs and P11 full 96-case/15-invariant audit. Existing frontend lint failure remains open. No frontend edit, production install/restart/reset, deployment or release.

## P06 native Card policy preview and stable observation identity - 2026-10-14

Starting pair: Core c4e790d9 / Community fd657b95, feature/v0.4.0. Previous turn classified as progress. P06 is still IN_PROGRESS.

Investigation corrected a premise in the resume notes: the native SDLC registry DOES NOT require Policy Compliance for Spec in_progress -> done; it DOES require it on Card closure. No new Spec gate was added. Readiness consults transition_requires_policy_compliance and invokes only the existing read-only GuidelineService.preview_policy_transition for applicable edges. The fenced enforcement path is never called by readiness.

Card observation now validates persisted Card status/version/Board/Spec against the authorized batch before native policy preview. Missing/stale policy authority is not a negative semantic verdict: the typed persistence error becomes not_evaluated without exposing error prose. A known policy blocker yields lifecycle_gate_pending with zero proof/score impact. Passing this subset still requires native report/other transition inputs, not final closure authority. Policy details follow the same guidelines.assessments.read boundary as allowed-transitions; readers without it receive only a coarse policy blocker.

Native policy fence/decision digests include evaluation time and therefore cannot identify a stable readiness page. Added observation_digest to the native policy snapshot/decision: it includes the same factual inputs and evaluated binding outcomes, excluding only the clock tick. The original mutation fence/decision digest computation and gate authority remain unchanged. Expiration changes the observed outcomes and hence the observation identity. The internal readiness basis_digest fingerprints that observation without publishing policy evidence. Absent observation basis fails closed.

Validation:
- Initial Core policy tests: 5 failed / 48 passed in 14.79s; tests assumed a Spec policy edge absent from the registry and an inconsistent unavailable-evidence fixture. Corrected the implementation/tests to the real Card edge and native unavailable contract; did not add a gate or relax checks.
- Subsequent Core runs: 55 passed in 10.58s, then 65 passed in 15.14s.
- Final Core command: run_readiness_tests.py core -q tests/test_delivery_readiness_lifecycle.py tests/test_delivery_readiness_state.py tests/test_delivery_readiness_shared_evaluation.py tests/test_delivery_readiness_access.py tests/test_delivery_readiness_paging.py tests/test_delivery_readiness_contracts.py tests/test_skb3_semantic_policy_transition_gate.py tests/test_skb_b10_policy_transition_transport.py tests/test_skb_b10_allowed_transition_preview.py --tb=short --show-capture=no: 174 passed in 33.92s.
- Community command: run_readiness_tests.py community -q tests/test_delivery_readiness_policy.py tests/test_skb_b10_policy_transition_rest.py tests/test_delivery_readiness_authorization.py --tb=short --show-capture=no: 1 failed / 39 passed in 98.55s. The new fixture created a Card directly and omitted native semantic-version registration. The final test first asserts read-only unavailability, then registers authority through record_semantic_subject_mutation and exercises the genuine persisted policy; no policy/gate mock or automatic repair in a read.
- Final focused Community retry: run_readiness_tests.py community -q tests/test_delivery_readiness_policy.py --tb=short --show-capture=no: 1 passed in 14.26s. Real native policy blocks a 100/100 Card for a missing assessment, preserves proof, emits no writes, yields stable repeated snapshots and redacts reasons after assessment-read permission revocation.
- Ruff and git diff --check passed. Isolated installed/source parity: 859 Core / 323 Community Python files. Final audit_readiness.py: 8925 ownership rows, zero findings, all eight budgets ZERO; README projections regenerated and validated. All test/audit handles are terminal. P11 final wheel audit remains pending.

Resume with remaining native Card completion checks (validation, dependencies, cognitive/resource/traceability and report/learning inputs) through shared native evaluation, plus remaining Spec source/critical-context integration; do not reintroduce Policy Compliance on Spec Done. Complete real transition races/rollback beyond the tested delivery-gate seam, outstanding P02-P05 acceptance/performance, P07 REST/MCP readiness/errors, P08/P09 UI/events/browser, P10 package/docs and P11 all named cases/invariants/final wheels. The existing frontend lint failure remains open. No frontend modification, production install/restart/reset, deployment or release.

## P06 shared Card admission - requested commit/push checkpoint, 2026-10-14

User requested committing and pushing all pending Pulse work. Starting pair: Core 283d99ef / Community a2bea8be on feature/v0.4.0. This is an IN_PROGRESS checkpoint, not completed P06 or full-plan acceptance.

CardService.validate_completion_admission now shares the existing task-validation and authenticated Test-scenario prerequisites between move_card and readiness. Native mutation order and prerequisites are preserved; the task-validation error now uses the existing typed CardOperationError contract. Readiness also invokes the existing read-only cognitive and resource completion checks. Lifecycle blockers retain implementation/verification proof scores, redact private error text, and do not grant final transition authority when report or other inputs remain absent.

Validation:
- Core broad admission regression before the final cognitive/resource additions: 185 passed in 31.11s.
- Final Core lifecycle, cognitive wiring, transition parity and SKB B10 preview regression: 76 passed in 26.22s.
- Final Community command: run_readiness_tests.py community -q tests/test_delivery_readiness_authorization.py tests/test_delivery_readiness_policy.py tests/test_delivery_report.py --tb=short --show-capture=no: 55 passed / 1 failed in 158.24s.
- Unresolved test: test_authenticated_test_outcome_can_finish_execution_without_requiring_pass[passed-native_schema], assertion at test_delivery_readiness_authorization.py:374. Native completion admission accepts the authenticated result, but readiness verification is 0 instead of the test's expected 100, with verification_association_missing. Investigate the fixture association and authoritative population before changing either the expectation or production semantics. The failed-outcome case passed. No gate was relaxed and this checkpoint is NOT a green Community milestone.
- Ruff passed all changed Python files; git diff --check passed.
- Paired isolated installed/source parity: 859 Core / 323 Community Python files. Final official source closure: 8926 ownership rows, zero findings, eight architecture budgets ZERO; README projections regenerated and validated. Final wheel acceptance remains P11.
- All test/audit handles are terminal. No frontend change, production reinstall/restart/reset, deployment or release. Generated .playwright-cli/ and output/ browser artifacts remain local and are excluded from commits.

Resume first at the failing authenticated passed-result test above. Then finish remaining native dependency/traceability/report/learning checks, Spec source/critical-context integration, actual transition race/rollback acceptance and the already recorded P02-P11 scope. P06 remains partial; this checkpoint introduces no new milestone or scope.

## P06 native dependencies, references and source traceability - 2026-10-14

Starting pushed pair: Core 158b781a / Community f00cc1dc. Previous turn classified as progress. The failing authenticated-result test is resolved: prepare_reports authenticates the outcome but does not associate it with delivery. The test now covers both associated/unassociated passed/failed results. Native Test completion permits execution to end for both outcomes; verification credit requires a passing result AND its native association. No production proof rule was relaxed.

Extracted CardService.validate_dependency_admission and validate_completion_references from move_card without changing their original position or rules. Readiness shares these native read checks. Open dependencies block advancement; Done/cancelled dependencies satisfy that prerequisite. Missing-link advisory/blocking behavior remains the existing human policy. Private titles and reference IDs do not escape via lifecycle diagnostics.

Readiness now composes evaluate_code_traceability_transition(enforce=False) on admitted relational attestations. It never investigates source or executes tests. Native blocking reasons retain zero score impact; technical currentness unavailability stays not_evaluated. The stable native observation joins the snapshot basis without disclosing source details. The native evaluator now loads external target Cards in one query instead of per-Card reads; its status predicate is unchanged.

Validation through the paired isolated run_readiness_tests.py wrapper, with source/install byte parity (859 Core / 323 Community):
- Core lifecycle + Card lifecycle + transition parity: 127 passed in 26.53s.
- Final Core lifecycle + code_traceability_gate + transition parity: 73 passed in 16.27s.
- Core code_traceability_application + readiness gates: 45 passed in 7.82s.
- First Community authorization/policy/native-missing-link run: 47 passed / 1 failed in 116.01s. The new cancelled-dependency fixture cancelled the actual verification Test Card, legitimately removing its verification plan. Replaced that fixture prerequisite with a separate Card; production semantics unchanged. Focused retry: 3 passed in 17.90s.
- Final Community authorization + policy: 47 passed in 119.36s, including all four authenticated-outcome/association combinations and native dependency/reference cases.
- New native-schema source tests: 2 passed in 17.65s. A separately created unexecuted Target yields the same native blocker in blocking mode; advisory remains non-blocking, I/V stay 100/100 for already proven obligations, snapshot changes and captured SQL contains no writes.
- Ruff and source closure passed: 8931 ownership rows, zero findings, all eight architecture budgets ZERO; exact README projections regenerated. All test/audit handles terminal.

P06 remains IN_PROGRESS. Next inspect and compose the native critical-context read guard without its audit writer; qualify missing report/learning inputs explicitly and finish historical current qualification and real transition acceptance. Remaining P02-P05 audits and P07-P11 scope are unchanged. No frontend or production runtime/data modification.

## P06 source provenance and critical-context preview - 2026-10-14

Starting pushed pair: Core 1664d97a / Community fa53e7cd. Extracted evaluate_critical_context from the existing mutation wrapper: both paths use the same FullContextCriticalActionGuard and default public-port resolvers. The mutation wrapper still records its native success/denial audit; readiness calls only the read evaluator. Unavailable full context is not_evaluated without private error prose or a manufactured negative judgment. The native context fingerprint joins the snapshot identity; resolution time and latency do not.

SpecService.validate_transition_source_context now contains the unchanged advancing-transition provenance/manifest rule extracted from move_spec. Readiness calls it before the other Spec completion checks. Missing context, a mismatched digest and foreign lineage produce the same native code without changing delivery proof. Test fixtures for already advancing Specs now explicitly create their native frozen source identity.

Inspection of move_spec confirms that source, code-traceability, critical-context and shared completion checks cover its current read prerequisites for Done; delivery proof remains separately assessed, and the writer still reloads/rechecks under its fence. A successful Spec native preview now reports allowed with zero other blockers. This is informational, never a write authorization token. Cards still report not_evaluated when their transition report is absent. Historical Done qualification and the remaining Card report/learning paths are not claimed complete.

Validation via run_readiness_tests.py with isolated installed/source parity (859 Core / 323 Community):
- Core critical-context/lifecycle/retirement: 48 passed in 12.04s.
- Community native critical-context/authorization/policy: 50 passed in 127.88s.
- Core source-lineage/transition-parity/critical-context/lifecycle: 112 passed in 26.15s.
- Initial broader Community run: 54 passed / 1 failed in 153.55s. The missing-context fixture violated the schema pairing rule by clearing only delivery_context. Corrected it to clear both context/provenance and manifest/digest with SQL NULL; schema guards and production gates were preserved.
- Final Core command: core -q tests/test_delivery_readiness_lifecycle.py tests/test_delivery_readiness_state.py tests/test_allowed_transitions_mutation_parity_regressions.py tests/test_board_governance_critical_context_guard.py --tb=short --show-capture=no: 104 passed in 18.33s.
- Final Community command: community -q tests/test_delivery_readiness_source_context.py tests/test_delivery_readiness_critical_context.py tests/test_delivery_readiness_authorization.py tests/test_delivery_readiness_policy.py tests/test_delivery_readiness_traceability.py --tb=short --show-capture=no: 55 passed in 137.12s.
- Native-schema critical-context cases verify stable/changed fingerprints, disabled policy, missing context, and SELECT-only execution with no pending ORM writes. Source cases compare the actual native prerequisite and readiness, retaining I/V=100 with status unchanged.
- Ruff and git diff --check passed. Final official paired source closure: 8932 ownership rows, zero findings, eight budgets ZERO; README projections regenerated. All handles terminal. P11 final wheel acceptance remains pending.

Resume at remaining P06 Card report/learning input diagnostics and historical Done current qualification. Successful Spec preview orchestration is tested, but positive full native closeout and real transition race/rollback acceptance still require their complete evidence. Finish remaining earlier named-case/performance audits before closing the prerequisite milestones, then P07-P11 as already planned. No production reinstall/restart/reset, frontend modification, deployment or release.

## P06 completion input guidance - requested commit/push checkpoint, 2026-10-14

User requested committing and pushing all pending Pulse work. Starting pushed pair: Core c482c610 / Community 451af14c on feature/v0.4.0. This remains an IN_PROGRESS checkpoint; no milestone or full-plan completion is claimed.

Native readiness observations now identify the existing completion report inputs and, when required by the current native policy, Bug Learning and impact evidence. Diagnostics provide completion guidance for missing inputs and read guidance for unavailable native context. These observations remain not_evaluated, do not authorize a transition, do not create proof and do not reduce implementation/verification percentages. Required-input guidance is internal readiness groundwork; public REST/MCP/UI delivery remains pending.

Validation:
- Paired isolated installed/source byte parity: 859 Core / 323 Community Python files.
- run_readiness_tests.py core -q tests/test_delivery_readiness_lifecycle.py tests/test_delivery_readiness_diagnostics.py tests/test_delivery_readiness_native_diagnostics.py tests/test_delivery_readiness_paging.py --tb=short --show-capture=no: 122 passed in 11.04s.
- Ruff passed all four changed Python files.
- Final official paired source closure: 8934 ownership rows, zero findings, all eight architecture budgets ZERO; README projections regenerated and validated. Test and audit handles are terminal. Final wheel acceptance remains P11.
- The prior session handle 44224 was no longer available; the focused suite above was rerun to obtain an observable terminal result.

Resume at P06 historical Done current qualification and positive full native closeout/race/rollback acceptance; then finish remaining P02-P05 evidence and P07-P11 as recorded. Historical Done handling was investigated but is not implemented in this checkpoint. No frontend change, production reinstall/restart/reset, deployment or release. Generated .playwright-cli/ and output/ browser artifacts remain local and excluded from commits.

## P06 historical completion and real Spec closeout - 2026-10-14

Starting pushed pair: Core 79568427 / Community e5356c2f. Previous goal turn classified as progress (verified changes committed/pushed). Implemented the plan section 6.1 distinction between persisted native completion and prospective transition authority. An authorized same-snapshot read validates the actual Spec/Card identity, version, edition and status, and records native Done as lifecycle history. It does not invent a Done-to-Done transition, request another report, re-evaluate an already executed closeout, or grant mutation authority. Missing read permission still denies access before scalar reads.

Current qualification independently consumes the current factual assessment and delivery gate. Complete applicable proof plus observed native completion yields delivered; revoked/missing/stale/failed/unknown proof retains its current deficit and historical divergence without reopening or rewriting the entity. Normal/Bug verification can remain pending after Done. The internal completion observation is not a new public response field or a proof/score shortcut. Diagnostics no longer request completion or recovery solely because Done has no outgoing completion transition.

Community native-schema evidence:
- Historical M04/G12 cases cover Spec, normal Card and Test Card, with both agent and human viewer. Complete signed proof yields delivered; a native human revocation changes the snapshot and current qualification, preserves persisted Done and leaves read SQL SELECT-only. Revoked Board access remains denied.
- A separate real SpecService.move_spec test composes native Community resource, knowledge, cognitive and critical-context adapters. I/V=100 and native preview allowed can actually close the Spec. Revoking proof after readiness causes the real subsequent move to reject; readiness is not a cached authorization.
- Initial wider native run: 52 passed / 2 failed in 105.44s. Earlier input-guidance work added a legitimate read-only restore_evaluation action. Two tests assumed every action was a forbidden proof mutation. Updated assertions to keep those proof actions forbidden while separately permitting the authorized read guidance. One interim retry failed due to a test typo (phase versus phases); corrected it. No permission or gate was relaxed.
- First real-closeout fixture omitted the knowledge propagation port and both cases failed before closure. Registered the genuine Community adapter; focused retry: 2 passed in 13.62s. No closeout gate was mocked or bypassed beyond the explicitly configured native cognitive skip policy in this fixture.

Validation:
- Isolated installed/source byte parity: 859 Core / 323 Community files.
- Core lifecycle/state/diagnostics/native_diagnostics/paging/shared_evaluation: 194 passed in 17.93s.
- Core readiness gates/override authorization/Card readiness/native transition parity: 76 passed in 13.09s.
- Ruff passed changed Python files; git diff --check passed.
- Final Community command: community -q tests/test_delivery_readiness_historical.py tests/test_delivery_readiness_closeout.py tests/test_delivery_readiness_authorization.py tests/test_delivery_readiness_policy.py tests/test_delivery_readiness_critical_context.py tests/test_delivery_readiness_source_context.py tests/test_delivery_readiness_transactions.py tests/test_adopted_delivery_report.py tests/test_delivery_report.py --tb=short --show-capture=no: 90 passed in 185.52s. Includes native report/selection/replay, real implementation Done with verification pending, late-failure/inline-execution rollback, concurrent reports, source changes across connections and full Spec closeout.
- Final official paired source closure: 8934 ownership rows, zero findings, all eight architecture budgets ZERO; exact README projections validated. All test/audit handles terminal; final wheel acceptance remains P11.

P06 historical handling and real Spec acceptance are now implemented; retain the full plan scope and audit its named cases before closing prerequisite phases. Next consolidate outstanding P02-P06 case evidence (including actual report rollback and source-fence tests), then P07 REST/MCP and P08-P11 frontend/docs/final acceptance in dependency order. No frontend/runtime install, production data mutation, deployment or release. Browser artifacts remain local.


## P07 REST/MCP - requested commit/push checkpoint, 2026-10-14

User requested committing and pushing all pending Pulse work. Starting pushed pair: Core 0d7d1982 / Community 02a41c23 on feature/v0.4.0. This is an IN_PROGRESS checkpoint, not an accepted milestone or release.

Implemented the readiness view on existing REST/MCP delivery-evidence reads, closed query validation, service/port wiring, typed safe read errors, current authorization, pagination/detail queries and significant null preservation. Delivery mutation failures now carry a compact assessment summary and a fresh readiness query; the assessment fingerprint is deliberately not a readiness cursor snapshot. Existing history/progress contracts remain available. Updated tool documentation and added routing, error and native-schema surface tests.

Observed validation:
- Isolated installed/source parity reported 860 Core / 323 Community Python files.
- Core routing/errors/progress/contracts/diagnostics/gates/catalog drift: 122 passed in 15.79s.
- Community surfaces/error routes/progress/report/adopted report/real closeout: 58 passed in 122.46s. Terminal result recovered from session 38291 before checkpoint.
- git diff --check passed in both repositories.
- Ruff on all changed Python files reports E402 in Core mcp/server.py:206 and Community api/specs.py:771; both imports already exist at the starting HEAD. These lint findings remain open.
- Official paired source closure FAILED (session 31202). It reports 8 adapter provenance violations, 5 import boundary violations, and community_adapter_bridges=7 (required ZERO). Blocking imports include DeliveryGateBlocked from core.services.delivery_evidence in Community cards, specs, code_traceability and rest_adapter, and READINESS_ERRORS/project_readiness_error from core.inbound.delivery_readiness_error in Community code_traceability. Functional test success does not override this architectural regression. No exception or budget was relaxed.

Resume priorities:
1. Correct P07 public contracts/error projection boundaries and adapter provenance through the public architecture; rerun closure until all eight budgets are ZERO.
2. RESTORE_EVALUATION guidance still invokes get_delivery_evidence without explicit view=readiness. Wire the correct view/current scope and update action-contract tests.
3. Finish P07 acceptance and remaining P02-P06 evidence reconciliation, then P08-P11 in the agreed plan. No full-plan completion is claimed.
No production install/restart/reset, data mutation, deployment or release. Generated .playwright-cli/ and output/ artifacts remain local and excluded from commits.


## P07 public error boundaries and actionable readiness queries - 2026-10-14

Starting pushed pair: Core 164dd0d6 / Community 60065aa9. The previous goal turn made progress: it committed/pushed the implementation and captured the failing architecture audit, preserving the concrete resume point.

Resolved the checkpoint regression. DeliveryGateBlocked is now defined in application/delivery_errors.py as a public transport-neutral application result; Community adapters and Core transports consume that contract instead of importing the private delivery service. The service still raises the same typed error and its compact envelope is unchanged. The shared readiness error projector is declared by its two exact public symbols in the Core manifest and matching Community contract expectation. No service-internal namespace was made public, no adapter bridge was added and no budget or architectural rule was relaxed. Tests assert that delivery_store remains private.

RESTORE_EVALUATION actions now explicitly request view=readiness and preserve the current Spec/Card scope in both factual diagnostics and native lifecycle guidance. Regression tests parse the emitted queries through the actual closed query contract and verify unavailable Spec/Card context guidance without proof penalties. Moved two pre-existing module imports to the import sections to resolve the E402 findings recorded in the previous checkpoint.

Validation (paired isolated environment, no production installation changes):
- Installed/source byte parity: 861 Core / 323 Community files; fresh paired source imports.
- Core lifecycle/diagnostics/native diagnostics/routing/errors/gates/catalog: 159 passed in 17.20s.
- Community surfaces/error routes/transactions/real closeout: 23 passed in 45.45s.
- First architecture retry correctly refused a Core/Community manifest mismatch before audit. Updated the matching Community expectation, preserving this guard.
- Final Core public manifest/errors/diagnostics/lifecycle/catalog drift: 97 passed in 13.72s.
- Final Community adapter provenance/import boundary/surfaces/error routes: 44 passed in 61.60s.
- Ruff passed all changed Python files in both repositories; git diff --check passed.
- Final official paired source closure: 8960 ownership rows, zero findings, all eight budgets ZERO. Exact README projections regenerated and validated. All test/audit handles terminal. This does not substitute for P11 final wheel acceptance.

P07 requirement evidence:
- P07.1: closed DeliveryEvidenceReadQuery, existing use-case dispatch and existing MCP registry tool; routing tests and prior progress/ledger/resume regression suites remain green. No new tool registered.
- P07.2: native-schema REST/MCP tests cover Spec, normal/Test Card, null preservation, current authorization, detail queries, pagination and stale snapshot rejection. Invalid/exclusive and unknown readiness inputs reject explicitly.
- P07.3: compact mutation envelopes preserve assessed facts and supply a fresh readiness query without treating the assessment fingerprint as a read cursor snapshot. Native REST/MCP error route parity and actual closeout/transaction tests passed.
- P07.4: action references and argument fields validate against the live tool registry and reference docs; catalog byte drift gate passes. Restore actions now route to readiness explicitly.
P07 implementation/contract evidence is established; keep phase IN_PROGRESS until the required P02-P06 named-case acceptance reconciliation is complete. Resume that reconciliation against the normative matrix, then P08-P11 in order. No frontend modification, production restart/reset, data mutation, deployment or release in this checkpoint. Browser artifacts remain local.


## M15/R01/R02/V22/V24 acceptance gaps and native provenance - 2026-10-14

Starting pushed pair: Core 1a7bedaa / Community bcd5bf8c. Previous goal turn made verified progress by restoring all eight ZERO budgets and correcting public readiness recovery queries.

Reconciled targeted P02-P07 acceptance against actual tests and found two concrete information gaps, without changing scoring, authority or gate semantics:
- M15: canonical FR-to-BR allocation/proof already worked, but readiness omitted the qualified contribution path. ReadinessFact now exposes paired source_obligation_ref/source_scope_sha256 with the destination obligation, contribution digest and Card. Closed validation rejects incomplete provenance. Diagnostics consume the existing canonical contribution sources, never create another Card, test or allocation.
- R02: material checkpoints already invalidated proof correctly, but readiness omitted their record IDs. Observed facts/evidence/pinpoints now identify the actual checkpoint and affected Target/execution. The Community batch reader retains the complete internal checkpoint identity set for detail pagination. Existing legacy reads keep their 20-entry summary cap; the native invalidation predicate is shared and unchanged.
- New native-schema V22/V24 tests prove that an independent Target/source-head advance and an expired investigation observed with a later read clock do not remove valid historical delivery proof or rewrite records.
- New R01/R02 tests distinguish narrative-only notes from material changes, preserve payloads, verify SELECT-only reads and recover all 25 checkpoint IDs beyond the legacy summary cap.

Reproduction and fixture investigation:
- Initial acceptance run: V22/V24 passed; M15 stopped at code_investigation_receipt_request_invalid because the old multi-Card fixture inserted receipts directly. Updated its seed to use the actual request-consumption/receipt-append/head-CAS adapter. An intermediate retry detected a real existing source head; fixture now supplies its observed generation/predecessor/revision. No database constraint was relaxed.
- Once the fixture was valid, M15 failed specifically because the derivation path was missing.
- R02 failed specifically because the checkpoint/Target pair was absent from observed diagnostic facts.
- Both regressions pass after the targeted changes. The broader existing multi-Card suite passes with the native fixture writer.

Validation:
- Paired installed/source byte parity: 861 Core / 323 Community files in isolated environment.
- First Core diagnostics/contracts/paging/access: 90 passed in 10.32s.
- Community new acceptance + existing multi-Card integration: 18 passed in 54.18s.
- Community new progress readiness + REST/MCP surfaces + existing progress-currentness: 21 passed in 55.79s.
- Final Core contracts/native diagnostics/paging/access/shared evaluation: 117 passed in 13.43s.
- Ruff and git diff --check passed both repositories.
- Official paired source closure: 8962 ownership rows, zero findings, all eight budgets ZERO; exact README projections regenerated and validated. All handles terminal. Final wheel acceptance is still P11.

Evidence mapping added by this checkpoint: M15 -> test_delivery_readiness_acceptance.py::test_M15_inherited_business_rule_preserves_path_and_reuses_actual_proof; V22/V24 -> its currentness test; R01/R02/S07 -> test_delivery_readiness_progress.py; R02 domain detail -> test_delivery_readiness_native_diagnostics.py; M15 closed shape -> test_delivery_readiness_contracts.py. The initial matrix NOT_RUN list is not a claim that these cases remain unexecuted; these concrete results supersede it for the identified backend assertions only. UI rendering remains unimplemented.

Resume with P08: summarized board projection, shared client/types and two distinct readiness bars. P02-P07 implementation has focused acceptance evidence; retain the remaining full named-case reconciliation, integrated regressions and performance verification in P11 as the plan requires, rather than making additional pre-UI audits a new milestone. Do not mark the full plan complete. No frontend build/production install/restart/reset, data mutation, deployment or release in this checkpoint. Browser artifacts remain local.

## P08 work-in-progress checkpoint requested by user - 2026-10-14

User requested commit and push of all current work. Starting pushed pair: Core 13162f8c / Community 4ca38c8a. This checkpoint preserves the implementation in progress; it is not P08 acceptance or completion of the plan.

Changes preserved:
- Core: authorized Spec-wide board summary with one read batch and shared evaluation, closed summary DTOs, application use case, and regression tests. Persisted Done facts from the same authorized batch remain distinct from proof qualification and transition authority.
- Community: REST summary endpoint, typed readiness clients, scope-checked hooks, and separate implementation/verification bars connected to normal/Bug/Test cards and Card/Spec delivery panels.
- DoD pending counter now recognizes the same ready implementation proof used by its requirement row.

Validation observed on this checkpoint:
- Fresh isolated installation/source byte parity: 861 Core / 323 Community Python files. Production installation was not changed.
- Core board summary, state and shared-evaluation suites: 76 passed in 9.48s.
- git diff --check passed in both repositories.
- TypeScript build FAILED with four diagnostics: DeliveryCompleteness.test.tsx lines 21, 34 and 48 still pass removed KanbanCard deliveryCompleteness prop; DeliveryReadinessView.tsx line 52 uses String.replaceAll outside the configured target library.
- No frontend acceptance, visual verification, new REST integration acceptance or architecture closure result is claimed for these changes. The last zero-budget closure belongs to the prior checkpoint.

Exact resume point:
1. Replace obsolete UI score tests with readiness contract/view/hook tests; resolve the replaceAll target incompatibility and organize imports currently appended to api.ts and DeliveryEvidencePanel.tsx.
2. Validate the new REST summary with native-schema authorization/no-store/scope tests; run focused frontend tests, TypeScript and isolated visual checks, then architecture closure with all eight budgets ZERO.
3. Finish P08 and continue P09-P11 from the agreed plan, including actionable navigation/refresh, final named-case reconciliation and packaged validation. Existing legacy backend delivery_completeness remains as required by the plan.
4. No production reinstall/restart/reset, data mutation or release was performed. Generated .playwright-cli/ and output/ captures/logs remain local, outside the implementation commits.

## P08 frontend integration and executable UI evidence - 2026-10-14

Starting pair: Core d3fc073f / Community a746e4c0. The previous turn made progress by committing and pushing the requested in-progress checkpoint. Resumed from those authoritative trees without restarting prior milestones.

P08 implementation now compiles. Removed the obsolete UI weighted-score component/hook and their tests; the deprecated backend delivery_completeness contract is retained. Official Card/Spec consumers use backend delivery_readiness, with distinct blue implementation and green verification bars. New tests cover normal/Bug/Test covers and Delivery, Spec-specific metrics, closure pending at 100/100, historical Done divergence, N/A, waivers, failure indicators, unavailable/incompatible data, current authorization, shared per-Spec reads and stale-response cancellation.

The U05 regression found a remaining real defect: the DoD count was zero while its banner still relied on legacy mine.satisfied and claimed rejection. The banner now uses the same pending implementation set as the rows/counter. No native transition gate, permission or proof rule changed.

The native REST summary tests initially compared dedicated Card snapshot IDs with the batch Spec snapshot. Inspection confirmed that dedicated reads include their scoped native lifecycle assessment in the hash, whereas board projections deliberately reuse the Spec batch. Tests now assert actual metric/scope equivalence, Spec snapshot identity and one shared snapshot across all board rows; they do not pretend independently scoped gate assessments have identical hashes.

Validation:
- TypeScript tsc -b: passed after replacing the unsupported replaceAll and obsolete test props.
- Focused frontend run: 74 passed / U05 failed; after correcting the banner, DoD + CardDeliveryDoDPanel: 29 passed (including the previously failing U05). Together the five focused files cover 75 passing cases.
- Community native REST/MCP surface suite including summary/no-store/current permission denial/SELECT-only checks: 14 passed in 36.63s. Installed/source parity 861 Core / 323 Community files.
- Browser test delivery-readiness.spec.ts: passed (Chromium). Real app board/modal with all API requests intercepted; no database access. First run exposed incomplete fixture permissions, correctly hiding Delivery; adding all existing projection-read permissions enabled it without changing product authority.
- Inspected actual screenshots: compact cover 100/75 and 8/8 versus 6/8, expanded light mode panels, dark mobile stacked panels at 390px. Separate bar bounds, no component overflow, keyboard refresh and reduced-motion setting verified. No claim of manual screen-reader acceptance.
- Targeted ESLint on all new readiness components/hooks/types/tests: passed. Full lint ratchet remains FAILED: react-refresh/only-export-components has 34 warnings against budget 32 (315 total warnings, zero errors). Diagnostics point to existing exports outside the new readiness files; no budget was raised. Resolve this final integration check in P11.
- Ruff for changed Python files and git diff --check passed.
- Official source closure: 8964 ownership rows, zero findings, all eight budgets ZERO; README projections regenerated. Final wheel acceptance remains P11.

P08 implementation has focused and visual evidence; the full U09 accessibility/state matrix and global lint acceptance remain explicit final checks. P09 next: native direct Delivery navigation, actionable diagnostics/detail pagination/copy with truthful limits, and cross-scope invalidation. P10/P11 remain required. No production install/restart/reset, mutation or release. Generated browser output stays local.

## P09 diagnosis UI and direct Card navigation checkpoint - 2026-10-14

Continued after the P08 push (Core b0800548 / Community fe3c1aaa), without treating that milestone as full-plan completion.

Implemented:
- Typed summary/detail/page/action/issue contracts and client for the existing view=readiness API. No new backend proof engine or score formula.
- DeliveryReadinessPanel in Card and Spec Delivery: existing metrics, collapsible diagnosis, cause pagination, snapshot/scope validation, stale-request cancellation and explicit refresh on mismatched generation.
- DeliveryReadinessIssues: backend-state action groups, expected/observed facts, affected references, owners, revision-qualified pinpoints, dependencies, permission/input caveats and lazy paginated details. No manual resolved checkbox or automatic execution of action text.
- Deterministic copy includes IDs/snapshot, exceptions, mutation reauthorization, actual known action arguments, missing inputs, partial-summary warning and next/detail queries.
- What is missing? on the Card cover opens the existing modal directly at Delivery, focuses the diagnosis heading and returns focus on close. Click propagation had overwritten the requested tab; the browser regression caught it and the button now stops that propagation.
- Detail/card reads now refresh on window focus as board summaries already did.

Validation:
- New diagnosis + readiness view/hooks + DoD tests: 27 passed. Covers backend grouping, escaped evidence text, exact detail requests, deterministic partial copy, focus, page append, snapshot mismatch hiding old metrics and aborted late details.
- Existing CardDeliveryDoDPanel, DeliveryEvidencePanel and ModalStackRenderer suites: 57 passed in the broader run.
- Broader CardModal run initially failed because its existing whole-store mock was being used through a new selector subscription. Reused the component's existing whole-store subscription; all 79 CardModal tests then passed in 62.18s, without changing their assertions or relaxing product permissions.
- Final TypeScript build passed. Targeted ESLint on new diagnosis files/types/store/test passed.
- Extended Chromium browser regression passed (4.9s total): real app with intercepted API only, direct keyboard navigation to Delivery, focused heading, expanded Expected/Observed cause, separate bars, narrow dark/reduced-motion view, Escape and focus return. Inspected resulting screenshots. Browser fixture's first close attempt used a nonexistent Close label; replaced with the supported Escape interaction.
- Backend source unchanged since the successful 8964-row ZERO-budget audit in P08. No production installation or data mutation.

P09 is IN_PROGRESS, not complete:
1. Wire onNavigate from the real Card/Spec callers to every supported native detail (Cards/Tests, requirements/scenarios/Decisions, Targets and records), retaining exact ownership/scope and focus. The diagnosis currently renders references as text unless a callback is supplied; unit callback coverage is not proof of production navigation.
2. Extend existing delivery-evidence-changed invalidation to all relevant successful mutations/events, including test outcome/binding, Target/selection/contribution, edition, Decision, waiver/revocation, status and policy. Current evidence-save callback and focus refresh alone do not prove U07.
3. Finish detail pagination/restriction/snapshot races and the full accessibility/layout state matrix, including compact loading/error height consistency and N/A CTA semantics. Final global lint (34 refresh warnings / budget 32) remains unresolved as previously recorded.
4. Then P10 documentation/compatibility/generated packaged frontend and P11 full matrix, integrated flows, payload/performance/read-only/final-wheel checks. Do not restart completed backend milestones or claim final delivery.

## P09 mutation refresh checkpoint requested by user - 2026-10-14

Starting pair: Core d2198e0a / Community fafdb229. Saved all pending implementation changes at the user's request.

- Added a shared, coalesced delivery-readiness invalidation signal after successful relevant writes through AuthenticatedFetch and the direct Board settings client. Reads, previews and rejected writes do not emit it.
- Reused the existing Card modal refresh cycle to refresh verification affected by external-agent writes on related Cards; no new polling loop or persisted score.
- Added regression coverage for mutation families, rejected writes, coalescing and U07: an authenticated Test-result write refreshes both the board cover and open Spec projection.
- Validation at this checkpoint: 60 tests passed across readinessEvents, authFetch and useDeliveryReadiness; TypeScript build passed; targeted ESLint completed with zero errors and 13 existing CardModal warnings; git diff --check passed. No new browser acceptance or full global lint success is claimed.
- No production installation, restart or data mutation. Generated .playwright-cli/ and output/ captures/logs remain local and outside implementation commits.

Resume: P09 remains IN_PROGRESS. Production reference navigation is still unwired; continue native Card/Spec references with exact ownership/scope and focus, then pagination/restriction/snapshot-race and accessibility/layout checks. Mutation refresh now has focused integration evidence; browser-wide acceptance remains pending. Continue P10 documentation/packaging and P11 full matrix/final validation as already recorded. The global lint ratchet issue remains open. This checkpoint does not declare final delivery.


## P09 native reference navigation checkpoint - 2026-10-14

Starting pair: Core 48ec56b6 / Community 5a648d5e. Previous goal turn made progress: the pending mutation-refresh work was tested, recorded and pushed. Continued from the actual synced working trees.

Implemented in Community frontend:
- Real diagnosis consumers now use native navigation for Cards/Tests, the current Spec, canonical FR/AC/BR/API/IR/OR/TR/Decision references and test scenarios. Pinpoints preserve entity type when constructing canonical references.
- Native Card and Spec reads recheck permission and Board/Spec identity before navigation; late responses after scope changes are ignored. Unknown reference kinds remain text rather than nonfunctional links.
- Native Spec rows expose exact focus anchors. Navigation expands only the marked read-only header, focuses and scrolls to the exact row within the destination modal. It does not click edit controls or select rows by free text.
- Spec Coverage navigates to its own sections in place; cross-entity navigation reuses the existing modal stack. Changing the destination inside the same stacked Spec replaces its route without adding another layer. Closing a Spec returns focus to its connected invoking control.

Validation:
- Navigation, diagnosis, modal-stack and native FR/AC suites: 29 passed before the additional denied/moved-Spec regression. Final navigation + diagnosis run: 19 passed (includes the additional regression).
- Existing Spec validation/dependencies and Card/Spec Delivery suites: 127 passed in 56.64s.
- Final TypeScript build passed. Targeted ESLint on the new navigation/focus modules, diagnosis components and navigation tests passed without warnings. git diff --check passed.
- Chromium E2E delivery-readiness.spec.ts passed in 6.0s total. Real frontend, all API reads intercepted, writes rejected. Covered keyboard navigation from Card diagnosis to the actual expanded/focused AC, dark 390px viewport, return focus on close, and navigation from Spec Coverage to its AC in place with exactly one Spec dialog. Inspected the native criterion screenshot.
- The first new unit run exposed unawaited async act in the test harness; fixed the waits. Browser iterations corrected fixture interactions: reopening a cause after explicit refresh, including the AC count in its accessible tab name, and selecting Coverage's Implementation subtab. No assertions or native permissions were relaxed.
- Backend source unchanged from the prior ZERO-budget closure. No production install/restart/data mutation. The isolated Vite server was explicitly stopped after validation; browser artifacts remain local.

Exact continuation, P09 still IN_PROGRESS:
1. Wire Targets and records/receipts/inspections to their actual native detail surfaces, resolving ownership from authoritative scoped data. These references currently remain text; canonical fallback card:<id> obligations also need explicit Card routing. Do not claim U06 complete yet.
2. Finish detail pagination/restricted responses/snapshot races and U09 compact loading/error layout and N/A CTA semantics. Check focus/expanded-state behavior across background refresh as part of that existing acceptance scope.
3. Continue P10 documentation, compatibility contract and generated frontend packaging, then P11 named-case reconciliation, integrated flows, performance/payload/read-only/final-wheel acceptance. The previously recorded global lint ratchet remains open. Full-plan delivery is not complete.


## P09 native evidence and refresh checkpoint requested by user - 2026-10-14

Starting pair: Core e9cb86bc / Community 15a464f0. Saved pending implementation at the user's request; this is a continuation checkpoint, not final plan acceptance.

Implemented in Community frontend:
- Native navigation to exact Card implementation Targets, executions and delivery ledger records, plus canonical card:<id> obligations. Reads recheck Board/Spec/Card identity; ownership resolution rejects missing or ambiguous owners. Unsupported receipt/inspection/exception destinations remain text.
- Reuse the existing Card modal and avoid duplicate stacked/store modals. Focus exact native rows and open read-only ancestor disclosures. Exact ledger record IDs are validated alongside existing scope and edition checks.
- Preserve expanded diagnosis and keyboard focus across a same-snapshot background refresh, without retaining stale metrics. Restricted detail and subsequent permission denial clear previously visible detail.
- Stable compact metric height across loading/known/N/A/unknown/error states, appropriate View scope/View evidence actions, and no fabricated progress bars.
- Remove two unused exports in obligationPresentation to satisfy the existing global lint ratchet; no warning budget increased.

Validation completed before this checkpoint:
- Native navigation/diagnosis/ledger/modal-stack: 34 passed across four files.
- CardModal and CardDeliveryDoDPanel: 107 passed across two files.
- Readiness view/diagnosis/Kanban refresh: 21 passed across three files; final diagnosis suite after additional restricted/403 cases and synchronous expansion fix: 7 passed.
- TypeScript build passed. Targeted ESLint passed. Global lint passed with 313 warnings, zero errors, within existing limits; the previously recorded lint blocker is resolved.
- Final Chromium delivery-readiness E2E passed (8.0s total). Real frontend with all API reads intercepted and writes rejected: exact record/execution navigation, single modal, background expansion/focus preservation, stable compact heights and N/A semantics.
- Native record and execution screenshots inspected. Narrow error screenshot remains partially occluded by the existing board sticky header; component overflow assertions passed, but this is not full unobscured mobile visual acceptance.
- An additional seven-file aggregate Vitest run (session 10349, including KanbanBoard.impactEvidence) remained running without a terminal result on reinspection. Interrupted with Ctrl+C to save this requested checkpoint (exit 1 due to interruption). Do not count that run as passed; investigate/re-run it during continuation.
- git diff --check passed. Backend source unchanged; no new backend audit or final wheel acceptance claimed. No production installation/restart/data mutation.

Resume: P09 remains IN_PROGRESS. Finish native test-receipt, inspection and exception destinations with exact authorized ownership; complete pagination/snapshot/accessibility acceptance and investigate the interrupted aggregate run. Then P10 documentation/compatibility/generated frontend packaging and P11 full named-case/invariant reconciliation and final acceptance. Browser captures and local logs (.playwright-cli/ and output/) stay outside implementation commits.

## P09 remaining native evidence destinations and regression closure - 2026-10-14

Starting pair: Core 7d3ee815 / Community 8b03cb6c. The prior goal turn made progress by committing/pushing the pending native Target/record work and its evidence.

Implemented:
- Native inspection navigation opens the exact Decision-review batch in Decisions, showing all observations, author, edition, revocation, sources and reconciliation. Multi-Decision batches are not reduced to an arbitrary first Decision.
- Native test receipt navigation compares the opaque receipt exactly against the authorized Spec scenario evidence. It never decodes the receipt, guesses a ledger ID, or substitutes a newer run. The native Tests detail exposes recorded output/attestation or the existing external report; changed, missing or ambiguous pointers are rejected.
- Spec-owned delivery exceptions open Coverage > Implementation, with exact record, actor, justification, phase, disposition, recorded bindings and revocation. They are explicitly not proof and never routed to a Card ledger. Missing historical disposition is not silently reclassified by this display.
- Native reads retain Board/Spec ownership checks, late-response invalidation and existing authorization. In-place exception navigation refreshes evidence, so the displayed revocation is reread.
- Additional detail-page tests cover append/deduplication and exact native references, plus invalidation on changed snapshot, scope or issue.
- Resolved the interrupted seven-file run: KanbanBoard.impactEvidence used a fresh API mock on each render, unlike the memoized production hook. Stabilized the mock and supplied a rejected readiness read; preserved every DnD assertion. The same aggregate run then passed all 61 tests in 11.75s.

Validation:
- Inspection/navigation/Spec validation: 90 passed across three files (61.27s).
- Receipt/navigation/inspection/Spec activity: 59 passed across four files (22.03s).
- Exception/navigation/DeliveryEvidence: 60 passed across three files (4.99s).
- Coverage/DeliveryEvidence/Decision badge regressions: final 46 passed across three files (6.05s). Initial run exposed a fixture missing Board/Spec identity; added those native contract fields, without changing its assertions.
- Final diagnosis-detail suite: 11 passed (3.65s), including all added pagination-race tests.
- Final TypeScript build passed. Global lint passed at 313 warnings / zero errors within unchanged limits; targeted final ESLint passed. git diff --check passed.
- Final Chromium E2E passed (17.1s total, test 15.3s): keyboard navigation to all native evidence kinds, exact focused objects, return focus, single modal, compact states, background refresh, dark 390px viewport, and no writes. All API requests were intercepted; no production database was used.
- Inspected screenshots of the complete multi-Decision batch, exact test receipt and revoked exception. The earlier compact-error sticky-header capture limitation remains a final U09 visual review item, not proof of full screen-reader acceptance. Native evidence screenshots are unobscured after adapting capture dimensions.
- No backend source change, production install/restart, data mutation or release. Isolated Vite stopped; captures remain local.

P09 implementation is established with focused and real-frontend evidence. Proceed to P10 documentation, explicit deprecated-contract compatibility and packaged frontend. P11 must still reconcile every named case/invariant, verify full cross-surface mutation flows, accessibility/state coverage, performance/payload/read-only behavior and final wheels. Do not declare the entire plan delivered from the focused UI checks.

## P10 documentation, compatibility and packaged frontend - 2026-10-14

Starting pair: Core 893a36dc / Community 975b021b. Continued directly after the P09 push.

- Updated Code Traceability reference/tool docs, Card/Spec workflows and agent operating instructions. Documented independent floored proof counts, Card/Test/Spec units, N/A versus unknown and waiver, current proof versus lifecycle, bounded diagnostic/action/detail reads, native links, minimal association-first recovery and historical Done.
- Corrected the obsolete Card workflow ordering that required completion before implementation association. Explicitly retained the deprecated delivery_completeness contract and prohibited translating its maturity stages into new metrics; no evidence rewrite/backfill/migration.
- Regenerated tools_catalog, ska_resource_manifest and ska_tool_manifest using their official modules. Only the resource manifest changed semantically; catalog/tool inventory remained identical.
- Found both uv lockfiles still describing 0.3.3. Pinned the Core development edition to 0.4.0 and added paired local editable uv sources for the documented sibling repositories. Regenerated both locks without upgrading unrelated dependencies. Initial non-editable sources conflicted with uv's editable root; using the same editable identity for both local roots resolved it. Published wheel metadata retains the exact version dependency, not a local URL.
- Ran the native frontend build/contract/sync pipeline and verify:frontend-dist: 79 packaged files, release 0.4.0, pulse-edition-api/4; tree SHA-256 6497e5e1b3177b45b067d2355c3efd094be84f67a3f7e24a38fdcc6f0335afc3. Existing bundle-size/Browserslist warnings were not suppressed.
- Tested the production bundle with vite preview and the real Chromium readiness flow: 1 passed (9.5s total, 7.6s test), all API requests intercepted and writes refused. Preview was stopped afterwards.
- Core old/new contract, deprecated completeness, catalog drift, SK-A manifest and agent-instruction suites: 95 passed (12.39s). Frontend version/incompatibility/metric hooks: 22 passed (5.21s), including U10 refusing a legacy score as readiness.
- uv lock --check passed for both repositories (157/152 packages). Official source closure passed: 8964 ownership rows, zero findings, all eight budgets ZERO, exact README projections.
- Built and force-reinstalled the pair only inside PULSE_REFACTOR/.validation-v040/dv-venv with no dependency install. Re-proved exact bytes for 861 Core and 323 Community Python files, 59 MCP Markdown/JSON resources and all 79 installed frontend files. Installed metadata confirms paired 0.4.0, exact Core version pin and no local URL dependency. This is not production reinstall and does not substitute for P11 final-wheel acceptance.

P10 implementation and focused packaging evidence are established. P11 remains required: reconcile all 96 named cases and 15 invariants against actual assertions/results, run the integrated scenarios and broader relevant suites, measure read-only/performance/payload behavior and perform final wheel acceptance. A preliminary name-only inventory is local at PULSE_REFACTOR/.validation-v040/readiness-case-name-inventory.json; missing names there are not proof of missing behavior (parameter IDs and frontend tests need inspection). Full delivery is not yet claimed.
