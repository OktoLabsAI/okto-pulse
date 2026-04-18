/**
 * Agents Modal - Global agent management + board access control
 */

import React, { useState, useEffect } from 'react';
import { X, Plus, Copy, RefreshCw, Trash2, Key, FileJson, Terminal, Shield, ChevronRight } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import { PermissionFlagsEditor, PermissionDiffView } from '@/components/permissions';
import type { FlagsMap } from '@/components/permissions';
import type { Agent, AgentSummary, PermissionPreset } from '@/types';

type McpFormat = 'claude' | 'cursor' | 'vscode' | 'windsurf' | 'claude-cli' | 'okto-cli';
type Tab = 'my-agents' | 'board-access';

const MCP_CONSUMERS: { format: McpFormat; label: string; file: string; icon: 'json' | 'terminal' }[] = [
  { format: 'claude', label: 'Claude Desktop / Claude Code', file: 'claude_desktop_config.json', icon: 'json' },
  { format: 'claude-cli', label: 'Claude Code (CLI)', file: 'terminal', icon: 'terminal' },
  { format: 'okto-cli', label: 'Okto CLI (/mcp add)', file: 'terminal', icon: 'terminal' },
  { format: 'cursor', label: 'Cursor', file: '.cursor/mcp.json', icon: 'json' },
  { format: 'vscode', label: 'VS Code (Copilot)', file: '.vscode/mcp.json', icon: 'json' },
  { format: 'windsurf', label: 'Windsurf / Cline', file: 'cline_mcp_settings.json', icon: 'json' },
];

// Get MCP base URL from runtime configuration (injected by server)
function getMcpBaseUrl(): string {
  if (typeof window !== 'undefined' && (window as any).OKTO_PULSE_CONFIG?.MCP_URL) {
    return (window as any).OKTO_PULSE_CONFIG.MCP_URL;
  }
  // Fallback to build-time env var or default
  const mcpPort = import.meta.env.VITE_MCP_PORT || '8101';
  return `http://127.0.0.1:${mcpPort}`;
}

function getMcpConfigJson(format: McpFormat, apiKey: string): string {
  const mcpUrl = getMcpBaseUrl();
  const url = `${mcpUrl}/mcp?api_key=${apiKey}`;

  if (format === 'claude-cli') {
    return `claude mcp add -t http okto-pulse "${url}"`;
  }

  if (format === 'okto-cli') {
    return `/mcp add '${JSON.stringify({ name: 'okto-pulse', type: 'http', server_config: { type: 'http', url } })}'`;
  }

  if (format === 'vscode') {
    return JSON.stringify({ servers: { 'okto-pulse': { type: 'http', url } } }, null, 2);
  }

  return JSON.stringify({ mcpServers: { 'okto-pulse': { url } } }, null, 2);
}

interface AgentsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function AgentsModal({ isOpen, onClose }: AgentsModalProps) {
  const api = useDashboardApi();
  const currentBoard = useCurrentBoard();

  const [activeTab, setActiveTab] = useState<Tab>('my-agents');
  const [myAgents, setMyAgents] = useState<Agent[]>([]);
  const [boardAgents, setBoardAgents] = useState<AgentSummary[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newAgentName, setNewAgentName] = useState('');
  const [newAgentDescription, setNewAgentDescription] = useState('');
  const [newAgentObjective, setNewAgentObjective] = useState('');
  const [newAgentPresetId, setNewAgentPresetId] = useState('');
  const [expandedAgentId, setExpandedAgentId] = useState<string | null>(null);
  const [grantAgentId, setGrantAgentId] = useState('');
  const [presets, setPresets] = useState<PermissionPreset[]>([]);

  // Load my agents and presets on open
  useEffect(() => {
    if (isOpen) {
      loadMyAgents();
      loadPresets();
    }
  }, [isOpen]);

  const loadPresets = async () => {
    try {
      const data = await api.listPresets();
      setPresets(data);
    } catch { /* ignore */ }
  };

  // Load board agents when switching to board-access tab
  useEffect(() => {
    if (isOpen && activeTab === 'board-access' && currentBoard) {
      loadBoardAgents();
    }
  }, [isOpen, activeTab, currentBoard]);

  const loadMyAgents = async () => {
    setIsLoading(true);
    try {
      const agents = await api.listMyAgents();
      setMyAgents(agents);
    } catch {
      toast.error('Failed to load agents');
    } finally {
      setIsLoading(false);
    }
  };

