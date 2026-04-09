/**
 * Header component
 */

import { useState, useRef, useEffect } from 'react';
import { authAdapter, portalAdapter } from '@/adapters';
import { Plus, Users, Share2, RefreshCw, PanelLeftClose, PanelLeftOpen, Moon, Sun, Settings, BookOpen, BarChart3, Menu, ChevronDown, HelpCircle, Info, X } from 'lucide-react';
import { GuidelinesPanel } from '@/components/guidelines';
import { HelpPanel } from '@/components/help';
import { useCurrentBoard } from '@/store/dashboard';
import logoLight from '@/assets/logo-light.jpg';
import logoDark from '@/assets/logo-dark.jpg';
import { useTheme } from '@/hooks/useTheme';
import { useDashboardApi } from '@/services/api';
import toast from 'react-hot-toast';
import type { BoardSettings } from '@/types';

interface HeaderProps {
  onCreateBoard?: () => void;
  onOpenAgents?: () => void;
  onShareBoard?: () => void;
  onRefreshBoard?: () => void;
  onDeleteBoard?: () => void;
  isRefreshing?: boolean;
  sidebarOpen?: boolean;
  onToggleSidebar?: () => void;
  onBoardUpdated?: () => void;
  onOpenAnalytics?: () => void;
}

export function Header({ onCreateBoard, onOpenAgents, onShareBoard, onRefreshBoard, onDeleteBoard, isRefreshing, sidebarOpen, onToggleSidebar, onBoardUpdated, onOpenAnalytics }: HeaderProps) {
  const { isSignedIn, isLoaded } = authAdapter.useAuth();
  const AdapterUserButton = authAdapter.UserButton;
  const currentBoard = useCurrentBoard();
  const { theme, toggle: toggleTheme } = useTheme();
  const api = useDashboardApi();
  const [showSettings, setShowSettings] = useState(false);
  const [showGuidelines, setShowGuidelines] = useState(false);
  const [showMenu, setShowMenu] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!showSettings && !showMenu) return;
    const handler = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) setShowSettings(false);
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setShowMenu(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showSettings, showMenu]);

  const settings: BoardSettings = currentBoard?.settings
    ? { max_scenarios_per_card: currentBoard.settings.max_scenarios_per_card ?? 3, skip_test_coverage_global: currentBoard.settings.skip_test_coverage_global ?? false, skip_rules_coverage_global: currentBoard.settings.skip_rules_coverage_global ?? false, skip_trs_coverage_global: currentBoard.settings.skip_trs_coverage_global ?? false }
    : { max_scenarios_per_card: 3, skip_test_coverage_global: false, skip_rules_coverage_global: false, skip_trs_coverage_global: false };

  const updateSettings = async (patch: Partial<BoardSettings>) => {
    if (!currentBoard) return;
    const newSettings = { ...settings, ...patch };
    try {
      await api.updateBoard(currentBoard.id, { settings: newSettings } as any);
      onBoardUpdated?.();
      toast.success('Board settings updated');
    } catch {
      toast.error('Failed to update settings');
    }
  };

  return (
    <>
    <header className="px-4 py-2 border-b backdrop-blur-md bg-white/80 dark:bg-[#0b1929]/90 border-surface-200/50 dark:border-[#142840]/60 relative z-20">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {onToggleSidebar && (
            <button
              onClick={onToggleSidebar}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg transition-colors"
              title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            >
              {sidebarOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
            </button>
          )}
          <div className="flex items-center gap-2">
            <img src={theme === 'dark' ? logoDark : logoLight} alt="Okto Pulse" className="h-7 w-7 rounded" />
            <h1 className="text-xl font-bold text-surface-900 dark:text-white font-display tracking-tight">
              Okto Pulse
            </h1>
          </div>
          {currentBoard && (
            <span className="text-sm text-gray-500 dark:text-gray-400">
              / {currentBoard.name}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {currentBoard && (
            <>
              {/* Refresh — always visible */}
              <button
                onClick={onRefreshBoard}
                disabled={isRefreshing}
                className="btn btn-secondary flex items-center gap-1 text-sm"
                title="Refresh board"
              >
                <RefreshCw size={16} className={isRefreshing ? 'animate-spin' : ''} />
              </button>

              {/* Menu dropdown */}
              <div className="relative" ref={menuRef}>
                <button
                  onClick={() => setShowMenu(!showMenu)}
                  className={`btn btn-secondary flex items-center gap-1 text-sm ${showMenu ? 'ring-2 ring-blue-300' : ''}`}
                >
                  <Menu size={16} />
                  <ChevronDown size={12} />
                </button>

                {showMenu && (
                  <div className="absolute right-0 top-full mt-2 w-56 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-xl z-50 py-1">
                    {/* + New Dashboard */}
                    <button
                      onClick={() => { setShowMenu(false); onCreateBoard?.(); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Plus size={14} />
                      New Dashboard
                    </button>

                    <hr className="my-1 border-gray-200 dark:border-gray-700" />

                    {/* Guidelines */}
                    <button
                      onClick={() => { setShowMenu(false); setShowGuidelines(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <BookOpen size={14} />
                      Guidelines
                    </button>

                    {/* Analytics */}
                    <button
                      onClick={() => { setShowMenu(false); onOpenAnalytics?.(); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <BarChart3 size={14} />
                      Analytics
                    </button>

                    {/* Agents */}
                    <button
                      onClick={() => { setShowMenu(false); onOpenAgents?.(); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Users size={14} />
                      Agents
                    </button>

                    <hr className="my-1 border-gray-200 dark:border-gray-700" />

                    {/* Share — only when portal adapter provides it */}
                    {portalAdapter.ShareBoardModal && (
                      <button
                        onClick={() => { setShowMenu(false); onShareBoard?.(); }}
                        className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                      >
                        <Share2 size={14} />
                        Share
                      </button>
                    )}

                    {/* Settings */}
                    <button
                      onClick={() => { setShowMenu(false); setShowSettings(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Settings size={14} />
                      Settings
                    </button>

                    {/* Toggle View Mode */}
                    <button
                      onClick={() => { setShowMenu(false); toggleTheme(); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
                      {theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
                    </button>

                    <hr className="my-1 border-gray-200 dark:border-gray-700" />

                    {/* Help */}
                    <button
                      onClick={() => { setShowMenu(false); setShowHelp(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <HelpCircle size={14} />
                      Help
                    </button>

                    {/* About — standalone/community only */}
                    {!portalAdapter.ShareBoardModal && (
                      <button
                        onClick={() => { setShowMenu(false); setShowAbout(true); }}
                        className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                      >
                        <Info size={14} />
                        About
                      </button>
                    )}
                  </div>
                )}
              </div>

              {/* Settings panel (opens from menu) */}
              {showSettings && (
                <div className="fixed inset-0 z-50 flex items-start justify-end pt-16 pr-4" onClick={() => setShowSettings(false)}>
                  <div className="w-80 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-xl p-4 space-y-4" onClick={(e) => e.stopPropagation()}>
                    <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200">Board Settings</h3>

                    <div>
                      <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block mb-1">
                        Max test scenarios per card
                      </label>
                      <p className="text-[10px] text-gray-400 mb-1.5">
                        Limits how many scenarios a single card can be linked to.
                      </p>
                      <div className="flex items-center gap-2">
                        {[1, 2, 3, 5, 10].map((n) => (
                          <button
                            key={n}
                            onClick={() => updateSettings({ max_scenarios_per_card: n })}
                            className={`w-8 h-8 rounded text-xs font-medium transition-colors ${
                              settings.max_scenarios_per_card === n
                                ? 'bg-blue-500 text-white'
                                : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
                            }`}
                          >
                            {n}
                          </button>
                        ))}
                      </div>
                    </div>

                    <div className="flex items-center justify-between">
                      <div>
                        <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block">
                          Skip test coverage (global)
                        </label>
                        <p className="text-[10px] text-gray-400">
                          Bypass test coverage checks for all specs
                        </p>
                      </div>
                      <button
                        onClick={() => updateSettings({ skip_test_coverage_global: !settings.skip_test_coverage_global })}
                        className={`relative w-10 h-5 rounded-full transition-colors ${settings.skip_test_coverage_global ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.skip_test_coverage_global ? 'translate-x-5' : ''}`} />
                      </button>
                    </div>

                    <div className="flex items-center justify-between">
                      <div>
                        <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block">
                          Skip rules coverage (global)
                        </label>
                        <p className="text-[10px] text-gray-400">
                          Bypass FR→BR coverage checks for all specs
                        </p>
                      </div>
                      <button
                        onClick={() => updateSettings({ skip_rules_coverage_global: !settings.skip_rules_coverage_global })}
                        className={`relative w-10 h-5 rounded-full transition-colors ${settings.skip_rules_coverage_global ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.skip_rules_coverage_global ? 'translate-x-5' : ''}`} />
                      </button>
                    </div>

                    <div className="flex items-center justify-between">
                      <div>
                        <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block">
                          Skip TRs coverage (global)
                        </label>
                        <p className="text-[10px] text-gray-400">
                          Bypass TR→Task coverage checks for all specs
                        </p>
                      </div>
                      <button
                        onClick={() => updateSettings({ skip_trs_coverage_global: !settings.skip_trs_coverage_global })}
                        className={`relative w-10 h-5 rounded-full transition-colors ${settings.skip_trs_coverage_global ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.skip_trs_coverage_global ? 'translate-x-5' : ''}`} />
                      </button>
                    </div>

                    <hr className="border-gray-200 dark:border-gray-700" />
                    <button
                      onClick={onDeleteBoard}
                      className="w-full text-left text-xs text-red-500 hover:text-red-600 flex items-center gap-1"
                    >
                      Delete board
                    </button>
                  </div>
                </div>
              )}
            </>
          )}

          {AdapterUserButton && isLoaded && isSignedIn && (
            <AdapterUserButton afterSignOutUrl="/" />
          )}
        </div>
      </div>

    </header>

      {showGuidelines && currentBoard && (
        <GuidelinesPanel
          boardId={currentBoard.id}
          onClose={() => setShowGuidelines(false)}
        />
      )}

      {showHelp && (
        <HelpPanel onClose={() => setShowHelp(false)} />
      )}

      {showAbout && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={() => setShowAbout(false)}>
          <div
            className="relative w-[480px] max-w-[90vw] bg-white dark:bg-[#0b1929] rounded-2xl shadow-2xl border border-surface-200/50 dark:border-[#142840] overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            <button
              onClick={() => setShowAbout(false)}
              className="absolute top-3 right-3 p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg transition-colors z-10"
            >
              <X size={16} />
            </button>

            <div className="flex flex-col items-center px-8 pt-8 pb-6">
              <img src={theme === 'dark' ? logoDark : logoLight} alt="Okto Labs" className="w-[200px] h-[200px] object-contain rounded-xl" />
              <h2 className="text-xl font-bold text-surface-900 dark:text-white mt-4 font-display">
                Okto Pulse
              </h2>
              <p className="text-sm text-surface-500 dark:text-surface-400 mt-1">
                Community Edition — v0.1.0
              </p>
            </div>

            <div className="border-t border-surface-200/50 dark:border-[#142840] px-8 py-5 max-h-[40vh] overflow-y-auto">
              <h3 className="text-xs font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mb-3 font-display">
                License
              </h3>
              <div className="text-xs text-surface-600 dark:text-surface-400 leading-relaxed space-y-2">
                <p className="font-medium text-surface-700 dark:text-surface-300">Elastic License 2.0</p>
                <p>Copyright 2024–2026 Okto Labs</p>
                <p>
                  <strong>Grant of License.</strong> The licensor grants you a non-exclusive, royalty-free,
                  worldwide, non-sublicensable, non-transferable license to use, copy, distribute, make
                  available, and prepare derivative works of the software, subject to the limitations below.
                </p>
                <p>
                  <strong>Limitations.</strong> You may not provide the software to third parties as a hosted
                  or managed service, where the service provides users with access to any substantial set of
                  the features or functionality of the software. You may not move, change, disable, or
                  circumvent the license key functionality in the software, and you may not remove or obscure
                  any functionality protected by the license key. You may not alter, remove, or obscure any
                  licensing, copyright, or other notices of the licensor.
                </p>
                <p>
                  <strong>Termination.</strong> If you violate these terms, your licenses terminate
                  automatically. If the licensor notifies you and you cease violation within 30 days,
                  your licenses are reinstated retroactively. A second violation terminates all licenses
                  permanently.
                </p>
                <p>
                  <strong>Limitation on Liability.</strong> The software comes as is, without any warranty
                  or condition, and the licensor will not be liable to you for any damages arising out of
                  these terms or the use or nature of the software.
                </p>
                <p className="text-surface-400 dark:text-surface-500 pt-1">
                  Full license text at{' '}
                  <a
                    href="https://github.com/okto-labs/okto-pulse/blob/main/LICENSE"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-accent-500 hover:underline"
                  >
                    github.com/okto-labs/okto-pulse
                  </a>
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
