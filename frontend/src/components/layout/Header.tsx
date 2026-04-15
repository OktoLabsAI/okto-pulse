/**
 * Header component
 */

import { useState, useRef, useEffect } from 'react';
import { authAdapter, portalAdapter } from '@/adapters';
import { Plus, Users, Share2, RefreshCw, PanelLeftClose, PanelLeftOpen, Moon, Sun, Settings, BookOpen, BarChart3, Menu, ChevronDown, HelpCircle, Info, X, Shield } from 'lucide-react';
import { GuidelinesPanel } from '@/components/guidelines';
import { HelpPanel } from '@/components/help';
import { PresetListModal } from '@/components/permissions';
import { useCurrentBoard } from '@/store/dashboard';
import logoLight from '@/assets/logo-light.png';
import logoDark from '@/assets/logo-dark.png';
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
  const [showPresets, setShowPresets] = useState(false);
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
    ? { max_scenarios_per_card: currentBoard.settings.max_scenarios_per_card ?? 3, skip_test_coverage_global: currentBoard.settings.skip_test_coverage_global ?? false, skip_rules_coverage_global: currentBoard.settings.skip_rules_coverage_global ?? false, skip_trs_coverage_global: currentBoard.settings.skip_trs_coverage_global ?? false, skip_contract_coverage_global: currentBoard.settings.skip_contract_coverage_global ?? false, require_task_validation: currentBoard.settings.require_task_validation ?? false, min_confidence: currentBoard.settings.min_confidence ?? 70, min_completeness: currentBoard.settings.min_completeness ?? 80, max_drift: currentBoard.settings.max_drift ?? 50, require_spec_validation: currentBoard.settings.require_spec_validation ?? false, min_spec_completeness: currentBoard.settings.min_spec_completeness ?? 80, min_spec_assertiveness: currentBoard.settings.min_spec_assertiveness ?? 80, max_spec_ambiguity: currentBoard.settings.max_spec_ambiguity ?? 30 }
    : { max_scenarios_per_card: 3, skip_test_coverage_global: false, skip_rules_coverage_global: false, skip_trs_coverage_global: false, skip_contract_coverage_global: false, require_task_validation: false, min_confidence: 70, min_completeness: 80, max_drift: 50, require_spec_validation: false, min_spec_completeness: 80, min_spec_assertiveness: 80, max_spec_ambiguity: 30 };

  // Local draft state for numeric gate inputs — committed on blur to avoid
  // refresh-on-keystroke wiping partial values while the user is typing.
  const [minConfidenceDraft, setMinConfidenceDraft] = useState<string>(String(settings.min_confidence));
  const [minCompletenessDraft, setMinCompletenessDraft] = useState<string>(String(settings.min_completeness));
  const [maxDriftDraft, setMaxDriftDraft] = useState<string>(String(settings.max_drift));
  // Spec Validation Gate draft inputs — same onBlur pattern as the Task Gate.
  const [minSpecCompletenessDraft, setMinSpecCompletenessDraft] = useState<string>(String(settings.min_spec_completeness ?? 80));
  const [minSpecAssertivenessDraft, setMinSpecAssertivenessDraft] = useState<string>(String(settings.min_spec_assertiveness ?? 80));
  const [maxSpecAmbiguityDraft, setMaxSpecAmbiguityDraft] = useState<string>(String(settings.max_spec_ambiguity ?? 30));

  useEffect(() => { setMinConfidenceDraft(String(settings.min_confidence)); }, [settings.min_confidence]);
  useEffect(() => { setMinCompletenessDraft(String(settings.min_completeness)); }, [settings.min_completeness]);
  useEffect(() => { setMaxDriftDraft(String(settings.max_drift)); }, [settings.max_drift]);
  useEffect(() => { setMinSpecCompletenessDraft(String(settings.min_spec_completeness ?? 80)); }, [settings.min_spec_completeness]);
  useEffect(() => { setMinSpecAssertivenessDraft(String(settings.min_spec_assertiveness ?? 80)); }, [settings.min_spec_assertiveness]);
  useEffect(() => { setMaxSpecAmbiguityDraft(String(settings.max_spec_ambiguity ?? 30)); }, [settings.max_spec_ambiguity]);

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

  type NumericSettingKey = 'min_confidence' | 'min_completeness' | 'max_drift' | 'min_spec_completeness' | 'min_spec_assertiveness' | 'max_spec_ambiguity';

  const commitNumericSetting = (key: NumericSettingKey, raw: string) => {
    const parsed = Math.min(100, Math.max(0, Number(raw)));
    const current = (settings[key] ?? 0) as number;
    const safe = Number.isFinite(parsed) ? parsed : current;
    if (safe === current) {
      // Re-sync draft in case of invalid input (e.g. empty string)
      if (key === 'min_confidence') setMinConfidenceDraft(String(safe));
      if (key === 'min_completeness') setMinCompletenessDraft(String(safe));
      if (key === 'max_drift') setMaxDriftDraft(String(safe));
      if (key === 'min_spec_completeness') setMinSpecCompletenessDraft(String(safe));
      if (key === 'min_spec_assertiveness') setMinSpecAssertivenessDraft(String(safe));
      if (key === 'max_spec_ambiguity') setMaxSpecAmbiguityDraft(String(safe));
      return;
    }
    updateSettings({ [key]: safe } as Partial<BoardSettings>);
  };

  return (
    <>
    <header className="px-4 py-2 border-b backdrop-blur-md bg-white/80 dark:bg-black/90 border-surface-200/50 dark:border-gray-800/60 relative z-20">
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

                    {/* Presets */}
                    <button
                      onClick={() => { setShowMenu(false); setShowPresets(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Shield size={14} />
                      Presets
                    </button>

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

                    <div className="flex items-center justify-between">
                      <div>
                        <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block">
                          Skip contract coverage (global)
                        </label>
                        <p className="text-[10px] text-gray-400">
                          Bypass API contract→Task coverage checks
                        </p>
                      </div>
                      <button
                        onClick={() => updateSettings({ skip_contract_coverage_global: !settings.skip_contract_coverage_global })}
                        className={`relative w-10 h-5 rounded-full transition-colors ${settings.skip_contract_coverage_global ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.skip_contract_coverage_global ? 'translate-x-5' : ''}`} />
                      </button>
                    </div>

                    {/* Task Validation Gate */}
                    <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                      <h4 className="text-xs font-semibold text-gray-800 dark:text-gray-200 mb-3 flex items-center gap-1.5">
                        <Shield size={12} />
                        Task Validation Gate
                      </h4>

                      <div className="flex items-center justify-between mb-3">
                        <div>
                          <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block">
                            Require task validation
                          </label>
                          <p className="text-[10px] text-gray-400">
                            Tasks must pass validation before moving to Done
                          </p>
                        </div>
                        <button
                          onClick={() => updateSettings({ require_task_validation: !settings.require_task_validation })}
                          className={`relative w-10 h-5 rounded-full transition-colors ${settings.require_task_validation ? 'bg-violet-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                        >
                          <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.require_task_validation ? 'translate-x-5' : ''}`} />
                        </button>
                      </div>

                      {settings.require_task_validation && (
                        <div className="space-y-2.5 pl-1">
                          <div>
                            <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 block mb-1">
                              Min Confidence
                            </label>
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={0}
                                max={100}
                                value={minConfidenceDraft}
                                onChange={(e) => setMinConfidenceDraft(e.target.value)}
                                onBlur={(e) => commitNumericSetting('min_confidence', e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                                className="w-16 text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                              />
                              <span className="text-[10px] text-gray-400">/ 100</span>
                            </div>
                          </div>
                          <div>
                            <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 block mb-1">
                              Min Completeness
                            </label>
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={0}
                                max={100}
                                value={minCompletenessDraft}
                                onChange={(e) => setMinCompletenessDraft(e.target.value)}
                                onBlur={(e) => commitNumericSetting('min_completeness', e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                                className="w-16 text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                              />
                              <span className="text-[10px] text-gray-400">/ 100</span>
                            </div>
                          </div>
                          <div>
                            <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 block mb-1">
                              Max Drift
                            </label>
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={0}
                                max={100}
                                value={maxDriftDraft}
                                onChange={(e) => setMaxDriftDraft(e.target.value)}
                                onBlur={(e) => commitNumericSetting('max_drift', e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                                className="w-16 text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                              />
                              <span className="text-[10px] text-gray-400">/ 100</span>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>

                    {/* Spec Validation Gate */}
                    <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
                      <h4 className="text-xs font-semibold text-gray-800 dark:text-gray-200 mb-3 flex items-center gap-1.5">
                        <Shield size={12} />
                        Spec Validation Gate
                      </h4>

                      <div className="flex items-center justify-between mb-3">
                        <div>
                          <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block">
                            Require spec validation
                          </label>
                          <p className="text-[10px] text-gray-400">
                            Specs must pass Completeness/Assertiveness/Ambiguity gate before Validated
                          </p>
                        </div>
                        <button
                          onClick={() => updateSettings({ require_spec_validation: !settings.require_spec_validation })}
                          className={`relative w-10 h-5 rounded-full transition-colors ${settings.require_spec_validation ? 'bg-violet-500' : 'bg-gray-300 dark:bg-gray-600'}`}
                        >
                          <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${settings.require_spec_validation ? 'translate-x-5' : ''}`} />
                        </button>
                      </div>

                      {settings.require_spec_validation && (
                        <div className="space-y-2.5 pl-1">
                          <div>
                            <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 block mb-1">
                              Min Completeness
                            </label>
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={0}
                                max={100}
                                value={minSpecCompletenessDraft}
                                onChange={(e) => setMinSpecCompletenessDraft(e.target.value)}
                                onBlur={(e) => commitNumericSetting('min_spec_completeness', e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                                className="w-16 text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                              />
                              <span className="text-[10px] text-gray-400">/ 100</span>
                            </div>
                          </div>
                          <div>
                            <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 block mb-1">
                              Min Assertiveness
                            </label>
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={0}
                                max={100}
                                value={minSpecAssertivenessDraft}
                                onChange={(e) => setMinSpecAssertivenessDraft(e.target.value)}
                                onBlur={(e) => commitNumericSetting('min_spec_assertiveness', e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                                className="w-16 text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                              />
                              <span className="text-[10px] text-gray-400">/ 100</span>
                            </div>
                          </div>
                          <div>
                            <label className="text-[10px] font-medium text-gray-500 dark:text-gray-400 block mb-1">
                              Max Ambiguity <span className="text-gray-400">(lower is better)</span>
                            </label>
                            <div className="flex items-center gap-2">
                              <input
                                type="number"
                                min={0}
                                max={100}
                                value={maxSpecAmbiguityDraft}
                                onChange={(e) => setMaxSpecAmbiguityDraft(e.target.value)}
                                onBlur={(e) => commitNumericSetting('max_spec_ambiguity', e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                                className="w-16 text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                              />
                              <span className="text-[10px] text-gray-400">/ 100</span>
                            </div>
                          </div>
                        </div>
                      )}
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

      {showPresets && (
        <PresetListModal onClose={() => setShowPresets(false)} />
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

            <div className="border-t border-surface-200/50 dark:border-[#142840] px-8 py-5 max-h-[50vh] overflow-y-auto">
              <h3 className="text-xs font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mb-3 font-display">
                License — Elastic License 2.0
              </h3>
              <div className="text-xs text-surface-600 dark:text-surface-400 leading-relaxed space-y-3">
                <p className="font-medium text-surface-700 dark:text-surface-300">Copyright 2024–2026 Okto Labs</p>

                <p><strong>Acceptance.</strong> By using the software, you agree to all of the terms and conditions below.</p>

                <p><strong>1. Grant of License.</strong> The licensor grants you a non-exclusive, royalty-free, worldwide, non-sublicensable, non-transferable license to use, copy, distribute, make available, and prepare derivative works of the software, in each case subject to the limitations and conditions below.</p>

                <p><strong>2. Limitations.</strong> You may not provide the software to third parties as a hosted or managed service, where the service provides users with access to any substantial set of the features or functionality of the software. You may not move, change, disable, or circumvent the license key functionality in the software, and you may not remove or obscure any functionality in the software that is protected by the license key. You may not alter, remove, or obscure any licensing, copyright, or other notices of the licensor in the software.</p>

                <p><strong>3. Patent License.</strong> The licensor grants you a license, under any patent claims the licensor can license, or becomes able to license, to make, have made, use, sell, offer for sale, import and have imported the software. However, this license does not cover any patent claims that you cause to be infringed by modifications or additions to the software.</p>

                <p><strong>4. Distribution.</strong> You may not alter, remove, or obscure any licensing, copyright, or other notices of the licensor in the software. Any distribution of the software must include a copy of these terms and conditions, and anyone who receives the software from you is bound by these terms and conditions.</p>

                <p><strong>5. Notices.</strong> You must include a copy of these terms with any distribution of the software. If you modify the software, you must mark the modifications clearly and include the date of the modifications.</p>

                <p><strong>6. Termination.</strong> If you violate these terms, your licenses will terminate automatically. If the licensor notifies you of your violation, and you cease all violation of this license no later than 30 days after you receive that notice, your licenses will be reinstated retroactively. However, if you violate these terms after the reinstatement, all of your licenses will terminate permanently.</p>

                <p><strong>7. No Other Rights.</strong> Except as expressly stated herein, no other rights or licenses are granted, express or implied.</p>

                <p><strong>8. Limitation on Liability.</strong> As far as the law allows, the software comes as is, without any warranty or condition, and the licensor will not be liable to you for any damages arising out of these terms or the use or nature of the software, under any kind of legal claim.</p>

                <p><strong>9. Definitions.</strong> "Licensor" means Okto Labs and its affiliates. "Software" means the software the licensor makes available under these terms, including any portions, modifications, or derivative works. "You" means you, individually. "Your company" means any legal entity, sole proprietorship, or other organization that you work for, plus all other organizations that control, are controlled by, or are under common control with that organization.</p>

                <div className="border-t border-surface-200/30 dark:border-[#142840] pt-3 mt-3">
                  <p className="font-semibold text-surface-700 dark:text-surface-300">Addendum: SaaS and Competing Service Definition</p>
                  <p className="mt-2">For the purposes of Section 2, the following constitute providing the software as a "hosted or managed service":</p>
                  <ul className="list-disc pl-4 mt-1 space-y-1">
                    <li>Offering a hosted platform, application, or API where end users interact with the features or functionality of the software.</li>
                    <li>Providing the software as a white-label, embedded, or rebranded offering to third parties.</li>
                    <li>Operating a multi-tenant service where the software's capabilities are a primary value driver.</li>
                  </ul>
                  <p className="mt-2">The following are expressly <strong className="text-green-600 dark:text-green-400">PERMITTED</strong>:</p>
                  <ul className="list-disc pl-4 mt-1 space-y-1">
                    <li>Using the software internally within your organization, including for commercial purposes.</li>
                    <li>Using the software to manage your own projects, teams, or products.</li>
                    <li>Integrating the software's MCP tools with AI agents for your own or your organization's use.</li>
                    <li>Modifying the software for personal or internal organizational use.</li>
                  </ul>
                  <p className="mt-2 text-surface-400">Contact <a href="mailto:dev@oktolabs.ai" className="text-accent-500 hover:underline">dev@oktolabs.ai</a> for clarification.</p>
                </div>

                <p className="text-surface-400 dark:text-surface-500 pt-2 border-t border-surface-200/30 dark:border-[#142840]">
                  Source code at{' '}
                  <a
                    href="https://github.com/OktoLabsAI/okto-pulse"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-accent-500 hover:underline"
                  >
                    github.com/OktoLabsAI/okto-pulse
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
