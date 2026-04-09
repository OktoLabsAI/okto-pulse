/**
 * Sidebar component for board navigation (collapsible with animation)
 * Shows "My Boards" and "Shared with me" sections.
 */

import { LayoutDashboard, Plus, Share2 } from 'lucide-react';
import { useBoards, useSharedBoards, useCurrentBoard } from '@/store/dashboard';
import type { BoardSummary } from '@/types';

interface SidebarProps {
  onSelectBoard: (boardId: string) => void;
  onCreateBoard: () => void;
  isOpen: boolean;
}

function BoardList({
  boards,
  currentBoard,
  onSelectBoard,
  icon: Icon = LayoutDashboard,
}: {
  boards: BoardSummary[];
  currentBoard: { id: string } | null;
  onSelectBoard: (id: string) => void;
  icon?: typeof LayoutDashboard;
}) {
  return (
    <ul className="space-y-1">
      {boards.map((board) => (
        <li key={board.id}>
          <button
            onClick={() => onSelectBoard(board.id)}
            className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-left text-sm transition-colors ${
              currentBoard?.id === board.id
                ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200'
                : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
            }`}
          >
            <Icon size={16} />
            <span className="truncate">{board.name}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function Sidebar({ onSelectBoard, onCreateBoard, isOpen }: SidebarProps) {
  const boards = useBoards();
  const sharedBoards = useSharedBoards();
  const currentBoard = useCurrentBoard();

  return (
    <aside
      className={`backdrop-blur-md bg-surface-50/80 dark:bg-surface-900/80 border-r border-surface-200/50 dark:border-surface-700/30 flex flex-col overflow-hidden transition-all duration-300 ease-in-out ${
        isOpen ? 'w-64' : 'w-0 border-r-0'
      }`}
    >
      <div className="w-64 flex flex-col h-full">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-gray-700 dark:text-gray-200">Boards</h2>
            <button
              onClick={onCreateBoard}
              className="p-1 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-800 rounded"
              title="New board"
            >
              <Plus size={16} />
            </button>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto p-2">
          {/* My Boards */}
          <div className="mb-4">
            <h3 className="px-3 py-1 text-xs font-medium uppercase tracking-wider text-gray-400 dark:text-gray-500">
              My Boards
            </h3>
            {boards.length === 0 ? (
              <div className="text-center py-4 text-gray-400 dark:text-gray-500 text-sm">
                No boards created
                <button
                  onClick={onCreateBoard}
                  className="block mx-auto mt-2 text-blue-500 hover:text-blue-600"
                >
                  Create first board
                </button>
              </div>
            ) : (
              <BoardList boards={boards} currentBoard={currentBoard} onSelectBoard={onSelectBoard} />
            )}
          </div>

          {/* Shared with me */}
          {sharedBoards.length > 0 && (
            <div>
              <h3 className="px-3 py-1 text-xs font-medium uppercase tracking-wider text-gray-400 dark:text-gray-500">
                Shared with me
              </h3>
              <BoardList
                boards={sharedBoards}
                currentBoard={currentBoard}
                onSelectBoard={onSelectBoard}
                icon={Share2}
              />
            </div>
          )}
        </nav>
      </div>
    </aside>
  );
}
