import {
  Component,
  type ErrorInfo,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { v4 as uuidv4 } from 'uuid';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Box,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  File,
  Folder,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
  Unlink,
  X,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { getErrorMessage } from '@/lib/getErrorMessage';
import { useDashboardApi } from '@/services/api';
import type {
  CardSummaryForSpec,
  ProjectStructureMutationOperation,
  ProjectStructureMutationResponse,
  ProjectStructureNode,
  ProjectStructureNodeClassification,
  ProjectStructureNodeKind,
  ProjectStructureNodeState,
  ProjectStructureProjectionResponse,
  ProjectStructureSnapshot,
  ProjectStructureTaskRole,
  ProjectStructureTestRole,
  Spec,
} from '@/types';
import {
  descendantIds,
  flattenProjectStructure,
  nextSiblingPosition,
  parseProjectStructureNodes,
  siblingNodes,
} from './projectStructureModel';

const KIND_LABELS: Record<ProjectStructureNodeKind, string> = {
  folder: 'Folder',
  file: 'File',
  artifact: 'Artifact',
};

const CLASSIFICATION_LABELS: Record<ProjectStructureNodeClassification, string> = {
  as_is: 'AS-IS',
  to_be: 'TO-BE',
  reference_scaffold: 'Reference scaffold',
};

const STATE_LABELS: Record<ProjectStructureNodeState, string> = {
  existing: 'Existing',
  planned: 'Planned',
  modified: 'Modified',
  removed: 'Removed',
};

const TASK_ROLE_LABELS: Record<ProjectStructureTaskRole, string> = {
  create: 'Create',
  modify: 'Modify',
  read: 'Read',
  remove: 'Remove',
};

const TEST_ROLE_LABELS: Record<ProjectStructureTestRole, string> = {
  target: 'Target',
  test_file: 'Test file',
  fixture: 'Fixture',
  integration_point: 'Integration point',
};

interface NodeDraft {
  parent_id: string | null;
  position: number;
  kind: ProjectStructureNodeKind;
  name: string;
  note: string;
  classification: ProjectStructureNodeClassification;
  state: ProjectStructureNodeState | null;
}

interface NodeEditor {
  mode: 'create' | 'edit';
  nodeId: string | null;
  draft: NodeDraft;
}

interface ConflictState {
  operation: ProjectStructureMutationOperation;
  reviewed: boolean;
  message: string;
}

export interface ProjectStructureChange {
  nodes: ProjectStructureNode[];
  specVersion: number;
  structureRevision: number | null;
  state: ProjectStructureSnapshot['state'];
  source: 'read' | 'mutation';
}

export interface ProjectStructureTabProps {
  boardId: string;
  spec: Spec;
  focusNodeId?: string | null;
  canCreate: boolean;
  canUpdate: boolean;
  canRevoke: boolean;
  canRestore: boolean;
  canReorder: boolean;
  canLinkTask: boolean;
  canUnlinkTask: boolean;
  canLinkTest: boolean;
  canUnlinkTest: boolean;
  canLinkEvidence: boolean;
  canUnlinkEvidence: boolean;
  canReadEvidence?: boolean;
  onStructureChange: (change: ProjectStructureChange) => void;
}

function stateFor(nodes: readonly ProjectStructureNode[], authored: boolean) {
  if (!authored) return 'not_authored' as const;
  return nodes.length === 0 ? 'authored_empty' as const : 'authored' as const;
}

function nodeIcon(kind: ProjectStructureNodeKind) {
  if (kind === 'folder') return <Folder size={15} aria-hidden="true" />;
  if (kind === 'file') return <File size={15} aria-hidden="true" />;
  return <Box size={15} aria-hidden="true" />;
}

function defaultDraft(
  nodes: readonly ProjectStructureNode[],
  parentId: string | null,
): NodeDraft {
  return {
    parent_id: parentId,
    position: nextSiblingPosition(nodes, parentId),
    kind: 'file',
    name: '',
    note: '',
    classification: 'to_be',
    state: 'planned',
  };
}

function noteForNode(node: ProjectStructureNode): string {
  if (node.classification === 'reference_scaffold') {
    return node.interpretation_limit?.trim() || node.note;
  }
  return node.note;
}

function draftForNode(node: ProjectStructureNode): NodeDraft {
  return {
    parent_id: node.parent_id,
    position: node.position,
    kind: node.kind,
    name: node.name,
    note: noteForNode(node),
    classification: node.classification,
    state: node.state,
  };
}

function fieldClassName() {
  return 'w-full rounded-md border border-gray-300 bg-white px-2.5 py-2 text-sm text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100';
}

function iconButtonClassName(danger = false) {
  return `inline-flex min-h-8 min-w-8 items-center justify-center rounded-md border p-1.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-40 ${
    danger
      ? 'border-red-200 text-red-600 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/30'
      : 'border-gray-200 text-gray-500 hover:bg-gray-50 hover:text-gray-800 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-100'
  }`;
}

function humanError(error: unknown): string {
  if (error instanceof AuthenticatedFetchError) {
    if (error.status === 403) return 'You do not have permission to change this project structure.';
    if (error.status === 404) return 'This Spec is no longer available.';
    if (error.code === 'project_structure_folder_not_empty') {
      return 'Move or remove the folder contents before removing this folder.';
    }
    if (error.code === 'project_structure_invalid') {
      return 'The project structure is invalid. Review the highlighted node data and try again.';
    }
  }
  return getErrorMessage(error);
}

function isConflict(error: unknown): error is AuthenticatedFetchError {
  return error instanceof AuthenticatedFetchError
    && error.status === 409
    && (!error.code || ['version_conflict', 'idempotency_conflict'].includes(error.code));
}

function cardLabel(card: CardSummaryForSpec): string {
  return card.title?.trim() || 'Unavailable card';
}

function NodeReferences({
  node,
  cards,
  evidence,
  disabled,
  canLinkTask,
  canUnlinkTask,
  canLinkTest,
  canUnlinkTest,
  canLinkEvidence,
  canUnlinkEvidence,
  onOperation,
}: {
  node: ProjectStructureNode;
  cards: CardSummaryForSpec[];
  evidence: Array<{ id: string; label: string }>;
  disabled: boolean;
  canLinkTask: boolean;
  canUnlinkTask: boolean;
  canLinkTest: boolean;
  canUnlinkTest: boolean;
  canLinkEvidence: boolean;
  canUnlinkEvidence: boolean;
  onOperation: (operation: ProjectStructureMutationOperation) => Promise<boolean>;
}) {
  const tasks = cards.filter((card) => !['bug', 'test'].includes(card.card_type ?? 'normal'));
  const tests = cards.filter((card) => card.card_type === 'test');
  const [taskId, setTaskId] = useState('');
  const [taskRole, setTaskRole] = useState<ProjectStructureTaskRole>('modify');
  const [testId, setTestId] = useState('');
  const [testRole, setTestRole] = useState<ProjectStructureTestRole>('target');
  const [evidenceId, setEvidenceId] = useState('');
  const cardById = useMemo(() => new Map(cards.map((card) => [card.id, card])), [cards]);

  const link = async (
    operation: ProjectStructureMutationOperation['operation'],
    reference: Omit<ProjectStructureMutationOperation, 'operation' | 'entity_id'>,
  ) => onOperation({ operation, entity_id: node.id, ...reference });

  return (
    <details className="mt-2 rounded-md border border-gray-200 bg-gray-50/70 dark:border-gray-700 dark:bg-gray-900/30">
      <summary className="cursor-pointer px-2.5 py-2 text-xs font-medium text-gray-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-gray-300">
        References ({node.task_references.length + node.test_references.length + node.evidence_ids.length})
      </summary>
      <div className="space-y-3 border-t border-gray-200 p-2.5 dark:border-gray-700">
        <section aria-labelledby={`project-node-${node.id}-tasks`}>
          <h5 id={`project-node-${node.id}-tasks`} className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Tasks
          </h5>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {node.task_references.map((reference) => (
              <span key={reference.task_id} className="inline-flex max-w-full items-center gap-1 rounded-full bg-blue-50 px-2 py-1 text-[11px] text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
                <span className="truncate">{cardById.has(reference.task_id) ? cardLabel(cardById.get(reference.task_id)!) : 'Unavailable task'}</span>
                <span className="font-semibold">· {TASK_ROLE_LABELS[reference.role]}</span>
                {canUnlinkTask && (
                  <button
                    type="button"
                    aria-label={`Unlink ${cardById.has(reference.task_id) ? cardLabel(cardById.get(reference.task_id)!) : 'unavailable task'}`}
                    disabled={disabled}
                    onClick={() => void link('unlink_task', { task_id: reference.task_id })}
                    className="rounded-full p-0.5 hover:bg-blue-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-blue-900"
                  >
                    <Unlink size={11} />
                  </button>
                )}
              </span>
            ))}
          </div>
          {canLinkTask && tasks.length > 0 && (
            <div className="mt-2 grid grid-cols-[minmax(0,1fr)_auto_auto] gap-1.5">
              <select aria-label={`Task to link to ${node.name}`} value={taskId} onChange={(event) => setTaskId(event.target.value)} className={fieldClassName()}>
                <option value="">Select task…</option>
                {tasks.filter((card) => !node.task_references.some((reference) => reference.task_id === card.id)).map((card) => (
                  <option key={card.id} value={card.id}>{cardLabel(card)}</option>
                ))}
              </select>
              <select aria-label="Task role" value={taskRole} onChange={(event) => setTaskRole(event.target.value as ProjectStructureTaskRole)} className={fieldClassName()}>
                {Object.entries(TASK_ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
              <button type="button" disabled={disabled || !taskId} onClick={async () => {
                if (await link('link_task', { task_id: taskId, task_role: taskRole })) setTaskId('');
              }} className="btn btn-secondary text-xs disabled:opacity-40">Link</button>
            </div>
          )}
        </section>

        <section aria-labelledby={`project-node-${node.id}-tests`}>
          <h5 id={`project-node-${node.id}-tests`} className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Tests
          </h5>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {node.test_references.map((reference) => (
              <span key={reference.test_id} className="inline-flex max-w-full items-center gap-1 rounded-full bg-purple-50 px-2 py-1 text-[11px] text-purple-700 dark:bg-purple-950/40 dark:text-purple-300">
                <span className="truncate">{cardById.has(reference.test_id) ? cardLabel(cardById.get(reference.test_id)!) : 'Unavailable test'}</span>
                <span className="font-semibold">· {TEST_ROLE_LABELS[reference.role]}</span>
                {canUnlinkTest && (
                  <button type="button" aria-label={`Unlink ${cardById.has(reference.test_id) ? cardLabel(cardById.get(reference.test_id)!) : 'unavailable test'}`} disabled={disabled} onClick={() => void link('unlink_test', { test_id: reference.test_id })} className="rounded-full p-0.5 hover:bg-purple-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-purple-500 dark:hover:bg-purple-900">
                    <Unlink size={11} />
                  </button>
                )}
              </span>
            ))}
          </div>
          {canLinkTest && tests.length > 0 && (
            <div className="mt-2 grid grid-cols-[minmax(0,1fr)_auto_auto] gap-1.5">
              <select aria-label={`Test to link to ${node.name}`} value={testId} onChange={(event) => setTestId(event.target.value)} className={fieldClassName()}>
                <option value="">Select test…</option>
                {tests.filter((card) => !node.test_references.some((reference) => reference.test_id === card.id)).map((card) => (
                  <option key={card.id} value={card.id}>{cardLabel(card)}</option>
                ))}
              </select>
              <select aria-label="Test role" value={testRole} onChange={(event) => setTestRole(event.target.value as ProjectStructureTestRole)} className={fieldClassName()}>
                {Object.entries(TEST_ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
              <button type="button" disabled={disabled || !testId} onClick={async () => {
                if (await link('link_test', { test_id: testId, test_role: testRole })) setTestId('');
              }} className="btn btn-secondary text-xs disabled:opacity-40">Link</button>
            </div>
          )}
        </section>

        <section aria-labelledby={`project-node-${node.id}-evidence`}>
          <h5 id={`project-node-${node.id}-evidence`} className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Code Evidence
          </h5>
          {node.classification === 'to_be' && (
            <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">TO-BE nodes cannot reference AS-IS Code Evidence.</p>
          )}
          <div className="mt-1 flex flex-wrap gap-1.5">
            {node.evidence_ids.map((id) => {
              const evidenceLabel = evidence.find((item) => item.id === id)?.label || 'Unavailable evidence';
              return (
              <span key={id} className="inline-flex max-w-full items-center gap-1 rounded-full bg-emerald-50 px-2 py-1 text-[11px] text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                <span className="truncate">{evidenceLabel}</span>
                {canUnlinkEvidence && (
                  <button type="button" aria-label={`Unlink ${evidenceLabel}`} disabled={disabled} onClick={() => void link('unlink_evidence', { evidence_id: id })} className="rounded-full p-0.5 hover:bg-emerald-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:hover:bg-emerald-900">
                    <Unlink size={11} />
                  </button>
                )}
              </span>
              );
            })}
          </div>
          {canLinkEvidence && node.classification !== 'to_be' && evidence.length > 0 && (
            <div className="mt-2 flex gap-1.5">
              <select
                aria-label={`Code Evidence to link to ${node.name}`}
                value={evidenceId}
                onChange={(event) => setEvidenceId(event.target.value)}
                className={fieldClassName()}
              >
                <option value="">Select Code Evidence…</option>
                {evidence.filter((item) => !node.evidence_ids.includes(item.id)).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
              <button type="button" disabled={disabled || !evidenceId.trim()} onClick={async () => {
                if (await link('link_evidence', { evidence_id: evidenceId.trim() })) setEvidenceId('');
              }} className="btn btn-secondary text-xs disabled:opacity-40">Link</button>
            </div>
          )}
        </section>
      </div>
    </details>
  );
}

export function ProjectStructureTab({
  boardId,
  spec,
  focusNodeId,
  canCreate,
  canUpdate,
  canRevoke,
  canRestore,
  canReorder,
  canLinkTask,
  canUnlinkTask,
  canLinkTest,
  canUnlinkTest,
  canLinkEvidence,
  canUnlinkEvidence,
  canReadEvidence = false,
  onStructureChange,
}: ProjectStructureTabProps) {
  const api = useDashboardApi();
  const initial = useMemo(
    () => parseProjectStructureNodes(spec.project_structure).nodes,
    [spec.project_structure],
  );
  const [nodes, setNodes] = useState<ProjectStructureNode[]>(initial);
  const [snapshotState, setSnapshotState] = useState<ProjectStructureSnapshot['state']>(
    stateFor(initial, spec.project_structure !== null && spec.project_structure !== undefined),
  );
  const [specVersion, setSpecVersion] = useState(spec.version);
  const [structureRevision, setStructureRevision] = useState<number>(
    spec.project_structure_revision ?? 0,
  );
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [readIssues, setReadIssues] = useState<string[]>([]);
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [mode, setMode] = useState<'view' | 'edit'>('view');
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [editor, setEditor] = useState<NodeEditor | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [busyNodeId, setBusyNodeId] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [evidenceOptions, setEvidenceOptions] = useState<Array<{ id: string; label: string }>>([]);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const expansionInitializedFor = useRef<string | null>(null);
  const onStructureChangeRef = useRef(onStructureChange);
  useEffect(() => {
    onStructureChangeRef.current = onStructureChange;
  }, [onStructureChange]);
  const readOnly = !(
    canCreate
    || canUpdate
    || canRevoke
    || canRestore
    || canReorder
    || canLinkTask
    || canUnlinkTask
    || canLinkTest
    || canUnlinkTest
    || canLinkEvidence
    || canUnlinkEvidence
  );
  const isEditing = mode === 'edit' && !readOnly;

  const publishSnapshot = useCallback((snapshot: ProjectStructureSnapshot) => {
    const parsed = parseProjectStructureNodes(snapshot.nodes);
    setNodes(parsed.nodes);
    setReadIssues(parsed.issues);
    setSnapshotState(snapshot.state);
    setSpecVersion(snapshot.spec_version);
    setStructureRevision(snapshot.structure_revision);
    onStructureChangeRef.current({
      nodes: parsed.nodes,
      specVersion: snapshot.spec_version,
      structureRevision: snapshot.structure_revision,
      state: snapshot.state,
      source: 'read',
    });
    return parsed.nodes;
  }, []);

  const loadSnapshot = useCallback(async (reviewConflict = false) => {
    setLoading(true);
    setLoadError(null);
    try {
      const snapshot = await api.getProjectStructure(boardId, spec.id);
      const loadedNodes = publishSnapshot(snapshot);
      if (reviewConflict) {
        setConflict((current) => current ? { ...current, reviewed: true } : current);
      }
      if (expansionInitializedFor.current !== spec.id) {
        expansionInitializedFor.current = spec.id;
        setExpandedIds(new Set(
          loadedNodes
            .filter((node) => node.kind === 'folder' && node.status === 'active')
            .map((node) => node.id),
        ));
      }
    } catch (error) {
      setLoadError(humanError(error));
    } finally {
      setLoading(false);
    }
  }, [api, boardId, publishSnapshot, spec.id]);

  useEffect(() => {
    expansionInitializedFor.current = null;
    setMode('view');
    setEditor(null);
    setConflict(null);
    void loadSnapshot();
  }, [loadSnapshot, spec.id]);

  useEffect(() => {
    if (!readOnly) return;
    setMode('view');
    setEditor(null);
    setConflict(null);
  }, [readOnly]);

  useEffect(() => {
    if (!canReadEvidence) {
      setEvidenceOptions([]);
      return;
    }
    let active = true;
    api.getCodeTraceabilityProjection(boardId, 'spec', spec.id, spec.version, {
      profile: 'detail',
    }).then((projection) => {
      if (!active) return;
      setEvidenceOptions(
        projection.evidence
          .filter((item) => item.lifecycle_status === 'active')
          .map((item) => ({
            id: item.id,
            label: item.claim?.trim() || item.relative_path || item.id,
          })),
      );
    }).catch(() => {
      if (active) setEvidenceOptions([]);
    });
    return () => { active = false; };
  }, [api, boardId, canReadEvidence, spec.id, spec.version]);

  const flattened = useMemo(
    () => flattenProjectStructure(nodes, expandedIds, includeRevoked),
    [expandedIds, includeRevoked, nodes],
  );
  const combinedIssues = useMemo(
    () => Array.from(new Set([...readIssues, ...flattened.issues])),
    [flattened.issues, readIssues],
  );

  useEffect(() => {
    if (!focusNodeId || loading) return;
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const ancestors = new Set<string>();
    let cursor = byId.get(focusNodeId);
    const lineage = new Set<string>();
    while (cursor?.parent_id && !lineage.has(cursor.id)) {
      lineage.add(cursor.id);
      ancestors.add(cursor.parent_id);
      cursor = byId.get(cursor.parent_id);
    }
    setExpandedIds((current) => new Set([...current, ...ancestors]));
    const frame = window.requestAnimationFrame(() => {
      const row = rowRefs.current.get(focusNodeId);
      row?.focus();
      row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusNodeId, loading, nodes]);

  const applyResult = useCallback((result: ProjectStructureMutationResponse) => {
    const parsed = parseProjectStructureNodes(result.nodes);
    const state = stateFor(parsed.nodes, true);
    setNodes(parsed.nodes);
    setReadIssues(parsed.issues);
    setSnapshotState(state);
    setSpecVersion(result.spec_version);
    setStructureRevision(result.structure_revision);
    setConflict(null);
    onStructureChangeRef.current({
      nodes: parsed.nodes,
      specVersion: result.spec_version,
      structureRevision: result.structure_revision,
      state,
      source: 'mutation',
    });
  }, []);

  const performOperation = useCallback(async (
    operation: ProjectStructureMutationOperation,
  ): Promise<boolean> => {
    setBusyNodeId(operation.entity_id ?? '__collection__');
    setEditorError(null);
    try {
      const result = await api.mutateProjectStructure(boardId, spec.id, {
        expected_spec_version: specVersion,
        expected_structure_revision: structureRevision,
        idempotency_key: `project-structure-ui-${uuidv4()}`,
        operations: [operation],
      });
      applyResult(result);
      return true;
    } catch (error) {
      if (isConflict(error)) {
        setConflict({
          operation,
          reviewed: false,
          message: 'The Spec changed while you were editing. Your input is preserved.',
        });
      } else {
        setEditorError(humanError(error));
      }
      return false;
    } finally {
      setBusyNodeId(null);
    }
  }, [api, applyResult, boardId, spec.id, specVersion, structureRevision]);

  const openCreate = (parentId: string | null) => {
    setEditor({ mode: 'create', nodeId: null, draft: defaultDraft(nodes, parentId) });
    setEditorError(null);
    setConflict(null);
  };

  const openEdit = (node: ProjectStructureNode) => {
    setEditor({ mode: 'edit', nodeId: node.id, draft: draftForNode(node) });
    setEditorError(null);
    setConflict(null);
  };

  const updateDraft = (patch: Partial<NodeDraft>) => {
    setEditor((current) => current
      ? { ...current, draft: { ...current.draft, ...patch } }
      : current);
    setEditorError(null);
    setConflict(null);
  };

  const saveEditor = async () => {
    if (!editor) return;
    const name = editor.draft.name.trim();
    if (!name) {
      setEditorError('Name is required.');
      return;
    }
    const note = editor.draft.note.trim();
    if (editor.draft.classification === 'reference_scaffold' && !note) {
      setEditorError('Reference scaffold nodes require a Note / Description that states what the scaffold does not prove.');
      return;
    }
    const existing = editor.nodeId
      ? nodes.find((node) => node.id === editor.nodeId) ?? null
      : null;
    if (editor.draft.classification === 'to_be' && (existing?.evidence_ids.length ?? 0) > 0) {
      setEditorError('Unlink Code Evidence before classifying this node as TO-BE.');
      return;
    }
    if (editor.draft.kind !== 'folder'
      && existing
      && nodes.some((node) => node.parent_id === existing.id && node.status === 'active')) {
      setEditorError('A node with children must remain a folder.');
      return;
    }

    const payload: Record<string, unknown> = {
      parent_id: editor.draft.parent_id,
      position: editor.draft.position,
      kind: editor.draft.kind,
      name,
      note,
      classification: editor.draft.classification,
      state: editor.draft.state,
      interpretation_limit: editor.draft.classification === 'reference_scaffold'
        ? note
        : null,
    };
    if (editor.mode === 'create') {
      Object.assign(payload, {
        status: 'active',
        task_references: [],
        test_references: [],
        evidence_ids: [],
      });
    }
    const success = await performOperation({
      operation: editor.mode === 'create' ? 'create' : 'update',
      ...(editor.nodeId ? { entity_id: editor.nodeId } : {}),
      payload,
    });
    if (success) {
      setEditor(null);
      toast.success(editor.mode === 'create' ? 'Project node created' : 'Project node saved');
    }
  };

  const moveWithinSiblings = async (node: ProjectStructureNode, delta: -1 | 1) => {
    const siblings = siblingNodes(nodes, node.parent_id);
    const index = siblings.findIndex((item) => item.id === node.id);
    const nextIndex = index + delta;
    if (index < 0 || nextIndex < 0 || nextIndex >= siblings.length) return;
    const reordered = siblings.map((item) => item.id);
    [reordered[index], reordered[nextIndex]] = [reordered[nextIndex], reordered[index]];
    if (await performOperation({
      operation: 'reorder',
      payload: { parent_id: node.parent_id, ordered_ids: reordered },
    })) {
      window.requestAnimationFrame(() => rowRefs.current.get(node.id)?.focus());
    }
  };

  const moveToParent = async (node: ProjectStructureNode, parentId: string | null) => {
    if (parentId === node.parent_id) return;
    if (await performOperation({
      operation: 'update',
      entity_id: node.id,
      payload: {
        parent_id: parentId,
        position: nextSiblingPosition(nodes, parentId),
      },
    })) {
      if (parentId) setExpandedIds((current) => new Set(current).add(parentId));
      window.requestAnimationFrame(() => rowRefs.current.get(node.id)?.focus());
    }
  };

  const handleTreeKeyDown = (
    event: KeyboardEvent<HTMLDivElement>,
    node: ProjectStructureNode,
    rowIndex: number,
  ) => {
    if (isEditing && event.altKey && canReorder && event.key === 'ArrowUp') {
      event.preventDefault();
      void moveWithinSiblings(node, -1);
      return;
    }
    if (isEditing && event.altKey && canReorder && event.key === 'ArrowDown') {
      event.preventDefault();
      void moveWithinSiblings(node, 1);
      return;
    }
    if (isEditing && event.altKey && canUpdate && event.key === 'ArrowLeft' && node.parent_id) {
      event.preventDefault();
      const parent = nodes.find((item) => item.id === node.parent_id);
      void moveToParent(node, parent?.parent_id ?? null);
      return;
    }
    if (isEditing && event.altKey && canUpdate && event.key === 'ArrowRight') {
      const siblings = siblingNodes(nodes, node.parent_id);
      const index = siblings.findIndex((item) => item.id === node.id);
      const previous = index > 0 ? siblings[index - 1] : null;
      if (previous?.kind === 'folder') {
        event.preventDefault();
        void moveToParent(node, previous.id);
      }
      return;
    }
    if (isEditing && event.altKey && event.key.toLowerCase() === 'm') {
      event.preventDefault();
      document.getElementById(`project-node-${node.id}-move-to`)?.focus();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      rowRefs.current.get(flattened.rows[rowIndex + 1]?.node.id ?? '')?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      rowRefs.current.get(flattened.rows[rowIndex - 1]?.node.id ?? '')?.focus();
    } else if (event.key === 'ArrowRight' && node.kind === 'folder') {
      event.preventDefault();
      setExpandedIds((current) => new Set(current).add(node.id));
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (expandedIds.has(node.id)) {
        setExpandedIds((current) => {
          const next = new Set(current);
          next.delete(node.id);
          return next;
        });
      } else if (node.parent_id) {
        rowRefs.current.get(node.parent_id)?.focus();
      }
    }
  };

  const retryConflict = async () => {
    if (!conflict?.reviewed) return;
    if (editor) {
      await saveEditor();
      return;
    }
    await performOperation(conflict.operation);
  };

  if (loading && nodes.length === 0) {
    return (
      <div role="status" className="flex items-center justify-center gap-2 rounded-lg border border-gray-200 py-12 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
        <Loader2 size={16} className="animate-spin" /> Loading project structure…
      </div>
    );
  }

  if (loadError && nodes.length === 0) {
    return (
      <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
        <p>{loadError}</p>
        <button type="button" onClick={() => void loadSnapshot()} className="btn btn-secondary mt-3 inline-flex items-center gap-1.5 text-xs">
          <RefreshCw size={13} /> Retry
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-3" data-testid="project-structure-tab">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Project structure</h3>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Each node keeps its single canonical note directly below its name and metadata.
            {structureRevision !== null ? ` Revision ${structureRevision}.` : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {!readOnly && (
            <div role="group" aria-label="Project structure mode" className="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5 dark:border-gray-700 dark:bg-gray-900/50">
              <button
                type="button"
                aria-pressed={mode === 'view'}
                onClick={() => setMode('view')}
                className={`rounded px-2.5 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${mode === 'view' ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-gray-100' : 'text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100'}`}
              >
                View
              </button>
              <button
                type="button"
                aria-pressed={mode === 'edit'}
                onClick={() => setMode('edit')}
                className={`rounded px-2.5 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${mode === 'edit' ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-gray-100' : 'text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-100'}`}
              >
                Edit
              </button>
            </div>
          )}
          {nodes.some((node) => node.status === 'revoked') && (
            <label className="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
              <input type="checkbox" checked={includeRevoked} onChange={(event) => setIncludeRevoked(event.target.checked)} />
              Show removed
            </label>
          )}
          <button type="button" onClick={() => void loadSnapshot()} disabled={loading || busyNodeId !== null} className="btn btn-secondary inline-flex items-center gap-1.5 text-xs">
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh
          </button>
          {isEditing && canCreate && (
            <button type="button" onClick={() => openCreate(null)} disabled={busyNodeId !== null} className="btn btn-primary inline-flex items-center gap-1.5 text-xs">
              <Plus size={13} /> Add root
            </button>
          )}
        </div>
      </div>

      {readOnly && (
        <div role="note" className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300">
          Read-only. You can review the canonical structure, classifications and references.
        </div>
      )}

      {combinedIssues.length > 0 && (
        <div role="alert" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
          <div className="flex items-center gap-1.5 font-semibold"><AlertTriangle size={13} /> Degraded structure data</div>
          <p className="mt-1">{combinedIssues[0]}{combinedIssues.length > 1 ? ` (+${combinedIssues.length - 1} more)` : ''}</p>
        </div>
      )}

      {isEditing && conflict && (
        <div role="alert" className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          <div className="flex items-start gap-2">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="font-semibold">Concurrent change</p>
              <p className="mt-0.5 text-xs">{conflict.message}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button type="button" onClick={() => void loadSnapshot(true)} disabled={loading} className="btn btn-secondary inline-flex items-center gap-1.5 text-xs">
                  <RefreshCw size={12} /> Refresh & review
                </button>
                <button type="button" onClick={() => void retryConflict()} disabled={!conflict.reviewed || busyNodeId !== null} className="btn btn-primary text-xs disabled:opacity-40">
                  Retry saved input
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {isEditing && editor && editor.mode === 'create' && (
        <NodeEditorForm
          editor={editor}
          nodes={nodes}
          busy={busyNodeId !== null}
          error={editorError}
          onChange={updateDraft}
          onSave={saveEditor}
          onCancel={() => { setEditor(null); setEditorError(null); setConflict(null); }}
        />
      )}

      {snapshotState === 'not_authored' && nodes.length === 0 && !editor && (
        <div className="rounded-lg border border-dashed border-gray-300 px-5 py-12 text-center dark:border-gray-600">
          <Folder size={24} className="mx-auto text-gray-400" />
          <p className="mt-2 text-sm font-medium text-gray-700 dark:text-gray-200">No project structure has been authored</p>
          <p className="mx-auto mt-1 max-w-lg text-xs text-gray-500 dark:text-gray-400">
            This is optional. Add a root only when a concrete folder, file or artifact view improves delivery clarity.
          </p>
          {isEditing && canCreate && <button type="button" onClick={() => openCreate(null)} className="btn btn-primary mt-4 text-xs">Start project structure</button>}
        </div>
      )}

      {snapshotState === 'authored_empty' && nodes.length === 0 && !editor && (
        <div className="rounded-lg border border-gray-200 px-5 py-10 text-center dark:border-gray-700">
          <p className="text-sm font-medium text-gray-700 dark:text-gray-200">The project structure is intentionally empty</p>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">The authored-empty state is preserved separately from “not authored”.</p>
        </div>
      )}

      {nodes.length > 0 && (
        <div role="tree" aria-label="Project structure tree" className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
          {flattened.rows.map(({ node, depth, hasChildren }, rowIndex) => {
            const isExpanded = expandedIds.has(node.id);
            const currentEditor = isEditing && editor?.mode === 'edit' && editor.nodeId === node.id
              ? editor
              : null;
            const siblings = siblingNodes(nodes, node.parent_id);
            const siblingIndex = siblings.findIndex((item) => item.id === node.id);
            const descendants = descendantIds(nodes, node.id);
            const parentOptions = nodes.filter((candidate) => (
              candidate.kind === 'folder'
              && candidate.status === 'active'
              && candidate.id !== node.id
              && !descendants.has(candidate.id)
            ));
            const hasActiveChildren = nodes.some((candidate) => candidate.parent_id === node.id && candidate.status === 'active');
            const referenceCount = node.task_references.length + node.test_references.length + node.evidence_ids.length;
            const note = noteForNode(node);
            return (
              <div
                key={node.id}
                ref={(element) => {
                  if (element) rowRefs.current.set(node.id, element);
                  else rowRefs.current.delete(node.id);
                }}
                role="treeitem"
                aria-level={depth + 1}
                aria-expanded={hasChildren ? isExpanded : undefined}
                aria-selected={focusNodeId === node.id}
                tabIndex={focusNodeId === node.id || (!focusNodeId && rowIndex === 0) ? 0 : -1}
                onKeyDown={(event) => handleTreeKeyDown(event, node, rowIndex)}
                data-node-id={node.id}
                className={`border-b border-gray-100 px-3 py-3 outline-none last:border-b-0 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500 dark:border-gray-700/70 ${
                  node.status === 'revoked' ? 'bg-gray-50/70 opacity-75 dark:bg-gray-900/20' : ''
                } ${focusNodeId === node.id ? 'bg-blue-50/50 dark:bg-blue-950/20' : ''}`}
                style={{ paddingLeft: `${12 + depth * 22}px` }}
              >
                <div className="flex min-w-0 items-start gap-1.5">
                  {node.kind === 'folder' ? (
                    <button
                      type="button"
                      aria-label={`${isExpanded ? 'Collapse' : 'Expand'} ${node.name}`}
                      onClick={() => setExpandedIds((current) => {
                        const next = new Set(current);
                        if (next.has(node.id)) next.delete(node.id);
                        else next.add(node.id);
                        return next;
                      })}
                      className="mt-0.5 rounded p-0.5 text-gray-400 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-gray-700"
                    >
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                  ) : <span className="w-[18px] shrink-0" />}
                  <span className="mt-0.5 shrink-0 text-gray-500 dark:text-gray-400">{nodeIcon(node.kind)}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="break-words text-sm font-medium text-gray-900 dark:text-gray-100">{node.name}</span>
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-300">{CLASSIFICATION_LABELS[node.classification]}</span>
                      {node.state && <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">{STATE_LABELS[node.state]}</span>}
                      {node.status === 'revoked' && <span className="rounded bg-red-50 px-1.5 py-0.5 text-[10px] text-red-700 dark:bg-red-950/40 dark:text-red-300">Removed</span>}
                    </div>
                  </div>
                </div>

                <div className="ml-[42px] mt-1.5">
                  {currentEditor ? (
                    <NodeEditorForm
                      editor={currentEditor}
                      nodes={nodes}
                      busy={busyNodeId !== null}
                      error={editorError}
                      onChange={updateDraft}
                      onSave={saveEditor}
                      onCancel={() => { setEditor(null); setEditorError(null); setConflict(null); }}
                    />
                  ) : (
                    <div>
                      <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Note / Description</p>
                      <div className="group/note mt-0.5 flex min-w-0 items-start gap-2">
                        <p className={`min-w-0 flex-1 whitespace-pre-wrap break-words text-sm leading-5 ${note ? 'text-gray-600 dark:text-gray-300' : 'italic text-gray-400'}`}>
                          {note || 'No note'}
                        </p>
                        {isEditing && node.status === 'active' && canUpdate && (
                          <button type="button" aria-label={`Edit ${node.name}`} onClick={() => openEdit(node)} disabled={busyNodeId !== null} className={`${iconButtonClassName()} opacity-70 group-hover/note:opacity-100 focus:opacity-100`} title="Edit node">
                            <Pencil size={13} />
                          </button>
                        )}
                      </div>
                    </div>
                  )}
                </div>

                {isEditing && !currentEditor && (
                  <div className="ml-[42px] mt-2 flex flex-wrap items-center gap-1.5">
                    {node.status === 'active' && node.kind === 'folder' && canCreate && (
                      <button type="button" aria-label={`Add child to ${node.name}`} onClick={() => { setExpandedIds((current) => new Set(current).add(node.id)); openCreate(node.id); }} disabled={busyNodeId !== null} className={iconButtonClassName()} title="Add child">
                        <Plus size={13} />
                      </button>
                    )}
                    {node.status === 'active' && canReorder && (
                      <>
                        <button type="button" aria-label={`Move ${node.name} up`} onClick={() => void moveWithinSiblings(node, -1)} disabled={busyNodeId !== null || siblingIndex <= 0} className={iconButtonClassName()} title="Move up (Alt+↑)"><ArrowUp size={13} /></button>
                        <button type="button" aria-label={`Move ${node.name} down`} onClick={() => void moveWithinSiblings(node, 1)} disabled={busyNodeId !== null || siblingIndex < 0 || siblingIndex >= siblings.length - 1} className={iconButtonClassName()} title="Move down (Alt+↓)"><ArrowDown size={13} /></button>
                      </>
                    )}
                    {node.status === 'active' && canUpdate && (
                      <label className="inline-flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400">
                        <span>Move to</span>
                        <select
                          id={`project-node-${node.id}-move-to`}
                          aria-label={`Move ${node.name} to another folder`}
                          value={node.parent_id ?? ''}
                          disabled={busyNodeId !== null}
                          onChange={(event) => void moveToParent(node, event.target.value || null)}
                          className="rounded border border-gray-200 bg-white px-1.5 py-1 text-[11px] text-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
                        >
                          <option value="">Root</option>
                          {parentOptions.map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}
                        </select>
                      </label>
                    )}
                    {node.status === 'active' && canRevoke && (
                      <button type="button" aria-label={`Remove ${node.name}`} onClick={() => {
                        if (hasActiveChildren) {
                          setEditorError('Move or remove this folder’s children first.');
                          return;
                        }
                        if (window.confirm(`Remove “${node.name}” from the active project structure?`)) {
                          void performOperation({ operation: 'revoke', entity_id: node.id });
                        }
                      }} disabled={busyNodeId !== null || hasActiveChildren} className={iconButtonClassName(true)} title={hasActiveChildren ? 'Move or remove child nodes first' : 'Remove'}>
                        <Trash2 size={13} />
                      </button>
                    )}
                    {node.status === 'revoked' && canRestore && (
                      <button type="button" onClick={() => void performOperation({ operation: 'restore', entity_id: node.id, position: nextSiblingPosition(nodes, node.parent_id) })} disabled={busyNodeId !== null} className="btn btn-secondary inline-flex items-center gap-1 text-xs">
                        <RotateCcw size={12} /> Restore
                      </button>
                    )}
                  </div>
                )}

                {node.status === 'active' && (isEditing || referenceCount > 0) && (
                  <div className="ml-[42px]">
                    <NodeReferences
                      node={node}
                      cards={spec.cards ?? []}
                      evidence={evidenceOptions}
                      disabled={busyNodeId !== null}
                      canLinkTask={isEditing && canLinkTask}
                      canUnlinkTask={isEditing && canUnlinkTask}
                      canLinkTest={isEditing && canLinkTest}
                      canUnlinkTest={isEditing && canUnlinkTest}
                      canLinkEvidence={isEditing && canLinkEvidence}
                      canUnlinkEvidence={isEditing && canUnlinkEvidence}
                      onOperation={performOperation}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {isEditing && editorError && !editor && (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">{editorError}</div>
      )}

      <p className="text-[11px] text-gray-500 dark:text-gray-400">
        {isEditing
          ? 'Keyboard: ↑/↓ navigates; ←/→ collapses or expands; Alt+↑/↓ reorders; Alt+←/→ outdents or indents; Alt+M focuses “Move to”. Save an editor with Ctrl/Cmd+Enter.'
          : 'Keyboard: ↑/↓ navigates; ←/→ collapses or expands.'}
      </p>
    </div>
  );
}

function NodeEditorForm({
  editor,
  nodes,
  busy,
  error,
  compact = false,
  onChange,
  onSave,
  onCancel,
}: {
  editor: NodeEditor;
  nodes: ProjectStructureNode[];
  busy: boolean;
  error: string | null;
  compact?: boolean;
  onChange: (patch: Partial<NodeDraft>) => void;
  onSave: () => Promise<void>;
  onCancel: () => void;
}) {
  return (
    <form
      aria-label={editor.mode === 'create' ? 'Create project node' : 'Edit project node'}
      className={`space-y-3 rounded-lg border border-blue-200 bg-blue-50/40 p-3 dark:border-blue-900 dark:bg-blue-950/20 ${compact ? 'border-0 bg-transparent p-0 dark:bg-transparent' : ''}`}
      onSubmit={(event) => { event.preventDefault(); void onSave(); }}
      onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
          event.preventDefault();
          void onSave();
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          onCancel();
        }
      }}
    >
      {!compact && (
        <NodeMetadataFields
          editor={editor}
          nodes={nodes}
          onChange={onChange}
        />
      )}
      <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
        Note / Description
        <textarea
          value={editor.draft.note}
          maxLength={4000}
          rows={compact ? 6 : 4}
          onChange={(event) => onChange({ note: event.target.value })}
          placeholder={editor.draft.classification === 'reference_scaffold'
            ? 'Describe the scaffold and what it does not prove.'
            : 'One concise note for this node'}
          className={`mt-1 resize-y ${fieldClassName()}`}
        />
        <span className="mt-1 block text-right text-[10px] font-normal text-gray-400">{editor.draft.note.length}/4000</span>
      </label>
      {error && <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">{error}</div>}
      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={onCancel} disabled={busy} className="btn btn-secondary inline-flex items-center gap-1 text-xs"><X size={12} /> Cancel</button>
        <button type="submit" disabled={busy || !editor.draft.name.trim()} className="btn btn-primary inline-flex items-center gap-1 text-xs disabled:opacity-40">
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Save
        </button>
      </div>
    </form>
  );
}

function NodeMetadataFields({
  editor,
  nodes,
  onChange,
  onSave,
  onCancel,
}: {
  editor: NodeEditor;
  nodes: ProjectStructureNode[];
  onChange: (patch: Partial<NodeDraft>) => void;
  onSave?: () => Promise<void>;
  onCancel?: () => void;
}) {
  const excluded = editor.nodeId ? descendantIds(nodes, editor.nodeId) : new Set<string>();
  const parentOptions = nodes.filter((node) => (
    node.kind === 'folder'
    && node.status === 'active'
    && node.id !== editor.nodeId
    && !excluded.has(node.id)
  ));
  const formId = `project-structure-${editor.mode}-${editor.nodeId ?? 'new'}`;

  return (
    <div
      className="space-y-2"
      onKeyDown={(event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && onSave) {
          event.preventDefault();
          void onSave();
        }
        if (event.key === 'Escape' && onCancel) {
          event.preventDefault();
          onCancel();
        }
      }}
    >
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="text-xs font-medium text-gray-700 dark:text-gray-300">
          Name
          <input autoFocus id={`${formId}-name`} value={editor.draft.name} maxLength={255} onChange={(event) => onChange({ name: event.target.value })} className={`mt-1 ${fieldClassName()}`} />
        </label>
        <label className="text-xs font-medium text-gray-700 dark:text-gray-300">
          Type
          <select value={editor.draft.kind} onChange={(event) => onChange({ kind: event.target.value as ProjectStructureNodeKind })} className={`mt-1 ${fieldClassName()}`}>
            {Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="text-xs font-medium text-gray-700 dark:text-gray-300">
          Classification
          <select value={editor.draft.classification} onChange={(event) => onChange({ classification: event.target.value as ProjectStructureNodeClassification })} className={`mt-1 ${fieldClassName()}`}>
            {Object.entries(CLASSIFICATION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="text-xs font-medium text-gray-700 dark:text-gray-300">
          Delivery state
          <select value={editor.draft.state ?? ''} onChange={(event) => onChange({ state: event.target.value ? event.target.value as ProjectStructureNodeState : null })} className={`mt-1 ${fieldClassName()}`}>
            <option value="">Not specified</option>
            {Object.entries(STATE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="text-xs font-medium text-gray-700 dark:text-gray-300 sm:col-span-2">
          Parent
          <select value={editor.draft.parent_id ?? ''} onChange={(event) => onChange({ parent_id: event.target.value || null, position: nextSiblingPosition(nodes, event.target.value || null) })} className={`mt-1 ${fieldClassName()}`}>
            <option value="">Root</option>
            {parentOptions.map((node) => <option key={node.id} value={node.id}>{node.name}</option>)}
          </select>
        </label>
      </div>
    </div>
  );
}

export function ProjectStructureProjectionPanel({
  projection,
  loading,
  error,
  onRetry,
  onOpenFull,
}: {
  projection: ProjectStructureProjectionResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onOpenFull: (nodeId?: string) => void;
}) {
  if (loading) {
    return <div role="status" className="flex items-center gap-2 py-8 text-sm text-gray-500 dark:text-gray-400"><Loader2 size={14} className="animate-spin" /> Loading project structure projection…</div>;
  }
  if (error) {
    return (
      <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
        <p>{error}</p>
        <button type="button" onClick={onRetry} className="btn btn-secondary mt-2 inline-flex items-center gap-1 text-xs"><RefreshCw size={12} /> Retry</button>
      </div>
    );
  }
  if (!projection?.authored) {
    return <p className="rounded-lg border border-gray-200 p-4 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">No project structure has been authored for this Spec.</p>;
  }
  if (projection.nodes.length === 0 && projection.affected_references.length === 0) {
    return <p className="rounded-lg border border-gray-200 p-4 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">No project structure nodes are linked to this card.</p>;
  }
  return (
    <div className="space-y-3" data-testid="project-structure-projection">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Project structure</h3>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">Direct nodes and their active ancestors · revision {projection.structure_revision}</p>
        </div>
        <button type="button" onClick={() => onOpenFull()} className="btn btn-secondary inline-flex items-center gap-1.5 text-xs"><ExternalLink size={12} /> Open full tree</button>
      </div>
      {projection.affected_references.length > 0 && (
        <div role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
          <p>{projection.affected_references.length} linked reference{projection.affected_references.length === 1 ? ' needs' : 's need'} review.</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {projection.affected_references.map((reference, index) => (
              <li key={`${reference.node_id}-${reference.state}-${index}`}>
                {reference.state === 'classification_changed'
                  ? `A linked project item was reclassified${reference.classification ? ` as ${CLASSIFICATION_LABELS[reference.classification]}` : ''}.`
                  : 'A linked project item is no longer available.'}
              </li>
            ))}
          </ul>
          <p className="mt-1">Open the full tree to review.</p>
        </div>
      )}
      <div role="tree" aria-label="Read-only project structure projection" className="rounded-lg border border-gray-200 dark:border-gray-700">
        {projection.nodes.map((item) => (
          <div key={item.node.id} role="treeitem" aria-level={item.depth} className="flex items-start gap-2 border-b border-gray-100 px-3 py-2.5 last:border-b-0 dark:border-gray-700/70" style={{ paddingLeft: `${12 + Math.max(0, item.depth - 1) * 20}px` }}>
            <span className="mt-0.5 text-gray-500">{nodeIcon(item.node.kind)}</span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{item.node.name}</span>
                <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${item.direct ? 'bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'}`}>{item.direct ? 'Direct' : 'Context only'}</span>
                {item.reference_role && <span className="rounded bg-purple-50 px-1.5 py-0.5 text-[10px] text-purple-700 dark:bg-purple-950/40 dark:text-purple-300">{item.reference_role.replace(/_/g, ' ')}</span>}
              </div>
              {noteForNode(item.node) && <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-gray-600 dark:text-gray-300">{noteForNode(item.node)}</p>}
            </div>
            <button type="button" aria-label={`Open ${item.node.name} in the full project structure`} onClick={() => onOpenFull(item.node.id)} className={iconButtonClassName()} title="Open in full tree"><ExternalLink size={12} /></button>
          </div>
        ))}
      </div>
    </div>
  );
}

export class ProjectStructureErrorBoundary extends Component<
  { children: ReactNode; onRetry?: () => void },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Project structure UI failed', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
        <p className="font-semibold">Project structure could not be displayed</p>
        <p className="mt-1 text-xs">The rest of the Spec is still available. Refresh this section and try again.</p>
        <button type="button" className="btn btn-secondary mt-3 inline-flex items-center gap-1 text-xs" onClick={() => {
          this.setState({ error: null });
          this.props.onRetry?.();
        }}><RefreshCw size={12} /> Retry</button>
      </div>
    );
  }
}
