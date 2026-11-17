export type TaskRequirementChoice = 'required' | 'optional' | 'waived';

export interface TaskRequirementOptions {
  contract_version: 'task-requirement-options/v1';
  board_id: string; spec_id: string; edition: number; spec_version: number; basis: string;
  cards: Array<{ card_id: string; title: string; lifecycle_status: string;
    decision: TaskRequirementChoice; decision_id: string | null; scope_sha256: string;
    mutable: boolean; reason_code: string | null; history_count: number; user_note: string | null }>;
  allowed_decisions: TaskRequirementChoice[]; note_optional: true; can_apply: boolean;
  effects: { lifecycle_unchanged: true; proof_credit: false; dependencies_unchanged: true;
    dependent_tasks: Array<{ card_id: string; title: string; status: string; prerequisite_id: string }>;
    test_scenarios_remain_governed: boolean };
}

export interface TaskRequirementInput {
  contract_version: 'task-requirement-decision/v1'; card_ids: string[];
  decision: TaskRequirementChoice; basis: string; note: string | null; idempotency_key: string;
}

export interface TaskRequirementOperation {
  contract_version: 'task-requirement-operation/v1'; operation_id: string;
  status: 'applied'; edition: number; decision_ids: string[]; affected_cards: string[];
  decision: TaskRequirementChoice; replayed: boolean;
}

export interface TaskRequirementHistory {
  contract_version: 'task-requirement-history/v1'; board_id: string; spec_id: string; card_id: string;
  edition: number; current_edition: number; total: number; next_before_revision: number | null;
  items: Array<{ id: string; revision: number; decision: TaskRequirementChoice;
    previous_decision: TaskRequirementChoice; actor_id: string; recorded_at: string;
    scope_sha256: string; note: string | null; note_truncated: boolean }>;
}
