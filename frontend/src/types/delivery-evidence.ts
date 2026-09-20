export interface DeliverySelectionInput {
  expected_card_version: number;
  expected_spec_edition: number;
  expected_delivery_revision: number;
  record_ids: string[];
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
  contract_version: 'card-delivery-selection/v1';
  board_id: string; card_id: string; spec_id: string; spec_edition: number;
  card_version: number; delivery_revision: number; sha256: string;
  scope_sha256: string; impact_sha256: string;
  records: Array<{ id: string; kind: 'progress' | 'implementation' | 'test'; sha256: string }>;
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
  }>;
  implementations: Array<{ id: string; card_id: string; relative_path: string; result_revision: string; current_accepted_execution: boolean; source_ref?: string; symbol?: string; explanation?: string; bindings?: Array<{ obligation_ref: string; semantic_sha256?: string }>; contributions?: Array<{ binding: { obligation_ref: string; semantic_sha256: string }; contribution: 'partial' | 'complete'; execution_ids?: string[] }> | null; admitted_obligation_refs?: string[]; ready_obligation_refs?: string[]; executions?: Array<{ execution_id: string; target_id: string; relative_path: string; result_revision: string; current_accepted_execution: boolean }> | null }>;
  tests?: Array<{ id: string; card_id: string; scenario_id: string; result: string }>;
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
};

export interface DeliveryPerCardObligation {
  ref: string;
  title: string;
  implementation_satisfied: boolean;
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
