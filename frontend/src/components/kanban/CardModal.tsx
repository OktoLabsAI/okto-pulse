/**
 * CardModal - Modal for viewing/editing card details
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { X, Paperclip, HelpCircle, Trash2, Download, Clock, Link, Unlink, RefreshCw, FileText, FlaskConical, Maximize2, Minimize2, Bug, AlertCircle, Check, Scale, Shield, ChevronDown, ChevronUp, CheckCircle, XCircle } from 'lucide-react';
import toast from 'react-hot-toast';
import { exportCard, downloadMarkdown, slugify } from '@/lib/exportMarkdown';
import { useDashboardApi } from '@/services/api';
import {
  useDashboardStore,
  useSelectedCard,
  useIsCardModalOpen,
  useColumns,
} from '@/store/dashboard';
import type { Card, CardStatus, CardPriority, Comment, TestScenario, BugSeverity, Spec } from '@/types';
import { STATUS_LABELS, CARD_STATUSES, PRIORITY_LABELS, CARD_PRIORITIES, BUG_SEVERITY_LABELS } from '@/types';
import { SpecModal } from '@/components/specs/SpecModal';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { EditableField } from '@/components/shared/EditableField';
import { CardKnowledgeTab } from './CardKnowledgeTab';

/** Resolve an actor ID to a display name using the members list. */
function resolveActorName(id: string | null | undefined, members: { id: string; name: string }[]): string {
  if (!id) return 'Unknown';
  const member = members.find(m => m.id === id);
  if (member) return member.name;
  // Fallback: if it looks like a user ID, show a short label
  if (id.startsWith('user_')) return 'User';
  if (id.includes('@')) return id.split('@')[0];
  return id.length > 16 ? id.slice(0, 12) + '…' : id;
}

interface CardModalProps {
  boardId: string;
}