  const loadBoardAgents = async () => {
    if (!currentBoard) return;
    setIsLoading(true);
    try {
      const agents = await api.listAgentsForBoard(currentBoard.id);
      setBoardAgents(agents);
    } catch {
      toast.error('Failed to load board agents');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newAgentName.trim()) return;

    try {
      const agent = await api.createAgent({
        name: newAgentName.trim(),
        description: newAgentDescription.trim() || undefined,
        objective: newAgentObjective.trim() || undefined,
        preset_id: newAgentPresetId || undefined,
      });
      setMyAgents((prev) => [...prev, agent]);
      setExpandedAgentId(agent.id);
      setNewAgentName('');
      setNewAgentDescription('');
      setNewAgentObjective('');
      setNewAgentPresetId('');
      setShowCreateForm(false);
      toast.success('Agent created!');
    } catch {
      toast.error('Failed to create agent');
    }
  };

  const handleCopy = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    toast.success(`${label} copied!`);
  };

  const handleRegenerateKey = async (agentId: string) => {
    if (!confirm('Are you sure? The old key will stop working.')) return;
    try {
      const result = await api.regenerateAgentKey(agentId);
      setMyAgents((prev) => prev.map((a) => (a.id === agentId ? { ...a, api_key: result.api_key } : a)));
      toast.success('Key regenerated!');
    } catch {
      toast.error('Failed to regenerate key');
    }
  };

  const handleDeleteAgent = async (agentId: string) => {
    if (!confirm('Are you sure you want to delete this agent?')) return;
    try {
      await api.deleteAgent(agentId);
      setMyAgents((prev) => prev.filter((a) => a.id !== agentId));
      toast.success('Agent deleted');
    } catch {
      toast.error('Failed to delete agent');
    }
  };

  const handleGrantAccess = async () => {
    if (!currentBoard || !grantAgentId) return;
    try {
      await api.grantAgentBoardAccess(grantAgentId, currentBoard.id);
      toast.success('Access granted!');
      setGrantAgentId('');
      loadBoardAgents();
    } catch (err: any) {
      const msg = err?.message?.includes('409') ? 'Access already granted' : 'Failed to grant access';
      toast.error(msg);
    }
  };

  const handleRevokeAccess = async (agentId: string) => {
    if (!currentBoard) return;
    if (!confirm('Revoke this agent access to the board?')) return;
    try {
      await api.revokeAgentBoardAccess(agentId, currentBoard.id);
      toast.success('Access revoked');
      loadBoardAgents();
    } catch {
      toast.error('Failed to revoke access');
    }
  };

  if (!isOpen) return null;

  // Agents that have no access to the current board (for grant select)
  const boardAgentIds = new Set(boardAgents.map((a) => a.id));
  const agentsWithoutAccess = myAgents.filter((a) => !boardAgentIds.has(a.id));

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content max-w-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="font-semibold text-lg flex items-center gap-2">
            <Key size={20} />
            Agents
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-200 dark:hover:bg-gray-700 rounded">
            <X size={20} />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-200 dark:border-gray-700">
          <button
            onClick={() => setActiveTab('my-agents')}
            className={`flex-1 py-2 px-4 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'my-agents'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            <Key size={14} className="inline mr-1.5 -mt-0.5" />
            My Agents
          </button>
          {currentBoard && (
            <button
              onClick={() => setActiveTab('board-access')}
              className={`flex-1 py-2 px-4 text-sm font-medium border-b-2 transition-colors ${
                activeTab === 'board-access'
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
              }`}
            >
              <Shield size={14} className="inline mr-1.5 -mt-0.5" />
              Board Access
            </button>
          )}
        </div>

        <div className="modal-body space-y-4">
          {/* ================ TAB 1: MY AGENTS ================ */}
          {activeTab === 'my-agents' && (
            <>
              {/* Create agent form */}
              {showCreateForm ? (
                <form onSubmit={handleCreateAgent} className="space-y-3 p-4 bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded-lg">
                  <div>
                    <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">Agent Name *</label>
                    <input
                      type="text"
                      value={newAgentName}
                      onChange={(e) => setNewAgentName(e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 text-gray-700 dark:text-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                      placeholder="Ex: Claude Assistant"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">Description</label>
                    <input
                      type="text"
                      value={newAgentDescription}
                      onChange={(e) => setNewAgentDescription(e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 text-gray-700 dark:text-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                      placeholder="E.g.: Task automation agent"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">Agent Objective</label>
                    <textarea
                      value={newAgentObjective}
                      onChange={(e) => setNewAgentObjective(e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 text-gray-700 dark:text-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                      rows={2}
                      placeholder="E.g.: Review PRs and create feedback cards"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">Permission Preset</label>
                    <select
                      value={newAgentPresetId}
                      onChange={(e) => setNewAgentPresetId(e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 text-gray-700 dark:text-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                    >
                      <option value="">Full Control (default)</option>
                      {presets.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}{p.is_builtin ? ' (built-in)' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="flex gap-2">
                    <button type="submit" className="btn btn-primary">
                      Create Agent
                    </button>
                    <button type="button" onClick={() => setShowCreateForm(false)} className="btn btn-secondary">
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <button
                  onClick={() => setShowCreateForm(true)}
                  className="w-full py-2 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg text-gray-500 dark:text-gray-400 hover:border-blue-500 hover:text-blue-500 flex items-center justify-center gap-2"
                >
                  <Plus size={16} />
                  New Agent
                </button>
              )}

              {/* Agents list */}
              {isLoading ? (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">Loading...</div>
              ) : myAgents.length === 0 ? (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                  No agents registered
                </div>
              ) : (
                <div className="space-y-2">
                  {myAgents.map((agent) => {
                    const isExpanded = expandedAgentId === agent.id;
                    return (
                      <div key={agent.id} className="bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded-lg overflow-hidden">
                        {/* Agent header */}
                        <div className="flex items-center justify-between p-3">
                          <button
                            onClick={() => setExpandedAgentId(isExpanded ? null : agent.id)}
                            className="flex-1 min-w-0 text-left flex items-center gap-2"
                          >
                            <ChevronRight size={14} className={`shrink-0 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                            <div className="min-w-0">
                              <h4 className="font-medium text-gray-900 dark:text-gray-100 truncate">{agent.name}</h4>
                              {agent.description && (
                                <p className="text-sm text-gray-500 dark:text-gray-400 truncate">{agent.description}</p>
                              )}
                            </div>
                          </button>
                          <div className="flex items-center gap-1 shrink-0">
                            <button
                              onClick={() => handleCopy(agent.api_key, 'Key')}
                              className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20 rounded"
                              title="Copy API key"
                            >
                              <Copy size={14} />
                            </button>
                            <button
                              onClick={() => handleRegenerateKey(agent.id)}
                              className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded"
                              title="Regenerate key"
                            >
                              <RefreshCw size={14} />
                            </button>
                            <button
                              onClick={() => handleDeleteAgent(agent.id)}
                              className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                              title="Delete agent"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </div>

                        {/* Expanded details */}
                        {isExpanded && (
                          <div className="px-3 pb-3 pt-0 space-y-3 border-t border-gray-200 dark:border-gray-700">
                            {/* API Key */}
                            <div className="mt-3">
                              <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">API Key</label>
                              <div className="flex items-center gap-2 mt-1">
                                <code className="flex-1 bg-white dark:bg-gray-900 px-3 py-1.5 rounded text-xs font-mono break-all border border-gray-200 dark:border-gray-700">
                                  {agent.api_key}
                                </code>
                                <button
                                  onClick={() => handleCopy(agent.api_key, 'Key')}
                                  className="p-1.5 bg-green-600 text-white rounded hover:bg-green-700 shrink-0"
                                >
                                  <Copy size={12} />
                                </button>
                              </div>
                            </div>

                            {agent.objective && (
                              <div>
                                <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">Objective</label>
                                <p className="text-sm text-blue-500 dark:text-blue-400 mt-0.5">{agent.objective}</p>
                              </div>
                            )}

                            <p className="text-xs text-gray-400 dark:text-gray-500">
                              ID: {agent.id} • {agent.is_active ? 'Active' : 'Inactive'}
                            </p>

                            {/* Permissions */}
                            <div>
                              <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">Permissions</label>
                              <div className="mt-1 space-y-2">
                                <div className="flex items-center gap-2">
                                  <select
                                    value={agent.preset_id || ''}
                                    onChange={async (e) => {
                                      try {
                                        // Send explicit null (not undefined) so JSON.stringify keeps the
                                        // field — the backend uses its presence to reset permission_flags
                                        // from the chosen preset (or to full registry when null).
                                        await api.updateAgent(agent.id, { preset_id: e.target.value || null } as any);
                                        await loadMyAgents();
                                        toast.success('Preset updated');
                                      } catch { toast.error('Failed to update preset'); }
                                    }}
                                    className="flex-1 px-2 py-1.5 border border-gray-300 text-gray-700 dark:text-gray-300 rounded text-xs dark:bg-gray-700 dark:border-gray-600"
                                  >
                                    <option value="">Full Control</option>
                                    {presets.map((p) => (
                                      <option key={p.id} value={p.id}>
                                        {p.name}{p.is_builtin ? ' (built-in)' : ''}
                                      </option>
                                    ))}
                                  </select>
                                </div>
                                {/* Permission flags editor */}
                                {agent.permission_flags && (
                                  <PermissionFlagsEditor
                                    flags={agent.permission_flags as FlagsMap}
                                    onChange={async (newFlags) => {
                                      try {
                                        await api.updateAgent(agent.id, { permission_flags: newFlags } as any);
                                        await loadMyAgents();
                                        toast.success('Permissions updated');
                                      } catch { toast.error('Failed to update'); }
                                    }}
                                  />
                                )}
                              </div>
                            </div>

                            {/* MCP Config buttons */}
                            <div>
                              <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">MCP Configuration</label>
                              <div className="grid grid-cols-2 lg:grid-cols-3 gap-2 mt-1">
                                {MCP_CONSUMERS.map(({ format, label, file, icon }) => (
                                  <button
                                    key={format}
                                    onClick={() => {
                                      navigator.clipboard.writeText(getMcpConfigJson(format, agent.api_key));
                                      toast.success('Configuration copied!');
                                    }}
                                    className="flex items-center gap-2 px-3 py-2 text-xs bg-white dark:bg-gray-900 rounded border border-gray-200 dark:border-gray-700 hover:bg-blue-50 dark:hover:bg-blue-900/20 text-left text-gray-700 dark:text-gray-300"
                                    title={icon === 'terminal' ? 'Terminal command' : file}
                                  >
                                    {icon === 'terminal' ? (
                                      <Terminal size={14} className="shrink-0 text-gray-500 dark:text-gray-400" />
                                    ) : (
                                      <FileJson size={14} className="shrink-0 text-gray-500 dark:text-gray-400" />
                                    )}
                                    <span className="truncate">{label}</span>
                                  </button>
                                ))}
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* MCP Server info */}
              <div className="border-t pt-4">
                <p className="text-xs text-gray-400 dark:text-gray-500">
                  MCP Server: <code>{getMcpBaseUrl()}/mcp</code>
                </p>
              </div>
            </>
          )}

          {/* ================ TAB 2: BOARD ACCESS ================ */}
          {activeTab === 'board-access' && currentBoard && (
            <>
              {/* Board ID copiable */}
              <div className="flex items-center gap-2 p-3 bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded-lg">
                <span className="text-sm font-medium text-gray-600 dark:text-gray-300">Board ID:</span>
                <code
                  className="flex-1 bg-white dark:bg-gray-900 px-3 py-1 rounded text-xs font-mono cursor-pointer hover:bg-blue-50 dark:hover:bg-blue-900/20 border border-gray-200 dark:border-gray-700"
                  onClick={() => handleCopy(currentBoard.id, 'board_id')}
                >
                  {currentBoard.id}
                </code>
                <button
                  onClick={() => handleCopy(currentBoard.id, 'board_id')}
                  className="p-1.5 text-gray-400 dark:text-gray-500 hover:text-blue-600 rounded"
                >
                  <Copy size={14} />
                </button>
              </div>

              {/* Grant access */}
              <div className="flex gap-2">
                <select
                  value={grantAgentId}
                  onChange={(e) => setGrantAgentId(e.target.value)}
                  className="flex-1 px-3 py-2 border border-gray-300 text-gray-700 dark:text-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600 text-sm"
                >
                  <option value="">Select an agent...</option>
                  {agentsWithoutAccess.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
                <button
                  onClick={handleGrantAccess}
                  disabled={!grantAgentId}
                  className="btn btn-primary whitespace-nowrap disabled:opacity-50"
                >
                  <Plus size={14} className="inline mr-1 -mt-0.5" />
                  Grant Access
                </button>
              </div>

              {/* Board agents list */}
              {isLoading ? (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">Loading...</div>
              ) : boardAgents.length === 0 ? (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                  No agents with access to this board
                </div>
              ) : (
                <div className="space-y-2">
                  {boardAgents.map((agent) => {
                    const isBoardExpanded = expandedAgentId === `board-${agent.id}`;
                    return (
                      <div key={agent.id} className="bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300 rounded-lg overflow-hidden">
                        <div className="flex items-center justify-between p-3">
                          <button
                            onClick={() => setExpandedAgentId(isBoardExpanded ? null : `board-${agent.id}`)}
                            className="flex-1 min-w-0 text-left flex items-center gap-2"
                          >
                            <ChevronRight size={14} className={`shrink-0 transition-transform ${isBoardExpanded ? 'rotate-90' : ''}`} />
                            <div className="min-w-0">
                              <h4 className="font-medium text-gray-900 dark:text-gray-100">{agent.name}</h4>
                              {agent.description && (
                                <p className="text-sm text-gray-500 dark:text-gray-400 truncate">{agent.description}</p>
                              )}
                              <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                                {agent.is_active ? 'Active' : 'Inactive'}
                              </p>
                            </div>
                          </button>
                          <button
                            onClick={() => handleRevokeAccess(agent.id)}
                            className="px-3 py-1.5 text-xs text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded border border-red-200 dark:border-red-800 shrink-0"
                          >
                            Revoke
                          </button>
                        </div>
                        {isBoardExpanded && (() => {
                          const myAgent = myAgents.find((a) => a.id === agent.id);
                          const baseFlags = myAgent?.permission_flags as FlagsMap | undefined;
                          return (
                            <div className="px-3 pb-3 border-t border-gray-200 dark:border-gray-700 space-y-2">
                              <div className="mt-2">
                                <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                                  Permission Overrides (ceiling model)
                                </label>
                                <p className="text-[10px] text-gray-400 mt-0.5 mb-1.5">
                                  Restrict agent permissions on this board. Overrides can only remove permissions, never add.
                                </p>
                                <div className="flex items-center gap-2 mb-2">
                                  <select
                                    defaultValue=""
                                    onChange={async (e) => {
                                      if (!currentBoard) return;
                                      const presetId = e.target.value;
                                      if (!presetId) {
                                        try {
                                          await api.updateAgentBoardOverrides(agent.id, currentBoard.id, null);
                                          toast.success('Overrides cleared');
                                          loadBoardAgents();
                                        } catch { toast.error('Failed'); }
                                        return;
                                      }
                                      const preset = presets.find((p) => p.id === presetId);
                                      if (preset) {
                                        try {
                                          await api.updateAgentBoardOverrides(agent.id, currentBoard.id, preset.flags);
                                          toast.success(`Overrides set to ${preset.name}`);
                                          loadBoardAgents();
                                        } catch { toast.error('Failed'); }
                                      }
                                    }}
                                    className="flex-1 px-2 py-1.5 border border-gray-300 text-gray-700 dark:text-gray-300 rounded text-xs dark:bg-gray-700 dark:border-gray-600"
                                  >
                                    <option value="">No overrides (full agent permissions)</option>
                                    {presets.map((p) => (
                                      <option key={p.id} value={p.id}>
                                        Restrict to: {p.name}{p.is_builtin ? ' (built-in)' : ''}
                                      </option>
                                    ))}
                                  </select>
                                  <button
                                    onClick={async () => {
                                      if (!currentBoard) return;
                                      try {
                                        await api.updateAgentBoardOverrides(agent.id, currentBoard.id, null);
                                        toast.success('Overrides cleared');
                                        loadBoardAgents();
                                      } catch { toast.error('Failed'); }
                                    }}
                                    className="text-[10px] px-2 py-1 rounded bg-gray-200 text-gray-600 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-400 shrink-0"
                                  >
                                    Clear
                                  </button>
                                </div>
                                {/* Diff view when base flags available */}
                                {baseFlags && (
                                  <PermissionDiffView
                                    baseFlags={baseFlags}
                                    effectiveFlags={baseFlags}
                                  />
                                )}
                              </div>
                            </div>
                          );
                        })()}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
