import { useEffect, useState } from 'react';
import { authAdapter, portalAdapter } from '@/adapters';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { useDashboardStore } from '@/store/dashboard';
import { Header, Sidebar, CreateBoardModal, AgentsModal } from '@/components/layout';
import { KanbanBoard } from '@/components/kanban';
import { IdeationsPanel } from '@/components/ideations';
import { RefinementsPanel } from '@/components/refinements';
import { SpecsPanel } from '@/components/specs';
import { SprintsPanel } from '@/components/sprints';
import { AnalyticsPage } from '@/components/analytics';
import logoLight from '@/assets/logo-light.png';
import logoDark from '@/assets/logo-dark.png';

function App() {
  const api = useDashboardApi();
  const { isLoaded, isSignedIn } = authAdapter.useAuth();
  const currentBoard = useDashboardStore((s) => s.currentBoard);
  const setBoards = useDashboardStore((s) => s.setBoards);
  const setSharedBoards = useDashboardStore((s) => s.setSharedBoards);
  const setCurrentBoard = useDashboardStore((s) => s.setCurrentBoard);
  const setColumns = useDashboardStore((s) => s.setColumns);
  const setLoading = useDashboardStore((s) => s.setLoading);
  const setError = useDashboardStore((s) => s.setError);

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [createBoardOpen, setCreateBoardOpen] = useState(false);
  const [agentsModalOpen, setAgentsModalOpen] = useState(false);
  const [shareModalOpen, setShareModalOpen] = useState(false);
  const [portalBarVisible, setPortalBarVisible] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [activeTab, setActiveTab] = useState<'ideations' | 'refinements' | 'specs' | 'sprints' | 'tasks'>('ideations');
  const [showAnalytics, setShowAnalytics] = useState(false);

  useEffect(() => {
    if (isLoaded && isSignedIn) {
      loadBoards();
    }
  }, [isLoaded, isSignedIn]);

  const loadBoards = async () => {
    setLoading(true);
    try {
      const [myBoards, shared] = await Promise.all([
        api.listBoards(0, 100, 'my'),
        api.listBoards(0, 100, 'shared'),
      ]);
      setBoards(myBoards);
      setSharedBoards(shared);
      const allBoards = [...myBoards, ...shared];
      if (allBoards.length > 0 && !currentBoard) {
        await selectBoard(allBoards[0].id);
      }
    } catch {
      setError('Failed to load boards');
      toast.error('Failed to load boards');
    } finally {
      setLoading(false);
    }
  };

  const selectBoard = async (boardId: string) => {
    setLoading(true);
    try {
      const board = await api.getBoard(boardId);
      setCurrentBoard(board);
      const columns = await api.getBoardColumns(boardId);
      setColumns(columns);
    } catch {
      setError('Failed to load board');
      toast.error('Failed to load board');
    } finally {
      setLoading(false);
    }
  };

  const refreshBoard = async () => {
    if (!currentBoard) return;
    setIsRefreshing(true);
    try {
      const board = await api.getBoard(currentBoard.id);
      setCurrentBoard(board);
      const columns = await api.getBoardColumns(currentBoard.id);
      setColumns(columns);
      setRefreshKey((k) => k + 1);
      toast.success('Board refreshed!');
    } catch {
      toast.error('Failed to refresh board');
    } finally {
      setIsRefreshing(false);
    }
  };

  const deleteBoard = async () => {
    if (!currentBoard) return;
    if (!confirm(`Delete board "${currentBoard.name}" and all its cards? This cannot be undone.`)) return;
    try {
      await api.deleteBoard(currentBoard.id);
      setCurrentBoard(null);
      setColumns({} as any);
      await loadBoards();
      toast.success('Board deleted');
    } catch {
      toast.error('Failed to delete board');
    }
  };

  if (!isLoaded) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-surface-50 dark:bg-surface-950">
        <img src={logoLight} alt="Okto Pulse" className="h-16 w-16 mb-4 animate-pulse rounded-xl dark:hidden" />
        <img src={logoDark} alt="Okto Pulse" className="h-16 w-16 mb-4 animate-pulse rounded-xl hidden dark:block" />
        <div className="text-gray-500 dark:text-gray-400">Loading...</div>
      </div>
    );
  }

  if (!isSignedIn) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-50 dark:bg-surface-950">
        <div className="text-center">
          <img src={logoLight} alt="Okto Pulse" className="h-20 w-20 mx-auto mb-4 rounded-xl dark:hidden" />
          <img src={logoDark} alt="Okto Pulse" className="h-20 w-20 mx-auto mb-4 rounded-xl hidden dark:block" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">
            Okto Pulse
          </h1>
          <p className="text-gray-500 dark:text-gray-400">Sign in to continue</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex flex-col bg-surface-50 dark:bg-surface-950">
      {portalAdapter.PortalBar && (
        <portalAdapter.PortalBar
          visible={portalBarVisible}
          onToggleVisibility={() => setPortalBarVisible((v) => !v)}
        />
      )}
      <Header
        onCreateBoard={() => setCreateBoardOpen(true)}
        onOpenAgents={() => setAgentsModalOpen(true)}
        onShareBoard={() => setShareModalOpen(true)}
        onRefreshBoard={refreshBoard}
        onDeleteBoard={deleteBoard}
        onBoardUpdated={refreshBoard}
        onOpenAnalytics={() => setShowAnalytics(true)}
        isRefreshing={isRefreshing}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
      />

      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          isOpen={sidebarOpen}
          onSelectBoard={selectBoard}
          onCreateBoard={() => setCreateBoardOpen(true)}
        />

        <main className="flex-1 overflow-auto p-4 flex flex-col">
          {currentBoard ? (
            <>
              {/* Tab switcher */}
              <div className="flex items-center gap-1 mb-4 bg-surface-200/60 dark:bg-surface-800/60 backdrop-blur-sm rounded-xl p-0.5 w-fit border border-surface-200/40 dark:border-surface-700/30">
                {([
                  { id: 'ideations' as const, label: 'Ideations' },
                  { id: 'refinements' as const, label: 'Refinements' },
                  { id: 'specs' as const, label: 'Specs' },
                  { id: 'sprints' as const, label: 'Sprints' },
                  { id: 'tasks' as const, label: 'Tasks' },
                ]).map((tab) => (
                  <button
                    key={tab.id}
                    onClick={() => setActiveTab(tab.id)}
                    className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 ${
                      activeTab === tab.id
                        ? 'bg-white dark:bg-surface-700 text-surface-900 dark:text-white shadow-sm'
                        : 'text-surface-500 dark:text-surface-400 hover:text-surface-900 dark:hover:text-white'
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>

              {/* Content */}
              <div className="flex-1 min-h-0">
                {activeTab === 'ideations' && <IdeationsPanel key={refreshKey} boardId={currentBoard.id} />}
                {activeTab === 'refinements' && <RefinementsPanel key={refreshKey} boardId={currentBoard.id} />}
                {activeTab === 'specs' && <SpecsPanel key={refreshKey} boardId={currentBoard.id} />}
                {activeTab === 'sprints' && <SprintsPanel key={refreshKey} boardId={currentBoard.id} />}
                {activeTab === 'tasks' && <KanbanBoard boardId={currentBoard.id} />}
              </div>
            </>
          ) : (
            <div className="h-full flex items-center justify-center">
              <div className="text-center text-gray-500 dark:text-gray-400">
                <p className="mb-4">Select or create a board to get started</p>
                <button
                  onClick={() => setCreateBoardOpen(true)}
                  className="btn btn-primary"
                >
                  Create Board
                </button>
              </div>
            </div>
          )}
        </main>
      </div>

      <CreateBoardModal
        isOpen={createBoardOpen}
        onClose={() => setCreateBoardOpen(false)}
      />

      <AgentsModal
        isOpen={agentsModalOpen}
        onClose={() => setAgentsModalOpen(false)}
      />

      {currentBoard && portalAdapter.ShareBoardModal && (
        <portalAdapter.ShareBoardModal
          isOpen={shareModalOpen}
          onClose={() => setShareModalOpen(false)}
          boardId={currentBoard.id}
          boardName={currentBoard.name}
        />
      )}

      {/* Analytics fullscreen overlay */}
      {showAnalytics && (
        <div className="fixed inset-0 z-50 bg-gray-50 dark:bg-gray-900 flex flex-col">
          <div className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-2 flex items-center justify-between shrink-0">
            <h1 className="text-lg font-bold text-gray-900 dark:text-white">Analytics</h1>
            <button
              onClick={() => setShowAnalytics(false)}
              className="btn btn-secondary text-sm"
            >
              ← Back to Board
            </button>
          </div>
          <div className="flex-1 overflow-auto">
            <AnalyticsPage />
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
