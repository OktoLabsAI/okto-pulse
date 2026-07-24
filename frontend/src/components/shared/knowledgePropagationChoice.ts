import type {
  KnowledgePropagationEnvelopeV2,
  KnowledgePropagationMode,
} from '@/types';

export type KnowledgePropagationAction = 'omitted' | KnowledgePropagationMode;

export interface KnowledgePropagationChoice {
  action: KnowledgePropagationAction;
  knowledgeIds: string[];
  justification: string;
}

export const EMPTY_KNOWLEDGE_PROPAGATION_CHOICE: KnowledgePropagationChoice = {
  action: 'omitted',
  knowledgeIds: [],
  justification: '',
};

function canonicalIds(ids: string[]): string[] {
  return Array.from(new Set(ids.filter(Boolean))).sort();
}

export function isKnowledgePropagationChoiceValid(
  choice: KnowledgePropagationChoice,
): boolean {
  if (choice.action === 'omitted') return true;
  if (!choice.justification.trim()) return false;
  if (choice.action === 'drop') return true;
  return choice.knowledgeIds.length > 0;
}

export function buildKnowledgePropagationEnvelope(
  choice: KnowledgePropagationChoice,
  idempotencyKey: string,
): KnowledgePropagationEnvelopeV2 {
  const knowledgeIds = canonicalIds(choice.knowledgeIds);
  if (choice.action === 'omitted') {
    return {
      contract_version: 2,
      selection_state: 'omitted',
      mode: null,
      knowledge_ids: [],
      justification: null,
      idempotency_key: idempotencyKey,
      expected_revision: 0,
      relevance_links: [],
    };
  }

  return {
    contract_version: 2,
    selection_state:
      choice.action === 'drop' && knowledgeIds.length === 0
        ? 'explicit_empty'
        : 'explicit_ids',
    mode: choice.action,
    knowledge_ids: knowledgeIds,
    justification: choice.justification.trim(),
    idempotency_key: idempotencyKey,
    expected_revision: 0,
    relevance_links: [],
  };
}
