export interface SpecExecutionContract {
  contract_version: 'spec-execution-contract/v1';
  board_id: string;
  spec_id: string;
  adopted_in_edition: number;
  actor_id: string;
  origin: 'new_spec' | 'explicit_revision';
}

export interface SpecExecutionContractAdoption {
  contract_version: 'spec-execution-contract/v1';
  expected_spec_version: number;
  expected_spec_edition: number;
}
