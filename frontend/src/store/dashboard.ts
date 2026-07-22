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
  ColumnPageResponse,
  ColumnsOptInResponse,
  KanbanColumnMeta,
} from '@/types';
import { CARD_STATUSES } from '@/types';

export interface ColumnPageToken {
  readonly generation: number;
  readonly offset: number;
  readonly request_id: string;
  readonly column: CardStatus;
}

export interface ColumnPageLoadState {
  requestId: string | null;
  error: string | null;
}

let columnRequestSequence = 0;

function emptyColumnPageState(): Record<CardStatus, ColumnPageLoadState> {
  return CARD_STATUSES.reduce<Record<CardStatus, ColumnPageLoadState>>(
    (result, status) => {
      result[status] = { requestId: null, error: null };
      return result;
    },
    {} as Record<CardStatus, ColumnPageLoadState>,
  );
}

interface DashboardState {
  // Data
  boards: BoardSummary[];
  sharedBoards: BoardSummary[];
  currentBoard: Board | null;
  columns: Record<CardStatus, CardSummary[]>;
  columnsMeta: Partial<Record<CardStatus, KanbanColumnMeta>>;
  columnsGeneration: number;
  columnPageState: Record<CardStatus, ColumnPageLoadState>;
  agents: Agent[];

  // UI State
  isLoading: boolean;
  error: string | null;
  selectedCardId: string | null;
  isCardModalOpen: boolean;

  // Actions
  setBoards: (boards: BoardSummary[]) => void;
  addBoard: (board: BoardSummary) => void;
  setSharedBoards: (boards: BoardSummary[]) => void;
  setCurrentBoard: (board: Board | null) => void;
  setColumns: (columns: Record<CardStatus, CardSummary[]>) => void;
  beginColumnsGeneration: () => number;
  applyColumnsBatch: (generation: number, response: ColumnsOptInResponse) => boolean;
  beginColumnPage: (column: CardStatus, offset: number) => ColumnPageToken | null;
  applyColumnPage: (token: ColumnPageToken, response: ColumnPageResponse) => boolean;
  failColumnPage: (token: ColumnPageToken, error: string) => boolean;
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
  columnsMeta: {},
  columnsGeneration: 0,
  columnPageState: emptyColumnPageState(),
  agents: [],
  isLoading: false,
  error: null,
  selectedCardId: null,
  isCardModalOpen: false,

  // Setters
  setBoards: (boards) => set({ boards }),
  addBoard: (board) => set((state) => ({ boards: [board, ...state.boards] })),
  setSharedBoards: (sharedBoards) => set({ sharedBoards }),
  setCurrentBoard: (board) => set((state) => {
    if (state.currentBoard?.id === board?.id) return { currentBoard: board };
    return {
      currentBoard: board,
      columns: {} as Record<CardStatus, CardSummary[]>,
      columnsMeta: {},
      columnsGeneration: state.columnsGeneration + 1,
      columnPageState: emptyColumnPageState(),
    };
  }),
  setColumns: (columns) => set((state) => ({
    columns,
    columnsMeta: {},
    columnsGeneration: state.columnsGeneration + 1,
    columnPageState: emptyColumnPageState(),
  })),
  beginColumnsGeneration: () => {
    const generation = get().columnsGeneration + 1;
    set({
      columnsGeneration: generation,
      columnPageState: emptyColumnPageState(),
    });
    return generation;
  },
  applyColumnsBatch: (generation, response) => {
    const state = get();
    if (
      generation !== state.columnsGeneration
      || (state.currentBoard !== null && response.board_id !== state.currentBoard.id)
    ) return false;
    set({
      columns: response.columns,
      columnsMeta: response.columns_meta.columns,
      columnPageState: emptyColumnPageState(),
    });
    return true;
  },
  beginColumnPage: (column, offset) => {
    if (!Number.isInteger(offset) || offset < 0) return null;
    const state = get();
    if (state.columnPageState[column]?.requestId != null) return null;
    const requestId = `column-${state.columnsGeneration}-${++columnRequestSequence}`;
    const token = Object.freeze({
      generation: state.columnsGeneration,
      offset,
      request_id: requestId,
      column,
    });
    set({
      columnPageState: {
        ...state.columnPageState,
        [column]: { requestId, error: null },
      },
    });
    return token;
  },
  applyColumnPage: (token, response) => {
    const state = get();
    if (
      token.generation !== state.columnsGeneration
      || state.columnPageState[token.column]?.requestId !== token.request_id
      || (state.currentBoard !== null && response.board_id !== state.currentBoard.id)
      || response.column !== token.column
      || response.offset !== token.offset
    ) {
      return false;
    }
    const existing = state.columns[token.column] ?? [];
    const seen = new Set(existing.map((card) => card.id));
    const appended = response.items.filter((card) => {
      if (seen.has(card.id)) return false;
      seen.add(card.id);
      return true;
    });
    set({
      columns: {
        ...state.columns,
        [token.column]: [...existing, ...appended],
      },
      columnsMeta: {
        ...state.columnsMeta,
        [token.column]: response.meta,
      },
      columnPageState: {
        ...state.columnPageState,
        [token.column]: { requestId: null, error: null },
      },
    });
    return true;
  },
  failColumnPage: (token, error) => {
    const state = get();
    if (
      token.generation !== state.columnsGeneration
      || state.columnPageState[token.column]?.requestId !== token.request_id
    ) {
      return false;
    }
    set({
      columnPageState: {
        ...state.columnPageState,
        [token.column]: { requestId: null, error },
      },
    });
    return true;
  },
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
    if (!Number.isInteger(newPosition) || newPosition < 0) return;
    const { columns } = get();
    const card = (columns[fromStatus] || []).find((c) => c.id === cardId);
    
    if (!card) return;

    // Remove from old column
    const fromColumn = (columns[fromStatus] || []).filter((c) => c.id !== cardId);
    
    // Add to new column
    const toColumn = (columns[toStatus] || []).filter((c) => c.id !== cardId);
    const updatedCard = { ...card, status: toStatus, position: newPosition };
    
    // Insert at position
    toColumn.splice(Math.min(newPosition, toColumn.length), 0, updatedCard);
    
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
    if (!Number.isInteger(newPosition) || newPosition < 0) return null;
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
    const toColumn = (columns[toStatus] || []).filter((c) => c.id !== cardId);
    const updatedCard = { ...card, status: toStatus, position: newPosition };
    toColumn.splice(Math.min(newPosition, toColumn.length), 0, updatedCard);
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
export const useColumnsMeta = () => useDashboardStore((state) => state.columnsMeta);
export const useColumnPageState = () => useDashboardStore((state) => state.columnPageState);
export const useCurrentBoard = () => useDashboardStore((state) => state.currentBoard);
export const useBoards = () => useDashboardStore((state) => state.boards);
export const useSharedBoards = () => useDashboardStore((state) => state.sharedBoards);
export const useAgents = () => useDashboardStore((state) => state.agents);
export const useSelectedCard = () => useDashboardStore((state) => state.selectedCardId);
export const useIsCardModalOpen = () => useDashboardStore((state) => state.isCardModalOpen);
