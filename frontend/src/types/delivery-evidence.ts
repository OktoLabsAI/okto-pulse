export interface DeliveryEvidenceInput {
  expected_edition: number;
  expected_version: number;
  idempotency_key: string;
  kind: 'implementation' | 'test' | 'waiver' | 'revoke';
  obligation_refs: string[];
  justification: string;
  card_id?: string;
  execution_id?: string;
  scenario_id?: string;
  implementation_ids?: string[];
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
  implementations: Array<{ id: string; card_id: string; relative_path: string; result_revision: string; current_accepted_execution: boolean; source_ref?: string; symbol?: string; explanation?: string; bindings?: Array<{ obligation_ref: string; semantic_sha256?: string }> }>;
  tests?: Array<{ id: string; card_id: string; scenario_id: string; result: string }>;
  candidates: Array<{ kind: 'implementation' | 'test'; id: string; card_id: string; card_version?: number; label: string }>;
  records: Array<{ id: string; kind: string; actor_id: string; created_at: string; revoked: boolean; payload: { justification: string; card_id?: string; scenario_id?: string; execution_id?: string; phase?: string } }>;
}

// --- Card-scoped surface (0.3.4, spec 793c43d0) ---

export interface CardDeliveryEvidenceInput {
  expected_card_version: number;
  expected_spec_edition: number;
  idempotency_key: string;
  kind: 'implementation' | 'test' | 'revoke';
  obligation_refs: string[];
  justification: string;
  execution_id?: string;
  scenario_id?: string;
  implementation_ids?: string[];
  record_id?: string;
}

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
}
// Rollup projection extension (0.3.4): the spec read returns the aggregated
// card-ledger view. Assign to DeliveryEvidenceProjection via declaration
// merging below.
export interface DeliveryEvidenceProjection {
  per_card?: DeliveryPerCard[];
}
