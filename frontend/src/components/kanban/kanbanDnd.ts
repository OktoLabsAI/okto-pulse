import {
  CARD_STATUSES,
  type CardStatus,
  type CardSummary,
  type MoveCardRequest,
} from '@/types';

type AnchorMoveRequest = Pick<
  MoveCardRequest,
  'status' | 'before_id' | 'after_id' | 'placement'
>;

export interface KanbanDropDestination {
  targetStatus: CardStatus;
  targetIndex: number;
  request: AnchorMoveRequest;
}

/** Resolve a visible drop without ever putting a client index on the wire. */
export function resolveKanbanDropDestination(
  columns: Record<CardStatus, CardSummary[]>,
  cardId: string,
  overId: string,
): KanbanDropDestination | null {
  const sourceExists = CARD_STATUSES.some((status) =>
    (columns[status] ?? []).some((card) => card.id === cardId),
  );
  if (!sourceExists) return null;

  if (CARD_STATUSES.includes(overId as CardStatus)) {
    const targetStatus = overId as CardStatus;
    const targetCards = (columns[targetStatus] ?? []).filter(
      (card) => card.id !== cardId,
    );
    return {
      targetStatus,
      targetIndex: targetCards.length,
      request: { status: targetStatus, placement: 'end' },
    };
  }

  if (overId === cardId) return null;
  for (const targetStatus of CARD_STATUSES) {
    const targetCards = (columns[targetStatus] ?? []).filter(
      (card) => card.id !== cardId,
    );
    const anchorIndex = targetCards.findIndex((card) => card.id === overId);
    if (anchorIndex >= 0) {
      return {
        targetStatus,
        targetIndex: anchorIndex,
        request: { status: targetStatus, before_id: overId },
      };
    }
  }
  return null;
}
