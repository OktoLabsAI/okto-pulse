export interface DeliverySelectionInput {
  expected_card_version: number;
  expected_spec_edition: number;
  expected_delivery_revision: number;
  record_ids: string[];
  reuse_impact?: boolean;
}
export interface DeliveryNetImpact {
  contract_version: 'delivery-net-impact/v1';
  status: 'empty' | 'composed' | 'needs_reconciliation';
  claim_only: true;
  history_count: number;
  sources: Array<{ source_ref: string; base_revision: string; result_revision: string;
    record_ids: string[]; impact_evidence: import('./index').ImpactEvidence }>;
  issues: Array<{ source_ref: string | null; code: string; record_ids: string[];
    record_count?: number; records_truncated?: boolean }>;
  issue_count: number;
  issues_truncated: boolean;
}
export interface DeliverySelectionManifest {
  contract_version: 'card-delivery-selection/v1' | 'card-delivery-selection/v2';
  board_id: string; card_id: string; spec_id: string; spec_edition: number;
  card_version: number; delivery_revision: number; sha256: string;
  scope_sha256: string; impact_sha256: string;
  records: Array<{ id: string; kind: 'progress' | 'implementation' | 'test'; sha256: string }>;
  impact_basis?: Array<{ source_ref: string; source_identity_sha256: string; base_revision: string;
    result_revision: string; observation_receipt_id: string; record_ids: string[] }>;
}
export interface DeliveryEvidenceInput {
  expected_edition: number;
  expected_version: number;
  idempotency_key: string;
  kind: 'waiver' | 'revoke';
  obligation_refs: string[];
  justification: string;
  phase?: 'implementation' | 'test';
  record_id?: string;
}
export interface DeliveryEvidenceProjection {
  board_id: string; spec_id: string; edition: number; version: number; status: string;
  allowed: boolean; complete: boolean; blockers: string[]; rejected_record_ids: string[];
  rows: Array<{
    obligation: { title: string; binding: { obligation_ref: string; semantic_sha256: string } };
    implementation_ids: string[]; test_ids: string[];
    implementation_waiver_ids: string[]; test_waiver_ids: string[];
    implementation_satisfied: boolean; test_satisfied: boolean;
    required_card_ids?: string[];
    missing_card_ids?: string[];
    missing_criteria?: Array<[string, string]>;
  }>;
  implementations: Array<{ id: string; card_id: string; relative_path: string; result_revision: string; current_accepted_execution: boolean; source_ref?: string; symbol?: string; explanation?: string; bindings?: Array<{ obligation_ref: string; semantic_sha256?: string }>; contributions?: Array<{ binding: { obligation_ref: string; semantic_sha256: string }; contribution: 'partial' | 'complete'; execution_ids?: string[] }> | null; admitted_obligation_refs?: string[]; ready_obligation_refs?: string[]; executions?: Array<{ execution_id: string; target_id: string; relative_path: string; result_revision: string; current_accepted_execution: boolean }> | null }>;
  tests?: Array<{ id: string; card_id: string; scenario_id: string; result: string; current_verified_run?: boolean }>;
  candidates: Array<{ kind: 'implementation' | 'test'; id: string; card_id: string; card_version?: number; label: string }>;
  records: Array<{ id: string; kind: string; actor_id: string; created_at: string; revoked: boolean; payload: { justification: string; card_id?: string; scenario_id?: string; execution_id?: string; phase?: string } }>;
}

// --- Card-scoped surface (0.3.4, spec 793c43d0) ---

export interface CardDeliveryEvidenceInput {
  expected_card_version: number;
  expected_spec_edition: number;
  idempotency_key: string;
  kind: 'implementation' | 'test' | 'revoke' | 'progress';
  obligation_refs: string[];
  bindings?: Array<{ obligation_ref: string; contribution: 'partial' | 'complete'; execution_refs?: Array<{ execution_id: string }> }>;
  justification: string;
  execution_id?: string;
  scenario_id?: string;
  implementation_ids?: string[];
  record_id?: string;
  progress?: {
    contract_version: 'delivery-progress/v1' | 'delivery-progress/v2';
    material_change?: 'none' | 'targets' | 'source' | 'unknown';
    target_ids?: string[];
    source_state: { workspace_state: 'unknown' | 'dirty' | 'clean'; recoverability: 'unknown' | 'external_workspace' | 'declared_commit'; source_ref?: string | null; declared_revision?: string | null };
    remaining: string;
    impact_delta?: import('./index').ImpactEvidence;
    impact_base_revision?: string;
  };
}

export interface CardDeliveryBatchInput {
  contract_version: 'card-delivery-batch/v1';
  expected_card_version: number;
  expected_spec_edition: number;
  expected_delivery_revision: number;
  idempotency_key: string;
  entries: Array<Omit<CardDeliveryEvidenceInput, 'expected_card_version' | 'expected_spec_edition' | 'idempotency_key' | 'kind' | 'record_id'> & {
    client_ref: string; kind: 'progress' | 'implementation' | 'test';
  }>;
}

