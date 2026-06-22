/**
 * Header component
 */

import { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { authAdapter, portalAdapter } from '@/adapters';
import { Plus, Users, Share2, RefreshCw, PanelLeftClose, PanelLeftOpen, Moon, Sun, Settings, SlidersHorizontal, BookOpen, BarChart3, Menu, ChevronDown, HelpCircle, Info, X, Shield, Network, Activity, HeartPulse, Trash2, AlertTriangle, Palette } from 'lucide-react';
import { GuidelinesPanel } from '@/components/guidelines';
import { DefaultBoardConfigPanel } from '@/components/board/DefaultBoardConfigPanel';
import { DesignSystemPanel } from '@/components/board/DesignSystemPanel';
import { BoardSettingsForm, normalizeDesignSystemGateMode } from '@/components/board/BoardSettingsForm';
import { HelpPanel } from '@/components/help';
import { PresetListModal } from '@/components/permissions';
import { KnowledgeGraphPage } from '@/components/knowledge';
import { RuntimeSettingsPanel } from '@/components/layout/RuntimeSettingsPanel';
import { MetricsSettingsPanel } from '@/components/layout/MetricsSettingsPanel';
import { MetricsHealthPanel } from '@/components/layout/MetricsHealthPanel';
import { useCurrentBoard } from '@/store/dashboard';
import pulseWordmark from '@/assets/pulse-wordmark.svg';
import pulseWordmarkLight from '@/assets/pulse-wordmark-light.svg';
import pulseIcon from '@/assets/pulse-icon.svg';
import oktolabsIcon from '@/assets/oktolabs-icon.svg';
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
  onOpenKGHealth?: () => void;
  helpOpen?: boolean;
  onHelpOpenChange?: (open: boolean) => void;
  knowledgeGraphOpen?: boolean;
  onKnowledgeGraphOpenChange?: (open: boolean) => void;
}

