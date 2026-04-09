/**
 * Dashboard Store - Zustand state management
 */

import { create } from 'zustand';
import type {
  Board,
  BoardSummary,
  CardSummary,
  CardStatus,
  Agent,
} from '@/types';
import { CARD_STATUSES } from '@/types';

interface DashboardState {
  // Data
  boards: BoardSummary[];
  sharedBoards: BoardSummary[];
  currentBoard: Board | null;
  columns: Record<CardStatus, CardSummary[]>;
  agents: Agent[];

  // UI State
  isLoading: boolean;
  error: string | null;
  selectedCardId: string | null;
  isCardModalOpen: boolean;

  // Actions
  setBoards: (boards: BoardSummary[]) => void;
  setSharedBoards: (boards: BoardSummary[]) => void;
  setCurrentBoard: (board: Board | null) => void;
  setColumns: (columns: Record<CardStatus, CardSummary[]>) => void;
  setAgents: (agents: Agent[]) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
  
  // Card actions
  selectCard: (cardId: string | null) => void;
  openCardModal: (cardId: string) => void;
  closeCardModal: () => void;
  
  // Card CRUD in columns
  addCardToColumn: (card: CardSummary) => void;
  updateCardInColumn: (card: CardSummary) => void;
  removeCardFromColumn: (cardId: string) => void;
  moveCardBetweenColumns: (
    cardId: string,
    fromStatus: CardStatus,
    toStatus: CardStatus,
    newPosition: number
  ) => void;
  
  // Optimistic updates
  optimisticMoveCard: (
    cardId: string,
    toStatus: CardStatus,
    newPosition: number
  ) => CardSummary | null;
}

export const useDashboardStore = create<DashboardState>((set, get) => ({
  // Initial state
  boards: [],
  sharedBoards: [],
  currentBoard: null,
  columns: {} as Record<CardStatus, CardSummary[]>,
  agents: [],
  isLoading: false,
  error: null,
  selectedCardId: null,
  isCardModalOpen: false,

  // Setters
  setBoards: (boards) => set({ boards }),
  setSharedBoards: (sharedBoards) => set({ sharedBoards }),
  setCurrentBoard: (board) => set({ currentBoard: board }),
  setColumns: (columns) => set({ columns }),
  setAgents: (agents) => set({ agents }),
  setLoading: (isLoading) => set({ isLoading }),
  setError: (error) => set({ error }),

  // Card selection
  selectCard: (cardId) => set({ selectedCardId: cardId }),
  openCardModal: (cardId) => set({ selectedCardId: cardId, isCardModalOpen: true }),
  closeCardModal: () => set({ isCardModalOpen: false, selectedCardId: null }),

  // Card CRUD
  addCardToColumn: (card) => {
    const { columns } = get();
    const column = columns[card.status] || [];
    set({
      columns: {
        ...columns,
        [card.status]: [...column, card].sort((a, b) => a.position - b.position),
      },
    });
  },

  updateCardInColumn: (card) => {
    const { columns } = get();
    const column = columns[card.status] || [];
    set({
      columns: {
        ...columns,
        [card.status]: column
          .map((c) => (c.id === card.id ? card : c))
          .sort((a, b) => a.position - b.position),
      },
    });
  },

  removeCardFromColumn: (cardId) => {
    const { columns } = get();
    const newColumns = { ...columns };
    
    for (const status of CARD_STATUSES) {
      newColumns[status] = (newColumns[status] || []).filter((c) => c.id !== cardId);
    }
    
    set({ columns: newColumns });
  },

  moveCardBetweenColumns: (cardId, fromStatus, toStatus, newPosition) => {
    const { columns } = get();
    const card = (columns[fromStatus] || []).find((c) => c.id === cardId);
    
    if (!card) return;

    // Remove from old column
    const fromColumn = (columns[fromStatus] || []).filter((c) => c.id !== cardId);
    
    // Add to new column
    const toColumn = [...(columns[toStatus] || [])];
    const updatedCard = { ...card, status: toStatus, position: newPosition };
    
    // Insert at position
    toColumn.splice(newPosition, 0, updatedCard);
    
    // Reindex positions
    const reindexedColumn = toColumn.map((c, idx) => ({ ...c, position: idx }));

    set({
      columns: {
        ...columns,
        [fromStatus]: fromColumn,
        [toStatus]: reindexedColumn,
      },
    });
  },

  // Optimistic update - returns the card that was moved
  optimisticMoveCard: (cardId, toStatus, newPosition) => {
    const { columns } = get();
    
    // Find the card in any column
    let card: CardSummary | null = null;
    let fromStatus: CardStatus | null = null;
    
    for (const status of CARD_STATUSES) {
      const found = (columns[status] || []).find((c) => c.id === cardId);
      if (found) {
        card = found;
        fromStatus = status;
        break;
      }
    }
    
    if (!card || !fromStatus) return null;

    // Remove from old column
    const fromColumn = (columns[fromStatus] || []).filter((c) => c.id !== cardId);
    
    // Add to new column
    const toColumn = [...(columns[toStatus] || [])];
    const updatedCard = { ...card, status: toStatus, position: newPosition };
    toColumn.splice(newPosition, 0, updatedCard);
    const reindexedColumn = toColumn.map((c, idx) => ({ ...c, position: idx }));

    set({
      columns: {
        ...columns,
        [fromStatus]: fromColumn,
        [toStatus]: reindexedColumn,
      },
    });

    return updatedCard;
  },
}));

// Selectors
export const useColumns = () => useDashboardStore((state) => state.columns);
export const useCurrentBoard = () => useDashboardStore((state) => state.currentBoard);
export const useBoards = () => useDashboardStore((state) => state.boards);
export const useSharedBoards = () => useDashboardStore((state) => state.sharedBoards);
export const useAgents = () => useDashboardStore((state) => state.agents);
export const useSelectedCard = () => useDashboardStore((state) => state.selectedCardId);
export const useIsCardModalOpen = () => useDashboardStore((state) => state.isCardModalOpen);
