/**
 * CreateCardModal - Modal for creating new cards (normal or bug)
 */

import React, { useEffect, useState, useMemo } from 'react';
import { X, Bug } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { useDashboardStore, useColumns } from '@/store/dashboard';
import type { CardStatus, CardPriority, CardType, BugSeverity, SpecSummary } from '@/types';
import { STATUS_LABELS, CARD_STATUSES, PRIORITY_LABELS, CARD_PRIORITIES, BUG_SEVERITY_LABELS } from '@/types';

interface CreateCardModalProps {
  boardId: string;
  initialStatus: CardStatus;
  onClose: () => void;
}

export function CreateCardModal({ boardId, initialStatus, onClose }: CreateCardModalProps) {
  const api = useDashboardApi();
  const { addCardToColumn } = useDashboardStore();
  const columns = useColumns();

  // All board cards for origin task selection
  const allBoardCards = useMemo(() => Object.values(columns).flat(), [columns]);

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [status, setStatus] = useState<CardStatus>(initialStatus);
  const [priority, setPriority] = useState<CardPriority>('none');
  const [assigneeId, setAssigneeId] = useState('');
  const [labels, setLabels] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [boardMembers, setBoardMembers] = useState<{ id: string; name: string }[]>([]);

  // Spec selection
  const [specs, setSpecs] = useState<SpecSummary[]>([]);
  const [selectedSpecId, setSelectedSpecId] = useState('');

  // Card type
  const [cardType, setCardType] = useState<CardType>('normal');

  // Test fields
  const [testScenarios, setTestScenarios] = useState<{ id: string; title: string; status: string }[]>([]);
  const [selectedScenarioIds, setSelectedScenarioIds] = useState<string[]>([]);

  // Bug fields
  const [originTaskId, setOriginTaskId] = useState('');
  const [severity, setSeverity] = useState<BugSeverity>('major');
  const [expectedBehavior, setExpectedBehavior] = useState('');
  const [observedBehavior, setObservedBehavior] = useState('');
  const [stepsToReproduce, setStepsToReproduce] = useState('');
  const [actionPlan, setActionPlan] = useState('');

  // Filter tasks by selected spec (for bug origin task picker)
  const tasksForSpec = useMemo(() => {
    if (!selectedSpecId) return allBoardCards;
    return allBoardCards.filter((c) => c.spec_id === selectedSpecId);
  }, [allBoardCards, selectedSpecId]);

  useEffect(() => {
    api.listAgentsForBoard(boardId)
      .then((agents) => setBoardMembers(agents.map((a) => ({ id: a.id, name: a.name }))))
      .catch(() => {});

    // Load specs that accept card creation (approved + validated + in_progress for tasks, + done for bugs)
    Promise.all([
      api.listSpecs(boardId, 'approved'),
      api.listSpecs(boardId, 'validated'),
      api.listSpecs(boardId, 'in_progress'),
      api.listSpecs(boardId, 'done'),
    ]).then(([approved, validated, inProgress, done]) => {
      setSpecs([...approved, ...validated, ...inProgress, ...done]);
    }).catch(() => {});
  }, [boardId]);

  // Load test scenarios when spec changes and card type is test
  useEffect(() => {
    if (cardType === 'test' && selectedSpecId) {
      api.getSpec(selectedSpecId).then((spec) => {
        const scenarios = (spec.test_scenarios || []).map((s: any) => ({
          id: s.id, title: s.title, status: s.status,
        }));
        setTestScenarios(scenarios);
      }).catch(() => setTestScenarios([]));
    } else {
      setTestScenarios([]);
      setSelectedScenarioIds([]);
    }
  }, [cardType, selectedSpecId]);

  // When card type changes to bug, auto-select spec from origin task
  useEffect(() => {
    if (cardType === 'bug' && originTaskId) {
      const originTask = allBoardCards.find((c) => c.id === originTaskId);
      if (originTask?.spec_id) {
        setSelectedSpecId(originTask.spec_id);
      }
    }
  }, [originTaskId, cardType, allBoardCards]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!title.trim()) {
      toast.error('Title is required');
      return;
    }
    if (!selectedSpecId) {
      toast.error('A spec must be selected');
      return;
    }
    if (cardType === 'test' && selectedScenarioIds.length === 0) {
      toast.error('Select at least one test scenario');
      return;
    }
    if (cardType === 'bug') {
      if (!originTaskId) {
        toast.error('Origin task is required for bug cards');
        return;
      }
      if (!expectedBehavior.trim()) {
        toast.error('Expected behavior is required');
        return;
      }
      if (!observedBehavior.trim()) {
        toast.error('Observed behavior is required');
        return;
      }
    }

    setIsLoading(true);

    try {
      const card = await api.createCard(boardId, {
        title: title.trim(),
        description: description.trim() || undefined,
        status,
        priority: priority !== 'none' ? priority : undefined,
        assignee_id: assigneeId || undefined,
        labels: labels ? labels.split(',').map((l) => l.trim()).filter(Boolean) : undefined,
        // Spec
        spec_id: selectedSpecId,
        // Card type
        ...(cardType !== 'normal' ? { card_type: cardType } : {}),
        // Test fields
        ...(cardType === 'test' ? {
          test_scenario_ids: selectedScenarioIds,
        } : {}),
        // Bug fields
        ...(cardType === 'bug' ? {
          origin_task_id: originTaskId,
          severity,
          expected_behavior: expectedBehavior.trim(),
          observed_behavior: observedBehavior.trim(),
          steps_to_reproduce: stepsToReproduce.trim() || undefined,
          action_plan: actionPlan.trim() || undefined,
        } : {}),
      } as any);

      addCardToColumn({
        id: card.id,
        board_id: card.board_id,
        spec_id: card.spec_id,
        title: card.title,
        description: card.description,
        status: card.status,
        priority: card.priority,
        position: card.position,
        assignee_id: card.assignee_id,
        created_by: card.created_by,
        created_at: card.created_at,
        updated_at: card.updated_at,
        due_date: card.due_date,
        labels: card.labels,
        test_scenario_ids: card.test_scenario_ids,
        conclusions: card.conclusions,
        card_type: card.card_type,
        origin_task_id: card.origin_task_id,
        severity: card.severity,
        linked_test_task_ids: card.linked_test_task_ids,
      });

      toast.success('Card created');
      onClose();
    } catch (error: any) {
      const detail = error?.message || 'Failed to create card';
      toast.error(detail);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content max-w-lg max-h-[90vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="font-semibold text-lg">New Card</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-200 dark:hover:bg-gray-700 rounded">
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col min-h-0 flex-1">
          <div className="modal-body space-y-4 overflow-y-auto flex-1">

            {/* Card Type toggle */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Type
              </label>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setCardType('normal')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ${
                    cardType === 'normal'
                      ? 'bg-blue-50 border-blue-300 text-blue-700 dark:bg-blue-900/30 dark:border-blue-600 dark:text-blue-300'
                      : 'bg-white border-gray-300 text-gray-600 dark:bg-gray-700 dark:border-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-600'
                  }`}
                >
                  Task
                </button>
                <button
                  type="button"
                  onClick={() => setCardType('test')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ${
                    cardType === 'test'
                      ? 'bg-purple-50 border-purple-300 text-purple-700 dark:bg-purple-900/30 dark:border-purple-600 dark:text-purple-300'
                      : 'bg-white border-gray-300 text-gray-600 dark:bg-gray-700 dark:border-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-600'
                  }`}
                >
                  Test
                </button>
                <button
                  type="button"
                  onClick={() => setCardType('bug')}
                  className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border transition-colors inline-flex items-center justify-center gap-1.5 ${
                    cardType === 'bug'
                      ? 'bg-red-50 border-red-300 text-red-700 dark:bg-red-900/30 dark:border-red-600 dark:text-red-300'
                      : 'bg-white border-gray-300 text-gray-600 dark:bg-gray-700 dark:border-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-600'
                  }`}
                >
                  <Bug size={14} />
                  Bug
                </button>
              </div>
            </div>

            {/* Spec selector */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Spec *
              </label>
              <select
                value={selectedSpecId}
                onChange={(e) => setSelectedSpecId(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                disabled={cardType === 'bug' && !!originTaskId}
              >
                <option value="">Select a spec...</option>
                {specs
                  .filter((s) => cardType === 'bug'
                    ? ['approved', 'in_progress', 'done'].includes(s.status)
                    : cardType === 'test'
                    ? ['approved', 'validated', 'in_progress'].includes(s.status)
                    : ['approved', 'in_progress'].includes(s.status))
                  .map((s) => (
                  <option key={s.id} value={s.id}>{s.title} ({s.status})</option>
                ))}
              </select>
              {cardType === 'bug' && originTaskId && (
                <p className="text-xs text-gray-400 mt-0.5">Auto-resolved from origin task</p>
              )}
            </div>

            {/* Test: Scenario selector */}
            {cardType === 'test' && selectedSpecId && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Test Scenarios * <span className="text-xs text-gray-400 font-normal">({selectedScenarioIds.length} selected, max 3)</span>
                </label>
                {testScenarios.length === 0 ? (
                  <p className="text-xs text-gray-400">No test scenarios in this spec</p>
                ) : (
                  <div className="max-h-40 overflow-y-auto border border-gray-200 dark:border-gray-600 rounded-lg p-2 space-y-1">
                    {testScenarios.map((s) => (
                      <label
                        key={s.id}
                        className={`flex items-center gap-2 p-1.5 rounded text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 ${
                          selectedScenarioIds.includes(s.id) ? 'bg-purple-50 dark:bg-purple-900/20' : ''
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={selectedScenarioIds.includes(s.id)}
                          onChange={(e) => {
                            if (e.target.checked) {
                              if (selectedScenarioIds.length < 3) {
                                setSelectedScenarioIds([...selectedScenarioIds, s.id]);
                              } else {
                                toast.error('Max 3 scenarios per card');
                              }
                            } else {
                              setSelectedScenarioIds(selectedScenarioIds.filter((id) => id !== s.id));
                            }
                          }}
                          className="rounded border-gray-300 dark:border-gray-600"
                        />
                        <span className="text-gray-800 dark:text-gray-200 truncate">{s.title}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Bug: Origin Task selector */}
            {cardType === 'bug' && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Origin Task *
                </label>
                <select
                  value={originTaskId}
                  onChange={(e) => setOriginTaskId(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                >
                  <option value="">Select origin task...</option>
                  {(selectedSpecId ? tasksForSpec : allBoardCards)
                    .filter((c) => c.card_type !== 'bug')
                    .map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.title} ({c.status})
                      </option>
                    ))}
                </select>
                {!selectedSpecId && (
                  <p className="text-xs text-amber-500 mt-0.5">Select a spec first to filter tasks</p>
                )}
              </div>
            )}

            {/* Title */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Title *
              </label>
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value.slice(0, 200))}
                maxLength={200}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                placeholder={cardType === 'bug' ? 'E.g.: Login returns 500 with uppercase email' : 'E.g.: Implement feature X'}
                autoFocus
              />
              <p className="text-xs text-gray-400 mt-0.5 text-right">{title.length}/200</p>
            </div>

            {/* Description */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Description
              </label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                rows={2}
                placeholder="Describe the card..."
              />
            </div>

            {/* Bug-specific fields */}
            {cardType === 'bug' && (
              <>
                {/* Severity */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Severity *
                  </label>
                  <div className="flex gap-2">
                    {(['critical', 'major', 'minor'] as BugSeverity[]).map((sev) => (
                      <button
                        key={sev}
                        type="button"
                        onClick={() => setSeverity(sev)}
                        className={`flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                          severity === sev
                            ? sev === 'critical' ? 'bg-red-500 text-white ring-2 ring-red-300'
                            : sev === 'major' ? 'bg-orange-500 text-white ring-2 ring-orange-300'
                            : 'bg-yellow-500 text-white ring-2 ring-yellow-300'
                            : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                        }`}
                      >
                        {BUG_SEVERITY_LABELS[sev]}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Expected Behavior */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Expected Behavior *
                  </label>
                  <textarea
                    value={expectedBehavior}
                    onChange={(e) => setExpectedBehavior(e.target.value)}
                    className="w-full px-3 py-2 border border-green-200 rounded-lg bg-green-50 dark:bg-green-900/10 dark:border-green-700/40 resize-none"
                    rows={2}
                    placeholder="What should happen..."
                  />
                </div>

                {/* Observed Behavior */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Observed Behavior *
                  </label>
                  <textarea
                    value={observedBehavior}
                    onChange={(e) => setObservedBehavior(e.target.value)}
                    className="w-full px-3 py-2 border border-red-200 rounded-lg bg-red-50 dark:bg-red-900/10 dark:border-red-700/40 resize-none"
                    rows={2}
                    placeholder="What actually happens..."
                  />
                </div>

                {/* Steps to Reproduce */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Steps to Reproduce
                  </label>
                  <textarea
                    value={stepsToReproduce}
                    onChange={(e) => setStepsToReproduce(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600 resize-none"
                    rows={2}
                    placeholder="1. Do X  2. Click Y  3. Observe error"
                  />
                </div>

                {/* Action Plan */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Action Plan
                  </label>
                  <textarea
                    value={actionPlan}
                    onChange={(e) => setActionPlan(e.target.value)}
                    className="w-full px-3 py-2 border border-blue-200 rounded-lg bg-blue-50 dark:bg-blue-900/10 dark:border-blue-700/40 resize-none"
                    rows={2}
                    placeholder="How to fix..."
                  />
                </div>
              </>
            )}

            {/* Status + Priority row */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Status
                </label>
                <select
                  value={status}
                  onChange={(e) => setStatus(e.target.value as CardStatus)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                >
                  {CARD_STATUSES.map((s) => (
                    <option key={s} value={s}>{STATUS_LABELS[s]}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Priority
                </label>
                <select
                  value={priority}
                  onChange={(e) => setPriority(e.target.value as CardPriority)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                >
                  {CARD_PRIORITIES.map((p) => (
                    <option key={p} value={p}>{PRIORITY_LABELS[p]}</option>
                  ))}
                </select>
              </div>
            </div>

            {/* Assignee */}
            {boardMembers.length > 0 && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Assignee
                </label>
                <select
                  value={assigneeId}
                  onChange={(e) => setAssigneeId(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                >
                  <option value="">None</option>
                  {boardMembers.map((m) => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                  ))}
                </select>
              </div>
            )}

            {/* Labels */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Labels
              </label>
              <input
                type="text"
                value={labels}
                onChange={(e) => setLabels(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg dark:bg-gray-700 dark:border-gray-600"
                placeholder="Comma separated: feature, urgent"
              />
            </div>
          </div>

          <div className="modal-footer">
            <button type="button" onClick={onClose} className="btn btn-secondary">
              Cancel
            </button>
            <button type="submit" disabled={isLoading} className="btn btn-primary">
              {isLoading ? 'Creating...' : cardType === 'bug' ? 'Create Bug' : 'Create Card'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
