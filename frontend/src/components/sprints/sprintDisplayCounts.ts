import type { CardSummaryForSpec } from '@/types';

export interface SprintDisplayCounts {
  cards: number;
  tasks: number;
  tests: number;
  bugs: number;
  workItemsTotal: number;
  workItemsDone: number;
  visibleCards: CardSummaryForSpec[];
  taskCards: CardSummaryForSpec[];
  testCards: CardSummaryForSpec[];
  bugCards: CardSummaryForSpec[];
}

type SprintCardTypeInput = string | { value?: string } | null | undefined;
type CardTypeLike = { card_type?: SprintCardTypeInput };

export function normalizeSprintCardType(cardType: SprintCardTypeInput): string {
  if (!cardType) return 'normal';
  if (typeof cardType === 'object') return normalizeSprintCardType(cardType.value);
  const normalized = String(cardType).replace(/^CardType\./i, '').toLowerCase();
  return normalized || 'normal';
}

export function isTestCard(card: CardTypeLike): boolean {
  return normalizeSprintCardType(card.card_type) === 'test';
}

export function isBugCard(card: CardTypeLike): boolean {
  return normalizeSprintCardType(card.card_type) === 'bug';
}

export function isTaskCard(card: CardTypeLike): boolean {
  return !isTestCard(card) && !isBugCard(card);
}

export function isSprintCard(card: CardTypeLike): boolean {
  return !isTestCard(card);
}

export function deriveSprintDisplayCounts(cards: CardSummaryForSpec[] = []): SprintDisplayCounts {
  const visibleCards = cards.filter(isSprintCard);
  const taskCards = cards.filter(isTaskCard);
  const testCards = cards.filter(isTestCard);
  const bugCards = cards.filter(isBugCard);

  return {
    cards: visibleCards.length,
    tasks: taskCards.length,
    tests: testCards.length,
    bugs: bugCards.length,
    workItemsTotal: cards.length,
    workItemsDone: cards.filter((card) => card.status === 'done').length,
    visibleCards,
    taskCards,
    testCards,
    bugCards,
  };
}