export function Header({ onCreateBoard, onOpenAgents, onShareBoard, onRefreshBoard, onDeleteBoard, isRefreshing, sidebarOpen, onToggleSidebar, onBoardUpdated, onOpenAnalytics, onOpenKGHealth, helpOpen, onHelpOpenChange, knowledgeGraphOpen, onKnowledgeGraphOpenChange }: HeaderProps) {
  const { isSignedIn, isLoaded } = authAdapter.useAuth();
  const AdapterUserButton = authAdapter.UserButton;
  const currentBoard = useCurrentBoard();
  const { theme, toggle: toggleTheme } = useTheme();
  const api = useDashboardApi();
  const [showSettings, setShowSettings] = useState(false);
  const [boardSettingsTab, setBoardSettingsTab] = useState<'board' | 'global'>('board');
  const [showRuntimeSettings, setShowRuntimeSettings] = useState(false);
  const [runtimeSettingsInitialTab, setRuntimeSettingsInitialTab] =
    useState<'graphdb' | 'eventqueue' | 'decaytick'>('graphdb');
  const [showMetricsSettings, setShowMetricsSettings] = useState(false);
  const [showMetricsHealth, setShowMetricsHealth] = useState(false);
  const [showGuidelines, setShowGuidelines] = useState(false);
  const [showDesignSystem, setShowDesignSystem] = useState(false);
  const [showMenu, setShowMenu] = useState(false);
  const [localShowHelp, setLocalShowHelp] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const [showPresets, setShowPresets] = useState(false);
  const [localShowKnowledgeGraph, setLocalShowKnowledgeGraph] = useState(false);
  const [boardGuidelineCount, setBoardGuidelineCount] = useState<number | null>(null);
  const [boardGuidelinesLoadFailed, setBoardGuidelinesLoadFailed] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const showHelp = helpOpen ?? localShowHelp;
  const setShowHelp = (open: boolean) => {
    if (helpOpen === undefined) {
      setLocalShowHelp(open);
    }
    onHelpOpenChange?.(open);
  };
  const showKnowledgeGraph = knowledgeGraphOpen ?? localShowKnowledgeGraph;
  const setShowKnowledgeGraph = (open: boolean) => {
    if (knowledgeGraphOpen === undefined) {
      setLocalShowKnowledgeGraph(open);
    }
    onKnowledgeGraphOpenChange?.(open);
  };

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

  // NC-9 Wave 2 frontend: open the Board panel (not the RuntimeSettingsPanel)
  // when the EvidenceGateSkipBanner link is clicked. The banner lives in
  // App.tsx and dispatches this event globally. The skip_test_evidence_global
  // toggle now lives inside the Board panel alongside the other skip toggles.
  useEffect(() => {
    const handler = () => {
      setBoardSettingsTab('board');
      setShowSettings(true);
    };
    window.addEventListener('okto:open-board-settings', handler);
    return () => window.removeEventListener('okto:open-board-settings', handler);
  }, []);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ initialTab?: 'graphdb' | 'eventqueue' | 'decaytick' }>).detail;
      setRuntimeSettingsInitialTab(detail?.initialTab ?? 'graphdb');
      setShowRuntimeSettings(true);
    };
    window.addEventListener('okto:open-runtime-settings', handler);
    return () => window.removeEventListener('okto:open-runtime-settings', handler);
  }, []);

  useEffect(() => {
    if (!showSettings || !currentBoard?.id) {
      setBoardGuidelineCount(null);
      setBoardGuidelinesLoadFailed(false);
      return;
    }

    let cancelled = false;
    setBoardGuidelineCount(null);
    setBoardGuidelinesLoadFailed(false);
    api.getBoardGuidelines(currentBoard.id)
      .then((entries) => {
        if (!cancelled) setBoardGuidelineCount(entries.length);
      })
      .catch(() => {
        if (!cancelled) setBoardGuidelinesLoadFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [showSettings, currentBoard?.id]);

  const settings: BoardSettings = currentBoard?.settings
    ? {
        max_scenarios_per_card: currentBoard.settings.max_scenarios_per_card ?? 3,
        skip_test_coverage_global: currentBoard.settings.skip_test_coverage_global ?? false,
        skip_rules_coverage_global: currentBoard.settings.skip_rules_coverage_global ?? false,
        skip_trs_coverage_global: currentBoard.settings.skip_trs_coverage_global ?? false,
        skip_contract_coverage_global: currentBoard.settings.skip_contract_coverage_global ?? false,
        skip_ir_coverage_global: currentBoard.settings.skip_ir_coverage_global ?? false,
        skip_or_coverage_global: currentBoard.settings.skip_or_coverage_global ?? false,
        skip_decisions_coverage_global: currentBoard.settings.skip_decisions_coverage_global ?? false,
        skip_cognitive_consolidation: currentBoard.settings.skip_cognitive_consolidation ?? false,
        allow_agent_self_answering: currentBoard.settings.allow_agent_self_answering ?? false,
        require_full_context_for_critical_actions: currentBoard.settings.require_full_context_for_critical_actions ?? true,
        qa_require_role_separation: currentBoard.settings.qa_require_role_separation ?? false,
        skip_test_evidence_global: currentBoard.settings.skip_test_evidence_global ?? false,
        require_task_validation: currentBoard.settings.require_task_validation ?? true,
        min_confidence: currentBoard.settings.min_confidence ?? 70,
        min_completeness: currentBoard.settings.min_completeness ?? 80,
        max_drift: currentBoard.settings.max_drift ?? 50,
        require_spec_validation: currentBoard.settings.require_spec_validation ?? true,
        min_spec_completeness: currentBoard.settings.min_spec_completeness ?? 80,
        min_spec_assertiveness: currentBoard.settings.min_spec_assertiveness ?? 80,
        max_spec_ambiguity: currentBoard.settings.max_spec_ambiguity ?? 30,
        require_ideation_ambiguity_gate: currentBoard.settings.require_ideation_ambiguity_gate ?? false,
        max_ideation_ambiguity: currentBoard.settings.max_ideation_ambiguity ?? 3,
        require_spec_resource_task_coverage: currentBoard.settings.require_spec_resource_task_coverage ?? true,
        auto_derive_spec_resources_enabled: currentBoard.settings.auto_derive_spec_resources_enabled ?? false,
        auto_derive_spec_resource_types: currentBoard.settings.auto_derive_spec_resource_types ?? [],
        design_system_gate_mode: normalizeDesignSystemGateMode(currentBoard.settings.design_system_gate_mode),
      }
    : {
        max_scenarios_per_card: 3,
        skip_test_coverage_global: false,
        skip_rules_coverage_global: false,
        skip_trs_coverage_global: false,
        skip_contract_coverage_global: false,
        skip_ir_coverage_global: false,
        skip_or_coverage_global: false,
        skip_decisions_coverage_global: false,
        skip_cognitive_consolidation: false,
        allow_agent_self_answering: false,
        require_full_context_for_critical_actions: true,
        qa_require_role_separation: false,
        skip_test_evidence_global: false,
        require_task_validation: true,
        min_confidence: 70,
        min_completeness: 80,
        max_drift: 50,
        require_spec_validation: true,
        min_spec_completeness: 80,
        min_spec_assertiveness: 80,
        max_spec_ambiguity: 30,
        require_ideation_ambiguity_gate: false,
        max_ideation_ambiguity: 3,
        require_spec_resource_task_coverage: true,
        auto_derive_spec_resources_enabled: false,
        auto_derive_spec_resource_types: [],
        design_system_gate_mode: 'off',
      };

  const updateSettings = async (patch: Partial<BoardSettings>) => {
    if (!currentBoard) return;
    const newSettings = { ...settings, ...patch };
    try {
      await api.updateBoard(currentBoard.id, { settings: newSettings });
      onBoardUpdated?.();
      toast.success('Board settings updated');
      // Bug fix (banner inversion): notify the global EvidenceGateSkipBanner
      // listener AFTER the PATCH commits so its refetch reads the new value.
      // Doing it from the toggle's onClick (before await) caused the App.tsx
      // listener to read the *previous* value from the backend, which made
      // the banner always reflect the state right before the click — i.e.
      // marking made the banner stay hidden, unmarking made it appear.
      window.dispatchEvent(new CustomEvent('okto:board-settings-changed'));
    } catch {
      toast.error('Failed to update settings');
    }
  };

  const missingBoardContextWarnings = [
    ...(!currentBoard?.description?.trim()
      ? ['Board description is empty. Agents have less product context for critical work.']
      : []),
    ...(boardGuidelineCount === 0
      ? ['Board guidelines are empty. Agents will rely only on global process rules.']
      : []),
  ];

  // Board-config-only context warnings, rendered inside the shared form's
  // Agent Governance section. The Global Default template editor renders the
  // same form WITHOUT these (a template has no board to warn about).
  const boardContextWarnings =
    missingBoardContextWarnings.length > 0 || boardGuidelinesLoadFailed ? (
      <div
        className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
        data-testid="board-context-warning"
      >
        <div className="mb-1 flex items-center gap-1.5 font-medium">
          <AlertTriangle size={12} />
          Context warnings
        </div>
        <ul className="list-disc space-y-1 pl-4">
          {missingBoardContextWarnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
          {boardGuidelinesLoadFailed && (
            <li>Board guidelines could not be checked right now. You can still save settings.</li>
          )}
        </ul>
      </div>
    ) : null;

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
          <div className="flex items-center">
            <img src={pulseWordmarkLight} alt="Okto Pulse" className="h-7 w-auto dark:hidden" />
            <img src={pulseWordmark} alt="Okto Pulse" className="h-7 w-auto hidden dark:block" />
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
                data-tour-id="board.refresh"
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

                    {/* Design System */}
                    <button
                      onClick={() => { setShowMenu(false); setShowDesignSystem(true); }}
                      data-testid="menu-design-system"
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Palette size={14} />
                      Design System
                    </button>

                    {/* Knowledge Graph */}
                    <button
                      onClick={() => { setShowMenu(false); setShowKnowledgeGraph(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Network size={14} />
                      Knowledge Graph
                    </button>

                    {/* Analytics */}
                    <button
                      onClick={() => { setShowMenu(false); onOpenAnalytics?.(); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <BarChart3 size={14} />
                      Analytics
                    </button>

                    {/* KG Health (spec d754d004) */}
                    {onOpenKGHealth && (
                      <button
                        onClick={() => { setShowMenu(false); onOpenKGHealth(); }}
                        className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                        data-testid="menu-kg-health"
                      >
                        <Activity size={14} />
                        KG Health
                      </button>
                    )}

                    {/* Agents */}
                    <button
                      onClick={() => { setShowMenu(false); onOpenAgents?.(); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                      data-tour-id="agents.modal.entry"
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

                    {/* Board (was "Settings" in ≤0.1.3 — renamed in 0.1.4 to
                        free the "Settings" label for runtime config) */}
                    <button
                      onClick={() => { setShowMenu(false); setBoardSettingsTab('board'); setShowSettings(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                    >
                      <Settings size={14} />
                      Board
                    </button>

                    {/* Settings (new in 0.1.4 — runtime graph database tuning) */}
                    <button
                      onClick={() => {
                        setShowMenu(false);
                        setRuntimeSettingsInitialTab('graphdb');
                        setShowRuntimeSettings(true);
                      }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                      data-testid="menu-settings"
                    >
                      <SlidersHorizontal size={14} />
                      Settings
                    </button>

                    <button
                      onClick={() => { setShowMenu(false); setShowMetricsSettings(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                      data-testid="menu-metrics"
                    >
                      <Activity size={14} />
                      Metrics
                    </button>

                    <button
                      onClick={() => { setShowMenu(false); setShowMetricsHealth(true); }}
                      className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-2"
                      data-testid="menu-metrics-health"
                    >
                      <HeartPulse size={14} />
                      Metrics Health
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
                      data-tour-id="help.guided_tours"
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

              {/* Board panel (opens from menu — renamed from "Settings" in 0.1.4) */}
              {showSettings && createPortal((
                <div className="modal-overlay p-4" onClick={() => setShowSettings(false)}>
                  <div
                    ref={settingsRef}
                    className="modal-content max-w-4xl !h-auto max-h-[90vh]"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="board-settings-title"
                    data-testid="board-settings-modal"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <div className="modal-header">
                      <div>
                        <h2 id="board-settings-title" className="text-base font-semibold text-gray-900 dark:text-white">
                          Board
                        </h2>
                        <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                          Validation gates, coverage checks and Spec resource automation
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={() => setShowSettings(false)}
                        className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-white/10 dark:hover:text-gray-300"
                        aria-label="Close board settings"
                      >
                        <X size={18} />
                      </button>
                    </div>

                    <div className="modal-body bg-gray-50/70 dark:bg-gray-950/30">
                      <div
                        className="mb-4 inline-flex rounded-lg border border-gray-200 bg-white p-1 dark:border-gray-800 dark:bg-gray-900"
                        role="tablist"
                        aria-label="Board settings sections"
                      >
                        <button
                          type="button"
                          role="tab"
                          aria-selected={boardSettingsTab === 'board'}
                          data-testid="board-settings-tab-board-config"
                          onClick={() => setBoardSettingsTab('board')}
                          className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                            boardSettingsTab === 'board'
                              ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
                              : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                          }`}
                        >
                          Board Config
                        </button>
                        <button
                          type="button"
                          role="tab"
                          aria-selected={boardSettingsTab === 'global'}
                          data-testid="board-settings-tab-global-default"
                          onClick={() => setBoardSettingsTab('global')}
                          className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                            boardSettingsTab === 'global'
                              ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
                              : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                          }`}
                        >
                          Global Default
                        </button>
                      </div>

                      {boardSettingsTab === 'board' && (
                        <BoardSettingsForm
                          settings={settings}
                          onChange={updateSettings}
                          contextWarnings={boardContextWarnings}
                        />
                      )}

                      {boardSettingsTab === 'global' && currentBoard?.id && (
                        <section
                          className="rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900/40"
                          data-testid="settings-default-board-config"
                        >
                          <DefaultBoardConfigPanel boardId={currentBoard.id} />
                        </section>
                      )}
                    </div>

                    <div className="modal-footer !justify-between">
                      <button
                        type="button"
                        onClick={onDeleteBoard}
                        className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 hover:text-red-700 dark:text-red-400 dark:hover:bg-red-950/30"
                      >
                        <Trash2 size={13} />
                        Delete board
                      </button>
                      <button
                        type="button"
                        onClick={() => setShowSettings(false)}
                        className="btn btn-secondary text-xs"
                      >
                        Close
                      </button>
                    </div>
                  </div>
                </div>
              ), document.body)}
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

      {showDesignSystem && currentBoard && (
        <DesignSystemPanel
          boardId={currentBoard.id}
          onClose={() => setShowDesignSystem(false)}
        />
      )}

      {showHelp && (
        <HelpPanel onClose={() => setShowHelp(false)} />
      )}

      {showRuntimeSettings && (
        <RuntimeSettingsPanel
          key={runtimeSettingsInitialTab}
          initialTab={runtimeSettingsInitialTab}
          onClose={() => setShowRuntimeSettings(false)}
        />
      )}

      {showMetricsSettings && (
        <MetricsSettingsPanel
          onClose={() => setShowMetricsSettings(false)}
        />
      )}

      {showMetricsHealth && (
        <MetricsHealthPanel
          onClose={() => setShowMetricsHealth(false)}
        />
      )}

      {showPresets && (
        <PresetListModal onClose={() => setShowPresets(false)} />
      )}

      {showKnowledgeGraph && currentBoard && (
        <div className="fixed inset-0 z-50 flex flex-col bg-white dark:bg-surface-950">
          <div className="flex items-center justify-between px-4 py-3 border-b border-surface-200 dark:border-gray-800">
            <div className="flex items-center gap-2">
              <Network size={18} className="text-blue-500" />
              <h2 className="text-lg font-semibold text-surface-900 dark:text-white">Knowledge Graph</h2>
              <span className="text-sm text-gray-500 dark:text-gray-400">/ {currentBoard.name}</span>
            </div>
            <button
              onClick={() => setShowKnowledgeGraph(false)}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg"
            >
              <X size={18} />
            </button>
          </div>
          <div className="flex-1 overflow-hidden">
            <KnowledgeGraphPage boardId={currentBoard.id} />
          </div>
        </div>
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
              <img src={pulseIcon} alt="Okto Pulse" className="w-[160px] h-[160px] object-contain" />
              <h2 className="text-xl font-bold text-surface-900 dark:text-white mt-4 font-display">
                Okto Pulse
              </h2>
              <p className="text-sm text-surface-500 dark:text-surface-400 mt-1">
                Community Edition — v0.2.5
              </p>
              <p className="text-[11px] text-surface-400 dark:text-surface-500 mt-0.5">
                Elastic License 2.0 + SaaS/Branding Addendum + Trademark Policy
              </p>
            </div>

            <div className="border-t border-surface-200/50 dark:border-[#142840] px-8 py-5 max-h-[60vh] overflow-y-auto">
              <h3 className="text-xs font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mb-3 font-display">
                License — Elastic License 2.0
              </h3>
              <div className="text-xs text-surface-600 dark:text-surface-400 leading-relaxed space-y-3">
                <p className="font-medium text-surface-700 dark:text-surface-300 flex items-center gap-2">
                  <img src={oktolabsIcon} alt="Okto Labs" className="h-4 w-4" />
                  Copyright 2026 Okto Labs
                </p>

                <p><strong>Acceptance.</strong> By using the software, you agree to all of the terms and conditions below.</p>

                <p><strong>1. Grant of License.</strong> The licensor grants you a non-exclusive, royalty-free, worldwide, non-sublicensable, non-transferable license to use, copy, distribute, make available, and prepare derivative works of the software, in each case subject to the limitations and conditions below.</p>

                <p><strong>2. Limitations.</strong> You may not provide the software to third parties as a hosted or managed service, where the service provides users with access to any substantial set of the features or functionality of the software. You may not move, change, disable, or circumvent the license key functionality in the software, and you may not remove or obscure any functionality in the software that is protected by the license key. You may not alter, remove, or obscure any licensing, copyright, or other notices of the licensor in the software.</p>

                <p><strong>3. Patent License.</strong> The licensor grants you a license, under any patent claims the licensor can license, or becomes able to license, to make, have made, use, sell, offer for sale, import and have imported the software. However, this license does not cover any patent claims that you cause to be infringed by modifications or additions to the software. If you or your company make any written claim that the software infringes or contributes to infringement of any patent, your patent license for the software granted under these terms ends immediately. Your company's patent license also ends immediately if your company makes any such written claim.</p>

                <p><strong>4. Distribution.</strong> You may not alter, remove, or obscure any licensing, copyright, or other notices of the licensor in the software. Any distribution of the software must include a copy of these terms and conditions, and anyone who receives the software from you is bound by these terms and conditions.</p>

                <p><strong>5. Notices.</strong> You must include a copy of these terms with any distribution of the software. If you modify the software, you must mark the modifications clearly and include the date of the modifications.</p>

                <p><strong>6. Termination.</strong> If you violate these terms, your licenses will terminate automatically. If the licensor notifies you of your violation, and you cease all violation of this license no later than 30 days after you receive that notice, your licenses will be reinstated retroactively. However, if you violate these terms after the reinstatement, all of your licenses will terminate permanently.</p>

                <p><strong>7. No Other Rights.</strong> Except as expressly stated herein, no other rights or licenses are granted, express or implied.</p>

                <p><strong>8. Limitation on Liability.</strong> As far as the law allows, the software comes as is, without any warranty or condition, and the licensor will not be liable to you for any damages arising out of these terms or the use or nature of the software, under any kind of legal claim.</p>

                <p><strong>9. Definitions.</strong> "Licensor" means Okto Labs and its affiliates. "Software" means the software the licensor makes available under these terms, including any portions, modifications, or derivative works. "You" means you, individually. "Your company" means any legal entity, sole proprietorship, or other organization that you work for, plus all other organizations that control, are controlled by, or are under common control with that organization. The term "control" means ownership of substantially all the assets of an entity.</p>
              </div>

              {/* Addendum — SaaS / Competing / Internal / Branding */}
              <h3 className="text-xs font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mt-5 mb-3 font-display">
                Addendum — SaaS, Competing Service, Internal Use, Branding
              </h3>
              <div className="text-xs text-surface-600 dark:text-surface-400 leading-relaxed space-y-3">
                <p className="text-[11px] italic text-surface-500 dark:text-surface-500">
                  This addendum clarifies and supplements Section 2 ("Limitations"). In case of conflict between the body of the license and this addendum, this addendum controls.
                </p>

                <p className="font-semibold text-red-700 dark:text-red-400">I. Hosted or Managed Service — PROHIBITED uses</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li><strong>(a)</strong> Operating a multi-tenant SaaS, platform, application, or API where end users from more than one client organization interact with the features or functionality of the software, whether directly or through a wrapper, proxy, or abstraction layer.</li>
                  <li><strong>(b)</strong> Providing the software as a white-label, embedded, OEM, or rebranded offering to third parties.</li>
                  <li><strong>(c)</strong> Offering the software, any derivative work of it, or any substantial portion of its features or functionality as a product, service, or platform that competes with Okto Pulse, regardless of whether the deployment is single-tenant or multi-tenant.</li>
                  <li><strong>(d) Internal large-scale platform exposure</strong> — operating the software as an internally hosted service that meets <strong>BOTH</strong> of the following conditions simultaneously:
                    <ul className="list-disc pl-4 mt-1 space-y-1">
                      <li><strong>(i)</strong> the software, or modules extracted/externalized/repackaged from it, are exposed as a hosted service, API, or internal platform to users within your organization who are not directly involved in administering, developing, configuring, or using the software for the projects, teams, or products it is intended to manage; <strong>AND</strong></li>
                      <li><strong>(ii)</strong> the total population of such exposed users exceeds <strong>two hundred (200)</strong> individuals.</li>
                    </ul>
                    <p className="mt-1 text-[11px] italic">Both conditions must be met. Local, desktop, or workstation use by any number of individuals is <strong>never</strong> restricted by this clause.</p>
                  </li>
                </ul>

                <p className="font-semibold text-green-700 dark:text-green-400 pt-1">II. PERMITTED uses (incl. commercial)</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li><strong>(a)</strong> Using the software internally within your organization, without any numeric or scale restriction, to manage your own projects, teams, products, or operations.</li>
                  <li><strong>(b)</strong> Hosting a single-tenant deployment for yourself or a single client organization. Each distinct client organization must receive its own dedicated instance.</li>
                  <li><strong>(c)</strong> Providing consulting, integration, customization, deployment, or managed-operations services using the software (including charging fees), provided that each client deployment is single-tenant, branding/attribution in Section III is honored, and it does not constitute a competing service or exceed the I(d) thresholds.</li>
                  <li><strong>(d)</strong> Integrating the software's MCP tools with AI agents for your own or your organization's use.</li>
                  <li><strong>(e)</strong> Modifying the software for personal or internal organizational use, including for commercial purposes, subject to Sections I and III.</li>
                </ul>

                <p className="font-semibold text-surface-700 dark:text-surface-300 pt-1">III. Branding and Attribution — REQUIRED preservation</p>
                <p>You may <strong>not</strong> alter, remove, obscure, hide, replace, minimize, or otherwise diminish the visibility of:</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li><strong>(a)</strong> The "Okto Labs" name and logo.</li>
                  <li><strong>(b)</strong> The "Okto Pulse" name and logo.</li>
                  <li><strong>(c)</strong> Copyright and licensing notices identifying Okto Labs as the licensor.</li>
                </ul>
                <p>These elements <strong>MUST</strong> remain visible in:</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li><strong>Web UI</strong>: login/auth screens, primary navigation/chrome, footer, settings/admin/operations consoles, any "About"/"Help"/"Powered by" surface.</li>
                  <li><strong>CLI</strong>: <code>--version</code> and equivalent output, help/usage screens, startup banner/splash/login output.</li>
                </ul>
                <p>You <strong>may</strong> add your own branding alongside the required Okto Labs and Okto Pulse marks, but you may <strong>not</strong> replace, substitute, or visually subordinate them in a way that misrepresents the origin of the software.</p>

                <p className="font-semibold text-surface-700 dark:text-surface-300 pt-1">IV. Clarifications</p>
                <p>If you are unsure whether your intended use constitutes a competing service under I(c), an internal large-scale platform exposure under I(d), or otherwise requires a commercial license, contact <a href="mailto:dev@oktolabs.ai" className="text-accent-500 hover:underline">dev@oktolabs.ai</a>.</p>
              </div>

              {/* Trademark Policy — synchronized with TRADEMARKS.md */}
              <h3 className="text-xs font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mt-5 mb-3 font-display">
                Trademark Policy
              </h3>
              <div className="text-xs text-surface-600 dark:text-surface-400 leading-relaxed space-y-3">
                <p className="text-[11px] italic text-surface-500 dark:text-surface-500">
                  Complements (and never overrides) the attribution obligations in Section III above. Governs the use of the marks as identifiers of <em>your own</em> product, service, company, or domain.
                </p>

                <p>The following are trademarks of <strong>Okto Labs</strong>:</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li><strong>Okto Labs</strong> — the company name and brand</li>
                  <li><strong>Okto Pulse</strong> — the product name</li>
                  <li><strong>Okto Labs logo</strong> — the octopus/circuit design mark (all variants)</li>
                  <li><strong>Okto Pulse logo</strong> — the product logo and all variants</li>
                </ul>

                <p className="font-semibold text-green-700 dark:text-green-400 pt-1">Permitted Use</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li>Preserve and display the Okto Labs / Okto Pulse names and logos as required by Section III (mandatory).</li>
                  <li>State that your project, fork, or derivative is "based on Okto Pulse", "powered by Okto Pulse", or "compatible with Okto Pulse".</li>
                  <li>Describe consulting/integration/managed-ops services using factual references (e.g. "We host Okto Pulse for our clients", "Managed Okto Pulse deployment operated by [Your Company]").</li>
                  <li>Use the Okto Pulse name in documentation, articles, comparisons, talks, academic work.</li>
                </ul>

                <p className="font-semibold text-red-700 dark:text-red-400 pt-1">Prohibited Use (without prior written authorization)</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li>Use "Okto Pulse", "Okto Labs", "Okto", or any of the logos as part of <strong>your own</strong> product, service, company, or domain name (e.g. <code>oktopulse.example.com</code>, "AcmePulse", "PulseHub", "Okto-Plus").</li>
                  <li>Create the impression that your product, service, company, or fork is endorsed, sponsored, certified, or affiliated with Okto Labs.</li>
                  <li>Use any of the logos (or a confusingly similar mark) as the <strong>primary brand</strong> of your product, marketing, app store listings, or social presence.</li>
                  <li>Offer a product/service under a name that combines "Okto" with "Pulse", "Labs", or similar terms.</li>
                  <li>Modify, distort, recolor, or recompose the logos in a way that misrepresents the marks.</li>
                </ul>

                <p className="font-semibold text-surface-700 dark:text-surface-300 pt-1">Derivative Works and Forks</p>
                <ul className="list-disc pl-4 space-y-1">
                  <li>You <strong>must add</strong> your own distinct name, logo, and brand identity — and that name <strong>must not</strong> include "Okto", "Pulse", or any confusingly similar term.</li>
                  <li>You <strong>must retain</strong> the Okto Labs and Okto Pulse names and logos in the web UI and CLI as attribution-of-origin (Section III). The required form is roughly: <em>"Powered by Okto Pulse — © Okto Labs"</em>, with the official logos.</li>
                  <li>You <strong>must retain</strong> the copyright and license notices required by the LICENSE.</li>
                  <li>You <strong>may</strong> describe your work factually as "based on Okto Pulse" or "a fork of Okto Pulse".</li>
                </ul>
                <p className="italic">In short: <strong>rename your fork, but keep the attribution.</strong></p>

                <p className="font-semibold text-surface-700 dark:text-surface-300 pt-1">Logo Usage</p>
                <p>Official assets under <code>frontend/src/assets/</code> are provided for two purposes only: (1) mandatory attribution use under Section III; (2) factual reference in documentation/articles/comparisons. Same assets <strong>may not</strong> be used as the primary brand of any third-party product, fork, marketing campaign, merchandise, or domain.</p>
                <p className="text-[11px]">Canonical asset paths: <code>logo-light.png</code>, <code>logo-dark.png</code>, <code>oktolabs-icon.svg</code>, <code>pulse-icon.svg</code>, <code>pulse-wordmark.svg</code>, <code>pulse-wordmark-light.svg</code>, <code>favicon.jpg</code>.</p>

                <p className="text-surface-400">Trademark requests: <a href="mailto:dev@oktolabs.ai" className="text-accent-500 hover:underline">dev@oktolabs.ai</a> · Full policy in <code>TRADEMARKS.md</code></p>
              </div>

              {/* Contributions — CLA */}
              <h3 className="text-xs font-semibold text-surface-500 dark:text-surface-400 uppercase tracking-wider mt-5 mb-3 font-display">
                Contributions — CLA
              </h3>
              <div className="text-xs text-surface-600 dark:text-surface-400 leading-relaxed space-y-2">
                <p>Contributions are governed by the <strong>Contributor License Agreement</strong> (<code>CLA.md</code>).</p>
                <p><strong>Grant of Rights.</strong> By submitting a pull request, you grant Okto Labs a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable license to use, reproduce, modify, display, perform, sublicense, and distribute your contribution as part of the project — and to relicense your contribution under any license, including proprietary licenses.</p>
                <p><strong>Ownership.</strong> You retain copyright ownership of your contribution. The CLA grants a license, not a transfer.</p>
                <p><strong>Representations.</strong> You represent that you are legally entitled to grant the above license, that the contribution is your original work (or you have the right to submit it), that it does not violate any third party's rights, and that you have employer authorization where applicable.</p>
                <p><strong>How to sign.</strong> Submitting a pull request to any Okto Pulse repository indicates your agreement with the CLA — no separate signature is required.</p>
              </div>

              <div className="mt-5">
                <p className="text-[11px] text-surface-400 dark:text-surface-500 pt-2 border-t border-surface-200/30 dark:border-[#142840]">
                  Source code at{' '}
                  <a
                    href="https://github.com/OktoLabsAI/okto-pulse"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-accent-500 hover:underline"
                  >
                    github.com/OktoLabsAI/okto-pulse
                  </a>
                  {' '} · Contact: <a href="mailto:dev@oktolabs.ai" className="text-accent-500 hover:underline">dev@oktolabs.ai</a>
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
