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

/**
 * Rejected is not a regular inbound workflow column. The server is the only
 * authority allowed to put a card there after an admitted failed completion
 * decision. Executors may only reorder the rework queue or take a card back
 * to In Progress.
 */
function allowsManualDrop(
  sourceStatus: CardStatus,
  targetStatus: CardStatus,
): boolean {
  if (targetStatus === 'rejected' && sourceStatus !== 'rejected') return false;
  if (
    sourceStatus === 'rejected'
    && targetStatus !== 'rejected'
    && targetStatus !== 'in_progress'
  ) return false;
  return true;
}

/** Resolve a visible drop without ever putting a client index on the wire. */
export function resolveKanbanDropDestination(
  columns: Record<CardStatus, CardSummary[]>,
  cardId: string,
  overId: string,
): KanbanDropDestination | null {
  const sourceStatus = CARD_STATUSES.find((status) =>
    (columns[status] ?? []).some((card) => card.id === cardId),
  );
  if (!sourceStatus) return null;

  if (CARD_STATUSES.includes(overId as CardStatus)) {
    const targetStatus = overId as CardStatus;
    if (!allowsManualDrop(sourceStatus, targetStatus)) return null;
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
      if (!allowsManualDrop(sourceStatus, targetStatus)) return null;
      return {
        targetStatus,
        targetIndex: anchorIndex,
        request: { status: targetStatus, before_id: overId },
      };
    }
  }
  return null;
}
