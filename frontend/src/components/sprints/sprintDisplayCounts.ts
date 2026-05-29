import type { CardSummaryForSpec } from '@/types';

export interface SprintDisplayCounts {
  cards: number;
  tests: number;
  workItemsTotal: number;
  workItemsDone: number;
  visibleCards: CardSummaryForSpec[];
  testCards: CardSummaryForSpec[];
}

export function isTestCard(card: Pick<CardSummaryForSpec, 'card_type'>): boolean {
  return card.card_type === 'test';
}

export function isSprintCard(card: Pick<CardSummaryForSpec, 'card_type'>): boolean {
  return !isTestCard(card);
}

export function deriveSprintDisplayCounts(cards: CardSummaryForSpec[] = []): SprintDisplayCounts {
  const visibleCards = cards.filter(isSprintCard);
  const testCards = cards.filter(isTestCard);

  return {
    cards: visibleCards.length,
    tests: testCards.length,
    workItemsTotal: cards.length,
    workItemsDone: cards.filter((card) => card.status === 'done').length,
    visibleCards,
    testCards,
  };
}
