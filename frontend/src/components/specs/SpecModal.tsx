/**
 * SpecModal - View and edit a spec, derive cards, manage knowledge bases
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  X,
  ChevronRight,
  CheckCircle2,
  Circle,
  Clock,
  Ban,
  FileText,
  Settings,
  Target,
  Link2,
  BookOpen,
  Plus,
  Trash2,
  MessageCircleQuestion,
  Send,
  History,
  Lightbulb,
  Layers,
  FlaskConical,
  Link,
  Unlink,
  Monitor,
  RefreshCw,
  Maximize2,
  Minimize2,
  Scale,
  FileCode,
  GitBranch,
  Download,
  Network,
  ShieldCheck,
  Gauge,
  Pencil,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { exportSpec, downloadMarkdown, markdownFilenameForSpec } from '@/lib/exportMarkdown';
import { getErrorMessage } from '@/lib/getErrorMessage';
import { useDashboardApi } from '@/services/api';
import { useCurrentBoard } from '@/store/dashboard';
import { openLineageGraph } from '@/components/traceability';
import type {
  ApiContract,
  BusinessRule,
  IntegrationRequirement,
  ObservabilityRequirement,
  Spec,
  SpecStatus,
  SpecQAItem,
  SpecHistoryEntry,
  SpecStructuredEntityOperation,
  SpecStructuredEntityType,
  TechnicalRequirement,
  TestScenario,
  TestScenarioType,
  BoardSettings,
  Decision,
} from '@/types';
import { SubmitSpecValidationModal } from './SubmitSpecValidationModal';
import { EvidenceBadge } from './EvidenceBadge';
import {
  SCENARIO_TYPES,
  ScenarioTypeBadge,
  isSupportedScenarioType,
} from './ScenarioTypeBadge';
import {
  TestScenarioPolicyCompliance,
  TestScenarioStatusBadge,
} from './TestScenarioPolicyCompliance';
import { persistTestScenariosWithWriteGuard } from './scenarioWriteGuard';
import { usePermissions } from '@/hooks/usePermissions';
import { MockupsTab } from './MockupsTab';
import { RulesTab } from './RulesTab';
import { ContractsTab } from './ContractsTab';
import { TechnicalRequirementsTab } from './TechnicalRequirementsTab';
import { DecisionsTab } from './DecisionsTab';
import { IntegrationRequirementsTab } from './IntegrationRequirementsTab';
import { ObservabilityRequirementsTab } from './ObservabilityRequirementsTab';
import { KGValidationTab } from './KGValidationTab';
import { SpecValidationPanel } from './SpecValidationPanel';
import {
  isAllowedTransitionActionable,
  policyTransitionRejectionMessage,
  readPolicyTransitionRejection,
  requirePolicyTransitionEnvelope,
  type PolicyTransitionRejection,
  type PolicyTransitionPreviewLoadState,
} from '@/components/policy-compliance';
import { isSpecValidationAvailable } from './specValidationAvailability';
import { ValidationErrorDisplay } from './ValidationErrorDisplay';
import { SprintSuggestionModal } from '@/components/sprints/SprintSuggestionModal';
import { SPEC_STATUSES, SPEC_STATUS_LABELS } from '@/types';
import { MentionInput, type Mentionable } from '@/components/shared/MentionInput';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { CancellationDetails, CancellationReasonDialog } from '@/components/shared/CancellationReasonDialog';
import { IdeationModal } from '@/components/ideations/IdeationModal';
import { RefinementModal } from '@/components/refinements/RefinementModal';
import { EditableField } from '@/components/shared/EditableField';
import { ValidationGateOverride } from '@/components/shared/ValidationGateOverride';
import { ActivityHistoryList } from '@/components/shared/ActivityHistoryList';
import { SpecEditionLabel } from './SpecEditionLabel';
import { ArchitectureTab } from '@/components/architecture';
import {
  getAcceptanceCriterionLabel,
  isAcceptanceCriterionLinked,
  normalizeAcceptanceCriteria,
} from './acceptanceCriteriaCoverage';
import { ResourceGateDisclosure } from '@/components/resources/ResourceGateDisclosure';
import { KnowledgeWorkspace } from '@/components/resources/KnowledgeWorkspace';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { useOptionalModalStack } from '@/contexts/ModalStackContext';
import { openContextualHelp } from '@/components/help';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';

interface SpecModalProps {
  specId: string;
  boardId: string;
  onClose: () => void;
  onEscape?: () => void;
  onChanged: () => void;
}

type ModalTab =
  | 'details'
  | 'tests'
  | 'rules'
  | 'contracts'
  | 'irs'
  | 'ors'
  | 'trs'
  | 'decisions'
  | 'resources'
  | 'qa'
  | 'references'
  | 'sprints'
  | 'kg'
  | 'validation'
  | 'activity';

type ResourceSubTab = 'mockups' | 'knowledge' | 'architecture';
type ReferenceSubTab = 'origin' | 'cards';

const STATUS_ICON: Record<SpecStatus, React.ReactNode> = {
  draft: <FileText size={14} />,
  review: <Clock size={14} />,
  approved: <CheckCircle2 size={14} />,
  validated: <CheckCircle2 size={14} />,
  in_progress: <Settings size={14} />,
  done: <CheckCircle2 size={14} />,
  cancelled: <Ban size={14} />,
};

const STATUS_COLORS: Record<SpecStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  review: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  approved: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  validated: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
  in_progress: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  done: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  cancelled: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

const CARD_STATUS_COLORS: Record<string, string> = {
  not_started: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
  started: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300',
  in_progress: 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900/40 dark:text-indigo-300',
  on_hold: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-300',
  done: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300',
  cancelled: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
};

function EditableRequirementsList({
  title,
  icon,
  items,
  onUpdate,
  placeholder,
  renderItemExtra,
  canAdd = true,
  canEdit = true,
  canRemove = true,
  onAddItem,
  onEditItem,
  onOpenItemEditor,
}: {
  title: string;
  icon: React.ReactNode;
  items: string[] | null;
  onUpdate: (items: string[]) => void;
  placeholder: string;
  renderItemExtra?: (item: string, index: number) => React.ReactNode;
  canAdd?: boolean;
  canEdit?: boolean;
  canRemove?: boolean;
  onAddItem?: () => void;
  onEditItem?: (index: number, value: string) => void | Promise<void>;
  onOpenItemEditor?: (index: number) => void;
}) {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState('');

  const add = () => {
    const trimmed = draft.trim();
    if (trimmed) {
      onUpdate([...(items || []), trimmed]);
      setDraft('');
    }
  };

  const remove = (idx: number) => {
    onUpdate((items || []).filter((_, i) => i !== idx));
  };

  const startEdit = (idx: number, value: string) => {
    setEditingIndex(idx);
    setEditDraft(value);
  };

  const cancelEdit = () => {
    setEditingIndex(null);
    setEditDraft('');
  };

  const saveEdit = async () => {
    if (editingIndex === null) return;
    const trimmed = editDraft.trim();
    if (!trimmed) return;
    if (onEditItem) {
      await onEditItem(editingIndex, trimmed);
    } else {
      onUpdate((items || []).map((item, index) => (index === editingIndex ? trimmed : item)));
    }
    cancelEdit();
  };

  const hasItems = items && items.length > 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-1.5">
          {icon} {title}
          {hasItems && <span className="text-xs font-normal text-gray-400">({items.length})</span>}
        </h4>
        {!editing && canAdd && (
          <button
            onClick={() => {
              if (onAddItem) {
                onAddItem();
              } else {
                setEditing(true);
              }
            }}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-0.5"
          >
            <Plus size={12} /> Add
          </button>
        )}
      </div>

      {hasItems ? (
        <ol className="space-y-1.5 ml-1">
          {items.map((item, i) => (
            <li key={i} className="flex items-start gap-2 text-sm text-gray-600 dark:text-gray-400 group">
              <span className="text-xs text-gray-400 mt-0.5 w-4 shrink-0">{i + 1}.</span>
              {editingIndex === i ? (
                <div className="flex-1 flex gap-2">
                  <input
                    type="text"
                    value={editDraft}
                    onChange={(event) => setEditDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') void saveEdit();
                      if (event.key === 'Escape') {
                        event.preventDefault();
                        event.stopPropagation();
                        cancelEdit();
                      }
                    }}
                    className="flex-1 px-2 py-1 border border-gray-300 rounded-md text-sm dark:bg-gray-700 dark:border-gray-600"
                    autoFocus
                  />
                  <button onClick={() => void saveEdit()} disabled={!editDraft.trim()} className="btn btn-primary text-xs">Save</button>
                  <button onClick={cancelEdit} className="btn btn-secondary text-xs">Cancel</button>
                </div>
              ) : (
                <>
                  <span className="flex-1">{item}</span>
                  {renderItemExtra?.(item, i)}
                  {canEdit && (
                    <button
                      onClick={() => {
                        if (onOpenItemEditor) {
                          onOpenItemEditor(i);
                        } else {
                          startEdit(i, item);
                        }
                      }}
                      className="opacity-0 group-hover:opacity-100 p-0.5 text-blue-400 hover:text-blue-600 transition-opacity"
                      title={onOpenItemEditor ? 'Edit details' : 'Edit'}
                    >
                      <Pencil size={12} />
                    </button>
                  )}
                  {canRemove && (
                    <button
                      onClick={() => remove(i)}
                      className="opacity-0 group-hover:opacity-100 p-0.5 text-red-400 hover:text-red-600 transition-opacity"
                      title="Remove"
                    >
                      <Trash2 size={12} />
                    </button>
                  )}
                </>
              )}
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-xs text-gray-400 dark:text-gray-500 italic ml-1">
          No {title.toLowerCase()} defined yet
        </p>
      )}

      {editing && (
        <div className="flex gap-2 mt-2">
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') add();
              if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                setEditing(false);
                setDraft('');
              }
            }}
            placeholder={placeholder}
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
            autoFocus
          />
          <button onClick={add} disabled={!draft.trim()} className="btn btn-primary text-xs">Add</button>
          <button onClick={() => { setEditing(false); setDraft(''); }} className="btn btn-secondary text-xs">Done</button>
        </div>
      )}
    </div>
  );
}

function newRequirementId(prefix: 'ir' | 'or'): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
}

function requirementDisplayText(item: { title?: string; description?: string }): string {
  return (item.title || item.description || '').trim();
}

function reconcileIntegrationRequirements(
  current: IntegrationRequirement[] | null,
  items: string[],
): IntegrationRequirement[] {
  const existing = current || [];
  const active = existing.filter((item) => item.status === 'active');
  const inactive = existing.filter((item) => item.status !== 'active');
  const byText = new Map(active.map((item) => [requirementDisplayText(item), item]));

  return [
    ...inactive,
    ...items.map((text) => {
      const trimmed = text.trim();
      const prior = byText.get(trimmed);
      if (prior) return prior;
      const next: IntegrationRequirement = {
        id: newRequirementId('ir'),
        title: trimmed,
        integration_type: 'other',
        description: trimmed,
        provider: null,
        consumer: null,
        contract_ref: null,
        endpoint: null,
        method: null,
        data_contract: null,
        linked_requirements: null,
        linked_api_contracts: null,
        linked_task_ids: null,
        status: 'active',
        notes: null,
      };
      return next;
    }),
  ];
}

function reconcileObservabilityRequirements(
  current: ObservabilityRequirement[] | null,
  items: string[],
): ObservabilityRequirement[] {
  const existing = current || [];
  const active = existing.filter((item) => item.status === 'active');
  const inactive = existing.filter((item) => item.status !== 'active');
  const byText = new Map(active.map((item) => [requirementDisplayText(item), item]));

  return [
    ...inactive,
    ...items.map((text) => {
      const trimmed = text.trim();
      const prior = byText.get(trimmed);
      if (prior) return prior;
      const next: ObservabilityRequirement = {
        id: newRequirementId('or'),
        title: trimmed,
        signal_type: 'other',
        description: trimmed,
        target: null,
        metric_name: null,
        threshold: null,
        severity: null,
        owner: null,
        linked_requirements: null,
        linked_integration_requirements: null,
        linked_task_ids: null,
        status: 'active',
        notes: null,
      };
      return next;
    }),
  ];
}

type StructuredObjectEntity =
  | BusinessRule
  | ApiContract
  | TechnicalRequirement
  | IntegrationRequirement
  | ObservabilityRequirement
  | Decision;

type StructuredCollectionField =
  | 'business_rules'
  | 'api_contracts'
  | 'technical_requirements'
  | 'integration_requirements'
  | 'observability_requirements'
  | 'decisions';

const STRUCTURED_ENTITY_BY_FIELD: Record<StructuredCollectionField, SpecStructuredEntityType> = {
  business_rules: 'business_rule',
  api_contracts: 'api_contract',
  technical_requirements: 'technical_requirement',
  integration_requirements: 'integration_requirement',
  observability_requirements: 'observability_requirement',
  decisions: 'decision',
};

function normalizeTextEntity(item: unknown, index: number): { id: string; text: string; status: string } {
  if (item && typeof item === 'object') {
    const record = item as Record<string, unknown>;
    return {
      id: String(record.id || index),
      text: String(record.text || record.title || ''),
      status: String(record.status || 'active'),
    };
  }
  return { id: String(index), text: String(item || ''), status: 'active' };
}

function stableEntityPayload(item: StructuredObjectEntity): Record<string, unknown> {
  return JSON.parse(JSON.stringify(item)) as Record<string, unknown>;
}

function TestScenariosTab({
  spec,
  onUpdate,
  onSpecUpdate,
  onSpecRefreshed,
  canReadPolicyCompliance,
  policyRefreshKey,
}: {
  spec: Spec;
  onUpdate: (scenarios: TestScenario[]) => void;
  onSpecUpdate: (data: Record<string, unknown>) => Promise<void>;
  onSpecRefreshed: (updated: Spec) => void;
  canReadPolicyCompliance: boolean;
  policyRefreshKey: number;
}) {
  const api = useDashboardApi();
  const [adding, setAdding] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [linkingScenarioId, setLinkingScenarioId] = useState<string | null>(null);

  // New scenario form
  const [newTitle, setNewTitle] = useState('');
  const [newType, setNewType] = useState<TestScenarioType>('integration');
  const [newGiven, setNewGiven] = useState('');
  const [newWhen, setNewWhen] = useState('');
  const [newThen, setNewThen] = useState('');
  const [newNotes, setNewNotes] = useState('');
  const [newCriteria, setNewCriteria] = useState<string[]>([]);

  const scenarios = spec.test_scenarios || [];
  const criteria = normalizeAcceptanceCriteria((spec.acceptance_criteria || []) as unknown[]);

  const handleAdd = () => {
    if (!newTitle.trim() || !newGiven.trim() || !newWhen.trim() || !newThen.trim()) return;
    const id = `ts_${Date.now()}`;
    const scenario: TestScenario = {
      id,
      title: newTitle.trim(),
      linked_criteria: newCriteria.length > 0 ? newCriteria : null,
      scenario_type: newType,
      given: newGiven.trim(),
      when: newWhen.trim(),
      then: newThen.trim(),
      notes: newNotes.trim() || null,
      status: 'draft',
      linked_task_ids: null,
    };
    onUpdate([...scenarios, scenario]);
    setAdding(false);
    setNewTitle(''); setNewType('integration'); setNewGiven(''); setNewWhen(''); setNewThen(''); setNewNotes(''); setNewCriteria([]);
  };

  const handleRemove = (id: string) => {
    onUpdate(scenarios.filter((s) => s.id !== id));
  };

  // Coverage matrix
  const coverageMap = new Map<string, string[]>();
  criteria.forEach((criterion) => {
    const covering = scenarios.filter((scenario) =>
      isAcceptanceCriterionLinked(scenario.linked_criteria, criterion, criteria)
    );
    coverageMap.set(criterion.key, covering.map((scenario) => scenario.id));
  });
  const uncoveredCriteria = criteria.filter((criterion) => !coverageMap.get(criterion.key)?.length);

  return (
    <div className="space-y-4">
      {/* Coverage summary */}
      {criteria.length > 0 && (() => {
        const coveredCount = criteria.length - uncoveredCriteria.length;
        const coveragePct = Math.round((coveredCount / criteria.length) * 100);
        return (
          <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                AC Coverage ({coveredCount}/{criteria.length})
              </h4>
              {coveredCount === criteria.length ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">
                  100% covered
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 font-medium">
                  {coveragePct}% covered
                </span>
              )}
            </div>
            {/* Progress bar */}
            <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-2">
              <div
                className={`h-full transition-all duration-500 rounded-full ${coveredCount === criteria.length ? 'bg-green-500' : 'bg-amber-500'}`}
                style={{ width: `${coveragePct}%` }}
              />
            </div>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {criteria.map((criterion) => {
                const covering = coverageMap.get(criterion.key) || [];
                const covered = covering.length > 0;
                return (
                  <div key={criterion.key} className="flex items-start gap-2 text-xs">
                    <span className={`mt-0.5 w-3 h-3 rounded-full shrink-0 ${covered ? 'bg-green-500' : 'bg-red-400'}`} />
                    <span className={`flex-1 line-clamp-1 ${covered ? 'text-gray-600 dark:text-gray-400' : 'text-red-600 dark:text-red-400 font-medium'}`}>
                      {criterion.label}
                    </span>
                    <span className="text-gray-400 shrink-0">{covering.length} test{covering.length !== 1 ? 's' : ''}</span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })()}

      {/* Skip test coverage toggle */}
      <div className="flex items-center justify-between px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/20">
        <div>
          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Skip test coverage requirement</span>
          <p className="text-[10px] text-gray-400">Allow moving spec to Done without full test coverage</p>
        </div>
        <button
          onClick={() => onSpecUpdate({ skip_test_coverage: !spec.skip_test_coverage })}
          className={`relative w-10 h-5 rounded-full transition-colors ${spec.skip_test_coverage ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
        >
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${spec.skip_test_coverage ? 'translate-x-5' : ''}`} />
        </button>
      </div>

      {/* Scenarios list */}
      {scenarios.length === 0 && !adding && (
        <div className="text-center py-6">
          <FlaskConical size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No test scenarios</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Define test scenarios to validate acceptance criteria</p>
        </div>
      )}

      {scenarios.map((scenario) => {
        const isExpanded = expandedId === scenario.id;
        const linkedCards = scenario.linked_task_ids?.length || 0;
        return (
          <div key={scenario.id} className={`border rounded-lg overflow-hidden ${linkedCards > 0 ? 'border-gray-200 dark:border-gray-700' : 'border-amber-300 dark:border-amber-700 border-dashed'}`}>
            <div
              className={`flex items-center gap-2 px-3 py-2 cursor-pointer ${linkedCards > 0 ? 'bg-gray-50 dark:bg-gray-700/50' : 'bg-amber-50/50 dark:bg-amber-900/10'}`}
              onClick={() => setExpandedId(isExpanded ? null : scenario.id)}
            >
              <FlaskConical size={14} className={linkedCards > 0 ? 'text-violet-500 shrink-0' : 'text-amber-500 shrink-0'} />
              <span className="text-sm font-medium text-gray-900 dark:text-white truncate flex-1">{scenario.title}</span>
              <ScenarioTypeBadge scenarioType={scenario.scenario_type} />
              <TestScenarioStatusBadge status={scenario.status} />
              <EvidenceBadge scenario={scenario} />
              {linkedCards > 0 ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 font-medium">
                  {linkedCards} task{linkedCards !== 1 ? 's' : ''}
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-600 dark:bg-amber-900/40 dark:text-amber-400 font-medium animate-pulse">
                  no tasks
                </span>
              )}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  handleRemove(scenario.id);
                }}
                className="p-0.5 text-gray-400 hover:text-red-500"
                aria-label={`Delete test scenario ${scenario.title}`}
              >
                <Trash2 size={12} />
              </button>
            </div>
            {isExpanded && (
              <div className="px-3 py-2 space-y-2 text-sm">
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <span className="text-[10px] font-semibold text-green-600 uppercase">Given</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{scenario.given}</p>
                  </div>
                  <div>
                    <span className="text-[10px] font-semibold text-blue-600 uppercase">When</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{scenario.when}</p>
                  </div>
                  <div>
                    <span className="text-[10px] font-semibold text-violet-600 uppercase">Then</span>
                    <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">{scenario.then}</p>
                  </div>
                </div>
                {scenario.notes && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 italic border-l-2 border-gray-300 dark:border-gray-600 pl-2">{scenario.notes}</p>
                )}
                {scenario.linked_criteria && scenario.linked_criteria.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-gray-400 mr-1">Validates:</span>
                    {scenario.linked_criteria.map((reference, i) => {
                      const label = getAcceptanceCriterionLabel(reference, criteria);
                      return (
                        <span key={`${reference}-${i}`} className="text-[10px] px-1.5 py-0.5 rounded bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300">
                          {label.length > 60 ? label.slice(0, 57) + '...' : label}
                        </span>
                      );
                    })}
                  </div>
                )}
                {/* Linked tasks */}
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-gray-400">Linked tasks:</span>
                    <button
                      onClick={() => setLinkingScenarioId(linkingScenarioId === scenario.id ? null : scenario.id)}
                      className="text-[10px] text-blue-500 hover:text-blue-600 dark:text-blue-400"
                    >
                      {linkingScenarioId === scenario.id ? 'Cancel' : '+ Link task'}
                    </button>
                  </div>
                  {scenario.linked_task_ids && scenario.linked_task_ids.length > 0 && (
                    <div className="space-y-1">
                      {scenario.linked_task_ids.map((taskId) => {
                        const card = spec.cards?.find((c) => c.id === taskId);
                        return (
                          <div key={taskId} className="flex items-center justify-between px-2 py-1 rounded bg-blue-50 dark:bg-blue-900/10 text-xs group">
                            <span className="text-gray-700 dark:text-gray-300 truncate">
                              {card ? card.title : taskId.slice(0, 12) + '...'}
                            </span>
                            <div className="flex items-center gap-1">
                              {card && (
                                <span className={`text-[10px] px-1 py-0.5 rounded ${
                                  card.status === 'done' ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' :
                                  card.status === 'in_progress' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' :
                                  'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                                }`}>
                                  {card.status.replace('_', ' ')}
                                </span>
                              )}
                              <button
                                onClick={async () => {
                                  try {
                                    await api.unlinkTaskFromScenario(spec.id, scenario.id, taskId);
                                    const updated = await api.getSpec(spec.id);
                                    onSpecRefreshed(updated);
                                    toast.success('Task unlinked');
                                  } catch { toast.error('Failed to unlink'); }
                                }}
                                className="p-0.5 text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                                title="Unlink task"
                              >
                                <Unlink size={10} />
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {(!scenario.linked_task_ids || scenario.linked_task_ids.length === 0) && (
                    <p className="text-[10px] text-gray-400 italic">No tasks linked to this scenario yet</p>
                  )}
                  {/* Task picker */}
                  {linkingScenarioId === scenario.id && spec.cards && spec.cards.length > 0 && (
                    <div className="mt-1 border border-gray-200 dark:border-gray-700 rounded p-1.5 max-h-32 overflow-y-auto space-y-0.5">
                      {spec.cards
                        .filter((c) => !(scenario.linked_task_ids || []).includes(c.id))
                        .map((c) => (
                          <button
                            key={c.id}
                            onClick={async () => {
                              try {
                                await api.linkTaskToScenario(spec.id, scenario.id, c.id);
                                const updated = await api.getSpec(spec.id);
                                onSpecRefreshed(updated);
                                setLinkingScenarioId(null);
                                toast.success('Task linked');
                              } catch { toast.error('Failed to link'); }
                            }}
                            className="w-full text-left px-2 py-1 rounded text-[11px] text-gray-600 dark:text-gray-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 truncate flex items-center gap-1"
                          >
                            <Link size={9} className="shrink-0 text-gray-400" />
                            {c.title}
                          </button>
                        ))}
                      {spec.cards.filter((c) => !(scenario.linked_task_ids || []).includes(c.id)).length === 0 && (
                        <p className="text-[10px] text-gray-400 italic px-1">All cards already linked</p>
                      )}
                    </div>
                  )}
                </div>
                <TestScenarioPolicyCompliance
                  boardId={spec.board_id}
                  specId={spec.id}
                  specArchived={Boolean(spec.archived)}
                  scenario={scenario}
                  canReadPolicyCompliance={canReadPolicyCompliance}
                  refreshKey={policyRefreshKey}
                  onSpecRefreshed={onSpecRefreshed}
                />
              </div>
            )}
          </div>
        );
      })}

      {/* Add scenario form */}
      {adding ? (
        <div className="border border-violet-200 dark:border-violet-700 rounded-lg p-3 space-y-2 bg-violet-50/50 dark:bg-violet-900/10">
          <div className="flex gap-2">
            <input type="text" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Scenario title" className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" autoFocus />
            <select
              value={newType}
              onChange={(e) => {
                if (isSupportedScenarioType(e.target.value)) {
                  setNewType(e.target.value);
                }
              }}
              className="px-2 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
            >
              {SCENARIO_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <textarea value={newGiven} onChange={(e) => setNewGiven(e.target.value)} placeholder="Given: precondition..." className="px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
            <textarea value={newWhen} onChange={(e) => setNewWhen(e.target.value)} placeholder="When: action..." className="px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
            <textarea value={newThen} onChange={(e) => setNewThen(e.target.value)} placeholder="Then: expected result..." className="px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={2} />
          </div>
          <textarea value={newNotes} onChange={(e) => setNewNotes(e.target.value)} placeholder="Notes (optional)" className="w-full px-2 py-1.5 border border-gray-300 rounded-lg text-xs dark:bg-gray-700 dark:border-gray-600 resize-none" rows={1} />
          {/* Link to acceptance criteria */}
          {criteria.length > 0 && (
            <div>
              <span className="text-[10px] text-gray-500 dark:text-gray-400 block mb-1">Link to acceptance criteria:</span>
              <div className="flex flex-wrap gap-1">
                {criteria.map((criterion) => {
                  const isLinked = newCriteria.includes(criterion.reference);
                  return (
                    <button
                      key={criterion.key}
                      onClick={() => setNewCriteria(
                        isLinked
                          ? newCriteria.filter((reference) => reference !== criterion.reference)
                          : [...newCriteria, criterion.reference]
                      )}
                      className={`text-[10px] px-1.5 py-0.5 rounded transition-colors ${
                        isLinked
                          ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 ring-1 ring-green-400'
                          : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400 hover:bg-gray-200'
                      }`}
                    >
                      {criterion.label.length > 60 ? criterion.label.slice(0, 57) + '...' : criterion.label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <button onClick={() => setAdding(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button onClick={handleAdd} disabled={!newTitle.trim() || !newGiven.trim() || !newWhen.trim() || !newThen.trim()} className="btn btn-primary text-xs">Add Scenario</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-violet-600 dark:text-violet-400 hover:text-violet-800 dark:hover:text-violet-300">
          <Plus size={14} /> Add Test Scenario
        </button>
      )}
    </div>
  );
}

/* ============================================================
   History Tab
   ============================================================ */

function HistoryTab({ specId }: { specId: string }) {
  const api = useDashboardApi();
  const [entries, setEntries] = useState<SpecHistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => { load(); }, [specId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecHistory(specId);
      setEntries(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  return (
    <ActivityHistoryList
      entries={entries}
      loading={loading}
      versionLabel={(version) => ({
        text: `r${version}`,
        title: `Technical revision r${version}`,
      })}
    />
  );
}

/* ============================================================
   Q&A Tab
   ============================================================ */

function ChoiceOptionsDisplay({ choices, selected }: { choices: SpecQAItem['choices']; selected: string[] | null }) {
  if (!choices) return null;
  return (
    <div className="space-y-1 mt-1">
      {choices.map((opt) => {
        const isSelected = selected?.includes(opt.id);
        return (
          <div key={opt.id} className={`flex items-center gap-2 text-sm px-2 py-1 rounded ${
            isSelected
              ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300 font-medium'
              : 'text-gray-600 dark:text-gray-400'
          }`}>
            <span className={`w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 ${
              isSelected ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-600'
            }`}>
              {isSelected && <CheckCircle2 size={12} className="text-white" />}
            </span>
            {opt.label}
          </div>
        );
      })}
    </div>
  );
}

function ChoiceAnswerForm({
  qa,
  onAnswer,
  onCancel,
}: {
  qa: SpecQAItem;
  onAnswer: (qaId: string, answer: string | null, selected: string[] | null) => void;
  onCancel: () => void;
}) {
  const [sel, setSel] = useState<string[]>([]);
  const [freeText, setFreeText] = useState('');

  const toggleOption = (optId: string) => {
    // `single_choice` is an alias of `choice` — accept both for single-select.
    const isSingle = qa.question_type === 'choice' || qa.question_type === 'single_choice';
    if (isSingle) {
      setSel([optId]);
    } else {
      setSel((prev) => prev.includes(optId) ? prev.filter((s) => s !== optId) : [...prev, optId]);
    }
  };

  const canSubmit = sel.length > 0 || (qa.allow_free_text && freeText.trim());

  return (
    <div className="mt-2 space-y-2">
      <div className="space-y-1">
        {qa.choices?.map((opt) => (
          <button
            key={opt.id}
            onClick={() => toggleOption(opt.id)}
            className={`flex items-center gap-2 w-full text-sm text-left px-2 py-1.5 rounded transition-colors ${
              sel.includes(opt.id)
                ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300'
                : 'bg-gray-50 dark:bg-gray-700/50 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
          >
            <span className={`w-4 h-4 rounded-full border-2 flex items-center justify-center shrink-0 ${
              sel.includes(opt.id) ? 'border-blue-500 bg-blue-500' : 'border-gray-300 dark:border-gray-600'
            }`}>
              {sel.includes(opt.id) && <CheckCircle2 size={10} className="text-white" />}
            </span>
            {opt.label}
          </button>
        ))}
      </div>
      {qa.allow_free_text && (
        <input
          type="text"
          value={freeText}
          onChange={(e) => setFreeText(e.target.value)}
          placeholder="Additional comment..."
          className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600"
        />
      )}
      <div className="flex justify-end gap-2">
        <button onClick={onCancel} className="btn btn-secondary text-xs">Cancel</button>
        <button
          onClick={() => onAnswer(qa.id, freeText.trim() || null, sel.length > 0 ? sel : null)}
          disabled={!canSubmit}
          className="btn btn-primary text-xs"
        >
          Submit
        </button>
      </div>
    </div>
  );
}

function QATab({ specId, mentionables }: { specId: string; mentionables: Mentionable[] }) {
  const api = useDashboardApi();
  const [items, setItems] = useState<SpecQAItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [answeringId, setAnsweringId] = useState<string | null>(null);
  const [answerDraft, setAnswerDraft] = useState('');

  // Ask question form
  const [askMode, setAskMode] = useState<'text' | 'choice'>('text');
  const [newQuestion, setNewQuestion] = useState('');
  const [newOptions, setNewOptions] = useState('');
  const [newMulti, setNewMulti] = useState(false);
  const [newAllowFreeText, setNewAllowFreeText] = useState(false);

  useEffect(() => { load(); }, [specId]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listSpecQA(specId);
      setItems(data);
    } catch { /* ignore */ } finally { setLoading(false); }
  };

  const handleAskText = async () => {
    if (!newQuestion.trim()) return;
    try {
      await api.createSpecQuestion(specId, newQuestion.trim());
      setNewQuestion('');
      toast.success('Question posted');
      await load();
    } catch { toast.error('Failed to post question'); }
  };

  const handleAskChoice = async () => {
    if (!newQuestion.trim() || !newOptions.trim()) return;
    const optLabels = newOptions.split(',').map((o) => o.trim()).filter(Boolean);
    if (optLabels.length < 2) { toast.error('Need at least 2 options'); return; }
    try {
      await api.createSpecChoiceQuestion(specId, {
        question: newQuestion.trim(),
        question_type: (newMulti ? 'multi_choice' : 'choice') as 'choice' | 'multi_choice',
        choices: optLabels.map((label, i) => ({ id: `opt_${i}`, label })),
        allow_free_text: newAllowFreeText,
      });
      setNewQuestion(''); setNewOptions(''); setNewMulti(false); setNewAllowFreeText(false);
      toast.success('Choice question posted');
      await load();
    } catch { toast.error('Failed to post choice question'); }
  };

  const handleAnswer = async (qaId: string, answer: string | null, selected: string[] | null) => {
    try {
      await api.answerSpecQuestion(specId, qaId, answer || '', selected);
      setAnsweringId(null);
      setAnswerDraft('');
      toast.success('Answer posted');
      await load();
    } catch { toast.error('Failed to post answer'); }
  };

  const handleTextAnswer = async (qaId: string) => {
    if (!answerDraft.trim()) return;
    await handleAnswer(qaId, answerDraft.trim(), null);
  };

  const handleDelete = async (qaId: string) => {
    if (!confirm('Delete this Q&A?')) return;
    try {
      await api.deleteSpecQuestion(specId, qaId);
      await load();
    } catch { toast.error('Failed to delete'); }
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">Loading Q&A...</div>;

  const isAnswered = (qa: SpecQAItem) => Boolean(qa.answered_at);
  const unanswered = items.filter((q) => !isAnswered(q));
  const answered = items.filter((q) => isAnswered(q));

  return (
    <div className="space-y-4">
      {/* Ask mode toggle + form */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setAskMode('text')}
            className={`text-xs px-2 py-1 rounded ${askMode === 'text' ? 'bg-blue-600 text-white' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'}`}
          >
            Free Text
          </button>
          <button
            onClick={() => setAskMode('choice')}
            className={`text-xs px-2 py-1 rounded ${askMode === 'choice' ? 'bg-blue-600 text-white' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'}`}
          >
            Choice / Form
          </button>
        </div>

        {askMode === 'text' ? (
          <div className="flex gap-2">
            <MentionInput
              value={newQuestion}
              onChange={setNewQuestion}
              onSubmit={handleAskText}
              placeholder="Ask a question... (type @ to mention)"
              mentionables={mentionables}
              className="flex-1"
            />
            <button onClick={handleAskText} disabled={!newQuestion.trim()} className="btn btn-primary flex items-center gap-1 text-sm shrink-0">
              <Send size={14} /> Ask
            </button>
          </div>
        ) : (
          <div className="border border-blue-200 dark:border-blue-700 rounded-lg p-3 space-y-2 bg-blue-50/30 dark:bg-blue-900/10">
            <MentionInput
              value={newQuestion}
              onChange={setNewQuestion}
              placeholder="Question... (type @ to mention)"
              mentionables={mentionables}
              className="w-full"
            />
            <input type="text" value={newOptions} onChange={(e) => setNewOptions(e.target.value)} placeholder="Options (comma-separated): OAuth2, API Keys, Both" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                <input type="checkbox" checked={newMulti} onChange={(e) => setNewMulti(e.target.checked)} className="rounded" />
                Multi-select
              </label>
              <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
                <input type="checkbox" checked={newAllowFreeText} onChange={(e) => setNewAllowFreeText(e.target.checked)} className="rounded" />
                Allow free text
              </label>
            </div>
            <div className="flex justify-end">
              <button onClick={handleAskChoice} disabled={!newQuestion.trim() || !newOptions.trim()} className="btn btn-primary flex items-center gap-1 text-sm">
                <Send size={14} /> Post Choice
              </button>
            </div>
          </div>
        )}
      </div>

      {items.length === 0 && (
        <div className="text-center py-6">
          <MessageCircleQuestion size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No questions yet</p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Ask questions to clarify spec requirements before work begins</p>
        </div>
      )}

      {/* Unanswered */}
      {unanswered.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-amber-600 dark:text-amber-400 uppercase tracking-wide mb-2">
            Unanswered ({unanswered.length})
          </h4>
          <div className="space-y-2">
            {unanswered.map((qa) => (
              <div key={qa.id} className="border border-amber-200 dark:border-amber-700/50 rounded-lg p-3 bg-amber-50/50 dark:bg-amber-900/10">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <p className="text-sm text-gray-900 dark:text-white">{qa.question}</p>
                      {qa.question_type !== 'text' && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300">
                          {qa.question_type === 'multi_choice' ? 'multi-select' : 'single-select'}
                        </span>
                      )}
                    </div>
                    {qa.question_type !== 'text' && qa.choices && (
                      <div className="mt-1 space-y-0.5">
                        {qa.choices.map((opt) => (
                          <div key={opt.id} className="text-xs text-gray-500 dark:text-gray-400 pl-2">
                            &bull; {opt.label}
                          </div>
                        ))}
                      </div>
                    )}
                    <span className="text-[10px] text-gray-400 mt-1 block">
                      Asked by {qa.asked_by.slice(0, 12)}... &middot; {new Date(qa.created_at).toLocaleDateString()}
                    </span>
                  </div>
                  <button onClick={() => handleDelete(qa.id)} className="p-1 text-gray-400 hover:text-red-500 shrink-0">
                    <Trash2 size={12} />
                  </button>
                </div>
                {answeringId === qa.id ? (
                  qa.question_type !== 'text' ? (
                    <ChoiceAnswerForm qa={qa} onAnswer={handleAnswer} onCancel={() => setAnsweringId(null)} />
                  ) : (
                    <div className="mt-2 flex gap-2">
                      <MentionInput
                        value={answerDraft}
                        onChange={setAnswerDraft}
                        onSubmit={() => handleTextAnswer(qa.id)}
                        placeholder="Type your answer... (@ to mention)"
                        mentionables={mentionables}
                        className="flex-1"
                        autoFocus
                      />
                      <button onClick={() => handleTextAnswer(qa.id)} disabled={!answerDraft.trim()} className="btn btn-primary text-xs">Answer</button>
                      <button onClick={() => { setAnsweringId(null); setAnswerDraft(''); }} className="btn btn-secondary text-xs">Cancel</button>
                    </div>
                  )
                ) : (
                  <button
                    onClick={() => { setAnsweringId(qa.id); setAnswerDraft(''); }}
                    className="mt-2 text-xs text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    Answer this question
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Answered */}
      {answered.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-green-600 dark:text-green-400 uppercase tracking-wide mb-2">
            Answered ({answered.length})
          </h4>
          <div className="space-y-2">
            {answered.map((qa) => (
              <div key={qa.id} className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <p className="text-sm text-gray-900 dark:text-white">{qa.question}</p>
                    {qa.question_type !== 'text' && (
                      <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-300">
                        {qa.question_type === 'multi_choice' ? 'multi' : 'choice'}
                      </span>
                    )}
                  </div>
                  <button onClick={() => handleDelete(qa.id)} className="p-1 text-gray-400 hover:text-red-500 shrink-0">
                    <Trash2 size={12} />
                  </button>
                </div>
                <span className="text-[10px] text-gray-400 block mt-0.5">Asked by {qa.asked_by.slice(0, 12)}...</span>
                <div className="mt-2 pl-3 border-l-2 border-green-300 dark:border-green-600">
                  {qa.question_type !== 'text' && qa.choices && (
                    <ChoiceOptionsDisplay choices={qa.choices} selected={qa.selected} />
                  )}
                  {qa.answer && <p className="text-sm text-gray-700 dark:text-gray-300 mt-1">{qa.answer}</p>}
                  <span className="text-[10px] text-gray-400 block mt-0.5">
                    Answered by {qa.answered_by?.slice(0, 12)}... &middot; {qa.answered_at ? new Date(qa.answered_at).toLocaleDateString() : ''}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SpecSprintsTab({ sprints, api }: { sprints: any[]; api: ReturnType<typeof useDashboardApi> }) {
  const [details, setDetails] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (sprints.length === 0) { setLoading(false); return; }
    Promise.all(sprints.map(s => api.getSprint(s.id).catch(() => null)))
      .then(results => {
        const map: Record<string, any> = {};
        for (const r of results) { if (r) map[r.id] = r; }
        setDetails(map);
      })
      .finally(() => setLoading(false));
  }, [sprints.length]);

  if (loading) return <p className="text-sm text-gray-400 text-center py-6">Loading sprints...</p>;
  if (sprints.length === 0) return <p className="text-sm text-gray-400 text-center py-6">No sprints linked to this spec</p>;

  return (
    <div className="space-y-3">
      {sprints.map((sprint: any) => {
        const detail = details[sprint.id];
        const cards = detail?.cards || [];
        const total = cards.length;
        const done = cards.filter((c: any) => c.status === 'done').length;
        const pct = total > 0 ? Math.round((done / total) * 100) : 0;
        return (
          <div key={sprint.id} className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium text-white ${
                  sprint.status === 'closed' ? 'bg-green-500' :
                  sprint.status === 'active' ? 'bg-blue-500' :
                  sprint.status === 'review' ? 'bg-amber-500' :
                  sprint.status === 'cancelled' ? 'bg-red-500' : 'bg-gray-500'
                }`}>{sprint.status}</span>
                <span className="text-sm font-medium text-gray-900 dark:text-white">{sprint.title}</span>
              </div>
              <span className="text-xs font-bold text-gray-600 dark:text-gray-300">{pct}%</span>
            </div>
            <div className="w-full bg-gray-200 dark:bg-gray-600 rounded-full h-2 mb-1">
              <div
                className={`h-2 rounded-full transition-all ${pct === 100 ? 'bg-green-500' : pct >= 50 ? 'bg-blue-500' : 'bg-amber-500'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="flex items-center justify-between mt-1">
              <p className="text-[10px] text-gray-400">{done}/{total} cards done · v{sprint.version}</p>
              {sprint.objective && <p className="text-[10px] text-gray-500 truncate max-w-[60%]">{sprint.objective}</p>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ============================================================
   Knowledge Base Tab
   ============================================================ */

function KnowledgeTab({
  specId,
  boardId,
  onChanged,
}: {
  specId: string;
  boardId: string;
  onChanged?: () => void;
}) {
  const api = useDashboardApi();
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const [adding, setAdding] = useState(false);

  // Add form
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');

  const refreshWorkspace = () => {
    setRefreshGeneration((current) => current + 1);
  };

  const handleAdd = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    try {
      await api.createSpecKnowledge(specId, {
        title: newTitle.trim(),
        description: newDesc.trim() || undefined,
        content: newContent.trim(),
      });
      toast.success('Knowledge base item added');
      setAdding(false);
      setNewTitle(''); setNewDesc(''); setNewContent('');
      refreshWorkspace();
      onChanged?.();
    } catch { toast.error('Failed to add knowledge'); }
  };

  const handleDelete = async (knowledgeId: string) => {
    if (!confirm('Delete this knowledge base item?')) return false;
    try {
      await api.deleteSpecKnowledge(specId, knowledgeId);
      toast.success('Deleted');
      onChanged?.();
      return true;
    } catch {
      toast.error('Failed to delete');
      return false;
    }
  };

  return (
    <div className="space-y-3">
      <KnowledgeWorkspace
        boardId={boardId}
        entityType="spec"
        entityId={specId}
        refreshKey={refreshGeneration}
        loadFallbackDetail={(id) => api.getSpecKnowledge(specId, id)}
        onDelete={handleDelete}
      />

      {adding ? (
        <div className="border border-amber-200 dark:border-amber-700 rounded-lg p-3 space-y-2 bg-amber-50/50 dark:bg-amber-900/10">
          <input type="text" value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Title" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <input type="text" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} placeholder="Description (optional)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" />
          <textarea value={newContent} onChange={(e) => setNewContent(e.target.value)} placeholder="Content (markdown, text, JSON...)" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm dark:bg-gray-700 dark:border-gray-600" rows={6} />
          <div className="flex justify-end gap-2">
            <button onClick={() => setAdding(false)} className="btn btn-secondary text-xs">Cancel</button>
            <button onClick={handleAdd} disabled={!newTitle.trim() || !newContent.trim()} className="btn btn-primary text-xs">Add</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setAdding(true)} className="flex items-center gap-1 text-sm text-amber-600 dark:text-amber-400 hover:text-amber-800 dark:hover:text-amber-300">
          <Plus size={14} /> Add Knowledge
        </button>
      )}
    </div>
  );
}

/* ============================================================
   Validation Error Display — parses gate errors into readable items
   ============================================================ */

/* ============================================================
   Main SpecModal
   ============================================================ */

export function SpecModal({ specId, boardId: _boardId, onClose, onEscape, onChanged }: SpecModalProps) {
  const api = useDashboardApi();
  const modalStack = useOptionalModalStack();
  const currentBoard = useCurrentBoard();
  const perms = usePermissions(_boardId || currentBoard?.id);
  const canStructured = (
    entityType: SpecStructuredEntityType,
    operation: SpecStructuredEntityOperation,
  ) => perms.has(`spec.structured_entity.${entityType}.${operation}`);
  const canReadIR = perms.has('spec.integration_requirements.read');
  const canCreateIR = canStructured('integration_requirement', 'create');
  const canEditIR = canStructured('integration_requirement', 'update');
  const canDeleteIR = canStructured('integration_requirement', 'revoke');
  const canLinkIRTasks = canStructured('integration_requirement', 'link_task') && perms.has('card.link_to.ir');
  const canReadOR = perms.has('spec.observability_requirements.read');
  const canCreateOR = canStructured('observability_requirement', 'create');
  const canEditOR = canStructured('observability_requirement', 'update');
  const canDeleteOR = canStructured('observability_requirement', 'revoke');
  const canLinkORTasks = canStructured('observability_requirement', 'link_task') && perms.has('card.link_to.or');
  const canEditCoverageFlags = perms.has('spec.entity.edit_coverage_flags');
  const canReadQuality = perms.has('spec.quality.read');
  const canReadChecklist = perms.has('spec.checklist.read');
  const canExecuteChecklist = perms.has('spec.checklist.execute');
  const canReadSpecValidation = perms.has('spec.validation.read');
  const canReadPolicyCompliance = perms.has(
    'guidelines.assessments.read',
  );
  const [spec, setSpec] = useState<Spec | null>(null);
  const specAnchorTexts = useMemo(() => {
    if (!spec) return undefined;
    const map: Record<string, string> = {};
    const collect = (items: unknown) => {
      if (!Array.isArray(items)) return;
      for (const item of items) {
        if (
          item
          && typeof item === 'object'
          && typeof (item as { id?: unknown }).id === 'string'
          && typeof (item as { text?: unknown }).text === 'string'
        ) {
          map[(item as { id: string }).id] = (item as { text: string }).text;
        }
      }
    };
    collect(spec.functional_requirements);
    collect(spec.acceptance_criteria);
    collect(spec.technical_requirements);
    return map;
  }, [spec]);
  const [loading, setLoading] = useState(true);
  const [movingTo, setMovingTo] = useState<SpecStatus | null>(null);
  const [nextStatuses, setNextStatuses] = useState<SpecStatus[]>([]);
  const [
    policyTransitionPreview,
    setPolicyTransitionPreview,
  ] = useState<PolicyTransitionPreviewLoadState>({
    status: 'loading',
    transitions: [],
    error: null,
  });
  const [
    policyTransitionRejection,
    setPolicyTransitionRejection,
  ] = useState<PolicyTransitionRejection | null>(null);
  const lastTransitionSubjectKey = useRef<string | null>(null);
  const transitionRequestId = useRef(0);
  const [activeTab, setActiveTab] = useState<ModalTab>('details');
  const [resourceTab, setResourceTab] =
    useState<ResourceSubTab>('mockups');
  const [referenceTab, setReferenceTab] =
    useState<ReferenceSubTab>('origin');
  const [resourceGateRefreshKey, setResourceGateRefreshKey] =
    useState(0);
  const [
    testScenarioPolicyRefreshKey,
    setTestScenarioPolicyRefreshKey,
  ] = useState(0);
  const [detailsStructuredEditor, setDetailsStructuredEditor] = useState<{
    tab: ModalTab;
    mode: 'add' | 'edit';
    entityId?: string;
    token: number;
  } | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [showValidateModal, setShowValidateModal] = useState(false);
  const [validateResult, setValidateResult] = useState<{ success: boolean; error: string | null }>({ success: false, error: null });
  const [validating, setValidating] = useState(false);
  const [sprintSuggestions, setSprintSuggestions] = useState<any[] | null>(null);
  const [linkedSprints, setLinkedSprints] = useState<any[]>([]);
  const [cancelDialogOpen, setCancelDialogOpen] = useState(false);
  const [validationHistoryRefreshKey, setValidationHistoryRefreshKey] =
    useState(0);
  const currentSpecStatus = spec?.status;

  useEscapeToClose(onEscape ?? onClose);
  useEscapeToClose(() => setShowValidateModal(false), {
    enabled: showValidateModal,
    canClose: !validating,
    priority: 10,
  });

  useEffect(() => {
    const validationAvailable =
      canReadQuality ||
      canReadPolicyCompliance ||
      Boolean(
        currentSpecStatus &&
        isSpecValidationAvailable(currentSpecStatus) &&
        (canReadChecklist || canReadSpecValidation),
      );
    if (
      activeTab === 'validation' &&
      !validationAvailable
    ) {
      setActiveTab('details');
    }
  }, [
    activeTab,
    canReadChecklist,
    canReadPolicyCompliance,
    canReadQuality,
    canReadSpecValidation,
    currentSpecStatus,
  ]);

  // Build mentionables from board agents + owner
  const mentionables: Mentionable[] = [];
  if (currentBoard) {
    if (currentBoard.owner_id) {
      mentionables.push({ id: currentBoard.owner_id, name: 'Owner', type: 'user' });
    }
    for (const agent of currentBoard.agents) {
      mentionables.push({ id: agent.id, name: agent.name, type: 'agent' });
    }
  }

  const [parentIdeation, setParentIdeation] = useState<{ id: string; title: string; version: number } | null>(null);
  const [parentRefinement, setParentRefinement] = useState<{ id: string; title: string; version: number } | null>(null);
  const [viewingIdeationId, setViewingIdeationId] = useState<string | null>(null);
  const [viewingRefinementId, setViewingRefinementId] = useState<string | null>(null);

  const openIdeationReference = (id: string) => {
    if (modalStack) {
      modalStack.push({ type: 'ideation', id });
    } else {
      setViewingIdeationId(id);
    }
  };

  const openRefinementReference = (id: string) => {
    if (modalStack) {
      modalStack.push({ type: 'refinement', id });
    } else {
      setViewingRefinementId(id);
    }
  };

  useEffect(() => { loadSpec(); }, [specId]);

  const loadAllowedTransitions = useCallback(async (data: Spec) => {
    const requestId = transitionRequestId.current + 1;
    transitionRequestId.current = requestId;
    lastTransitionSubjectKey.current = [
      data.id,
      data.version,
      data.status,
    ].join(':');
    setPolicyTransitionRejection(null);
    setNextStatuses([]);
    setPolicyTransitionPreview({
      status: 'loading',
      transitions: [],
      error: null,
    });
    try {
      const response = await api.getAllowedTransitions(data.board_id, {
        entity_type: 'spec',
        entity_id: data.id,
      });
      if (transitionRequestId.current !== requestId) {
        return;
      }
      const transitions = requirePolicyTransitionEnvelope(response, {
        boardId: data.board_id,
        entityType: 'spec',
        subjectId: data.id,
        currentStatus: data.status,
      });
      setPolicyTransitionPreview({
        status: 'ready',
        transitions,
        error: null,
      });
      setNextStatuses(
        transitions
          .filter(isAllowedTransitionActionable)
          .map((item) => item.to_status)
          .filter((status): status is SpecStatus => SPEC_STATUSES.includes(status as SpecStatus))
      );
    } catch (caught) {
      if (transitionRequestId.current !== requestId) {
        return;
      }
      setNextStatuses([]);
      setPolicyTransitionPreview({
        status: 'error',
        transitions: [],
        error: caught instanceof Error
          ? caught.message
          : 'The server transition contract could not be loaded.',
      });
    }
  }, [api]);

  useEffect(() => {
    if (!spec) {
      return;
    }
    const subjectKey = [
      spec.id,
      spec.version,
      spec.status,
    ].join(':');
    if (lastTransitionSubjectKey.current === subjectKey) {
      return;
    }
    void loadAllowedTransitions(spec);
  }, [loadAllowedTransitions, spec]);

  const loadSpec = async () => {
    setLoading(true);
    try {
      const data = await api.getSpec(specId);
      setSpec(data);
      await loadAllowedTransitions(data);
      if (data.ideation_id) {
        try {
          const ideation = await api.getIdeation(data.ideation_id);
          setParentIdeation({ id: ideation.id, title: ideation.title, version: ideation.version });
        } catch { setParentIdeation(null); }
      } else { setParentIdeation(null); }
      if (data.refinement_id) {
        try {
          const refinement = await api.getRefinement(data.refinement_id);
          setParentRefinement({ id: refinement.id, title: refinement.title, version: refinement.version });
        } catch { setParentRefinement(null); }
      } else { setParentRefinement(null); }
      // Load linked sprints
      try {
        const sprints = await api.listSprints(data.board_id, data.id);
        setLinkedSprints(sprints);
      } catch { setLinkedSprints([]); }
    } catch { toast.error('Failed to load spec'); } finally { setLoading(false); }
  };

  const reloadSpecAfterStructuredEdit = async () => {
    const updated = await api.getSpec(specId);
    setSpec(updated);
    await loadAllowedTransitions(updated);
    onChanged();
    return updated;
  };

  const applyImpactAwareOperation = async (
    entityType: SpecStructuredEntityType,
    entityId: string,
    operation: Extract<SpecStructuredEntityOperation, 'revoke' | 'supersede' | 'reorder'>,
    version: number | null,
    payload: Record<string, unknown> = {},
  ): Promise<number | null> => {
    const preview = await api.previewSpecEntityImpact(specId, entityType, entityId, operation, {
      payload,
      expected_spec_version: version,
    });
    const impacted = preview.impact_report?.impacted_refs || [];
    let ackToken = preview.ack_token || preview.impact_report?.ack_token || null;
    if (impacted.length > 0) {
      const counts = Object.entries(preview.impact_report?.counts_by_type || {})
        .map(([type, count]) => `${count} ${type}`)
        .join(', ');
      const accepted = window.confirm(
        `This ${operation.replace('_', ' ')} impacts ${counts || `${impacted.length} item(s)`}. Continue?`,
      );
      if (!accepted) {
        throw new Error('Operation cancelled');
      }
    }

    const result = await api.operateSpecEntity(specId, entityType, entityId, operation, {
      payload,
      expected_spec_version: version,
      ack_token: ackToken,
    });

    if (!result.success && result.error_code === 'impact_ack_required') {
      ackToken = result.ack_token || result.impact_report?.ack_token || null;
      const impactedRefs = result.impact_report?.impacted_refs || [];
      const accepted = window.confirm(
        `This ${operation.replace('_', ' ')} impacts ${impactedRefs.length} item(s). Continue?`,
      );
      if (!accepted || !ackToken) {
        throw new Error('Operation cancelled');
      }
      const acknowledged = await api.operateSpecEntity(specId, entityType, entityId, operation, {
        payload,
        expected_spec_version: version,
        ack_token: ackToken,
      });
      if (!acknowledged.success) {
        throw new Error(acknowledged.error_message || 'Structured operation failed');
      }
      return acknowledged.spec_version;
    }

    if (!result.success) {
      throw new Error(result.error_message || 'Structured operation failed');
    }
    return result.spec_version;
  };

  const syncTextEntityList = async (
    entityType: Extract<SpecStructuredEntityType, 'functional_requirement' | 'acceptance_criterion'>,
    currentItems: unknown[] | null,
    nextTexts: string[],
  ) => {
    if (!spec) return;
    try {
      let version: number | null = spec.version;
      const currentEntries = (currentItems || [])
        .map(normalizeTextEntity)
        .filter((item) => item.status === 'active');

      const nextCounts = new Map<string, number>();
      for (const text of nextTexts.map((item) => item.trim()).filter(Boolean)) {
        nextCounts.set(text, (nextCounts.get(text) || 0) + 1);
      }

      const keptCounts = new Map<string, number>();
      const entriesToRevoke: typeof currentEntries = [];
      for (const entry of currentEntries) {
        const allowed = nextCounts.get(entry.text) || 0;
        const seen = keptCounts.get(entry.text) || 0;
        if (seen < allowed) {
          keptCounts.set(entry.text, seen + 1);
          continue;
        }
        entriesToRevoke.push(entry);
      }
      entriesToRevoke.sort((a, b) => {
        const ai = Number(a.id);
        const bi = Number(b.id);
        return Number.isInteger(ai) && Number.isInteger(bi) ? bi - ai : 0;
      });
      for (const entry of entriesToRevoke) {
        version = await applyImpactAwareOperation(entityType, entry.id, 'revoke', version);
      }

      const currentCounts = new Map<string, number>();
      for (const entry of currentEntries) {
        currentCounts.set(entry.text, (currentCounts.get(entry.text) || 0) + 1);
      }
      const createdCounts = new Map<string, number>();
      for (const text of nextTexts.map((item) => item.trim()).filter(Boolean)) {
        const existing = currentCounts.get(text) || 0;
        const created = createdCounts.get(text) || 0;
        if (created < existing) {
          createdCounts.set(text, created + 1);
          continue;
        }
        const result = await api.createSpecEntity(specId, entityType, { text }, version);
        if (!result.success) throw new Error(result.error_message || 'Structured create failed');
        version = result.spec_version;
        createdCounts.set(text, created + 1);
      }

      await reloadSpecAfterStructuredEdit();
    } catch (error) {
      if ((error as Error).message !== 'Operation cancelled') {
        toast.error((error as Error).message || 'Failed to update');
      }
    }
  };

  const updateTextEntityAtIndex = async (
    entityType: Extract<SpecStructuredEntityType, 'functional_requirement' | 'acceptance_criterion'>,
    currentItems: unknown[] | null,
    index: number,
    text: string,
  ) => {
    if (!spec) return;
    const entry = (currentItems || [])
      .map(normalizeTextEntity)
      .filter((item) => item.status === 'active')[index];
    if (!entry) return;
    const result = await api.updateSpecEntity(specId, entityType, entry.id, { text }, spec.version);
    if (!result.success) throw new Error(result.error_message || 'Structured update failed');
    await reloadSpecAfterStructuredEdit();
  };

  const updateStructuredEntityAtIndex = async (
    entityType: SpecStructuredEntityType,
    items: StructuredObjectEntity[] | null,
    index: number,
    payload: Record<string, unknown>,
  ) => {
    if (!spec) return;
    const entry = (items || []).filter((item) => ((item as any).status || 'active') === 'active')[index] as any;
    if (!entry?.id) return;
    const result = await api.updateSpecEntity(specId, entityType, entry.id, payload, spec.version);
    if (!result.success) throw new Error(result.error_message || 'Structured update failed');
    await reloadSpecAfterStructuredEdit();
  };

  const syncStructuredCollection = async (
    field: StructuredCollectionField,
    currentItems: StructuredObjectEntity[] | null,
    nextItems: StructuredObjectEntity[],
  ) => {
    if (!spec) return;
    try {
      let version: number | null = spec.version;
      const entityType = STRUCTURED_ENTITY_BY_FIELD[field];
      const currentById = new Map((currentItems || []).map((item) => [item.id, item]));
      const nextById = new Map(nextItems.map((item) => [item.id, item]));

      for (const current of currentItems || []) {
        const currentStatus = String((stableEntityPayload(current).status as string | undefined) || 'active');
        if (currentStatus !== 'active' && !nextById.has(current.id)) {
          continue;
        }
        if (!nextById.has(current.id)) {
          version = await applyImpactAwareOperation(entityType, current.id, 'revoke', version);
        }
      }

      for (const next of nextItems) {
        const current = currentById.get(next.id);
        if (!current) {
          const result = await api.createSpecEntity(specId, entityType, stableEntityPayload(next), version);
          if (!result.success) throw new Error(result.error_message || 'Structured create failed');
          version = result.spec_version;
          continue;
        }

        const currentPayload = stableEntityPayload(current);
        const nextPayload = stableEntityPayload(next);
        const currentStatus = String(currentPayload.status || 'active');
        const nextStatus = String(nextPayload.status || 'active');
        if (currentStatus !== nextStatus) {
          if (nextStatus === 'revoked') {
            version = await applyImpactAwareOperation(entityType, next.id, 'revoke', version);
          } else if (nextStatus === 'superseded') {
            version = await applyImpactAwareOperation(entityType, next.id, 'supersede', version, {
              supersedes_decision_id: nextPayload.supersedes_decision_id,
            });
          } else if (nextStatus === 'active') {
            const result = await api.operateSpecEntity(specId, entityType, next.id, 'restore', {
              expected_spec_version: version,
            });
            if (!result.success) throw new Error(result.error_message || 'Structured restore failed');
            version = result.spec_version;
          }
          delete currentPayload.status;
          delete nextPayload.status;
        }

        if (JSON.stringify(currentPayload) !== JSON.stringify(nextPayload)) {
          const result = await api.updateSpecEntity(specId, entityType, next.id, nextPayload, version);
          if (!result.success) throw new Error(result.error_message || 'Structured update failed');
          version = result.spec_version;
        }
      }

      const currentIds = (currentItems || []).map((item) => item.id);
      const nextIds = nextItems.map((item) => item.id);
      const sameSet = currentIds.length === nextIds.length && currentIds.every((id) => nextIds.includes(id));
      const sameOrder = currentIds.length === nextIds.length && currentIds.every((id, index) => id === nextIds[index]);
      if (sameSet && !sameOrder && nextIds.length > 0) {
        version = await applyImpactAwareOperation(entityType, '__collection__', 'reorder', version, {
          ordered_entity_ids: nextIds,
        });
      }

      await reloadSpecAfterStructuredEdit();
    } catch (error) {
      if ((error as Error).message !== 'Operation cancelled') {
        toast.error((error as Error).message || 'Failed to update');
      }
    }
  };

  const boardSettings = (currentBoard?.settings || {}) as BoardSettings;
  const requireSpecValidation = boardSettings.require_spec_validation ?? true;
  const [showSubmitValidationModal, setShowSubmitValidationModal] = useState(false);

  const handleMoveSpec = async (status: SpecStatus, cancellationReason?: string) => {
    if (!spec) return;
    // ITEM 17: cancelling requires a justification — intercept with the dialog.
    if (status === 'cancelled' && !cancellationReason) {
      setCancelDialogOpen(true);
      return;
    }
    // Spec Validation Gate: when the board opts in, intercept approved→validated
    // to show the new SubmitSpecValidationModal. The modal calls the backend gate
    // which runs coverage checks and then computes outcome — on success the spec
    // is promoted to validated automatically, so we just refetch after.
    if (status === 'validated' && spec.status === 'approved' && requireSpecValidation) {
      setShowSubmitValidationModal(true);
      return;
    }
    // Legacy path (pre-gate): direct move_spec with coverage gate feedback in the
    // existing validate modal.
    if (status === 'validated' && spec.status === 'approved') {
      setShowValidateModal(true);
      setValidateResult({ success: false, error: null });
      setValidating(true);
      try {
        const updated = await api.moveSpec(specId, { status });
        setSpec(updated);
        await loadAllowedTransitions(updated);
        onChanged();
        setValidateResult({ success: true, error: null });
        if (updated.cards && updated.cards.length >= 6) {
          try {
            const result = await api.suggestSprints(updated.board_id, specId);
            if (result.suggestions && result.suggestions.length > 1) {
              setSprintSuggestions(result.suggestions);
            }
          } catch {
            // Suggestion is optional, don't block on failure
          }
        }
      } catch (err: any) {
        setValidateResult({ success: false, error: err?.message || 'Validation failed' });
        await loadAllowedTransitions(spec);
      } finally {
        setValidating(false);
      }
      return;
    }
    setMovingTo(status);
    setPolicyTransitionRejection(null);
    try {
      const updated = await api.moveSpec(specId, {
        status,
        ...(cancellationReason ? { cancellation_reason: cancellationReason } : {}),
      });
      setSpec(updated);
      await loadAllowedTransitions(updated);
      onChanged();
      toast.success(`Spec moved to ${SPEC_STATUS_LABELS[status]}`);
    } catch (err) {
      const rejection = readPolicyTransitionRejection(err, {
        boardId: spec.board_id,
        entityType: 'spec',
        subjectId: spec.id,
        currentStatus: spec.status,
        toStatus: status,
      });
      toast.error(
        rejection
          ? policyTransitionRejectionMessage(rejection)
          : getErrorMessage(err),
      );
      await loadAllowedTransitions(spec);
      setPolicyTransitionRejection(rejection);
    } finally { setMovingTo(null); }
  };

  const handleDelete = async () => {
    if (!spec) return;
    if (!confirm(`Delete spec "${spec.title}"? Linked cards will be unlinked but not deleted.`)) return;
    try {
      await api.deleteSpec(specId);
      toast.success('Spec deleted');
      onChanged();
      onClose();
    } catch { toast.error('Failed to delete spec'); }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-xl p-8">
          <div className="text-gray-500 dark:text-gray-400">Loading spec...</div>
        </div>
      </div>
    );
  }

  if (!spec) return null;

  const statusFlowStatuses = nextStatuses.filter((s) => !(s === 'validated' && spec.status === 'approved'));
  const canSubmitValidation = nextStatuses.includes('validated');
  const openDetailsStructuredEditor = (tab: ModalTab, mode: 'add' | 'edit', entityId?: string) => {
    setDetailsStructuredEditor({ tab, mode, entityId, token: Date.now() });
    setActiveTab(tab);
  };
  const clearDetailsStructuredEditor = () => setDetailsStructuredEditor(null);

  const unansweredQA = spec.qa_items?.filter((q) => !q.answered_at).length || 0;
  const showValidationTab =
    canReadQuality ||
    canReadPolicyCompliance ||
    (
      isSpecValidationAvailable(spec.status) &&
      (canReadChecklist || canReadSpecValidation)
    );
  const allTabs: { id: ModalTab; label: string; icon: React.ReactNode; count?: number; highlight?: boolean; permission?: string }[] = [
    { id: 'details', label: 'Details', icon: <FileText size={14} /> },
    { id: 'tests', label: 'Tests', icon: <FlaskConical size={14} />, count: spec.test_scenarios?.length || 0 },
    { id: 'rules', label: 'Rules', icon: <Scale size={14} />, count: spec.business_rules?.length || 0 },
    { id: 'contracts', label: 'Contracts', icon: <FileCode size={14} />, count: spec.api_contracts?.length || 0 },
    { id: 'irs', label: 'IRs', icon: <Network size={14} />, count: spec.integration_requirements?.length || 0, permission: 'spec.integration_requirements.read' },
    { id: 'ors', label: 'ORs', icon: <Gauge size={14} />, count: spec.observability_requirements?.length || 0, permission: 'spec.observability_requirements.read' },
    { id: 'trs', label: 'TRs', icon: <Settings size={14} />, count: spec.technical_requirements?.length || 0 },
    { id: 'decisions', label: 'Decisions', icon: <GitBranch size={14} />, count: spec.decisions?.length || 0 },
    { id: 'resources', label: 'Resources', icon: <BookOpen size={14} /> },
    { id: 'qa', label: 'Q&A', icon: <MessageCircleQuestion size={14} />, count: spec.qa_items?.length || 0, highlight: unansweredQA > 0 },
    { id: 'references', label: 'References', icon: <Link2 size={14} />, count: spec.cards?.length || 0 },
    { id: 'sprints', label: 'Sprints', icon: <Layers size={14} />, count: linkedSprints.length },
    { id: 'kg', label: 'KG Graph', icon: <Network size={14} /> },
    ...(showValidationTab
      ? [{ id: 'validation' as ModalTab, label: 'Validation', icon: <ShieldCheck size={14} /> }]
      : []),
    { id: 'activity', label: 'Activity', icon: <History size={14} /> },
  ];
  const tabs = allTabs.filter((tab) => !tab.permission || perms.has(tab.permission));

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className={`bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full ${expanded ? 'max-w-[95vw] h-[95vh]' : 'max-w-3xl h-[90vh]'} flex flex-col`}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-3 min-w-0">
            <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${STATUS_COLORS[spec.status]}`}>
              {STATUS_ICON[spec.status]}
              {SPEC_STATUS_LABELS[spec.status]}
            </span>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{spec.title}</h2>
            <SpecEditionLabel
              edition={spec.edition}
              technicalRevision={spec.version}
              className="text-xs text-gray-400 shrink-0"
            />
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => openLineageGraph('spec', spec.id)}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              title="Open lineage graph"
            >
              <GitBranch size={16} />
            </button>
            <button
              onClick={async () => {
                try {
                  const fullKnowledge = await Promise.all(
                    (spec.knowledge_bases || []).map((kb) =>
                      api.getSpecKnowledge(spec.id, kb.id).catch(() => kb)
                    )
                  );
                  // Hydrate architecture design summaries into full designs (entities,
                  // interfaces, diagram payloads) so the Markdown export can render the
                  // Mermaid diagram. spec.architecture_designs are summaries by default.
                  const fullArchitecture = await Promise.all(
                    (spec.architecture_designs || []).map((d) =>
                      api.getArchitectureDesign(d.id, true).catch(() => d)
                    )
                  );
                  const md = exportSpec({
                    ...spec,
                    knowledge_bases: fullKnowledge as any,
                    architecture_designs: fullArchitecture as any,
                  });
                  downloadMarkdown(md, markdownFilenameForSpec(spec));
                } catch {
                  toast.error('Failed to prepare markdown export');
                }
              }}
              disabled={loading}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors disabled:opacity-30"
              title="Download Markdown"
            >
              <Download size={16} />
            </button>
            <button onClick={loadSpec} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title="Refresh">
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            <button onClick={() => setExpanded(!expanded)} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors" title={expanded ? 'Collapse' : 'Expand'}>
              {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </button>
            <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
              <X size={20} />
            </button>
          </div>
        </div>

        {/* Status flow */}
        {statusFlowStatuses.length > 0 && (
          <div className="px-6 py-2.5 border-b border-gray-100 dark:border-gray-700/50 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-gray-500 dark:text-gray-400">Move to:</span>
            {statusFlowStatuses.map((status) => (
              <button
                key={status}
                onClick={() => handleMoveSpec(status)}
                disabled={movingTo !== null}
                className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium transition-colors
                  ${STATUS_COLORS[status]} hover:ring-2 hover:ring-offset-1 hover:ring-gray-300 dark:hover:ring-gray-600
                  disabled:opacity-50`}
              >
                <ChevronRight size={12} />
                {SPEC_STATUS_LABELS[status]}
                {movingTo === status && '...'}
              </button>
            ))}
          </div>
        )}

        {/* Provenance breadcrumb */}
        {(parentIdeation || parentRefinement) && (
          <div className="px-6 py-2 border-b border-gray-100 dark:border-gray-700/50 flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
            <span className="text-gray-400">From:</span>
            {parentIdeation && (
              <button
                onClick={() => openIdeationReference(parentIdeation.id)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300 hover:ring-2 hover:ring-amber-300 dark:hover:ring-amber-600 transition-all cursor-pointer"
              >
                <Lightbulb size={11} />
                {parentIdeation.title}
                <span className="text-[10px] text-amber-500 dark:text-amber-400">v{parentIdeation.version}</span>
              </button>
            )}
            {parentIdeation && parentRefinement && <ChevronRight size={12} className="text-gray-300" />}
            {parentRefinement && (
              <button
                onClick={() => openRefinementReference(parentRefinement.id)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-300 hover:ring-2 hover:ring-blue-300 dark:hover:ring-blue-600 transition-all cursor-pointer"
              >
                <Layers size={11} />
                {parentRefinement.title}
                <span className="text-[10px] text-blue-500 dark:text-blue-400">v{parentRefinement.version}</span>
              </button>
            )}
          </div>
        )}

        {/* Tabs */}
        <div data-tour-id="specs.resources.tabs" className="shrink-0">
          <AccessibleTabList
            idBase={`spec-${specId}`}
            ariaLabel="Spec sections"
            items={tabs.map((tab) => ({
              id: tab.id,
              label: tab.label,
              icon: tab.icon,
              count: tab.count,
              attention: tab.highlight,
            }))}
            value={activeTab}
            onValueChange={setActiveTab}
            className="px-6 pt-3 scrollbar-hide"
          />
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          <AccessibleTabPanel
            idBase={`spec-${specId}`}
            tabId={activeTab}
            value={activeTab}
            className="outline-none"
          >
          {activeTab === 'details' && (
            <div className="space-y-5">
              {spec.status === 'cancelled' && (
                <CancellationDetails
                  id="cancellation-panel"
                  entityLabel="spec"
                  reason={spec.cancellation_reason}
                  cancelledBy={spec.cancelled_by}
                  cancelledAt={spec.cancelled_at}
                />
              )}
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Description</h4>
                <EditableField
                  value={spec.description || ''}
                  onSave={async (val) => {
                    const updated = await api.updateSpec(specId, { description: val });
                    setSpec(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No description"
                />
              </div>
              <div>
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-1">Context</h4>
                <EditableField
                  value={spec.context || ''}
                  onSave={async (val) => {
                    const updated = await api.updateSpec(specId, { context: val });
                    setSpec(updated);
                  }}
                  multiline
                  renderView={(v) => <MarkdownContent content={v} />}
                  placeholder="No context"
                />
              </div>
              <EditableRequirementsList
                title="Functional Requirements"
                icon={<Circle size={14} />}
                items={((spec.functional_requirements || []) as unknown[])
                  .map(normalizeTextEntity)
                  .filter((item) => item.status === 'active')
                  .map((item) => item.text)}
                placeholder="Add a functional requirement..."
                canAdd={canStructured('functional_requirement', 'create')}
                canEdit={canStructured('functional_requirement', 'update')}
                canRemove={canStructured('functional_requirement', 'revoke')}
                onEditItem={async (index, text) => {
                  await updateTextEntityAtIndex('functional_requirement', spec.functional_requirements as unknown[] | null, index, text);
                }}
                onUpdate={async (items) => {
                  await syncTextEntityList('functional_requirement', spec.functional_requirements as unknown[] | null, items);
                }}
              />
              {canReadIR && (
                <EditableRequirementsList
                  title="Integration Requirements"
                  icon={<Network size={14} />}
                  items={(spec.integration_requirements || [])
                    .filter((item) => item.status === 'active')
                    .map(requirementDisplayText)
                    .filter(Boolean)}
                  placeholder="Add an integration requirement..."
                  canAdd={canCreateIR}
                  canEdit={canEditIR}
                  canRemove={canDeleteIR}
                  onAddItem={() => openDetailsStructuredEditor('irs', 'add')}
                  onOpenItemEditor={(index) => {
                    const item = (spec.integration_requirements || []).filter((entry) => entry.status === 'active')[index];
                    if (item) openDetailsStructuredEditor('irs', 'edit', item.id);
                  }}
                  onEditItem={async (index, text) => {
                    const item = (spec.integration_requirements || []).filter((entry) => entry.status === 'active')[index];
                    await updateStructuredEntityAtIndex(
                      'integration_requirement',
                      spec.integration_requirements || [],
                      index,
                      { title: text, description: item?.description || text },
                    );
                  }}
                  onUpdate={async (items) => {
                    await syncStructuredCollection(
                      'integration_requirements',
                      spec.integration_requirements || [],
                      reconcileIntegrationRequirements(spec.integration_requirements, items),
                    );
                  }}
                />
              )}
              {canReadOR && (
                <EditableRequirementsList
                  title="Observability Requirements"
                  icon={<Gauge size={14} />}
                  items={(spec.observability_requirements || [])
                    .filter((item) => item.status === 'active')
                    .map(requirementDisplayText)
                    .filter(Boolean)}
                  placeholder="Add an observability requirement..."
                  canAdd={canCreateOR}
                  canEdit={canEditOR}
                  canRemove={canDeleteOR}
                  onAddItem={() => openDetailsStructuredEditor('ors', 'add')}
                  onOpenItemEditor={(index) => {
                    const item = (spec.observability_requirements || []).filter((entry) => entry.status === 'active')[index];
                    if (item) openDetailsStructuredEditor('ors', 'edit', item.id);
                  }}
                  onEditItem={async (index, text) => {
                    const item = (spec.observability_requirements || []).filter((entry) => entry.status === 'active')[index];
                    await updateStructuredEntityAtIndex(
                      'observability_requirement',
                      spec.observability_requirements || [],
                      index,
                      { title: text, description: item?.description || text },
                    );
                  }}
                  onUpdate={async (items) => {
                    await syncStructuredCollection(
                      'observability_requirements',
                      spec.observability_requirements || [],
                      reconcileObservabilityRequirements(spec.observability_requirements, items),
                    );
                  }}
                />
              )}
              <EditableRequirementsList
                title="Technical Requirements"
                icon={<Settings size={14} />}
                items={(spec.technical_requirements || [])
                  .filter((tr) => typeof tr === 'string' || ((tr as TechnicalRequirement).status || 'active') === 'active')
                  .map((tr) => (typeof tr === 'string' ? tr : (tr as TechnicalRequirement).text || ''))}
                placeholder="Add a technical constraint..."
                canAdd={canStructured('technical_requirement', 'create')}
                canEdit={canStructured('technical_requirement', 'update')}
                canRemove={canStructured('technical_requirement', 'revoke')}
                onAddItem={() => openDetailsStructuredEditor('trs', 'add')}
                onOpenItemEditor={(index) => {
                  const item = (spec.technical_requirements || [])
                    .map((tr, trIndex) =>
                      typeof tr === 'string'
                        ? { id: `tr_legacy_${trIndex}`, text: tr, linked_task_ids: null }
                        : tr
                    )
                    .filter((tr) => (tr.status || 'active') === 'active')[index] as TechnicalRequirement | undefined;
                  if (item) openDetailsStructuredEditor('trs', 'edit', item.id);
                }}
                onEditItem={async (index, text) => {
                  const existingTRs = (spec.technical_requirements || []).map((tr, trIndex) =>
                    typeof tr === 'string'
                      ? { id: `tr_legacy_${trIndex}`, text: tr, linked_task_ids: null }
                      : tr
                  ).filter((tr) => (tr.status || 'active') === 'active') as TechnicalRequirement[];
                  await updateStructuredEntityAtIndex('technical_requirement', existingTRs as any, index, { text });
                }}
                onUpdate={async (items) => {
                  const existingTRs = (spec.technical_requirements || []).map((tr, index) =>
                    typeof tr === 'string'
                      ? { id: `tr_legacy_${index}`, text: tr, linked_task_ids: null }
                      : tr
                  ).filter((tr) => (tr.status || 'active') === 'active') as TechnicalRequirement[];
                  const byText = new Map(existingTRs.map((tr) => [tr.text, tr]));
                  const nextTRs = items.map((text) => byText.get(text) || {
                    id: `tr_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
                    text,
                    linked_task_ids: null,
                  });
                  await syncStructuredCollection('technical_requirements', existingTRs, nextTRs);
                }}
              />
              <EditableRequirementsList
                title="Acceptance Criteria"
                icon={<Target size={14} />}
                items={((spec.acceptance_criteria || []) as unknown[])
                  .map(normalizeTextEntity)
                  .filter((item) => item.status === 'active')
                  .map((item) => item.text)}
                placeholder="Add an acceptance criterion..."
                canAdd={canStructured('acceptance_criterion', 'create')}
                canEdit={canStructured('acceptance_criterion', 'update')}
                canRemove={canStructured('acceptance_criterion', 'revoke')}
                onEditItem={async (index, text) => {
                  await updateTextEntityAtIndex('acceptance_criterion', spec.acceptance_criteria as unknown[] | null, index, text);
                }}
                onUpdate={async (items) => {
                  await syncTextEntityList('acceptance_criterion', spec.acceptance_criteria as unknown[] | null, items);
                }}
              />
              {/* Decisions — contextual choices, same bulleted pattern as FR/AC.
                  Only active decisions show in the list; supersedence/revocation
                  happens via MCP tools + KG. Text is mapped to Decision.title
                  (and rationale mirrors it by default). */}
              <EditableRequirementsList
                title="Decisions"
                icon={<Lightbulb size={14} />}
                items={(spec.decisions || [])
                  .filter((d) => d.status === 'active')
                  .map((d) => d.title)}
                placeholder="Add a decision (e.g. 'Use embedded graph storage over an external graph database')..."
                canAdd={canStructured('decision', 'create')}
                canEdit={canStructured('decision', 'update')}
                canRemove={canStructured('decision', 'revoke')}
                onAddItem={() => openDetailsStructuredEditor('decisions', 'add')}
                onOpenItemEditor={(index) => {
                  const item = (spec.decisions || []).filter((decision) => decision.status === 'active')[index];
                  if (item) openDetailsStructuredEditor('decisions', 'edit', item.id);
                }}
                onEditItem={async (index, text) => {
                  await updateStructuredEntityAtIndex('decision', spec.decisions || [], index, { title: text });
                }}
                onUpdate={async (items) => {
                  const existing = spec.decisions || [];
                  const byTitle = new Map(
                    existing.filter((d) => d.status === 'active').map((d) => [d.title, d]),
                  );
                  const keptTitles = new Set(items);
                  const next: Decision[] = existing
                    .filter((d) => d.status !== 'active')
                    .map((d) => ({ ...d }));
                  for (const text of items) {
                    const prior = byTitle.get(text);
                    next.push(prior || {
                      id: `dec_${Date.now().toString(16).slice(-8)}${Math.random().toString(16).slice(2, 4)}`,
                      title: text,
                      rationale: text,
                      context: null,
                      alternatives_considered: null,
                      supersedes_decision_id: null,
                      linked_requirements: null,
                      linked_task_ids: null,
                      status: 'active',
                      notes: null,
                    });
                  }
                  for (const [title, d] of byTitle) {
                    if (!keptTitles.has(title)) next.push({ ...d, status: 'revoked' });
                  }
                  await syncStructuredCollection('decisions', existing, next);
                }}
              />
              {spec.labels && spec.labels.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {spec.labels.map((label, i) => (
                    <span key={i} className="text-xs px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300">{label}</span>
                  ))}
                </div>
              )}

              {/* Validation Gate Override */}
              <ValidationGateOverride
                title="Validation Gate"
                requireValue={spec.require_task_validation ?? null}
                minConfidence={spec.validation_min_confidence ?? null}
                minCompleteness={spec.validation_min_completeness ?? null}
                maxDrift={spec.validation_max_drift ?? null}
                parentLabel="Board default"
                onUpdate={async (patch) => {
                  try {
                    const updated = await api.updateSpec(specId, patch);
                    setSpec(updated);
                  } catch { toast.error('Failed to update validation gate'); }
                }}
              />

              {/* Sprints summary — details in Sprints tab */}
              {linkedSprints.length > 0 && (
                <button
                  onClick={() => setActiveTab('sprints')}
                  className="text-xs text-indigo-600 dark:text-indigo-400 hover:underline flex items-center gap-1"
                >
                  <Layers size={12} />
                  {linkedSprints.length} sprint{linkedSprints.length > 1 ? 's' : ''} linked — view details
                </button>
              )}
            </div>
          )}

          {activeTab === 'tests' && spec && (
            <TestScenariosTab
              spec={spec}
              canReadPolicyCompliance={canReadPolicyCompliance}
              policyRefreshKey={testScenarioPolicyRefreshKey}
              onUpdate={async (scenarios) => {
                try {
                  const updated = await persistTestScenariosWithWriteGuard(
                    api.updateSpec,
                    specId,
                    spec.test_scenarios || [],
                    scenarios,
                  );
                  setSpec(updated);
                  setTestScenarioPolicyRefreshKey((value) => value + 1);
                  onChanged();
                } catch (err) {
                  // Surface the backend validation message (e.g. the strict
                  // scenario_type contract) instead of a generic string so a
                  // stale client/agent can correct the request (spec ac16b3c9).
                  toast.error(err instanceof Error && err.message ? err.message : 'Failed to update test scenarios');
                }
              }}
              onSpecRefreshed={(updated) => {
                setSpec(updated);
                setTestScenarioPolicyRefreshKey((value) => value + 1);
                onChanged();
              }}
              onSpecUpdate={async (data) => {
                try {
                  const updated = await api.updateSpec(specId, data as any);
                  setSpec(updated);
                } catch (err) {
                  toast.error(err instanceof Error && err.message ? err.message : 'Failed to update spec');
                }
              }}
            />
          )}
          {activeTab === 'rules' && spec && (
            <RulesTab
              spec={spec}
              canCreate={canStructured('business_rule', 'create')}
              canEdit={canStructured('business_rule', 'update')}
              canDelete={canStructured('business_rule', 'revoke')}
              onUpdate={async (rules) => {
                await syncStructuredCollection('business_rules', spec.business_rules || [], rules);
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
            />
          )}
          {activeTab === 'contracts' && spec && (
            <ContractsTab
              spec={spec}
              canCreate={canStructured('api_contract', 'create')}
              canEdit={canStructured('api_contract', 'update')}
              canDelete={canStructured('api_contract', 'revoke')}
              canLinkTask={canStructured('api_contract', 'link_task')}
              onUpdate={async (contracts) => {
                await syncStructuredCollection('api_contracts', spec.api_contracts || [], contracts);
              }}
              onSpecUpdate={async (data) => {
                try {
                  const updated = await api.updateSpec(specId, data as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
              specCards={spec.cards || []}
              onLinkTask={async (contractId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'api_contracts', contractId, cardId);
                setSpec(updated);
              }}
              onUnlinkTask={async (contractId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'api_contracts', contractId, cardId);
                setSpec(updated);
              }}
            />
          )}
          {activeTab === 'irs' && spec && canReadIR && (
            <IntegrationRequirementsTab
              spec={spec}
              canCreate={canCreateIR}
              canEdit={canEditIR}
              canDelete={canDeleteIR}
              canLinkTask={canLinkIRTasks}
              canEditCoverageFlags={canEditCoverageFlags}
              focusEditId={detailsStructuredEditor?.tab === 'irs' && detailsStructuredEditor.mode === 'edit' ? detailsStructuredEditor.entityId || null : null}
              focusCreateToken={detailsStructuredEditor?.tab === 'irs' && detailsStructuredEditor.mode === 'add' ? detailsStructuredEditor.token : null}
              onFocusHandled={clearDetailsStructuredEditor}
              onUpdate={async (requirements) => {
                await syncStructuredCollection('integration_requirements', spec.integration_requirements || [], requirements);
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
              specCards={spec.cards || []}
              onLinkTask={canLinkIRTasks ? async (requirementId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'integration_requirements', requirementId, cardId);
                setSpec(updated);
              } : undefined}
              onUnlinkTask={canLinkIRTasks ? async (requirementId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'integration_requirements', requirementId, cardId);
                setSpec(updated);
              } : undefined}
            />
          )}
          {activeTab === 'ors' && spec && canReadOR && (
            <ObservabilityRequirementsTab
              spec={spec}
              canCreate={canCreateOR}
              canEdit={canEditOR}
              canDelete={canDeleteOR}
              canLinkTask={canLinkORTasks}
              canEditCoverageFlags={canEditCoverageFlags}
              focusEditId={detailsStructuredEditor?.tab === 'ors' && detailsStructuredEditor.mode === 'edit' ? detailsStructuredEditor.entityId || null : null}
              focusCreateToken={detailsStructuredEditor?.tab === 'ors' && detailsStructuredEditor.mode === 'add' ? detailsStructuredEditor.token : null}
              onFocusHandled={clearDetailsStructuredEditor}
              onUpdate={async (requirements) => {
                await syncStructuredCollection('observability_requirements', spec.observability_requirements || [], requirements);
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
              specCards={spec.cards || []}
              onLinkTask={canLinkORTasks ? async (requirementId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'observability_requirements', requirementId, cardId);
                setSpec(updated);
              } : undefined}
              onUnlinkTask={canLinkORTasks ? async (requirementId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'observability_requirements', requirementId, cardId);
                setSpec(updated);
              } : undefined}
            />
          )}
          {activeTab === 'trs' && spec && (
            <TechnicalRequirementsTab
              spec={spec}
              canCreate={canStructured('technical_requirement', 'create')}
              canEdit={canStructured('technical_requirement', 'update')}
              canDelete={canStructured('technical_requirement', 'revoke')}
              canLinkTask={canStructured('technical_requirement', 'link_task')}
              focusEditId={detailsStructuredEditor?.tab === 'trs' && detailsStructuredEditor.mode === 'edit' ? detailsStructuredEditor.entityId || null : null}
              focusCreateToken={detailsStructuredEditor?.tab === 'trs' && detailsStructuredEditor.mode === 'add' ? detailsStructuredEditor.token : null}
              onFocusHandled={clearDetailsStructuredEditor}
              onUpdate={async (trs) => {
                const currentTRs = (spec.technical_requirements || []).map((tr, index) =>
                  typeof tr === 'string'
                    ? { id: `tr_legacy_${index}`, text: tr, linked_task_ids: null }
                    : tr
                ).filter((tr) => (tr.status || 'active') === 'active') as TechnicalRequirement[];
                await syncStructuredCollection('technical_requirements', currentTRs, trs);
              }}
              specCards={spec.cards || []}
              onLinkTask={async (trId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'technical_requirements', trId, cardId);
                setSpec(updated);
              }}
              onUnlinkTask={async (trId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'technical_requirements', trId, cardId);
                setSpec(updated);
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
            />
          )}
          {activeTab === 'decisions' && spec && (
            <DecisionsTab
              spec={spec}
              canCreate={canStructured('decision', 'create')}
              canEdit={canStructured('decision', 'update')}
              canDelete={canStructured('decision', 'revoke')}
              canLinkTask={canStructured('decision', 'link_task')}
              focusEditId={detailsStructuredEditor?.tab === 'decisions' && detailsStructuredEditor.mode === 'edit' ? detailsStructuredEditor.entityId || null : null}
              focusCreateToken={detailsStructuredEditor?.tab === 'decisions' && detailsStructuredEditor.mode === 'add' ? detailsStructuredEditor.token : null}
              onFocusHandled={clearDetailsStructuredEditor}
              onUpdate={async (decisions) => {
                await syncStructuredCollection('decisions', spec.decisions || [], decisions);
              }}
              onSpecUpdate={async (patch) => {
                try {
                  const updated = await api.updateSpec(specId, patch as any);
                  setSpec(updated);
                } catch { toast.error('Failed to update spec'); }
              }}
              specCards={spec.cards || []}
              onLinkTask={async (decisionId, cardId) => {
                const updated = await api.linkTaskToSpecItem(specId, 'decisions', decisionId, cardId);
                setSpec(updated);
              }}
              onUnlinkTask={async (decisionId, cardId) => {
                const updated = await api.unlinkTaskFromSpecItem(specId, 'decisions', decisionId, cardId);
                setSpec(updated);
              }}
            />
          )}
          {activeTab === 'resources' && spec && (
            <div className="space-y-4" data-testid="spec-resources-panel">
              <ResourceGateDisclosure
                boardId={spec.board_id || _boardId}
                entityType="spec"
                entityId={specId}
                refreshKey={resourceGateRefreshKey}
              />
              <AccessibleTabList
                idBase={`spec-${specId}-resources`}
                ariaLabel="Spec resources"
                items={[
                  {
                    id: 'mockups',
                    label: 'Mockups',
                    icon: <Monitor size={13} />,
                    count: spec.screen_mockups?.length || 0,
                  },
                  {
                    id: 'knowledge',
                    label: 'Knowledge',
                    icon: <BookOpen size={13} />,
                    count: spec.knowledge_bases?.length || 0,
                  },
                  {
                    id: 'architecture',
                    label: 'Architecture',
                    icon: <Network size={13} />,
                    count: spec.architecture_designs?.length || 0,
                  },
                ] satisfies {
                  id: ResourceSubTab;
                  label: string;
                  icon: React.ReactNode;
                  count: number;
                }[]}
                value={resourceTab}
                onValueChange={setResourceTab}
                variant="secondary"
                className="max-w-full"
              />

              <AccessibleTabPanel
                idBase={`spec-${specId}-resources`}
                tabId="mockups"
                value={resourceTab}
                mount="lazy-keep"
              >
                  <MockupsTab
                    screenMockups={spec.screen_mockups}
                    boardId={spec.board_id}
                    entityType="spec"
                    entityId={specId}
                    expanded={expanded}
                    onUpdate={async (mockups) => {
                      const updated = await api.updateSpec(specId, { screen_mockups: mockups });
                      setSpec(updated);
                      setResourceGateRefreshKey((value) => value + 1);
                    }}
                  />
              </AccessibleTabPanel>
              <AccessibleTabPanel
                idBase={`spec-${specId}-resources`}
                tabId="knowledge"
                value={resourceTab}
                mount="lazy-keep"
              >
                  <KnowledgeTab
                    specId={specId}
                    boardId={spec.board_id}
                    onChanged={() => {
                      setResourceGateRefreshKey((value) => value + 1);
                      void loadSpec();
                    }}
                  />
              </AccessibleTabPanel>
              <AccessibleTabPanel
                idBase={`spec-${specId}-resources`}
                tabId="architecture"
                value={resourceTab}
                mount="lazy-keep"
              >
                  <ArchitectureTab
                    parentType="spec"
                    parentId={specId}
                    boardId={spec.board_id}
                    entityType="spec"
                    entityId={specId}
                    expanded={expanded}
                    locked={['validated', 'in_progress', 'done'].includes(spec.status)}
                    screenMockups={spec.screen_mockups || []}
                    onChanged={(items) => {
                      setSpec((current) => current
                        ? { ...current, architecture_designs: items }
                        : current);
                      setResourceGateRefreshKey((value) => value + 1);
                      void loadSpec();
                    }}
                  />
              </AccessibleTabPanel>
            </div>
          )}
          {activeTab === 'validation' && spec && (
            <SpecValidationPanel
              anchorTexts={specAnchorTexts}
              boardId={spec.board_id}
              specId={specId}
              specVersion={spec.version}
              specStatus={spec.status}
              canReadChecklist={canReadChecklist}
              canExecuteChecklist={canExecuteChecklist}
              canReadValidation={canReadSpecValidation}
              canReadQuality={canReadQuality}
              canReadPolicyCompliance={canReadPolicyCompliance}
              policyTransitionPreview={policyTransitionPreview}
              policyTransitionRejection={policyTransitionRejection}
              specArchived={spec.archived ?? false}
              validationHistoryRefreshKey={
                validationHistoryRefreshKey
              }
              onAssessmentRecorded={onChanged}
              onPolicyEvaluated={() => {
                void loadAllowedTransitions(spec);
              }}
              onOpenRequirementLintHelp={() =>
                openContextualHelp('requirement-lint')}
            />
          )}
          {activeTab === 'kg' && spec && (
            <KGValidationTab boardId={spec.board_id} specId={specId} />
          )}
          {activeTab === 'activity' && <HistoryTab specId={specId} />}
          {activeTab === 'qa' && <QATab specId={specId} mentionables={mentionables} />}

          {activeTab === 'sprints' && (
            <SpecSprintsTab sprints={linkedSprints} api={api} />
          )}

          {activeTab === 'references' && (
            <div className="space-y-4" data-testid="spec-references-panel">
              <AccessibleTabList
                idBase={`spec-${specId}-references`}
                ariaLabel="Spec references"
                items={[
                  { id: 'origin', label: 'Origin' },
                  {
                    id: 'cards',
                    label: 'Derived cards',
                    count: spec.cards?.length || 0,
                  },
                ] satisfies {
                  id: ReferenceSubTab;
                  label: string;
                  count?: number;
                }[]}
                value={referenceTab}
                onValueChange={setReferenceTab}
                variant="secondary"
                className="max-w-full"
              />

              <AccessibleTabPanel
                idBase={`spec-${specId}-references`}
                tabId="origin"
                value={referenceTab}
                className="space-y-3"
              >
                  {!parentIdeation && !parentRefinement ? (
                    <div className="rounded-xl border border-dashed border-gray-300 px-4 py-8 text-center dark:border-gray-700">
                      <GitBranch size={28} className="mx-auto mb-2 text-gray-300 dark:text-gray-600" />
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        No origin is registered for this spec.
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {parentIdeation && (
                        <button
                          type="button"
                          onClick={() => openIdeationReference(parentIdeation.id)}
                          className="flex w-full items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50/60 px-3 py-3 text-left hover:bg-amber-100/70 dark:border-amber-900/60 dark:bg-amber-950/20 dark:hover:bg-amber-950/35"
                        >
                          <span className="flex min-w-0 items-center gap-2">
                            <Lightbulb size={15} className="shrink-0 text-amber-600" />
                            <span className="truncate text-sm font-medium text-gray-800 dark:text-gray-100">
                              {parentIdeation.title}
                            </span>
                          </span>
                          <span className="shrink-0 text-xs text-amber-600 dark:text-amber-300">
                            Ideation · v{parentIdeation.version}
                          </span>
                        </button>
                      )}
                      {parentRefinement && (
                        <button
                          type="button"
                          onClick={() => openRefinementReference(parentRefinement.id)}
                          className="flex w-full items-center justify-between gap-3 rounded-xl border border-blue-200 bg-blue-50/60 px-3 py-3 text-left hover:bg-blue-100/70 dark:border-blue-900/60 dark:bg-blue-950/20 dark:hover:bg-blue-950/35"
                        >
                          <span className="flex min-w-0 items-center gap-2">
                            <Layers size={15} className="shrink-0 text-blue-600" />
                            <span className="truncate text-sm font-medium text-gray-800 dark:text-gray-100">
                              {parentRefinement.title}
                            </span>
                          </span>
                          <span className="shrink-0 text-xs text-blue-600 dark:text-blue-300">
                            Refinement · v{parentRefinement.version}
                          </span>
                        </button>
                      )}
                    </div>
                  )}
              </AccessibleTabPanel>

              <AccessibleTabPanel
                idBase={`spec-${specId}-references`}
                tabId="cards"
                value={referenceTab}
                className="space-y-2"
              >
                  {(!spec.cards || spec.cards.length === 0) ? (
                    <div className="text-center py-6">
                      <Link2 size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
                      <p className="text-sm text-gray-500 dark:text-gray-400">No linked cards</p>
                      <p className="text-xs text-gray-400 mt-1">Cards are created manually and linked to this spec</p>
                    </div>
                  ) : (
                    spec.cards.map((card) => {
                      const content = (
                        <>
                          <span className="text-sm text-gray-700 dark:text-gray-300 truncate">{card.title}</span>
                          <span className={`text-xs px-1.5 py-0.5 rounded ${CARD_STATUS_COLORS[card.status] || ''}`}>
                            {card.status.replace('_', ' ')}
                          </span>
                        </>
                      );
                      return modalStack ? (
                        <button
                          key={card.id}
                          type="button"
                          onClick={() => modalStack.push({ type: 'card', id: card.id })}
                          className="flex w-full items-center justify-between gap-3 rounded bg-gray-50 px-2 py-1.5 text-left hover:bg-blue-50 dark:bg-gray-700/50 dark:hover:bg-blue-950/20"
                        >
                          {content}
                        </button>
                      ) : (
                        <div key={card.id} className="flex items-center justify-between gap-3 rounded bg-gray-50 px-2 py-1.5 dark:bg-gray-700/50">
                          {content}
                        </div>
                      );
                    })
                  )}
              </AccessibleTabPanel>
            </div>
          )}
          </AccessibleTabPanel>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button onClick={handleDelete} className="text-sm text-red-500 hover:text-red-700 dark:hover:text-red-400">
            Delete spec
          </button>
          <div className="flex items-center gap-2">
            {spec.status === 'approved' && canSubmitValidation && perms.has('spec.validation.submit') && (
              <button
                onClick={() => handleMoveSpec('validated' as SpecStatus)}
                disabled={validating}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-semibold transition-colors
                  bg-purple-500 text-white hover:bg-purple-600 shadow-sm hover:shadow-md
                  disabled:opacity-50"
              >
                <CheckCircle2 size={16} />
                {validating ? 'Validating...' : 'Validate'}
              </button>
            )}
            {['validated', 'in_progress'].includes(spec.status) && (spec.cards?.length || 0) >= 4 && (
              <button
                onClick={async () => {
                  try {
                    const result = await api.suggestSprints(spec.board_id, specId);
                    if (result.suggestions?.length > 1) {
                      setSprintSuggestions(result.suggestions);
                    } else {
                      toast('Not enough tasks to split into sprints', { icon: 'ℹ️' });
                    }
                  } catch { toast.error('Failed to generate suggestions'); }
                }}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors
                  bg-indigo-50 text-indigo-700 hover:bg-indigo-100 dark:bg-indigo-900/30 dark:text-indigo-300 dark:hover:bg-indigo-900/50"
              >
                <Layers size={14} />
                Suggest Sprints
              </button>
            )}
            <button onClick={onClose} className="btn btn-secondary">Close</button>
          </div>
        </div>
      </div>

      {/* Parent modals */}
      {viewingIdeationId && (
        <IdeationModal
          ideationId={viewingIdeationId}
          boardId={_boardId}
          onClose={() => setViewingIdeationId(null)}
          onChanged={loadSpec}
        />
      )}
      {viewingRefinementId && (
        <RefinementModal
          refinementId={viewingRefinementId}
          boardId={_boardId}
          onClose={() => setViewingRefinementId(null)}
          onChanged={loadSpec}
        />
      )}

      {/* Sprint Suggestion Modal */}
      {sprintSuggestions && spec && (
        <SprintSuggestionModal
          boardId={spec.board_id}
          specId={specId}
          suggestions={sprintSuggestions}
          onClose={() => setSprintSuggestions(null)}
          onSkip={() => setSprintSuggestions(null)}
          onCreated={() => { setSprintSuggestions(null); loadSpec(); }}
        />
      )}

      {/* Validation Gate Results Modal */}
      {showValidateModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-[60]" onClick={() => !validating && setShowValidateModal(false)}>
          <div className="bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-lg p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-4">
              {validating ? (
                <>
                  <RefreshCw size={20} className="text-purple-500 animate-spin" />
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Running validation gates...</h3>
                </>
              ) : validateResult.success ? (
                <>
                  <CheckCircle2 size={20} className="text-green-500" />
                  <h3 className="text-lg font-semibold text-green-700 dark:text-green-400">Validation Passed</h3>
                </>
              ) : (
                <>
                  <Ban size={20} className="text-red-500" />
                  <h3 className="text-lg font-semibold text-red-700 dark:text-red-400">Validation Failed</h3>
                </>
              )}
            </div>

            {!validating && (
              <div className="space-y-3">
                {validateResult.success ? (
                  <p className="text-sm text-gray-600 dark:text-gray-400">
                    All coverage gates passed. Spec has been moved to <span className="font-semibold text-purple-600">Validated</span>.
                  </p>
                ) : (
                  <ValidationErrorDisplay error={validateResult.error || ''} />
                )}
                <div className="flex justify-end gap-2 pt-2">
                  <button
                    onClick={() => setShowValidateModal(false)}
                    className="btn btn-secondary text-sm"
                  >
                    Close
                  </button>
                  {!validateResult.success && (
                    <button
                      onClick={() => { setShowValidateModal(false); setActiveTab('tests'); }}
                      className="btn btn-primary text-sm"
                    >
                      Review Coverage
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Spec Validation Gate — submission modal (opens when board opts in and user clicks Validate) */}
      {showSubmitValidationModal && spec && (
        <SubmitSpecValidationModal
          specId={spec.id}
          specTitle={spec.title}
          boardId={spec.board_id}
          specVersion={spec.version}
          settings={boardSettings}
          canReadChecklist={canReadChecklist}
          canExecuteChecklist={canExecuteChecklist}
          onClose={() => setShowSubmitValidationModal(false)}
          onSubmitted={async () => {
            setShowSubmitValidationModal(false);
            await loadSpec();
            setValidationHistoryRefreshKey((current) => current + 1);
            onChanged();
          }}
        />
      )}

      {/* Cancellation justification (ITEM 17) */}
      <CancellationReasonDialog
        open={cancelDialogOpen}
        entityLabel="spec"
        submitting={movingTo === 'cancelled'}
        onConfirm={async (reason) => {
          setCancelDialogOpen(false);
          await handleMoveSpec('cancelled' as SpecStatus, reason);
        }}
        onCancel={() => setCancelDialogOpen(false)}
      />
    </div>
  );
}
