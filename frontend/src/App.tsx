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
import { GlobalKGActivityIndicator } from '@/components/knowledge/GlobalKGActivityIndicator';
import { KGHealthView } from '@/components/knowledge/KGHealthView';
import { ModalStackProvider } from '@/contexts/ModalStackContext';
import { ModalStackRenderer } from '@/components/modals/ModalStackRenderer';
import { LineageGraphModal } from '@/components/traceability';
import { EvidenceGateSkipBanner } from '@/components/banners/EvidenceGateSkipBanner';
import { getBoardSettings } from '@/services/board-settings-api';
import { useTermsAcceptance } from '@/hooks/useTermsAcceptance';
import { TermsAcceptanceModal } from '@/components/onboarding/TermsAcceptanceModal';
import { OnboardingModal } from '@/components/onboarding/OnboardingModal';
import { isCompleted as isOnboardingCompleted } from '@/components/onboarding/onboardingStorage';
import logoLight from '@/assets/logo-light.png';
import logoDark from '@/assets/logo-dark.png';

function App() {
  const api = useDashboardApi();
  const { isLoaded, isSignedIn } = authAdapter.useAuth();
  const terms = useTermsAcceptance();
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
  const [onboardingOpen, setOnboardingOpen] = useState(false);

  // Mount the OnboardingModal on the *first* render after Terms is accepted
  // (or skipped via CLI/env), but only if the user has not completed it yet.
  // Returning users (flag set) never see it — no flash, no nag.
  useEffect(() => {
    if (terms.loading) return;
    if (terms.needsAcceptance) return;
    if (isOnboardingCompleted()) return;
    setOnboardingOpen(true);
  }, [terms.loading, terms.needsAcceptance]);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [skipEvidenceActive, setSkipEvidenceActive] = useState(false);
  const [activeTab, setActiveTab] = useState<'ideations' | 'refinements' | 'specs' | 'sprints' | 'tasks'>('ideations');
  const [showAnalytics, setShowAnalytics] = useState(
    () => typeof window !== 'undefined' && window.location.pathname.startsWith('/analytics'),
  );
  const [showKGHealth, setShowKGHealth] = useState(
    () => typeof window !== 'undefined' && window.location.pathname.startsWith('/kg-health'),
  );

  // Mantém showAnalytics e showKGHealth sincronizados com back/forward do browser.
  useEffect(() => {
    const handlePopstate = () => {
      setShowAnalytics(window.location.pathname.startsWith('/analytics'));
      setShowKGHealth(window.location.pathname.startsWith('/kg-health'));
    };
    window.addEventListener('popstate', handlePopstate);
    return () => window.removeEventListener('popstate', handlePopstate);
  }, []);

  // NC-9 Wave 2 frontend (spec 5cb09dbc): poll board settings to drive the
  // EvidenceGateSkipBanner. Re-runs when the active board changes or when
  // the operator toggles via the Board tab in RuntimeSettingsPanel (custom
  // event refreshes the flag without a reload).
  //
  // Bug fix (board d0f6bab2): always reset to false on (re-)mount and on
  // board switch BEFORE the new fetch resolves. Without this, switching
  // from a board with skip=true to one with skip=false (or hitting a
  // transient backend error from the lock-contention bug) left the banner
  // "stuck" as SKIP ACTIVE because the catch path silently kept the prior
  // value. Reset-then-refresh guarantees the banner reflects the GET, not
  // the last-rendered state of a different board.
  useEffect(() => {
    setSkipEvidenceActive(false);
    if (!currentBoard) {
      return;
    }
    let active = true;
    const refresh = () => {
      getBoardSettings(currentBoard.id)
        .then((s) => {
          if (active) setSkipEvidenceActive(s?.skip_test_evidence_global ?? false);
        })
        .catch((err) => {
          // Non-fatal: keep banner OFF so we never show a stale "skip
          // active" warning when we can't actually confirm it from the
          // backend. Logged for diagnostics.
          if (active) {
            setSkipEvidenceActive(false);
            // eslint-disable-next-line no-console
            console.warn('[evidence-gate] settings fetch failed:', err);
          }
        });
    };
    refresh();
    const handler = () => refresh();
    window.addEventListener('okto:board-settings-changed', handler);
    return () => {
      active = false;
      window.removeEventListener('okto:board-settings-changed', handler);
    };
  }, [currentBoard?.id]);

  const openBoardSettings = () => {
    window.dispatchEvent(new CustomEvent('okto:open-board-settings'));
  };

  const openAnalytics = () => {
    if (!window.location.pathname.startsWith('/analytics')) {
      window.history.pushState({}, '', '/analytics');
    }
    setShowAnalytics(true);
  };

  const closeAnalytics = () => {
    if (window.location.pathname.startsWith('/analytics')) {
      window.history.pushState({}, '', '/');
    }
    setShowAnalytics(false);
  };

  const openKGHealth = () => {
    if (!window.location.pathname.startsWith('/kg-health')) {
      window.history.pushState({}, '', '/kg-health');
    }
    setShowKGHealth(true);
  };

  const closeKGHealth = () => {
    if (window.location.pathname.startsWith('/kg-health')) {
      window.history.pushState({}, '', '/');
    }
    setShowKGHealth(false);
  };

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
    <ModalStackProvider>
    {terms.needsAcceptance && (
      <TermsAcceptanceModal onAccept={terms.accept} />
    )}
    {onboardingOpen && (
      <OnboardingModal onClose={() => setOnboardingOpen(false)} />
    )}
    <div className={`min-h-screen flex flex-col bg-surface-50 dark:bg-surface-950 ${terms.needsAcceptance ? 'pointer-events-none select-none' : ''}`}>
      {portalAdapter.PortalBar && (
        <portalAdapter.PortalBar
          visible={portalBarVisible}
          onToggleVisibility={() => setPortalBarVisible((v) => !v)}
        />
      )}
      <EvidenceGateSkipBanner
        skipActive={skipEvidenceActive}
        onOpenBoardSettings={openBoardSettings}
      />
      <Header
        onCreateBoard={() => setCreateBoardOpen(true)}
        onOpenAgents={() => setAgentsModalOpen(true)}
        onShareBoard={() => setShareModalOpen(true)}
        onRefreshBoard={refreshBoard}
        onDeleteBoard={deleteBoard}
        onBoardUpdated={refreshBoard}
        onOpenAnalytics={openAnalytics}
        onOpenKGHealth={openKGHealth}
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

        <main className="flex-1 min-w-0 overflow-auto p-4 flex flex-col">
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
              <div className="flex-1 min-h-0 min-w-0">
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

      {/* Cross-app indicator: reflects KG consolidation work for the current
          board in every tab, not only when the KG page is mounted. */}
      <GlobalKGActivityIndicator boardId={currentBoard?.id ?? null} />

      {/* Analytics fullscreen overlay */}
      {showAnalytics && (
        <div className="fixed inset-0 z-50 bg-gray-50 dark:bg-gray-900 flex flex-col">
          <div className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-8 py-3 flex items-center justify-between shrink-0">
            <h1 className="text-lg font-bold text-gray-900 dark:text-white">Analytics</h1>
            <button
              onClick={closeAnalytics}
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

      {/* KG Health fullscreen overlay (spec d754d004) */}
      {showKGHealth && (
        <div className="fixed inset-0 z-50 bg-gray-50 dark:bg-gray-900 flex flex-col">
          <KGHealthView onClose={closeKGHealth} />
        </div>
      )}

      {/* Root-level drill-down modal renderer — ideação c13f7bd3.
          Mounted here so it's visible from every tab (including
          Knowledge, where the usual entity modals aren't hosted). */}
      {currentBoard && <LineageGraphModal boardId={currentBoard.id} />}
      {currentBoard && <ModalStackRenderer boardId={currentBoard.id} />}
    </div>
    </ModalStackProvider>
  );
}

export default App;
