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
  implementations: Array<{ id: string; card_id: string; relative_path: string; result_revision: string; current_accepted_execution: boolean; source_ref?: string; symbol?: string; explanation?: string }>;
  tests?: Array<{ id: string; card_id: string; scenario_id: string; result: string }>;
  candidates: Array<{ kind: 'implementation' | 'test'; id: string; card_id: string; label: string }>;
  records: Array<{ id: string; kind: string; actor_id: string; created_at: string; revoked: boolean; payload: { justification: string; card_id?: string; scenario_id?: string; execution_id?: string; phase?: string } }>;
}