export function CardModal({ boardId }: CardModalProps) {
  const api = useDashboardApi();
  const selectedCardId = useSelectedCard();
  const isOpen = useIsCardModalOpen();
  const { closeCardModal, removeCardFromColumn, updateCardInColumn } = useDashboardStore();
  const columns = useColumns();

  // Flat list of all cards on the board for dependency picker
  const allBoardCards = Object.values(columns).flat();

  const [card, setCard] = useState<Card | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<'details' | 'tests' | 'mockups' | 'knowledge' | 'conclusion' | 'validations' | 'qa' | 'comments' | 'activity'>('details');
  const [expanded, setExpanded] = useState(false);
  const [boardMembers, setBoardMembers] = useState<{ id: string; name: string }[]>([]);
  const [seenStatus, setSeenStatus] = useState<Record<string, { agent_name: string; seen_at: string }[]>>({});
  const [dependencies, setDependencies] = useState<{ id: string; title: string; status: string }[]>([]);
  const [dependents, setDependents] = useState<{ id: string; title: string; status: string }[]>([]);
  const [parentSpec, setParentSpec] = useState<{ id: string; title: string } | null>(null);
  const [fullSpec, setFullSpec] = useState<Spec | null>(null);
  const [specScenarios, setSpecScenarios] = useState<TestScenario[]>([]);
  const [specRules, setSpecRules] = useState<any[]>([]);
  const [specContracts, setSpecContracts] = useState<any[]>([]);
  const [specTRs, setSpecTRs] = useState<any[]>([]);
  const [viewingSpecId, setViewingSpecId] = useState<string | null>(null);
  const [specKBsFull, setSpecKBsFull] = useState<{ id: string; title: string; description?: string; content: string; mime_type?: string }[]>([]);
  const [showConclusionPrompt, setShowConclusionPrompt] = useState(false);
  const [conclusionDraft, setConclusionDraft] = useState('');
  const [conclusionCompleteness, setConclusionCompleteness] = useState(100);
  const [conclusionCompletenessJustification, setConclusionCompletenessJustification] = useState('');
  const [conclusionDrift, setConclusionDrift] = useState(0);
  const [conclusionDriftJustification, setConclusionDriftJustification] = useState('');

  const loadCard = (cardId: string) => {
    setIsLoading(true);
    api.getCard(cardId)
      .then((data) => {
        setCard(data);
        if (data.spec_id) {
          api.getSpec(data.spec_id)
            .then((spec) => {
              setParentSpec({ id: spec.id, title: spec.title });
              setFullSpec(spec);
              setSpecScenarios(spec.test_scenarios || []);
              setSpecRules(spec.business_rules || []);
              setSpecContracts(spec.api_contracts || []);
              setSpecTRs((spec.technical_requirements || []).map((tr: any, i: number) => typeof tr === 'string' ? { id: `tr_legacy_${i}`, text: tr, linked_task_ids: null } : tr));
              // Load full KB content for knowledge tab
              Promise.all(
                (spec.knowledge_bases || []).map((kb: any) => api.getSpecKnowledge(spec.id, kb.id).catch(() => null))
              ).then((kbs) => setSpecKBsFull(kbs.filter(Boolean) as any[])).catch(() => {});
            })
            .catch(() => { setParentSpec(null); setFullSpec(null); setSpecScenarios([]); });
        } else {
          setParentSpec(null);
          setFullSpec(null);
          setSpecScenarios([]);
        }
      })
      .catch(() => toast.error('Failed to load card'))
      .finally(() => setIsLoading(false));
    api.listAgentsForBoard(boardId)
      .then((agents) => setBoardMembers(agents.map((a) => ({ id: a.id, name: a.name }))))
      .catch(() => {});
    api.getCardSeenStatus(cardId)
      .then((data) => setSeenStatus(data.items))
      .catch(() => {});
    api.getCardDependencies(cardId).then(setDependencies).catch(() => {});
    api.getCardDependents(cardId).then(setDependents).catch(() => {});
  };

  // Silent refresh — updates card data without showing loading spinner
  const silentRefresh = useCallback((cardId: string) => {
    api.getCard(cardId)
      .then((data) => {
        setCard(data);
        if (data.spec_id) {
          api.getSpec(data.spec_id)
            .then((spec) => {
              setParentSpec({ id: spec.id, title: spec.title });
              setSpecScenarios(spec.test_scenarios || []);
              setSpecRules(spec.business_rules || []);
              setSpecContracts(spec.api_contracts || []);
              setSpecTRs((spec.technical_requirements || []).map((tr: any, i: number) => typeof tr === 'string' ? { id: `tr_legacy_${i}`, text: tr, linked_task_ids: null } : tr));
            })
            .catch(() => {});
        }
      })
      .catch(() => {});
    api.getCardSeenStatus(cardId)
      .then((data) => setSeenStatus(data.items))
      .catch(() => {});
    api.getCardDependencies(cardId).then(setDependencies).catch(() => {});
    api.getCardDependents(cardId).then(setDependents).catch(() => {});
  }, [api]);

  useEffect(() => {
    if (selectedCardId && isOpen) {
      loadCard(selectedCardId);
    } else {
      setCard(null);
    }
  }, [selectedCardId, isOpen]);

  // Auto-refresh every 10s while modal is open
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (selectedCardId && isOpen) {
      intervalRef.current = setInterval(() => {
        silentRefresh(selectedCardId);
      }, 10000);
    }
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [selectedCardId, isOpen, silentRefresh]);

  const handleRefresh = () => {
    if (selectedCardId) loadCard(selectedCardId);
  };

  const handleClose = () => {
    closeCardModal();
  };

  const handleStatusChange = async (status: CardStatus, conclusion?: string, metrics?: { completeness: number; completeness_justification: string; drift: number; drift_justification: string }) => {
    if (!card) return;

    // Intercept Done — require conclusion
    if (status === 'done' && !conclusion) {
      setShowConclusionPrompt(true);
      setConclusionDraft('');
      setConclusionCompleteness(100);
      setConclusionCompletenessJustification('');
      setConclusionDrift(0);
      setConclusionDriftJustification('');
      return;
    }

    try {
      const updated = await api.moveCard(card.id, {
        status,
        conclusion,
        completeness: metrics?.completeness,
        completeness_justification: metrics?.completeness_justification,
        drift: metrics?.drift,
        drift_justification: metrics?.drift_justification,
      });
      updateCardInColumn({
        id: updated.id,
        board_id: updated.board_id,
        spec_id: updated.spec_id,
        title: updated.title,
        description: updated.description,
        status: updated.status,
        priority: updated.priority,
        position: updated.position,
        assignee_id: updated.assignee_id,
        created_by: updated.created_by,
        created_at: updated.created_at,
        updated_at: updated.updated_at,
        due_date: updated.due_date,
        labels: updated.labels,
        test_scenario_ids: updated.test_scenario_ids,
        conclusions: updated.conclusions,
      });
      setCard(updated);
      toast.success('Status updated');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to update status');
    }
  };

  const handleAssigneeChange = async (assigneeId: string) => {
    if (!card) return;
    try {
      const updated = await api.updateCard(card.id, { assignee_id: assigneeId || undefined });
      setCard(updated);
      updateCardInColumn({
        id: updated.id,
        board_id: updated.board_id,
        spec_id: updated.spec_id,
        title: updated.title,
        description: updated.description,
        status: updated.status,
        priority: updated.priority,
        position: updated.position,
        assignee_id: updated.assignee_id,
        created_by: updated.created_by,
        created_at: updated.created_at,
        updated_at: updated.updated_at,
        due_date: updated.due_date,
        labels: updated.labels,
        test_scenario_ids: updated.test_scenario_ids,
        conclusions: updated.conclusions,
      });
      toast.success('Assignee updated');
    } catch {
      toast.error('Failed to update assignee');
    }
  };

  const handlePriorityChange = async (priority: string) => {
    if (!card) return;
    try {
      const updated = await api.updateCard(card.id, { priority: priority as CardPriority });
      setCard(updated);
      updateCardInColumn({
        id: updated.id,
        board_id: updated.board_id,
        spec_id: updated.spec_id,
        title: updated.title,
        description: updated.description,
        status: updated.status,
        priority: updated.priority,
        position: updated.position,
        assignee_id: updated.assignee_id,
        created_by: updated.created_by,
        created_at: updated.created_at,
        updated_at: updated.updated_at,
        due_date: updated.due_date,
        labels: updated.labels,
        test_scenario_ids: updated.test_scenario_ids,
        conclusions: updated.conclusions,
      });
      toast.success('Priority updated');
    } catch {
      toast.error('Failed to update priority');
    }
  };

  const handleDelete = async () => {
    if (!card) return;
    if (!confirm('Are you sure you want to delete this card?')) return;

    try {
      await api.deleteCard(card.id);
      removeCardFromColumn(card.id);
      handleClose();
      toast.success('Card deleted');
    } catch {
      toast.error('Failed to delete card');
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !card) return;

    try {
      const attachment = await api.uploadAttachment(card.board_id, card.id, file);
      setCard({ ...card, attachments: [...card.attachments, attachment] });
      toast.success('Attachment uploaded');
    } catch {
      toast.error('Failed to upload attachment');
    }
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div className={`modal-content ${expanded ? '!max-w-[95vw] !h-[95vh]' : ''}`} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="modal-header">
          <div className="flex items-center gap-3 flex-1">
            <select
              value={card?.status || ''}
              onChange={(e) => handleStatusChange(e.target.value as CardStatus)}
              className="text-sm border border-gray-300 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:border-gray-600 text-gray-900 dark:text-gray-100"
            >
              {CARD_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {STATUS_LABELS[status]}
                </option>
              ))}
            </select>
            {card?.card_type === 'bug' && (
              <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-bold bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 uppercase tracking-wide shrink-0">
                <Bug size={14} />
                BUG
              </span>
            )}
            {card?.card_type === 'test' && (
              <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-bold bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300 uppercase tracking-wide shrink-0">
                <FlaskConical size={14} />
                TEST
              </span>
            )}
            <h2
              className="font-semibold text-lg whitespace-pre-wrap break-words line-clamp-3 cursor-text hover:bg-gray-100 dark:hover:bg-gray-700 rounded px-1 -mx-1 outline-none focus:ring-2 focus:ring-blue-400 focus:bg-white dark:focus:bg-gray-700"
              contentEditable={!isLoading && !!card}
              suppressContentEditableWarning
              onBlur={async (e) => {
                if (!card) return;
                const newTitle = (e.currentTarget.textContent || '').trim();
                if (!newTitle || newTitle === card.title) {
                  e.currentTarget.textContent = card.title;
                  return;
                }
                try {
                  const updated = await api.updateCard(card.id, { title: newTitle });
                  setCard(updated);
                  updateCardInColumn({
                    id: updated.id,
                    board_id: updated.board_id,
                    spec_id: updated.spec_id,
                    title: updated.title,
                    description: updated.description,
                    status: updated.status,
                    priority: updated.priority,
                    position: updated.position,
                    assignee_id: updated.assignee_id,
                    created_by: updated.created_by,
                    created_at: updated.created_at,
                    updated_at: updated.updated_at,
                    due_date: updated.due_date,
                    labels: updated.labels,
                    test_scenario_ids: updated.test_scenario_ids,
                    conclusions: updated.conclusions,
                  });
                  toast.success('Title updated');
                } catch {
                  e.currentTarget.textContent = card.title;
                  toast.error('Failed to update title');
                }
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur(); }
                if (e.key === 'Escape') { e.currentTarget.textContent = card?.title || ''; e.currentTarget.blur(); }
              }}
            >
              {isLoading ? 'Loading...' : card?.title}
            </h2>
          </div>
          {parentSpec && (
            <button
              onClick={() => setViewingSpecId(parentSpec.id)}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300 hover:ring-2 hover:ring-violet-300 dark:hover:ring-violet-600 transition-all shrink-0"
              title="View linked spec"
            >
              <FileText size={11} />
              {parentSpec.title.length > 30 ? parentSpec.title.slice(0, 27) + '...' : parentSpec.title}
            </button>
          )}
          <div className="flex items-center gap-1">
            <button
              onClick={() => { if (!card) return; const md = exportCard(card, fullSpec); downloadMarkdown(md, `${card.card_type === 'bug' ? 'bug' : 'task'}_${slugify(card.title)}.md`); }}
              disabled={!card}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-30"
              title="Download Markdown"
            >
              <Download size={16} />
            </button>
            <button onClick={handleRefresh} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title="Refresh card">
              <RefreshCw size={16} className={isLoading ? 'animate-spin' : ''} />
            </button>
            <button onClick={() => setExpanded(!expanded)} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title={expanded ? 'Collapse' : 'Expand'}>
              {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            <button onClick={handleClose} className="p-1 hover:bg-gray-200 dark:hover:bg-gray-700 rounded">
              <X size={20} />
            </button>
          </div>
        </div>

        {/* Body */}
        {isLoading ? (
          <div className="modal-body text-center py-8 text-gray-500 dark:text-gray-400">Loading...</div>
        ) : card ? (
          <>
            {/* Tabs */}
            <div className="flex border-b border-gray-200 dark:border-gray-700 px-6">
              {(card.card_type === 'bug'
                ? ['details', 'tests', 'mockups', 'knowledge', 'conclusion', 'validations', 'qa', 'comments', 'activity'] as const
                : ['details', 'mockups', 'knowledge', 'conclusion', 'validations', 'qa', 'comments', 'activity'] as const
              ).map((tab) => (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px relative ${
                    activeTab === tab
                      ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                      : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'
                  }`}
                >
                  {tab === 'details' && 'Details'}
                  {tab === 'tests' && (
                    <>
                      Tests
                      {(card.linked_test_task_ids?.length ?? 0) > 0 ? (
                        <span className="absolute -top-0.5 -right-1 flex h-4 w-4 items-center justify-center rounded-full bg-green-500 text-white text-[9px]">
                          <Check size={8} />
                        </span>
                      ) : (
                        <span className="absolute -top-0.5 -right-1 flex h-4 w-4 items-center justify-center rounded-full bg-amber-500 text-white text-[9px] font-bold">!</span>
                      )}
                    </>
                  )}
                  {tab === 'mockups' && `Mockups${card.screen_mockups?.length ? ` (${card.screen_mockups.length})` : ''}`}
                  {tab === 'knowledge' && `Knowledge${card.knowledge_bases?.length ? ` (${card.knowledge_bases.length})` : ''}`}
                  {tab === 'conclusion' && `Conclusion${card.conclusions?.length ? ` (${card.conclusions.length})` : ''}`}
                  {tab === 'validations' && (
                    <>
                      <Shield size={13} className="inline mr-1" />
                      Validations
                      {(card.validations?.length ?? 0) > 0 && (
                        <span className="ml-1 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-violet-500 text-white text-[9px] px-1">
                          {card.validations!.length}
                        </span>
                      )}
                    </>
                  )}
                  {tab === 'qa' && `Q&A (${card.qa_items.length})`}
                  {tab === 'comments' && `Comments (${card.comments.length})`}
                  {tab === 'activity' && 'Activity'}
                </button>
              ))}
            </div>

            <div className="modal-body">
              {/* Details Tab */}
              {activeTab === 'details' && (
                <div className="space-y-4">
                  <div>
                    <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">Description</h3>
                    <EditableField
                      value={card.description || ''}
                      onSave={async (val) => {
                        const updated = await api.updateCard(card.id, { description: val });
                        setCard(updated);
                        updateCardInColumn(updated);
                      }}
                      multiline
                      renderView={(v) => <Md>{v}</Md>}
                      placeholder="No description"
                    />
                  </div>

                  {/* Bug-specific fields */}
                  {card.card_type === 'bug' && (
                    <>
                      {/* Severity */}
                      <div>
                        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Severity</h3>
                        <div className="flex gap-2">
                          {(['critical', 'major', 'minor'] as BugSeverity[]).map((sev) => (
                            <button
                              key={sev}
                              onClick={async () => {
                                const updated = await api.updateCard(card.id, { severity: sev });
                                setCard(updated);
                                updateCardInColumn(updated);
                              }}
                              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                                card.severity === sev
                                  ? sev === 'critical' ? 'bg-red-500 text-white ring-2 ring-red-300'
                                  : sev === 'major' ? 'bg-orange-500 text-white ring-2 ring-orange-300'
                                  : 'bg-yellow-500 text-white ring-2 ring-yellow-300'
                                  : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'
                              }`}
                            >
                              {BUG_SEVERITY_LABELS[sev]}
                            </button>
                          ))}
                        </div>
                      </div>

                      {/* Expected Behavior */}
                      <div>
                        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Expected Behavior</h3>
                        <div className="border border-gray-200 dark:border-gray-600 rounded-lg bg-green-50 dark:bg-green-900/10">
                          <EditableField
                            value={card.expected_behavior || ''}
                            onSave={async (val) => {
                              const updated = await api.updateCard(card.id, { expected_behavior: val });
                              setCard(updated);
                            }}
                            multiline
                            renderView={(v) => <div className="p-3 text-sm text-gray-800 dark:text-gray-200"><Md>{v}</Md></div>}
                            placeholder="Describe expected behavior..."
                          />
                        </div>
                      </div>

                      {/* Observed Behavior */}
                      <div>
                        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Observed Behavior</h3>
                        <div className="border border-red-200 dark:border-red-600/40 rounded-lg bg-red-50 dark:bg-red-900/10">
                          <EditableField
                            value={card.observed_behavior || ''}
                            onSave={async (val) => {
                              const updated = await api.updateCard(card.id, { observed_behavior: val });
                              setCard(updated);
                            }}
                            multiline
                            renderView={(v) => <div className="p-3 text-sm text-gray-800 dark:text-gray-200"><Md>{v}</Md></div>}
                            placeholder="Describe observed behavior..."
                          />
                        </div>
                      </div>

                      {/* Steps to Reproduce */}
                      <div>
                        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Steps to Reproduce</h3>
                        <div className="border border-gray-200 dark:border-gray-600 rounded-lg bg-gray-50 dark:bg-gray-800">
                          <EditableField
                            value={card.steps_to_reproduce || ''}
                            onSave={async (val) => {
                              const updated = await api.updateCard(card.id, { steps_to_reproduce: val });
                              setCard(updated);
                            }}
                            multiline
                            renderView={(v) => <div className="p-3 text-sm text-gray-800 dark:text-gray-200"><Md>{v}</Md></div>}
                            placeholder="Steps to reproduce..."
                          />
                        </div>
                      </div>

                      {/* Action Plan */}
                      <div>
                        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">Action Plan</h3>
                        <div className="border border-blue-200 dark:border-blue-600/40 rounded-lg bg-blue-50 dark:bg-blue-900/10">
                          <EditableField
                            value={card.action_plan || ''}
                            onSave={async (val) => {
                              const updated = await api.updateCard(card.id, { action_plan: val });
                              setCard(updated);
                            }}
                            multiline
                            renderView={(v) => <div className="p-3 text-sm text-gray-800 dark:text-gray-200"><Md>{v}</Md></div>}
                            placeholder="Action plan for fix..."
                          />
                        </div>
                      </div>
                    </>
                  )}

                  {/* Assignee */}
                  <div>
                    <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">Assignee</h3>
                    <select
                      value={card.assignee_id || ''}
                      onChange={(e) => handleAssigneeChange(e.target.value)}
                      className="text-sm border border-gray-300 rounded px-3 py-1.5 bg-white dark:bg-gray-700 dark:border-gray-600 text-gray-900 dark:text-gray-100 w-full"
                    >
                      <option value="">None</option>
                      {(() => {
                        // Build deduplicated assignee options: creator + board agents
                        const creatorName = boardMembers.find(m => m.id === card.created_by)?.name;
                        const creatorLabel = creatorName ? `${creatorName} (creator)` : `Owner (creator)`;
                        const options: { id: string; label: string }[] = [
                          { id: card.created_by, label: creatorLabel },
                        ];
                        for (const m of boardMembers) {
                          if (m.id !== card.created_by) {
                            options.push({ id: m.id, label: m.name });
                          }
                        }
                        return options.map(o => (
                          <option key={o.id} value={o.id}>{o.label}</option>
                        ));
                      })()}
                    </select>
                  </div>

                  {/* Priority */}
                  <div>
                    <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">Priority</h3>
                    <select
                      value={card.priority || 'none'}
                      onChange={(e) => handlePriorityChange(e.target.value)}
                      className="text-sm border border-gray-300 rounded px-3 py-1.5 bg-white dark:bg-gray-700 dark:border-gray-600 text-gray-900 dark:text-gray-100 w-full"
                    >
                      {CARD_PRIORITIES.map((p) => (
                        <option key={p} value={p}>{PRIORITY_LABELS[p]}</option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">Details</h3>
                    <EditableField
                      value={card.details || ''}
                      onSave={async (val) => {
                        const updated = await api.updateCard(card.id, { details: val });
                        setCard(updated);
                        updateCardInColumn(updated);
                      }}
                      multiline
                      renderView={(v) => <Md>{v}</Md>}
                      placeholder="No details"
                    />
                  </div>

                  {card.labels && card.labels.length > 0 && (
                    <div>
                      <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">Labels</h3>
                      <div className="flex flex-wrap gap-1">
                        {card.labels.map((label, idx) => (
                          <span
                            key={idx}
                            className="text-xs px-2 py-1 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                          >
                            {label}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Dependencies */}
                  <DependenciesSection
                    cardId={card.id}
                    dependencies={dependencies}
                    dependents={dependents}
                    setDependencies={setDependencies}
                    api={api}
                    allCards={allBoardCards}
                  />

                  {/* Test Scenarios */}
                  {card.spec_id && specScenarios.length > 0 && (
                    <TestScenariosSection
                      card={card}
                      specId={card.spec_id}
                      scenarios={specScenarios}
                      api={api}
                      onUpdate={(updatedCard, updatedScenarios) => {
                        setCard(updatedCard);
                        setSpecScenarios(updatedScenarios);
                        updateCardInColumn({
                          id: updatedCard.id,
                          board_id: updatedCard.board_id,
                          spec_id: updatedCard.spec_id,
                          title: updatedCard.title,
                          description: updatedCard.description,
                          status: updatedCard.status,
                          priority: updatedCard.priority,
                          position: updatedCard.position,
                          assignee_id: updatedCard.assignee_id,
                          created_by: updatedCard.created_by,
                          created_at: updatedCard.created_at,
                          updated_at: updatedCard.updated_at,
                          due_date: updatedCard.due_date,
                          labels: updatedCard.labels,
                          test_scenario_ids: updatedCard.test_scenario_ids,
                          conclusions: updatedCard.conclusions,
                        });
                      }}
                    />
                  )}

                  {/* Linked Business Rules */}
                  {card.spec_id && specRules.length > 0 && (
                    <LinkedSpecItemsSection
                      card={card}
                      specId={card.spec_id}
                      items={specRules}
                      field="business_rules"
                      label="Business Rules"
                      icon={<Scale size={14} className="inline mr-1" />}
                      api={api}
                      onSpecRefresh={() => {
                        api.getSpec(card.spec_id!).then((spec) => {
                          setSpecRules(spec.business_rules || []);
                        }).catch(() => {});
                      }}
                    />
                  )}

                  {/* Linked API Contracts */}
                  {card.spec_id && specContracts.length > 0 && (
                    <LinkedSpecItemsSection
                      card={card}
                      specId={card.spec_id}
                      items={specContracts}
                      field="api_contracts"
                      label="API Contracts"
                      icon={<FileText size={14} className="inline mr-1" />}
                      api={api}
                      onSpecRefresh={() => {
                        api.getSpec(card.spec_id!).then((spec) => {
                          setSpecContracts(spec.api_contracts || []);
                        }).catch(() => {});
                      }}
                    />
                  )}

                  {/* Linked Technical Requirements */}
                  {card.spec_id && specTRs.length > 0 && (
                    <LinkedSpecItemsSection
                      card={card}
                      specId={card.spec_id}
                      items={specTRs}
                      field="technical_requirements"
                      label="Technical Requirements"
                      icon={<FileText size={14} className="inline mr-1" />}
                      api={api}
                      onSpecRefresh={() => {
                        api.getSpec(card.spec_id!).then((spec) => {
                          setSpecTRs((spec.technical_requirements || []).map((tr: any, i: number) => typeof tr === 'string' ? { id: `tr_legacy_${i}`, text: tr, linked_task_ids: null } : tr));
                        }).catch(() => {});
                      }}
                    />
                  )}

                  {/* Attachments */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
                        <Paperclip size={14} className="inline mr-1" />
                        Attachments ({card.attachments.length})
                      </h3>
                      <label className="text-xs text-blue-600 hover:text-blue-700 dark:hover:text-blue-400 cursor-pointer">
                        <input
                          type="file"
                          className="hidden"
                          onChange={handleFileUpload}
                        />
                        + Add
                      </label>
                    </div>
                    <div className="space-y-1">
                      {card.attachments.map((att) => (
                        <div
                          key={att.id}
                          className="flex items-center justify-between p-2 bg-gray-50 dark:bg-gray-800 rounded text-gray-700 dark:text-gray-300"
                        >
                          <span className="text-sm truncate flex-1">{att.original_filename}</span>
                          <button
                            onClick={async (e) => {
                              e.preventDefault();
                              try {
                                await api.downloadAttachment(card.board_id, card.id, att.id, att.original_filename);
                              } catch {
                                toast.error('Failed to download attachment');
                              }
                            }}
                            className="p-1 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400"
                          >
                            <Download size={16} />
                          </button>
                        </div>
                      ))}
                      {card.attachments.length === 0 && (
                        <p className="text-sm text-gray-400 dark:text-gray-500">No attachments</p>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {/* Tests Tab (bug cards only) */}
              {activeTab === 'tests' && card.card_type === 'bug' && (
                <div className="space-y-4">
                  {/* Block/unblock indicator */}
                  {(card.linked_test_task_ids?.length ?? 0) > 0 ? (
                    <div className="flex items-start gap-3 p-3 rounded-lg bg-green-50 dark:bg-green-900/10 border border-green-200 dark:border-green-700/40">
                      <Check className="shrink-0 mt-0.5 text-green-500" size={18} />
                      <div>
                        <p className="text-sm font-semibold text-green-800 dark:text-green-300">Ready for In Progress</p>
                        <p className="text-xs text-green-700 dark:text-green-400 mt-0.5">
                          {card.linked_test_task_ids!.length} new test task(s) linked. This bug card can now be moved to "In Progress".
                        </p>
                      </div>
                    </div>
                  ) : (
                    <div className="flex items-start gap-3 p-4 rounded-lg bg-amber-50 dark:bg-amber-900/10 border border-amber-200 dark:border-amber-700/40">
                      <AlertCircle className="shrink-0 mt-0.5 text-amber-500" size={20} />
                      <div>
                        <p className="text-sm font-semibold text-amber-800 dark:text-amber-300">Blocked from In Progress</p>
                        <p className="text-xs text-amber-700 dark:text-amber-400 mt-0.5">
                          This bug card requires at least <strong>1 new test task</strong> linked before it can be moved to "In Progress".
                          Create a new test scenario in the spec and associate the resulting test task below.
                        </p>
                      </div>
                    </div>
                  )}

                  {/* Linked Test Tasks */}
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Linked Test Tasks</h3>
                    </div>
                    {(card.linked_test_task_ids?.length ?? 0) > 0 ? (
                      <div className="border border-gray-200 dark:border-gray-600 rounded-lg divide-y divide-gray-100 dark:divide-gray-700">
                        {card.linked_test_task_ids!.map((taskId) => {
                          const taskInBoard = allBoardCards.find(c => c.id === taskId);
                          return (
                            <div key={taskId} className="p-3 flex items-center justify-between">
                              <div className="flex items-center gap-3 min-w-0">
                                <span className="shrink-0 flex h-6 w-6 items-center justify-center rounded bg-violet-100 dark:bg-violet-900/40 text-violet-600 dark:text-violet-400">
                                  <FlaskConical size={12} />
                                </span>
                                <div className="min-w-0">
                                  <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                                    {taskInBoard?.title || taskId.slice(0, 12) + '…'}
                                  </p>
                                  {taskInBoard && (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 font-medium">
                                      {taskInBoard.status}
                                    </span>
                                  )}
                                </div>
                              </div>
                              <button
                                onClick={async () => {
                                  try {
                                    await api.unlinkTestTaskFromBug(card.id, taskId);
                                    const linked = (card.linked_test_task_ids || []).filter(id => id !== taskId);
                                    setCard({ ...card, linked_test_task_ids: linked });
                                    toast.success('Test task unlinked');
                                  } catch {
                                    toast.error('Failed to unlink');
                                  }
                                }}
                                className="p-1 text-gray-400 hover:text-red-500"
                                title="Unlink"
                              >
                                <X size={14} />
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="border border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6 flex flex-col items-center justify-center text-gray-400 dark:text-gray-500">
                        <Link size={32} strokeWidth={1.5} />
                        <p className="text-xs mt-2">No test tasks linked</p>
                        <p className="text-[10px] mt-0.5">Create test scenarios first, then link the resulting test tasks here</p>
                      </div>
                    )}
                  </div>

                  {/* Test Scenarios from spec */}
                  {specScenarios.length > 0 && (
                    <div>
                      <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-2">
                        Test Scenarios (from Spec) — {specScenarios.length}
                      </h3>
                      <div className="border border-gray-200 dark:border-gray-600 rounded-lg divide-y divide-gray-100 dark:divide-gray-700">
                        {specScenarios.map((scenario) => {
                          const isNew = scenario.created_at && card.created_at && scenario.created_at >= card.created_at;
                          return (
                            <div key={scenario.id} className="p-3 flex items-start gap-3">
                              <div className="shrink-0 mt-0.5">
                                <span className={`flex h-6 w-6 items-center justify-center rounded-full ${isNew ? 'bg-green-100 dark:bg-green-900/40 text-green-600 dark:text-green-400' : 'bg-gray-100 dark:bg-gray-700 text-gray-400'}`}>
                                  <FlaskConical size={12} />
                                </span>
                              </div>
                              <div className="flex-1 min-w-0">
                                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{scenario.title}</p>
                                <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                                  Given {scenario.given} → When {scenario.when} → Then {scenario.then}
                                </p>
                                <div className="flex items-center gap-2 mt-1.5">
                                  {isNew && (
                                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">NEW</span>
                                  )}
                                  <span className="text-[10px] text-gray-400">{scenario.scenario_type}</span>
                                </div>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Mockups Tab */}
              {activeTab === 'mockups' && (
                <div className="modal-body">
                  <MockupsTab screenMockups={card.screen_mockups} expanded={expanded} />
                </div>
              )}

              {/* Knowledge Tab */}
              {activeTab === 'knowledge' && (
                <CardKnowledgeTab
                  card={card}
                  specKnowledgeBases={specKBsFull}
                  onUpdate={async (kbs) => {
                    const updated = await api.updateCard(card.id, { knowledge_bases: kbs } as any);
                    setCard(updated);
                  }}
                />
              )}

              {/* Q&A Tab */}
              {activeTab === 'qa' && (
                <QATab card={card} setCard={setCard} api={api} members={boardMembers} seenStatus={seenStatus} />
              )}

              {/* Comments Tab */}
              {activeTab === 'comments' && (
                <CommentsTab card={card} setCard={setCard} api={api} members={boardMembers} seenStatus={seenStatus} />
              )}

              {/* Conclusion Tab */}
              {activeTab === 'conclusion' && (
                <div className="modal-body">
                  {card.conclusions && card.conclusions.length > 0 ? (
                    <div className="space-y-3">
                      {card.conclusions.map((c, i) => (
                        <div key={i} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                              Conclusion #{i + 1}
                            </span>
                            <span className="text-[10px] text-gray-400">
                              {c.author_id?.slice(0, 12)}... &middot; {new Date(c.created_at).toLocaleString()}
                            </span>
                          </div>
                          <Md>{c.text}</Md>
                          {/* Completeness & Drift metrics */}
                          <div className="flex flex-wrap gap-3 mt-3 pt-3 border-t border-gray-100 dark:border-gray-700/50">
                            <div className="flex-1 min-w-[180px]">
                              <div className="flex items-center gap-2 mb-1">
                                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                                  c.completeness >= 90 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                                  : c.completeness >= 70 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                                  : c.completeness >= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                                  : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                                }`}>
                                  Completeness: {c.completeness}%
                                </span>
                              </div>
                              {c.completeness_justification && (
                                <p className="text-xs text-gray-500 dark:text-gray-400 ml-1">{c.completeness_justification}</p>
                              )}
                            </div>
                            <div className="flex-1 min-w-[180px]">
                              <div className="flex items-center gap-2 mb-1">
                                <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                                  c.drift <= 10 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                                  : c.drift <= 25 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                                  : c.drift <= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                                  : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                                }`}>
                                  Drift: {c.drift}%
                                </span>
                              </div>
                              {c.drift_justification && (
                                <p className="text-xs text-gray-500 dark:text-gray-400 ml-1">{c.drift_justification}</p>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-center py-8">
                      <p className="text-gray-500 dark:text-gray-400 text-sm">No conclusion yet</p>
                      <p className="text-gray-400 dark:text-gray-500 text-xs mt-1">A conclusion is required when moving this card to Done</p>
                    </div>
                  )}
                </div>
              )}

              {/* Validations Tab */}
              {activeTab === 'validations' && (
                <ValidationsTab card={card} setCard={setCard} api={api} members={boardMembers} />
              )}

              {/* Activity Tab */}
              {activeTab === 'activity' && (
                <ActivityTab cardId={card.id} api={api} />
              )}
            </div>
          </>
        ) : null}

        {/* Conclusion prompt — shown when changing status to Done */}
        {showConclusionPrompt && (
          <div className="px-6 py-3 border-t border-gray-100 dark:border-gray-700/50 bg-green-50/50 dark:bg-green-900/10 max-h-[60vh] overflow-y-auto">
            <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">Conclusion Required</h4>
            <textarea
              value={conclusionDraft}
              onChange={(e) => setConclusionDraft(e.target.value)}
              placeholder={"## Implementation Summary\n\n### Changes\n- ...\n\n### Testing\n- ...\n\n### Follow-ups\n- ..."}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600 resize-none"
              rows={6}
              autoFocus
            />
            {/* Completeness metric */}
            <div className="mt-3">
              <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2">
                Completeness
                <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${
                  conclusionCompleteness >= 90 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                  : conclusionCompleteness >= 70 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                  : conclusionCompleteness >= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                  : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                }`}>{conclusionCompleteness}%</span>
              </label>
              <input
                type="range"
                min={0}
                max={100}
                value={conclusionCompleteness}
                onChange={(e) => setConclusionCompleteness(Number(e.target.value))}
                className="w-full mt-1"
              />
              <textarea
                value={conclusionCompletenessJustification}
                onChange={(e) => setConclusionCompletenessJustification(e.target.value)}
                placeholder="Justify the completeness score..."
                className="w-full mt-1 px-3 py-2 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
                rows={2}
              />
            </div>
            {/* Drift metric */}
            <div className="mt-3">
              <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2">
                Drift
                <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${
                  conclusionDrift <= 10 ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                  : conclusionDrift <= 25 ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                  : conclusionDrift <= 50 ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                  : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                }`}>{conclusionDrift}%</span>
              </label>
              <input
                type="range"
                min={0}
                max={100}
                value={conclusionDrift}
                onChange={(e) => setConclusionDrift(Number(e.target.value))}
                className="w-full mt-1"
              />
              <textarea
                value={conclusionDriftJustification}
                onChange={(e) => setConclusionDriftJustification(e.target.value)}
                placeholder="Justify the drift score..."
                className="w-full mt-1 px-3 py-2 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none"
                rows={2}
              />
            </div>
            <div className="flex justify-end gap-2 mt-3">
              <button onClick={() => setShowConclusionPrompt(false)} className="btn btn-secondary text-xs">Cancel</button>
              <button
                onClick={() => {
                  setShowConclusionPrompt(false);
                  handleStatusChange('done', conclusionDraft.trim(), {
                    completeness: conclusionCompleteness,
                    completeness_justification: conclusionCompletenessJustification.trim(),
                    drift: conclusionDrift,
                    drift_justification: conclusionDriftJustification.trim(),
                  });
                }}
                disabled={!conclusionDraft.trim() || !conclusionCompletenessJustification.trim() || !conclusionDriftJustification.trim()}
                className={`btn text-xs ${conclusionDraft.trim() && conclusionCompletenessJustification.trim() && conclusionDriftJustification.trim() ? 'btn-primary' : 'btn-secondary opacity-50'}`}
              >
                Complete & Move to Done
              </button>
            </div>
          </div>
        )}

        {/* Footer */}
        <div className="modal-footer">
          <button onClick={handleDelete} className="btn btn-danger flex items-center gap-1">
            <Trash2 size={16} />
            Delete
          </button>
        </div>
      </div>

      {/* Spec modal */}
      {viewingSpecId && (
        <SpecModal
          specId={viewingSpecId}
          boardId={boardId}
          onClose={() => setViewingSpecId(null)}
          onChanged={() => { if (selectedCardId) loadCard(selectedCardId); }}
        />
      )}
    </div>
  );
}

// Test Scenarios section in Details tab
function TestScenariosSection({
  card, specId, scenarios, api, onUpdate,
}: {
  card: Card;
  specId: string;
  scenarios: TestScenario[];
  api: ReturnType<typeof useDashboardApi>;
  onUpdate: (card: Card, scenarios: TestScenario[]) => void;
}) {
  const linkedIds = new Set(card.test_scenario_ids || []);
  const linked = scenarios.filter((s) => linkedIds.has(s.id));
  const unlinked = scenarios.filter((s) => !linkedIds.has(s.id));
  const [showPicker, setShowPicker] = useState(false);

  const handleLink = async (scenarioId: string) => {
    try {
      await api.linkTaskToScenario(specId, scenarioId, card.id);
      // Refresh card and spec
      const [updatedCard, updatedSpec] = await Promise.all([
        api.getCard(card.id),
        api.getSpec(specId),
      ]);
      onUpdate(updatedCard, updatedSpec.test_scenarios || []);
      toast.success('Scenario linked');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to link scenario');
    }
  };

  const handleUnlink = async (scenarioId: string) => {
    try {
      await api.unlinkTaskFromScenario(specId, scenarioId, card.id);
      const [updatedCard, updatedSpec] = await Promise.all([
        api.getCard(card.id),
        api.getSpec(specId),
      ]);
      onUpdate(updatedCard, updatedSpec.test_scenarios || []);
      toast.success('Scenario unlinked');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to unlink scenario');
    }
  };

  const statusColor = (s: string) => {
    switch (s) {
      case 'passed': return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
      case 'failed': return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
      case 'automated': return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
      case 'ready': return 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300';
      default: return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400';
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
          <FlaskConical size={14} className="inline mr-1" />
          Test Scenarios ({linked.length}/{scenarios.length})
        </h3>
        {unlinked.length > 0 && (
          <button
            onClick={() => setShowPicker(!showPicker)}
            className="text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
          >
            {showPicker ? 'Cancel' : '+ Link scenario'}
          </button>
        )}
      </div>

      {/* Linked scenarios */}
      <div className="space-y-1">
        {linked.map((s) => (
          <div
            key={s.id}
            className="flex items-center justify-between px-2 py-1.5 rounded bg-violet-50 dark:bg-violet-900/10 text-xs group"
          >
            <div className="flex items-center gap-2 flex-1 min-w-0">
              <span className={`text-[10px] px-1 py-0.5 rounded shrink-0 ${statusColor(s.status)}`}>
                {s.status}
              </span>
              <span className="text-gray-700 dark:text-gray-300 truncate">{s.title}</span>
              <span className="text-[10px] text-gray-400 shrink-0">{s.scenario_type}</span>
            </div>
            <button
              onClick={() => handleUnlink(s.id)}
              className="p-0.5 text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
              title="Unlink scenario"
            >
              <Unlink size={12} />
            </button>
          </div>
        ))}
        {linked.length === 0 && (
          <p className="text-xs text-gray-400 dark:text-gray-500 italic">
            No test scenarios linked to this card
          </p>
        )}
      </div>

      {/* Picker for unlinked scenarios */}
      {showPicker && unlinked.length > 0 && (
        <div className="mt-2 border border-gray-200 dark:border-gray-700 rounded-lg p-2 space-y-1 max-h-40 overflow-y-auto">
          <p className="text-[10px] text-gray-400 mb-1">Click to link:</p>
          {unlinked.map((s) => (
            <button
              key={s.id}
              onClick={() => handleLink(s.id)}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left hover:bg-violet-50 dark:hover:bg-violet-900/20 transition-colors"
            >
              <span className={`text-[10px] px-1 py-0.5 rounded shrink-0 ${statusColor(s.status)}`}>
                {s.status}
              </span>
              <span className="text-gray-600 dark:text-gray-400 truncate">{s.title}</span>
              <Link size={10} className="text-gray-400 shrink-0 ml-auto" />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Generic section for linking spec items (BRs, Contracts, TRs) to a card.
 * Mirrors TestScenariosSection but works with any spec item type that has {id, linked_task_ids}.
 */
function LinkedSpecItemsSection({
  card, specId, items, field, label, icon, api, onSpecRefresh,
}: {
  card: Card;
  specId: string;
  items: { id: string; title?: string; text?: string; method?: string; path?: string; linked_task_ids?: string[] | null }[];
  field: 'business_rules' | 'api_contracts' | 'technical_requirements';
  label: string;
  icon: React.ReactNode;
  api: ReturnType<typeof useDashboardApi>;
  onSpecRefresh: () => void;
}) {
  const linkedItems = items.filter(i => (i.linked_task_ids || []).includes(card.id));
  const unlinkedItems = items.filter(i => !(i.linked_task_ids || []).includes(card.id));
  const [showPicker, setShowPicker] = useState(false);

  const itemLabel = (item: typeof items[0]) =>
    item.title || item.text || (item.method && item.path ? `${item.method} ${item.path}` : item.id);

  const handleLink = async (itemId: string) => {
    try {
      await api.linkTaskToSpecItem(specId, field, itemId, card.id);
      onSpecRefresh();
      toast.success(`${label} linked`);
    } catch {
      toast.error(`Failed to link ${label.toLowerCase()}`);
    }
  };

  const handleUnlink = async (itemId: string) => {
    try {
      await api.unlinkTaskFromSpecItem(specId, field, itemId, card.id);
      onSpecRefresh();
      toast.success(`${label} unlinked`);
    } catch {
      toast.error(`Failed to unlink ${label.toLowerCase()}`);
    }
  };

  if (items.length === 0) return null;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
          {icon} {label} ({linkedItems.length}/{items.length})
        </h3>
        {unlinkedItems.length > 0 && (
          <button
            onClick={() => setShowPicker(!showPicker)}
            className="text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400"
          >
            {showPicker ? 'Cancel' : `+ Link ${label.toLowerCase()}`}
          </button>
        )}
      </div>
      <div className="space-y-1">
        {linkedItems.map((item) => (
          <div key={item.id} className="flex items-center justify-between px-2 py-1.5 rounded bg-indigo-50 dark:bg-indigo-900/10 text-xs group">
            <span className="text-gray-700 dark:text-gray-300 truncate flex-1">{itemLabel(item)}</span>
            <button onClick={() => handleUnlink(item.id)} className="p-0.5 text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity shrink-0" title="Unlink">
              <Unlink size={12} />
            </button>
          </div>
        ))}
        {linkedItems.length === 0 && (
          <p className="text-xs text-gray-400 dark:text-gray-500 italic">No {label.toLowerCase()} linked to this card</p>
        )}
      </div>
      {showPicker && unlinkedItems.length > 0 && (
        <div className="mt-2 border border-gray-200 dark:border-gray-700 rounded-lg p-2 space-y-1 max-h-40 overflow-y-auto">
          <p className="text-[10px] text-gray-400 mb-1">Click to link:</p>
          {unlinkedItems.map((item) => (
            <button key={item.id} onClick={() => handleLink(item.id)} className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-xs text-left hover:bg-indigo-50 dark:hover:bg-indigo-900/20 transition-colors">
              <span className="text-gray-600 dark:text-gray-400 truncate">{itemLabel(item)}</span>
              <Link size={10} className="text-gray-400 shrink-0 ml-auto" />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Q&A Tab Component
// Dependencies section in Details tab
function DependenciesSection({
  cardId, dependencies, dependents, setDependencies, api, allCards,
}: {
  cardId: string;
  dependencies: { id: string; title: string; status: string }[];
  dependents: { id: string; title: string; status: string }[];
  setDependencies: (d: { id: string; title: string; status: string }[]) => void;
  api: ReturnType<typeof useDashboardApi>;
  allCards: { id: string; title: string; status: string }[];
}) {
  const [selectedDepId, setSelectedDepId] = useState('');

  const statusColor = (s: string) => {
    if (s === 'done') return 'text-green-600';
    if (s === 'cancelled') return 'text-gray-400 dark:text-gray-500';
    return 'text-amber-600';
  };

  // Cards available for selection: exclude self, existing dependencies, and dependents
  const excludedIds = new Set([cardId, ...dependencies.map((d) => d.id), ...dependents.map((d) => d.id)]);
  const availableCards = allCards.filter((c) => !excludedIds.has(c.id));

  const handleAdd = async () => {
    if (!selectedDepId) return;
    try {
      await api.addCardDependency(cardId, selectedDepId);
      const fresh = await api.getCardDependencies(cardId);
      setDependencies(fresh);
      setSelectedDepId('');
      toast.success('Dependency added');
    } catch (e: any) {
      const msg = e?.message?.includes('409') ? 'Circular or duplicate dependency' : 'Failed to add dependency';
      toast.error(msg);
    }
  };

  const handleRemove = async (depId: string) => {
    try {
      await api.removeCardDependency(cardId, depId);
      setDependencies(dependencies.filter((d) => d.id !== depId));
      toast.success('Dependency removed');
    } catch {
      toast.error('Failed to remove dependency');
    }
  };

  const hasBlocking = dependencies.some((d) => d.status !== 'done' && d.status !== 'cancelled');

  return (
    <div>
      <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">
        <Link size={14} className="inline mr-1" />
        Dependencies
        {hasBlocking && (
          <span className="ml-2 text-xs text-amber-600 font-normal">Blocked</span>
        )}
      </h3>

      {/* Depends on */}
      {dependencies.length > 0 && (
        <div className="mb-2">
          <p className="text-xs text-gray-400 dark:text-gray-500 mb-1">Depends on:</p>
          <div className="space-y-1">
            {dependencies.map((d) => (
              <div key={d.id} className="flex items-center justify-between px-2 py-1 bg-gray-50 dark:bg-gray-800 rounded text-sm text-gray-700 dark:text-gray-300">
                <span className={statusColor(d.status)}>
                  {d.status === 'done' ? '✓' : d.status === 'cancelled' ? '✗' : '●'}{' '}
                  {d.title}
                </span>
                <button onClick={() => handleRemove(d.id)} className="p-0.5 text-gray-400 dark:text-gray-500 hover:text-red-500" title="Remove dependency">
                  <Unlink size={12} />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Dependents */}
      {dependents.length > 0 && (
        <div className="mb-2">
          <p className="text-xs text-gray-400 dark:text-gray-500 mb-1">Blocks:</p>
          <div className="space-y-1">
            {dependents.map((d) => (
              <div key={d.id} className="px-2 py-1 bg-gray-50 dark:bg-gray-800 rounded text-sm text-gray-600 dark:text-gray-400">
                {d.title}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Add dependency */}
      <div className="flex gap-2 mt-2">
        <select
          value={selectedDepId}
          onChange={(e) => setSelectedDepId(e.target.value)}
          className="flex-1 px-2 py-1 text-xs border border-gray-300 rounded dark:bg-gray-700 dark:border-gray-600 text-gray-900 dark:text-gray-100"
        >
          <option value="">Select a card...</option>
          {availableCards.map((c) => (
            <option key={c.id} value={c.id}>
              [{STATUS_LABELS[c.status as CardStatus] || c.status}] {c.title}
            </option>
          ))}
        </select>
        <button onClick={handleAdd} disabled={!selectedDepId} className="text-xs px-2 py-1 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed">
          Add
        </button>
      </div>
    </div>
  );
}

// Seen-by indicator for items
type SeenRecord = { agent_name: string; seen_at: string };
type SeenMap = Record<string, SeenRecord[]>;

function SeenByIndicator({ itemId, seenStatus }: { itemId: string; seenStatus: SeenMap }) {
  const viewers = seenStatus[itemId];
  if (!viewers || viewers.length === 0) return null;

  return (
    <div className="flex items-center gap-1 mt-1">
      <span className="text-[10px] text-gray-400 dark:text-gray-500">Seen by:</span>
      {viewers.map((v, i) => (
        <span
          key={i}
          className="inline-flex items-center px-1.5 py-0.5 rounded-full bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400 text-[10px] font-medium"
          title={`${v.agent_name} at ${new Date(v.seen_at).toLocaleString('en-US')}`}
        >
          {v.agent_name}
        </span>
      ))}
    </div>
  );
}

function QATab({ card, setCard, api, members, seenStatus }: { card: Card; setCard: (c: Card) => void; api: ReturnType<typeof useDashboardApi>; members: { id: string; name: string }[]; seenStatus: SeenMap }) {
  const [newQuestion, setNewQuestion] = useState('');
  const [answerInput, setAnswerInput] = useState<Record<string, string>>({});

  const handleAskQuestion = async () => {
    if (!newQuestion.trim()) return;

    try {
      const qa = await api.createQuestion(card.id, { question: newQuestion });
      setCard({ ...card, qa_items: [...card.qa_items, qa] });
      setNewQuestion('');
      toast.success('Question added');
    } catch {
      toast.error('Failed to add question');
    }
  };

  const handleAnswer = async (qaId: string) => {
    const answer = answerInput[qaId];
    if (!answer?.trim()) return;

    try {
      const updated = await api.answerQuestion(qaId, { answer });
      setCard({
        ...card,
        qa_items: card.qa_items.map((q) => (q.id === qaId ? updated : q)),
      });
      setAnswerInput({ ...answerInput, [qaId]: '' });
      toast.success('Answer saved');
    } catch {
      toast.error('Failed to answer');
    }
  };

  return (
    <div className="space-y-4">
      {/* Add question */}
      <div className="flex gap-2">
        <div className="flex-1">
          <MentionInput
            value={newQuestion}
            onChange={setNewQuestion}
            members={members}
            placeholder="Add a question... (use @ to mention)"
          />
        </div>
        <button onClick={handleAskQuestion} className="btn btn-primary">
          Ask
        </button>
      </div>

      {/* Questions list */}
      <div className="space-y-3">
        {card.qa_items.map((qa) => (
          <div key={qa.id} className="border rounded-lg p-3 dark:border-gray-700">
            <div className="font-medium text-gray-900 dark:text-gray-100">
              <HelpCircle size={14} className="inline mr-1 align-text-top" />
              <Md className="inline">{qa.question}</Md>
            </div>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5 pl-5">
              Asked by <span className="font-medium text-gray-500 dark:text-gray-400">{resolveActorName(qa.asked_by, members)}</span>
            </p>

            {qa.answer ? (
              <div className="mt-2 text-gray-600 dark:text-gray-400 text-sm pl-5">
                <Md>{qa.answer}</Md>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                  Answered by <span className="font-medium text-gray-500 dark:text-gray-400">{resolveActorName(qa.answered_by, members)}</span>
                </p>
              </div>
            ) : (
              <div className="mt-2 flex gap-2 pl-5">
                <input
                  type="text"
                  value={answerInput[qa.id] || ''}
                  onChange={(e) => setAnswerInput({ ...answerInput, [qa.id]: e.target.value })}
                  placeholder="Answer..."
                  className="flex-1 px-2 py-1 text-sm border border-gray-300 rounded dark:bg-gray-700 dark:border-gray-600 text-gray-900 dark:text-gray-100"
                />
                <button
                  onClick={() => handleAnswer(qa.id)}
                  className="text-sm px-3 py-1 bg-green-600 text-white rounded hover:bg-green-700"
                >
                  Answer
                </button>
              </div>
            )}
            <SeenByIndicator itemId={qa.id} seenStatus={seenStatus} />
          </div>
        ))}

        {card.qa_items.length === 0 && (
          <p className="text-gray-400 dark:text-gray-500 text-sm text-center py-4">
            No questions yet
          </p>
        )}
      </div>
    </div>
  );
}

// Validations Tab Component
function ValidationsTab({ card, setCard, api, members }: { card: Card; setCard: (c: Card) => void; api: ReturnType<typeof useDashboardApi>; members: { id: string; name: string }[] }) {
  const [confidence, setConfidence] = useState(80);
  const [completeness, setCompleteness] = useState(80);
  const [drift, setDrift] = useState(20);
  const [confidenceJustification, setConfidenceJustification] = useState('');
  const [completenessJustification, setCompletenessJustification] = useState('');
  const [driftJustification, setDriftJustification] = useState('');
  const [generalJustification, setGeneralJustification] = useState('');
  const [verdict, setVerdict] = useState<'pass' | 'fail'>('pass');
  const [submitting, setSubmitting] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      const data = {
        verdict,
        confidence,
        completeness,
        drift,
        confidence_justification: confidenceJustification.trim(),
        completeness_justification: completenessJustification.trim(),
        drift_justification: driftJustification.trim(),
        summary: generalJustification.trim() || null,
      };
      const updated = await api.submitTaskValidation(card.id, data);
      setCard(updated);
      // Reset form
      setConfidence(80);
      setCompleteness(80);
      setDrift(20);
      setConfidenceJustification('');
      setCompletenessJustification('');
      setDriftJustification('');
      setGeneralJustification('');
      setVerdict('pass');
      toast.success('Validation submitted');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to submit validation');
    } finally {
      setSubmitting(false);
    }
  };

  const scoreColor = (score: number, isInverse = false) => {
    const effective = isInverse ? 100 - score : score;
    if (effective >= 80) return 'bg-green-500';
    if (effective >= 60) return 'bg-blue-500';
    if (effective >= 40) return 'bg-amber-500';
    return 'bg-red-500';
  };

  const scoreBadgeColor = (score: number, isInverse = false) => {
    const effective = isInverse ? 100 - score : score;
    if (effective >= 80) return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
    if (effective >= 60) return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
    if (effective >= 40) return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
    return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
  };

  const validations = card.validations || [];

  return (
    <div className="space-y-6">
      {/* Section A: Submit Validation Form — only when status === 'validation' */}
      {card.status === 'validation' && (
        <div className="bg-violet-50 dark:bg-violet-900/20 border border-violet-200 dark:border-violet-700 rounded-xl p-5 space-y-5">
          <h3 className="text-sm font-semibold text-violet-800 dark:text-violet-200 flex items-center gap-2">
            <Shield size={16} />
            Submit Validation
          </h3>

          {/* Confidence */}
          <div>
            <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2 mb-1">
              Confidence
              <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${scoreBadgeColor(confidence)}`}>{confidence}</span>
            </label>
            <input
              type="range"
              min={0}
              max={100}
              value={confidence}
              onChange={(e) => setConfidence(Number(e.target.value))}
              className="w-full"
            />
            <textarea
              value={confidenceJustification}
              onChange={(e) => setConfidenceJustification(e.target.value)}
              placeholder="Justify the confidence score..."
              className="w-full mt-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-xs dark:bg-gray-700 resize-none text-gray-900 dark:text-gray-100"
              rows={2}
            />
          </div>

          {/* Completeness */}
          <div>
            <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2 mb-1">
              Completeness
              <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${scoreBadgeColor(completeness)}`}>{completeness}</span>
            </label>
            <input
              type="range"
              min={0}
              max={100}
              value={completeness}
              onChange={(e) => setCompleteness(Number(e.target.value))}
              className="w-full"
            />
            <textarea
              value={completenessJustification}
              onChange={(e) => setCompletenessJustification(e.target.value)}
              placeholder="Justify the completeness score..."
              className="w-full mt-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-xs dark:bg-gray-700 resize-none text-gray-900 dark:text-gray-100"
              rows={2}
            />
          </div>

          {/* Drift */}
          <div>
            <label className="text-xs font-medium text-gray-600 dark:text-gray-400 flex items-center gap-2 mb-1">
              Drift
              <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${scoreBadgeColor(drift, true)}`}>{drift}</span>
            </label>
            <input
              type="range"
              min={0}
              max={100}
              value={drift}
              onChange={(e) => setDrift(Number(e.target.value))}
              className="w-full"
            />
            <textarea
              value={driftJustification}
              onChange={(e) => setDriftJustification(e.target.value)}
              placeholder="Justify the drift score..."
              className="w-full mt-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-xs dark:bg-gray-700 resize-none text-gray-900 dark:text-gray-100"
              rows={2}
            />
          </div>

          {/* General Justification */}
          <div>
            <label className="text-xs font-medium text-gray-600 dark:text-gray-400 block mb-1">General Justification</label>
            <textarea
              value={generalJustification}
              onChange={(e) => setGeneralJustification(e.target.value)}
              placeholder="Overall validation summary..."
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-xs dark:bg-gray-700 resize-none text-gray-900 dark:text-gray-100"
              rows={3}
            />
          </div>

          {/* Approve/Reject Toggle */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => setVerdict('pass')}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                verdict === 'pass'
                  ? 'bg-green-600 text-white ring-2 ring-green-300 dark:ring-green-700'
                  : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              <CheckCircle size={16} />
              Approve
            </button>
            <button
              onClick={() => setVerdict('fail')}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                verdict === 'fail'
                  ? 'bg-red-600 text-white ring-2 ring-red-300 dark:ring-red-700'
                  : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              <XCircle size={16} />
              Reject
            </button>
          </div>

          {/* Submit */}
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className={`w-full py-2.5 rounded-lg text-sm font-medium transition-colors ${
              verdict === 'pass'
                ? 'bg-green-600 hover:bg-green-700 text-white'
                : 'bg-red-600 hover:bg-red-700 text-white'
            } disabled:opacity-50`}
          >
            {submitting ? 'Submitting...' : `Submit Validation (${verdict === 'pass' ? 'Approve' : 'Reject'})`}
          </button>
        </div>
      )}

      {/* Section B: Validation History — always visible */}
      <div>
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3 flex items-center gap-2">
          <Clock size={14} />
          Validation History
        </h3>

        {validations.length === 0 ? (
          <div className="text-center py-8">
            <Shield size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
            <p className="text-sm text-gray-500 dark:text-gray-400">No validations yet</p>
            {card.status !== 'validation' && (
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                Move this card to "Validation" status to submit a validation
              </p>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            {[...validations].reverse().map((v) => {
              const isExpanded = expandedId === v.id;
              return (
                <div key={v.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                  <div
                    className="flex items-center gap-2 px-3 py-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/30"
                    onClick={() => setExpandedId(isExpanded ? null : v.id)}
                  >
                    <div className={`w-2 h-2 rounded-full shrink-0 ${v.verdict === 'pass' ? 'bg-green-500' : 'bg-red-500'}`} />
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold shrink-0 ${
                      v.verdict === 'pass'
                        ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                        : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                    }`}>
                      {v.verdict === 'pass' ? 'SUCCESS' : 'FAILED'}
                    </span>
                    <span className="text-sm text-gray-700 dark:text-gray-300 truncate flex-1">
                      {v.summary || (v.verdict === 'pass' ? 'Validation passed' : 'Validation failed')}
                    </span>
                    <div className="flex items-center gap-2 shrink-0 text-[10px] text-gray-400">
                      <span className="px-1 py-0.5 rounded bg-violet-100 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300">
                        {resolveActorName(v.evaluator_id, members)}
                      </span>
                      <span>{v.created_at ? new Date(v.created_at).toLocaleString() : ''}</span>
                    </div>
                    <span className="text-gray-400 shrink-0">
                      {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                    </span>
                  </div>

                  {isExpanded && (
                    <div className="px-4 py-3 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 space-y-3">
                      {/* Score bars */}
                      {[
                        { label: 'Confidence', value: v.confidence, inverse: false },
                        { label: 'Completeness', value: v.completeness, inverse: false },
                        { label: 'Drift', value: v.drift, inverse: true },
                      ].map((metric) => (
                        <div key={metric.label}>
                          <div className="flex items-center justify-between mb-1">
                            <span className="text-xs font-medium text-gray-600 dark:text-gray-400">{metric.label}</span>
                            <span className={`text-xs font-semibold px-1.5 py-0.5 rounded-full ${scoreBadgeColor(metric.value, metric.inverse)}`}>
                              {metric.value}
                            </span>
                          </div>
                          <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 overflow-hidden">
                            <div
                              className={`h-full rounded-full transition-all ${scoreColor(metric.value, metric.inverse)}`}
                              style={{ width: `${metric.value}%` }}
                            />
                          </div>
                        </div>
                      ))}

                      {/* Summary */}
                      {v.summary && (
                        <div className="pt-2 border-t border-gray-200 dark:border-gray-700">
                          <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Summary</p>
                          <p className="text-sm text-gray-700 dark:text-gray-300">{v.summary}</p>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// Activity Tab Component
function ActivityTab({ cardId, api }: { cardId: string; api: ReturnType<typeof useDashboardApi> }) {
  const [logs, setLogs] = useState<{ id: string; action: string; actor_type: string; actor_name: string; details: Record<string, unknown> | null; created_at: string }[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.getCardActivity(cardId)
      .then(setLogs)
      .catch(() => toast.error('Failed to load activity'))
      .finally(() => setLoading(false));
  }, [cardId]);

  const actionLabels: Record<string, string> = {
    card_created: 'Created the card',
    card_updated: 'Updated the card',
    card_moved: 'Moved the card',
    card_deleted: 'Deleted the card',
    comment_added: 'Added a comment',
    comment_updated: 'Edited a comment',
    comment_deleted: 'Removed a comment',
    question_added: 'Added a question',
    question_answered: 'Answered a question',
    question_deleted: 'Removed a question',
    attachment_uploaded: 'Uploaded an attachment',
    attachment_deleted: 'Removed an attachment',
    items_seen: 'Marked items as seen',
  };

  if (loading) return <div className="text-center py-8 text-gray-500 dark:text-gray-400">Loading...</div>;

  return (
    <div className="space-y-2">
      {logs.length === 0 ? (
        <p className="text-gray-400 dark:text-gray-500 text-sm text-center py-4">No activity recorded</p>
      ) : (
        logs.map((log) => (
          <div key={log.id} className="flex gap-3 py-2 border-b border-gray-100 dark:border-gray-700 last:border-0">
            <Clock size={14} className="mt-0.5 text-gray-400 dark:text-gray-500 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-sm text-gray-700 dark:text-gray-300">
                <span className="font-medium">{log.actor_name}</span>
                {' '}
                <span className="text-gray-500 dark:text-gray-400">
                  {actionLabels[log.action] || log.action}
                </span>
                {log.details && log.action === 'card_moved' && (
                  <span className="text-xs text-gray-400 dark:text-gray-500 ml-1">
                    ({(log.details as any).from_status || (log.details as any).status} → {(log.details as any).to_status || (log.details as any).status})
                  </span>
                )}
                {log.details && (log.action === 'comment_added' || log.action === 'comment_updated') && (log.details as any).content && (
                  <span className="block text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate max-w-md">
                    "{(log.details as any).content}"
                  </span>
                )}
                {log.details && log.action === 'question_added' && (log.details as any).question && (
                  <span className="block text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate max-w-md">
                    "{(log.details as any).question}"
                  </span>
                )}
                {log.details && log.action === 'question_answered' && (log.details as any).answer && (
                  <span className="block text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate max-w-md">
                    "{(log.details as any).answer}"
                  </span>
                )}
              </p>
              <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                {new Date(log.created_at).toLocaleDateString('en-US', {
                  day: '2-digit',
                  month: 'short',
                  year: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
                {log.actor_type === 'agent' && (
                  <span className="ml-1 px-1 py-0.5 rounded bg-purple-100 text-purple-600 dark:bg-purple-900/40 dark:text-purple-300 text-[10px]">
                    agent
                  </span>
                )}
              </p>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

// Markdown renderer with prose styling
function Md({ children, className = '' }: { children: string; className?: string }) {
  return <MarkdownContent content={children} className={className} />;
}

// Mention autocomplete dropdown
function MentionInput({
  value,
  onChange,
  members,
  placeholder,
  multiline = false,
}: {
  value: string;
  onChange: (v: string) => void;
  members: { id: string; name: string }[];
  placeholder: string;
  multiline?: boolean;
}) {
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [filter, setFilter] = useState('');

  const handleChange = (text: string) => {
    onChange(text);
    // Check if user just typed @
    const atMatch = text.match(/@(\w*)$/);
    if (atMatch) {
      setFilter(atMatch[1].toLowerCase());
      setShowSuggestions(true);
    } else {
      setShowSuggestions(false);
    }
  };

  const insertMention = (name: string) => {
    const newVal = value.replace(/@\w*$/, `@${name} `);
    onChange(newVal);
    setShowSuggestions(false);
  };

  const filtered = members.filter((m) => m.name.toLowerCase().includes(filter));

  const InputTag = multiline ? 'textarea' : 'input';

  return (
    <div className="relative">
      <InputTag
        value={value}
        onChange={(e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => handleChange(e.target.value)}
        placeholder={placeholder}
        className={`w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600 text-gray-900 dark:text-gray-100 ${multiline ? 'resize-none' : ''}`}
        rows={multiline ? 3 : undefined}
      />
      {showSuggestions && filtered.length > 0 && (
        <div className="absolute z-10 top-full mt-1 w-48 bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg max-h-32 overflow-y-auto">
          {filtered.map((m) => (
            <button
              key={m.id}
              onClick={() => insertMention(m.name)}
              className="w-full text-left px-3 py-1.5 text-sm hover:bg-blue-50 dark:hover:bg-blue-900/20"
            >
              @{m.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Comments Tab Component
function ChoiceBoardRenderer({ comment, api, card, setCard, members }: {
  comment: Comment;
  api: ReturnType<typeof useDashboardApi>;
  card: Card;
  setCard: (c: Card) => void;
  members: { id: string; name: string }[];
}) {
  const isMulti = comment.comment_type === 'multi_choice';
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [freeText, setFreeText] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const toggle = (optId: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (isMulti) {
        if (next.has(optId)) next.delete(optId); else next.add(optId);
      } else {
        next.clear();
        next.add(optId);
      }
      return next;
    });
  };

  const handleSubmit = async () => {
    if (selected.size === 0) return;
    setSubmitting(true);
    try {
      const updated = await api.respondToChoice(comment.id, Array.from(selected), freeText || undefined);
      setCard({ ...card, comments: card.comments.map(c => c.id === comment.id ? updated : c) });
      setSelected(new Set());
      setFreeText('');
      toast.success('Response recorded');
    } catch {
      toast.error('Failed to respond');
    } finally {
      setSubmitting(false);
    }
  };

  const responses = comment.responses || [];
  const totalResponses = responses.length;

  return (
    <div className="space-y-2">
      <p className="font-medium text-sm text-gray-800 dark:text-gray-100">{comment.content}</p>
      <p className="text-xs text-gray-400">{isMulti ? 'Select one or more' : 'Select one'}</p>

      {/* Options */}
      <div className="space-y-1.5">
        {(comment.choices || []).map(opt => {
          const votes = responses.filter(r => r.selected.includes(opt.id)).length;
          const pct = totalResponses > 0 ? Math.round((votes / totalResponses) * 100) : 0;
          const isSelected = selected.has(opt.id);

          return (
            <button
              key={opt.id}
              onClick={() => toggle(opt.id)}
              className={`w-full text-left relative rounded-lg border px-3 py-2 text-sm transition-colors ${
                isSelected
                  ? 'border-blue-400 bg-blue-50 dark:bg-blue-900/30 dark:border-blue-500/50'
                  : 'border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'
              }`}
            >
              {/* Progress bar background */}
              {totalResponses > 0 && (
                <div
                  className="absolute inset-0 rounded-lg bg-blue-100/50 dark:bg-blue-900/20 transition-all"
                  style={{ width: `${pct}%` }}
                />
              )}
              <div className="relative flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={`w-4 h-4 rounded-${isMulti ? 'sm' : 'full'} border-2 flex items-center justify-center ${
                    isSelected ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-500'
                  }`}>
                    {isSelected && <span className="text-white text-[10px]">✓</span>}
                  </span>
                  <span className="text-gray-700 dark:text-gray-200">{opt.label}</span>
                </div>
                {totalResponses > 0 && (
                  <span className="text-xs text-gray-400">{votes} ({pct}%)</span>
                )}
              </div>
            </button>
          );
        })}
      </div>

      {/* Free text */}
      {comment.allow_free_text && (
        <input
          type="text"
          value={freeText}
          onChange={e => setFreeText(e.target.value)}
          placeholder="Additional comment (optional)"
          className="w-full px-3 py-1.5 text-sm border border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
        />
      )}

      {/* Submit */}
      <button
        onClick={handleSubmit}
        disabled={selected.size === 0 || submitting}
        className="btn btn-primary text-sm disabled:opacity-50"
      >
        {submitting ? 'Submitting...' : 'Submit Response'}
      </button>

      {/* Responses summary */}
      {totalResponses > 0 && (
        <details className="text-xs text-gray-400 dark:text-gray-500">
          <summary className="cursor-pointer hover:text-gray-600">{totalResponses} response(s)</summary>
          <div className="mt-1 space-y-1 pl-2 border-l-2 border-gray-200 dark:border-gray-600">
            {responses.map((r, i) => (
              <div key={i}>
                <span className="font-medium text-gray-600 dark:text-gray-300">
                  {resolveActorName(r.responder_id, members) || r.responder_name}
                </span>
                {': '}
                {r.selected.map(s => (comment.choices || []).find(c => c.id === s)?.label || s).join(', ')}
                {r.free_text && <span className="italic ml-1">"{r.free_text}"</span>}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function CommentsTab({ card, setCard, api, members, seenStatus }: { card: Card; setCard: (c: Card) => void; api: ReturnType<typeof useDashboardApi>; members: { id: string; name: string }[]; seenStatus: SeenMap }) {
  const [newComment, setNewComment] = useState('');
  const [mode, setMode] = useState<'text' | 'choice' | 'multi_choice'>('text');
  const [choiceOptions, setChoiceOptions] = useState('');
  const [allowFreeText, setAllowFreeText] = useState(false);

  const handleAddComment = async () => {
    if (mode === 'text') {
      if (!newComment.trim()) return;
      try {
        const comment = await api.createComment(card.id, { content: newComment });
        setCard({ ...card, comments: [...card.comments, comment] });
        setNewComment('');
        toast.success('Comment added');
      } catch {
        toast.error('Failed to add comment');
      }
    } else {
      if (!newComment.trim() || !choiceOptions.trim()) return;
      const options = choiceOptions.split('\n').map(s => s.trim()).filter(Boolean);
      if (options.length < 2) {
        toast.error('At least 2 options required');
        return;
      }
      try {
        const comment = await api.createComment(card.id, {
          content: newComment,
          comment_type: mode,
          choices: options.map((label, i) => ({ id: `opt_${i}`, label })),
          allow_free_text: allowFreeText,
        });
        setCard({ ...card, comments: [...card.comments, comment] });
        setNewComment('');
        setChoiceOptions('');
        setMode('text');
        toast.success('Choice board added');
      } catch {
        toast.error('Failed to create choice board');
      }
    }
  };

  return (
    <div className="space-y-4">
      {/* Mode selector */}
      <div className="flex gap-1 text-xs">
        {([['text', 'Text'], ['choice', 'Single Choice'], ['multi_choice', 'Multi Choice']] as const).map(([m, label]) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-2.5 py-1 rounded-md border transition-colors ${
              mode === m
                ? 'bg-blue-50 dark:bg-blue-900/30 border-blue-300 dark:border-blue-500/50 text-blue-700 dark:text-blue-300'
                : 'border-gray-200 dark:border-gray-600 text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-700'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Input */}
      <div>
        <MentionInput
          value={newComment}
          onChange={setNewComment}
          members={members}
          placeholder={mode === 'text' ? 'Write a comment... (use @ to mention)' : 'Enter the question or prompt...'}
          multiline
        />

        {/* Choice options */}
        {mode !== 'text' && (
          <div className="mt-2 space-y-2">
            <textarea
              value={choiceOptions}
              onChange={e => setChoiceOptions(e.target.value)}
              placeholder="Enter options, one per line..."
              rows={3}
              className="w-full px-3 py-2 text-sm border border-gray-200 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200"
            />
            <label className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
              <input
                type="checkbox"
                checked={allowFreeText}
                onChange={e => setAllowFreeText(e.target.checked)}
                className="rounded border-gray-300"
              />
              Allow free-text response
            </label>
          </div>
        )}

        <div className="flex justify-end mt-2">
          <button onClick={handleAddComment} className="btn btn-primary">
            {mode === 'text' ? 'Comment' : 'Create Choice Board'}
          </button>
        </div>
      </div>

      {/* Comments list */}
      <div className="space-y-3">
        {card.comments.map((comment) => (
          <div key={comment.id} className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3 text-gray-700 dark:text-gray-300">
            {/* Author */}
            <div className="flex items-center gap-1.5 mb-1.5">
              <span className="text-xs font-semibold text-gray-600 dark:text-gray-300">
                {resolveActorName(comment.author_id, members)}
              </span>
              <span className="text-xs text-gray-400 dark:text-gray-500">
                {new Date(comment.created_at).toLocaleDateString('en-US', {
                  day: '2-digit',
                  month: 'short',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </span>
            </div>

            {comment.comment_type && comment.comment_type !== 'text' ? (
              <ChoiceBoardRenderer comment={comment} api={api} card={card} setCard={setCard} members={members} />
            ) : (
              <Md>{comment.content}</Md>
            )}
            <SeenByIndicator itemId={comment.id} seenStatus={seenStatus} />
          </div>
        ))}

        {card.comments.length === 0 && (
          <p className="text-gray-400 dark:text-gray-500 text-sm text-center py-4">
            No comments yet
          </p>
        )}
      </div>
    </div>
  );
}