export type CardDeliveryWriteResult = { id: string; replayed: boolean } | {
  entries: Array<{ client_ref: string; id: string }>; delivery_revision: number; replayed: boolean;
  report?: { status: 'validation' | 'done'; manifest_sha256: string; delivery_revision: number };
};

export type CardDeliveryBatchDraft = Omit<CardDeliveryBatchInput, 'idempotency_key'>;

export interface CardDeliveryReportInput {
  contract_version: 'card-delivery-report/v1';
  expected_card_status: 'started' | 'in_progress';
  batch: CardDeliveryBatchInput;
  report: {
    status: 'validation' | 'done'; conclusion: string;
    completeness: number; completeness_justification: string;
    drift: number; drift_justification: string;
    impact_evidence?: import('./index').ImpactEvidence;
  };
  existing_record_ids: string[];
  reuse_impact: boolean;
}

export interface DeliveryPerCardObligation {
  ref: string;
  title: string;
  implementation_satisfied: boolean;
}

export interface DeliveryProgressHistory {
  board_id: string; card_id: string; spec_id: string; edition: number;
  card_version: number; delivery_revision: number; status: string;
  total: number; next_cursor: string | null; recovery_verified: false;
  order: 'newest_first'; detail: boolean;
  items: NonNullable<DeliveryPerCard['progress']>['items'];
}

export interface CardDeliveryResume {
  board_id: string; card_id: string; spec_id: string; edition: number;
  status: string; title: string; response_truncated: boolean;
  latest_checkpoint: DeliveryProgressHistory['items'][number] | null;
  progress: DeliveryProgressHistory;
  accumulated_impact: { status: string; history_count: number; detail_omitted?: boolean };
  obligations: { complete: boolean; total: number; truncated: boolean; items: Array<{
    ref: string; title: string; implementation_satisfied: boolean; test_satisfied: boolean;
  }> };
  implementation_proofs: { total: number; truncated: boolean; items: Array<{
    record_id: string; actor_id: string; source_ref: string; result_revision: string;
    relative_path: string; current_obligation_refs: string[]; bindings_truncated: boolean;
    declaration_origin: string; contributions: Array<{ obligation_ref: string; declaration: string }>;
  }> };
  verification_plan?: { complete: boolean; status: string; total: number; truncated: boolean; test_cards_truncated: boolean;
    items: Array<{ scenario_id: string; method: string | null; criterion_ids: string[]; test_card_ids: string[]; blockers: string[]; links_truncated: boolean }> };
  tests: { scope: 'this_card' | 'card_and_related_obligations'; total: number; total_exact?: boolean; truncated: boolean; items: Array<{
    record_id: string; card_id?: string; scenario_id: string; actor_id: string; result: string; current_verified_run: boolean; observes_this_card?: boolean;
  }> };
  targets: { truncated: boolean; items: Array<{ id: string; revision: number; source_ref: string; relative_path: string | null }> };
  actions: { record_progress: boolean; final_transitions: 'not_evaluated'; mutation_reauthorization_required: true };
}

export interface CardLedgerPage {
  board_id: string; card_id: string; spec_id: string; edition: number;
  current_edition: number; historical: boolean; total: number;
  next_cursor: string | null;
  items: Array<{ id: string; kind: string; actor_id: string; created_at: string;
    revoked: boolean; summary: string; text_truncated: boolean;
    obligation_refs: string[]; obligations_truncated: boolean;
    currentness: 'not_evaluated';
    payload?: { contributions?: Array<{ obligation_ref: string; contribution: string }>;
      execution_id?: string; scenario_id?: string; test_result?: string;
      progress?: { remaining: string; source_state: { workspace_state: string; recoverability: string } } };
  }>;
}

export interface DeliveryPerCard {
  card_id: string;
  title: string;
  card_type: string;
  status: string;
  obligations: DeliveryPerCardObligation[];
  satisfied: boolean;
  card_version?: number;
  delivery_revision?: number;
  accumulated_impact?: DeliveryNetImpact;
  report_impact?: { source: 'manual_or_absent' | 'accumulated'; current: boolean | null; reason: string | null };
  selection?: { total: number; truncated: boolean; records: Array<{ id: string; kind: string; summary: string }> };
  progress?: {
    total: number; truncated: boolean; recovery_verified: false;
    target_options?: Array<{ id: string; source_ref: string; label: string }>;
    targets_truncated?: boolean;
    items: Array<{ id: string; actor_id: string; created_at: string; revoked?: boolean; summary: string; remaining: string; text_truncated: boolean; source_state: { workspace_state: string; recoverability: string }; target_ids: string[]; material_change?: string; change_declaration_origin?: string }>;
  };
}
// Rollup projection extension (0.3.4): the spec read returns the aggregated
// card-ledger view. Assign to DeliveryEvidenceProjection via declaration
// merging below.
export interface DeliveryEvidenceProjection {
  per_card?: DeliveryPerCard[];
}
