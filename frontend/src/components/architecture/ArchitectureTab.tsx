import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  AlertTriangle,
  Boxes,
  ChevronDown,
  ChevronRight,
  Cloud,
  Copy,
  Cpu,
  Database,
  FileUp,
  Focus,
  GitBranch,
  Globe2,
  HardDrive,
  Lock,
  MessageSquare,
  Monitor,
  Network,
  Package,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Server,
  Shield,
  Smartphone,
  Terminal,
  Trash2,
  UserRound,
  Workflow,
  type LucideIcon,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type {
  ArchitectureDesign,
  ArchitectureDesignValidationResult,
  ArchitectureDesignSummary,
  ArchitectureDiagram,
  ArchitectureDiagramType,
  ArchitectureEntity,
  ArchitectureInterface,
  ArchitectureParentType,
  ArchitectureWarningRecord,
  CreateArchitectureDesignRequest,
  EffectiveResourceItem,
  ResourceGateEntityType,
  ScreenMockup,
} from '@/types';
import { ArchitectureDiagramEditor } from './ArchitectureDiagramEditor';
import { ExcalidrawImportDialog } from './ExcalidrawImportDialog';
import {
  ARCHITECTURE_COMPONENT_SEGMENTS,
  colorForArchitectureType,
  iconForArchitectureType,
  type ArchitectureVisualIcon,
} from './architectureVisualRegistry';

interface ArchitectureTabProps {
  parentType: ArchitectureParentType;
  parentId: string;
  boardId?: string | null;
  entityType?: ResourceGateEntityType;
  entityId?: string | null;
  specIdForCopy?: string | null;
  locked?: boolean;
  expanded?: boolean;
  screenMockups?: ScreenMockup[] | null;
  onChanged?: (designs: ArchitectureDesignSummary[]) => void;
}

type EffectiveArchitectureDesignSummary = ArchitectureDesignSummary & {
  inherited?: boolean;
  read_only?: boolean;
  source_entity_type?: string | null;
  source_entity_id?: string | null;
  source_entity_title?: string | null;
  effective_payload?: ArchitectureDesign;
};

interface ArchitectureCanvasElement {
  id: string;
  type: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  text?: string;
  strokeColor?: string;
  architectureKind?: string | null;
  displayType?: string | null;
  iconName?: string | null;
  linkedEntityId?: string | null;
  linkedInterfaceId?: string | null;
  linkedInterfaceIds?: string[] | null;
  sourceElementId?: string | null;
  targetElementId?: string | null;
  points?: number[][];
  connectionType?: 'direct' | 'elbow' | null;
}

type InterfaceDirection = 'source_to_target' | 'target_to_source' | 'bidirectional' | 'none';

type ArchitectureEntityDraft = ArchitectureEntity & {
  color?: string | null;
  icon?: ArchitectureVisualIcon | null;
};

type ArchitecturePanelKey = 'metadata' | 'components' | 'entities' | 'interfaces' | 'screens';

interface CollapsibleSectionProps {
  id: ArchitecturePanelKey;
  title: string;
  open: boolean;
  onToggle: (id: ArchitecturePanelKey) => void;
  children: ReactNode;
  action?: ReactNode;
}

interface ArchitectureComponentPreset {
  id: string;
  label: string;
  entityType: string;
  icon: ArchitectureVisualIcon;
  color: string;
}

interface ArchitectureComponentSegment {
  id: string;
  label: string;
  items: ArchitectureComponentPreset[];
}

const ENTITY_ICON_OPTIONS: Array<{ value: ArchitectureVisualIcon; label: string; icon: LucideIcon }> = [
  { value: 'boxes', label: 'Component', icon: Boxes },
  { value: 'server', label: 'Server/API', icon: Server },
  { value: 'database', label: 'Database', icon: Database },
  { value: 'message', label: 'Queue/Event', icon: MessageSquare },
  { value: 'user', label: 'Actor', icon: UserRound },
  { value: 'network', label: 'Network', icon: Network },
  { value: 'cloud', label: 'Cloud', icon: Cloud },
  { value: 'cpu', label: 'Compute', icon: Cpu },
  { value: 'globe', label: 'Web', icon: Globe2 },
  { value: 'hard_drive', label: 'Storage', icon: HardDrive },
  { value: 'lock', label: 'Security', icon: Lock },
  { value: 'monitor', label: 'Desktop/UI', icon: Monitor },
  { value: 'package', label: 'Package', icon: Package },
  { value: 'smartphone', label: 'Mobile', icon: Smartphone },
  { value: 'terminal', label: 'CLI/Job', icon: Terminal },
  { value: 'workflow', label: 'Workflow', icon: Workflow },
];

const COMPONENT_SEGMENTS: ArchitectureComponentSegment[] = ARCHITECTURE_COMPONENT_SEGMENTS;

const INTERFACE_DIRECTIONS: Array<{ value: InterfaceDirection; label: string }> = [
  { value: 'source_to_target', label: 'Source -> Target' },
  { value: 'target_to_source', label: 'Target -> Source' },
  { value: 'bidirectional', label: 'Bidirectional' },
  { value: 'none', label: 'No arrow' },
];

function CollapsibleSection({ id, title, open, onToggle, children, action }: CollapsibleSectionProps) {
  return (
    <section className="border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900 overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-2.5 py-2">
        <button
          type="button"
          onClick={() => onToggle(id)}
          className="min-w-0 flex flex-1 items-center gap-1.5 text-left text-sm font-semibold text-gray-800 dark:text-gray-100"
        >
          {open ? <ChevronDown size={14} className="shrink-0" /> : <ChevronRight size={14} className="shrink-0" />}
          <span className="truncate">{title}</span>
        </button>
        {action}
      </div>
      {open && <div className="border-t border-gray-200 dark:border-gray-700 p-2.5 space-y-2">{children}</div>}
    </section>
  );
}

interface ArchitectureWarningFocusTarget {
  diagramId: string;
  elementId: string;
}

function architectureWarningLocation(warning: ArchitectureWarningRecord): string {
  if (warning.diagram_id && warning.element_id) return `${warning.diagram_id} / ${warning.element_id}`;
  if (warning.diagram_id && warning.entity_id) return `${warning.diagram_id} / ${warning.entity_id}`;
  if (warning.diagram_id && warning.node_ref) return `${warning.diagram_id} / ${warning.node_ref}`;
  return warning.entity_id || warning.node_ref || warning.path;
}

function warningHasFocusTarget(warning: ArchitectureWarningRecord): warning is ArchitectureWarningRecord & { diagram_id: string; element_id: string } {
  return Boolean(warning.diagram_id && warning.element_id);
}

export function ArchitectureValidationPanel({
  result,
  loading,
  error,
  onFocusElement,
  warningAcknowledged = false,
  onWarningAcknowledgedChange,
}: {
  result: ArchitectureDesignValidationResult | null;
  loading: boolean;
  error: string | null;
  onFocusElement?: (target: ArchitectureWarningFocusTarget) => void;
  warningAcknowledged?: boolean;
  onWarningAcknowledgedChange?: (checked: boolean) => void;
}) {
  const issues = result?.issues || [];
  const warnings = result?.warnings || [];
  const structuredWarnings = result?.structured_warnings || [];
  const warningCount = warnings.length + structuredWarnings.length;
  if (!error && issues.length === 0 && warningCount === 0) return null;

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-900 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-100">
      <div className="flex items-start gap-2">
        <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-300" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5 font-semibold">
            <span>Design review</span>
            {loading && <span className="font-normal text-amber-700 dark:text-amber-300">checking...</span>}
            {issues.length > 0 && (
              <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] text-red-700 dark:bg-red-950 dark:text-red-200">
                {issues.length} issue{issues.length === 1 ? '' : 's'}
              </span>
            )}
            {warningCount > 0 && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-700 dark:bg-amber-900/60 dark:text-amber-100">
                {warningCount} warning{warningCount === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {error && <p className="mt-1 text-red-700 dark:text-red-300">{error}</p>}
          {(issues.length > 0 || warningCount > 0) && (
            <div className="mt-2 max-h-44 space-y-2 overflow-y-auto pr-1 [scrollbar-gutter:stable]">
              {issues.length > 0 && (
                <div>
                  <div className="mb-1 font-medium text-red-700 dark:text-red-300">Blocking issues</div>
                  <ul className="space-y-1">
                    {issues.map((item) => (
                      <li key={item} className="rounded border border-red-200 bg-white/70 px-2 py-1 text-red-800 dark:border-red-900/70 dark:bg-gray-950/50 dark:text-red-200">
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {structuredWarnings.length > 0 && (
                <div>
                  <div className="mb-1 font-medium text-amber-800 dark:text-amber-200">Connectivity and coverage</div>
                  <ul className="space-y-1">
                    {structuredWarnings.map((item, index) => {
                      const focusable = warningHasFocusTarget(item);
                      return (
                        <li
                          key={`${item.code}-${item.path}-${index}`}
                          className="rounded border border-amber-200 bg-white/70 px-2 py-1 dark:border-amber-900/70 dark:bg-gray-950/50"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0 space-y-0.5">
                              <div className="flex flex-wrap items-center gap-1">
                                <span className="rounded bg-amber-100 px-1.5 py-0.5 font-mono text-[10px] text-amber-800 dark:bg-amber-900/60 dark:text-amber-100">
                                  {item.code}
                                </span>
                                <span className="truncate text-[11px] text-amber-700 dark:text-amber-200">
                                  {architectureWarningLocation(item)}
                                </span>
                              </div>
                              <p>{item.message}</p>
                              <p className="text-amber-700 dark:text-amber-200">{item.suggested_fix}</p>
                            </div>
                            {focusable && (
                              <button
                                type="button"
                                onClick={() => onFocusElement?.({ diagramId: item.diagram_id, elementId: item.element_id })}
                                className="shrink-0 rounded p-1 text-amber-700 hover:bg-amber-100 dark:text-amber-200 dark:hover:bg-amber-900/60"
                                title="Focus diagram element"
                                aria-label={`Focus ${item.element_id}`}
                              >
                                <Focus size={13} />
                              </button>
                            )}
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
              {warnings.length > 0 && (
                <div>
                  <div className="mb-1 font-medium text-amber-800 dark:text-amber-200">Authoring warnings</div>
                  <ul className="space-y-1">
                    {warnings.map((item) => (
                      <li key={item} className="rounded border border-amber-200 bg-white/70 px-2 py-1 dark:border-amber-900/70 dark:bg-gray-950/50">
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {structuredWarnings.length > 0 && onWarningAcknowledgedChange && (
                <label className="flex items-start gap-2 rounded border border-amber-300 bg-white/80 px-2 py-2 text-amber-900 dark:border-amber-800 dark:bg-gray-950/60 dark:text-amber-100">
                  <input
                    type="checkbox"
                    checked={warningAcknowledged}
                    onChange={(event) => onWarningAcknowledgedChange(event.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-amber-400 text-amber-600 focus:ring-amber-500"
                  />
                  <span>
                    I reviewed these architecture warnings. Save may continue, but active warnings still block moving the owner to Done until they are resolved.
                  </span>
                </label>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function csvToList(value: string): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean);
}

function listToCsv(value: string[] | undefined): string {
  return (value || []).join(', ');
}

function entityRef(entity: ArchitectureEntity, index: number): string {
  return entity.id || entity.name || `entity_${index}`;
}

function schemaToText(value: Record<string, unknown> | null | undefined): string {
  if (!value) return '';
  if (typeof value.text === 'string' && Object.keys(value).length === 1) return value.text;
  return JSON.stringify(value, null, 2);
}

function contractToText(value: ArchitectureInterface['error_contract']): string {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

function interfaceRef(item: ArchitectureInterface, index: number): string {
  return item.id || item.name || `interface_${index}`;
}

function uniqueRefs(values: Array<string | null | undefined>): string[] {
  const refs: string[] = [];
  values.forEach((value) => {
    const ref = value?.trim();
    if (ref && !refs.includes(ref)) refs.push(ref);
  });
  return refs;
}

function entityRefsFor(item: ArchitectureEntity | undefined, index: number): string[] {
  return uniqueRefs([item?.id, item?.name, `entity_${index}`]);
}

function interfaceRefsFor(item: ArchitectureInterface | undefined, index: number): string[] {
  return uniqueRefs([item?.id, item?.name, `interface_${index}`]);
}

function linkedInterfaceRefs(element: ArchitectureCanvasElement): string[] {
  return uniqueRefs([
    element.linkedInterfaceId || undefined,
    ...(Array.isArray(element.linkedInterfaceIds) ? element.linkedInterfaceIds : []),
  ]);
}

function hasLinkedInterfaceRef(element: ArchitectureCanvasElement, refs: string[]): boolean {
  return linkedInterfaceRefs(element).some((ref) => refs.includes(ref));
}

function replaceLinkedInterfaceRefs(element: ArchitectureCanvasElement, previousRefs: string[], nextRef: string): ArchitectureCanvasElement {
  const refs = linkedInterfaceRefs(element).map((ref) => (previousRefs.includes(ref) ? nextRef : ref));
  const unique = uniqueRefs(refs);
  return {
    ...element,
    linkedInterfaceId: unique[0] || null,
    linkedInterfaceIds: unique,
  };
}

function removeLinkedInterfaceRefs(element: ArchitectureCanvasElement, refsToRemove: string[]): ArchitectureCanvasElement {
  const refs = linkedInterfaceRefs(element).filter((ref) => !refsToRemove.includes(ref));
  return {
    ...element,
    linkedInterfaceId: refs[0] || null,
    linkedInterfaceIds: refs,
  };
}

function sameRefs(left: string[] | undefined, right: string[]): boolean {
  const leftRefs = left || [];
  return leftRefs.length === right.length && leftRefs.every((value, index) => value === right[index]);
}

function syncInterfaceParticipantsFromDiagram(interfaces: ArchitectureInterface[], diagram: ArchitectureDiagram): ArchitectureInterface[] {
  const elements = canvasElements(diagram);
  const byId = new Map(elements.map((element) => [element.id, element]));
  const participantsByInterfaceRef = new Map<string, string[]>();

  elements
    .filter((element) => element.type === 'arrow')
    .forEach((edge) => {
      const source = edge.sourceElementId ? byId.get(edge.sourceElementId) : null;
      const target = edge.targetElementId ? byId.get(edge.targetElementId) : null;
      const participants = uniqueRefs([source?.linkedEntityId, target?.linkedEntityId]);
      if (participants.length !== 2) return;
      linkedInterfaceRefs(edge).forEach((ref) => {
        if (!participantsByInterfaceRef.has(ref)) participantsByInterfaceRef.set(ref, participants);
      });
    });

  let changed = false;
  const next = interfaces.map((item, index) => {
    const participants = interfaceRefsFor(item, index)
      .map((ref) => participantsByInterfaceRef.get(ref))
      .find(Boolean);
    if (!participants || sameRefs(item.participants, participants)) return item;
    changed = true;
    return { ...item, participants };
  });

  return changed ? next : interfaces;
}

function canvasPayload(diagram: ArchitectureDiagram | null): Record<string, unknown> {
  if (!diagram || typeof diagram.adapter_payload !== 'object' || Array.isArray(diagram.adapter_payload) || diagram.adapter_payload === null) {
    return { type: 'excalidraw', version: 2, elements: [], appState: {}, files: {} };
  }
  return diagram.adapter_payload as Record<string, unknown>;
}

function canvasElements(diagram: ArchitectureDiagram | null): ArchitectureCanvasElement[] {
  const payload = canvasPayload(diagram);
  return Array.isArray(payload.elements) ? payload.elements as ArchitectureCanvasElement[] : [];
}

function withCanvasElements(diagram: ArchitectureDiagram, elements: ArchitectureCanvasElement[]): ArchitectureDiagram {
  const payload = canvasPayload(diagram);
  return {
    ...diagram,
    format: 'excalidraw_json',
    adapter_payload: {
      ...payload,
      type: payload.type || 'excalidraw',
      version: payload.version || 2,
      appState: payload.appState || {},
      files: payload.files || {},
      elements,
    },
  };
}

function resolveSelectedDiagramId(
  diagrams: ArchitectureDiagram[],
  preferredId?: string,
  previous?: ArchitectureDiagram | null,
): string {
  if (preferredId && diagrams.some((diagram) => diagram.id === preferredId)) return preferredId;
  if (previous?.id && diagrams.some((diagram) => diagram.id === previous.id)) return previous.id;
  if (previous?.title) {
    const byTitle = diagrams.find((diagram) => diagram.title === previous.title);
    if (byTitle?.id) return byTitle.id;
  }
  if (previous?.order_index !== undefined) {
    const byOrder = diagrams.find((diagram) => diagram.order_index === previous.order_index);
    if (byOrder?.id) return byOrder.id;
  }
  return diagrams[0]?.id || '';
}

function nextCanvasId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2, 7)}`;
}

function colorForEntityType(type: string | null | undefined): string {
  return colorForArchitectureType(type);
}

function iconForEntityType(type: string | null | undefined): ArchitectureVisualIcon {
  return iconForArchitectureType(type);
}

function iconComponentForName(name: string | null | undefined): LucideIcon {
  return ENTITY_ICON_OPTIONS.find((item) => item.value === name)?.icon || Boxes;
}

function normalizeInterfaceDirection(value: string | null | undefined): InterfaceDirection {
  if (value === 'target_to_source' || value === 'bidirectional' || value === 'none') return value;
  return 'source_to_target';
}

function syncEntityElement(element: ArchitectureCanvasElement, entity: ArchitectureEntity, ref: string): ArchitectureCanvasElement {
  const visual = entity as ArchitectureEntityDraft;
  return {
    ...element,
    linkedEntityId: ref,
    text: entity.name,
    displayType: entity.entity_type || 'Entity',
    architectureKind: entity.entity_type || element.architectureKind || 'entity',
    strokeColor: visual.color || element.strokeColor || colorForEntityType(entity.entity_type),
    iconName: visual.icon || element.iconName || iconForEntityType(entity.entity_type),
  };
}

function syncInterfaceElement(element: ArchitectureCanvasElement, item: ArchitectureInterface, ref: string): ArchitectureCanvasElement {
  return {
    ...element,
    linkedInterfaceId: ref,
    linkedInterfaceIds: uniqueRefs([...linkedInterfaceRefs(element), ref]),
    text: item.name,
    displayType: item.endpoint || item.protocol || item.contract_type || 'Interface',
    architectureKind: 'interface',
  };
}

function textToSchema(value: string): Record<string, unknown> | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : { text: trimmed };
  } catch {
    return { text: trimmed };
  }
}

function makeBlankDiagram(order: number): ArchitectureDiagram {
  return {
    id: `diag_${Date.now()}`,
    title: `Diagram ${order + 1}`,
    diagram_type: 'container',
    format: 'excalidraw_json',
    description: null,
    order_index: order,
    adapter_payload: {
      type: 'excalidraw',
      version: 2,
      elements: [],
      appState: {},
      files: {},
    },
  };
}

function makeBlankEntity(): ArchitectureEntityDraft {
  return {
    id: `entity_${Date.now()}`,
    name: 'New entity',
    entity_type: 'service',
    color: '#0891b2',
    icon: 'boxes',
    responsibility: '',
    boundaries: '',
    technologies: [],
    relationships: [],
    notes: '',
  };
}

function makeBlankInterface(): ArchitectureInterface {
  return {
    id: `interface_${Date.now()}`,
    name: 'New interface',
    endpoint: '',
    description: '',
    participants: [],
    direction: 'source_to_target',
    protocol: '',
    contract_type: '',
    request_schema: null,
    response_schema: null,
    error_contract: '',
    schema_ref: '',
    notes: '',
  };
}

function effectiveArchitectureToSummary(item: EffectiveResourceItem): EffectiveArchitectureDesignSummary | null {
  const resource = item.resource && typeof item.resource === 'object'
    ? item.resource as Partial<ArchitectureDesign>
    : item as Partial<ArchitectureDesign>;
  const id = String(item.id || resource.id || '');
  if (!id) return null;
  const diagrams = Array.isArray(resource.diagrams) ? resource.diagrams : [];
  return {
    id,
    board_id: String(resource.board_id || ''),
    parent_type: (resource.parent_type || item.source_entity_type || 'ideation') as ArchitectureParentType,
    parent_id: String(resource.parent_id || item.source_entity_id || ''),
    title: String(resource.title || item.title || 'Inherited architecture'),
    version: Number(resource.version || 1),
    source_ref: resource.source_ref ?? null,
    source_version: resource.source_version ?? null,
    source_design_id: resource.source_design_id ?? null,
    stale: Boolean(resource.stale),
    breaking_change_flag: Boolean(resource.breaking_change_flag),
    requires_arch_review: Boolean(resource.requires_arch_review),
    diagrams_count: diagrams.length,
    adapter_payload_refs: diagrams
      .map((diagram) => diagram.adapter_payload_ref)
      .filter((ref): ref is string => Boolean(ref)),
    created_at: String(resource.created_at || ''),
    updated_at: String(resource.updated_at || ''),
    inherited: item.inherited,
    read_only: item.read_only,
    source_entity_type: item.source_entity_type ?? item.provenance?.source_entity_type ?? null,
    source_entity_id: item.source_entity_id ?? item.provenance?.source_entity_id ?? null,
    source_entity_title: item.source_entity_title ?? item.provenance?.source_entity_title ?? null,
    effective_payload: resource.global_description !== undefined ? resource as ArchitectureDesign : undefined,
  };
}

function effectiveMockupToScreen(item: EffectiveResourceItem): ScreenMockup | null {
  const resource = item.resource && typeof item.resource === 'object'
    ? item.resource as Partial<ScreenMockup>
    : item as Partial<ScreenMockup>;
  const id = String(item.id || resource.id || '');
  if (!id || !resource.html_content) return null;
  return {
    id,
    title: String(resource.title || item.title || 'Inherited mockup'),
    description: typeof resource.description === 'string' ? resource.description : null,
    screen_type: resource.screen_type || 'page',
    html_content: String(resource.html_content),
    annotations: resource.annotations ?? null,
    order: typeof resource.order === 'number' ? resource.order : 9999,
    origin_id: resource.origin_id ?? null,
    origin_story_id: resource.origin_story_id ?? null,
    origin_entity_type: resource.origin_entity_type ?? null,
    design_system_ref: resource.design_system_ref ?? null,
    design_system_evidence: resource.design_system_evidence ?? null,
  };
}

function inheritedSourceLabel(item: EffectiveArchitectureDesignSummary): string {
  const type = item.source_entity_type || 'source';
  const title = item.source_entity_title || item.source_entity_id || 'parent';
  return `${type}: ${title}`;
}

export function ArchitectureTab({
  parentType,
  parentId,
  boardId,
  entityType,
  entityId,
  specIdForCopy,
  locked: lockedProp = false,
  expanded = false,
  screenMockups = [],
  onChanged,
}: ArchitectureTabProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  const onChangedRef = useRef(onChanged);
  const [directSummaries, setDirectSummaries] = useState<ArchitectureDesignSummary[]>([]);
  const [effectiveArchitecture, setEffectiveArchitecture] = useState<EffectiveResourceItem[]>([]);
  const [effectiveMockups, setEffectiveMockups] = useState<EffectiveResourceItem[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [design, setDesign] = useState<ArchitectureDesign | null>(null);
  const [selectedDiagramId, setSelectedDiagramId] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [validation, setValidation] = useState<ArchitectureDesignValidationResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [warningAcknowledged, setWarningAcknowledged] = useState(false);
  const [focusRequest, setFocusRequest] = useState<{ diagramId: string; elementId: string; signal: number } | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [entityDraft, setEntityDraft] = useState<ArchitectureEntityDraft | null>(null);
  const [interfaceDraft, setInterfaceDraft] = useState<ArchitectureInterface | null>(null);
  const [editingEntityIndex, setEditingEntityIndex] = useState<number | null>(null);
  const [editingInterfaceIndex, setEditingInterfaceIndex] = useState<number | null>(null);
  const [openPanels, setOpenPanels] = useState<Record<ArchitecturePanelKey, boolean>>({
    metadata: true,
    components: true,
    entities: true,
    interfaces: true,
    screens: true,
  });
  const [selectedComponentSegmentId, setSelectedComponentSegmentId] = useState(COMPONENT_SEGMENTS[0].id);
  const summaries = useMemo<EffectiveArchitectureDesignSummary[]>(() => {
    const directIds = new Set(directSummaries.map((item) => item.id));
    const inherited = effectiveArchitecture
      .filter((item) => item.inherited && !directIds.has(String(item.id || '')))
      .map(effectiveArchitectureToSummary)
      .filter((item): item is EffectiveArchitectureDesignSummary => Boolean(item));
    return [...directSummaries, ...inherited];
  }, [directSummaries, effectiveArchitecture]);
  const availableMockups = useMemo(() => {
    const direct = screenMockups || [];
    const directIds = new Set(direct.map((item) => item.id));
    const inherited = effectiveMockups
      .filter((item) => item.inherited && !directIds.has(String(item.id || '')))
      .map(effectiveMockupToScreen)
      .filter((item): item is ScreenMockup => Boolean(item));
    return [...direct, ...inherited];
  }, [effectiveMockups, screenMockups]);
  const selectedSummary = summaries.find((item) => item.id === selectedId) || null;
  const selectedInheritedReadOnly = Boolean(selectedSummary?.inherited && selectedSummary.read_only);
  const cardSnapshotReadOnly = parentType === 'card';
  const locked = lockedProp || cardSnapshotReadOnly || selectedInheritedReadOnly;
  const authoringLocked = locked;

  const selectedDiagram = useMemo(
    () => design?.diagrams.find((item) => item.id === selectedDiagramId) || design?.diagrams[0] || null,
    [design, selectedDiagramId],
  );
  const selectedComponentSegment = COMPONENT_SEGMENTS.find((segment) => segment.id === selectedComponentSegmentId) || COMPONENT_SEGMENTS[0];
  const structuredWarningKeys = useMemo(
    () => (validation?.structured_warnings || [])
      .map((warning) => warning.finding_key)
      .filter((key): key is string => Boolean(key)),
    [validation?.structured_warnings],
  );
  const structuredWarningSignature = useMemo(
    () => (validation?.structured_warnings || [])
      .map((warning) => warning.finding_key || `${warning.code}:${warning.path}`)
      .sort()
      .join('|'),
    [validation?.structured_warnings],
  );

  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  useEffect(() => {
    setWarningAcknowledged(false);
  }, [structuredWarningSignature]);

  useEffect(() => {
    onChangedRef.current = onChanged;
  }, [onChanged]);

  useEffect(() => {
    setEntityDraft(null);
    setInterfaceDraft(null);
    setEditingEntityIndex(null);
    setEditingInterfaceIndex(null);
    setFocusRequest(null);
  }, [selectedId]);

  const focusArchitectureElement = useCallback((target: ArchitectureWarningFocusTarget) => {
    setSelectedDiagramId(target.diagramId);
    setFocusRequest({ ...target, signal: Date.now() });
  }, []);

  const loadList = useCallback(async (preferredSelectedId?: string) => {
    setLoading(true);
    try {
      const data = await apiRef.current.listArchitectureDesigns(parentType, parentId);
      setDirectSummaries(data);
      onChangedRef.current?.(data);
      setSelectedId((current) => {
        const requested = preferredSelectedId || current;
        if (requested) return requested;
        return data[0]?.id || '';
      });
      return data;
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to load architecture');
      return [];
    } finally {
      setLoading(false);
    }
  }, [parentId, parentType]);

  const loadEffectiveResources = useCallback(async () => {
    const resolvedBoardId = boardId || '';
    const resolvedEntityType = entityType || parentType;
    const resolvedEntityId = entityId || parentId;
    if (!resolvedBoardId || !resolvedEntityType || !resolvedEntityId) {
      setEffectiveArchitecture([]);
      setEffectiveMockups([]);
      return;
    }
    try {
      const response = await apiRef.current.getEffectiveResources(
        resolvedBoardId,
        resolvedEntityType,
        resolvedEntityId,
      );
      setEffectiveArchitecture(response.resources.architecture || []);
      setEffectiveMockups(response.resources.mockup || []);
    } catch {
      setEffectiveArchitecture([]);
      setEffectiveMockups([]);
    }
  }, [boardId, entityId, entityType, parentId, parentType]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    void loadEffectiveResources();
  }, [loadEffectiveResources]);

  useEffect(() => {
    if (summaries.length === 0) {
      if (selectedId) setSelectedId('');
      return;
    }
    if (!selectedId || !summaries.some((item) => item.id === selectedId)) {
      setSelectedId(summaries[0].id);
    }
  }, [selectedId, summaries]);

  useEffect(() => {
    if (!selectedId) {
      setDesign(null);
      return;
    }
    if (selectedSummary?.effective_payload) {
      const payload = selectedSummary.effective_payload;
      setDesign(payload);
      setSelectedDiagramId((current) => resolveSelectedDiagramId(payload.diagrams, current));
      return;
    }
    let cancelled = false;
    apiRef.current.getArchitectureDesign(selectedId, true)
      .then((data) => {
        if (cancelled) return;
        setDesign(data);
        setSelectedDiagramId((current) => resolveSelectedDiagramId(data.diagrams, current));
      })
      .catch((err) => {
        if (!cancelled) toast.error(err instanceof Error ? err.message : 'Failed to load architecture design');
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, selectedSummary]);

  useEffect(() => {
    if (!design) {
      setValidation(null);
      setValidationError(null);
      setValidating(false);
      return undefined;
    }

    const payload: CreateArchitectureDesignRequest = {
      design_id: design.id,
      title: design.title,
      global_description: design.global_description,
      entities: design.entities,
      interfaces: design.interfaces,
      diagrams: design.diagrams,
    };
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setValidating(true);
      setValidationError(null);
      apiRef.current.validateArchitectureDesign(payload)
        .then((result) => {
          if (!cancelled) setValidation(result);
        })
        .catch((err) => {
          if (!cancelled) {
            setValidation(null);
            setValidationError(err instanceof Error ? err.message : 'Failed to validate architecture design');
          }
        })
        .finally(() => {
          if (!cancelled) setValidating(false);
        });
    }, 450);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [design?.diagrams, design?.entities, design?.global_description, design?.interfaces, design?.title]);

  const refresh = async () => {
    const data = await loadList(selectedId);
    await loadEffectiveResources();
    const detailId = selectedId && data.some((item) => item.id === selectedId) ? selectedId : data[0]?.id;
    if (detailId) {
      try {
        const next = await apiRef.current.getArchitectureDesign(detailId, true);
        setDesign(next);
        setSelectedDiagramId((current) => resolveSelectedDiagramId(next.diagrams, current, selectedDiagram));
      } catch (err) {
        toast.error(err instanceof Error ? err.message : 'Failed to refresh architecture design');
      }
    }
  };

  const patchDesign = (patch: Partial<ArchitectureDesign>) => {
    setDesign((current) => (current ? { ...current, ...patch } : current));
  };

  const togglePanel = (key: ArchitecturePanelKey) => {
    setOpenPanels((current) => ({ ...current, [key]: !current[key] }));
  };

  const createDesign = async () => {
    if (authoringLocked || !newTitle.trim() || !newDescription.trim()) return;
    setCreating(true);
    try {
      const created = await apiRef.current.createArchitectureDesign(parentType, parentId, {
        title: newTitle.trim(),
        global_description: newDescription.trim(),
        entities: [],
        interfaces: [],
        diagrams: [makeBlankDiagram(0)],
      });
      setSelectedId(created.id);
      setDesign(created);
      setSelectedDiagramId(created.diagrams[0]?.id || '');
      setNewTitle('');
      setNewDescription('');
      toast.success('Architecture design created');
      await loadList(created.id);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to create architecture');
    } finally {
      setCreating(false);
    }
  };

  const saveDesign = async () => {
    if (!design || authoringLocked) return;
    const structuredWarnings = validation?.structured_warnings || [];
    if (structuredWarnings.length > 0 && !warningAcknowledged) {
      toast.error('Review and acknowledge architecture warnings before saving.');
      return;
    }
    const previousDiagram = selectedDiagram;
    setSaving(true);
    try {
      const updated = await apiRef.current.updateArchitectureDesign(design.id, {
        title: design.title,
        global_description: design.global_description,
        entities: design.entities,
        interfaces: design.interfaces,
        diagrams: design.diagrams,
        change_summary: 'Updated from dashboard architecture tab',
        architecture_warning_acknowledgement: structuredWarnings.length > 0
          ? {
              accepted: true,
              warning_keys: structuredWarningKeys,
              statement: 'Reviewed in Architecture tab before save.',
            }
          : null,
      });
      const full = await apiRef.current.getArchitectureDesign(updated.id, true);
      setSelectedId(full.id);
      setDesign(full);
      setWarningAcknowledged(false);
      setSelectedDiagramId(resolveSelectedDiagramId(full.diagrams, selectedDiagramId, previousDiagram));
      toast.success('Architecture design saved');
      await loadList(full.id);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to save architecture');
    } finally {
      setSaving(false);
    }
  };

  const deleteDesign = async () => {
    if (!design || authoringLocked || !confirm('Delete this architecture design?')) return;
    await apiRef.current.deleteArchitectureDesign(design.id);
    toast.success('Architecture design deleted');
    setDesign(null);
    setSelectedId('');
    await loadList();
  };

  const copyFromSpec = async () => {
    if (!specIdForCopy) return;
    setSaving(true);
    try {
      const copied = await apiRef.current.copyArchitectureToCard(parentId, specIdForCopy);
      toast.success(`${copied.length} architecture design${copied.length === 1 ? '' : 's'} copied`);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to copy architecture');
    } finally {
      setSaving(false);
    }
  };

  const importExcalidraw = async (data: {
    title: string;
    description?: string;
    diagramType: ArchitectureDiagramType;
    payload: Record<string, unknown>;
    replaceDiagramId?: string | null;
  }) => {
    if (authoringLocked) return;
    if (!design) {
      const created = await apiRef.current.createArchitectureDesign(parentType, parentId, {
        title: data.title,
        global_description: data.description || `Imported ${data.title}`,
        entities: [],
        interfaces: [],
        diagrams: [{
          title: data.title,
          description: data.description || null,
          diagram_type: data.diagramType,
          format: 'excalidraw_json',
          order_index: 0,
          adapter_payload: data.payload,
        }],
      });
      setSelectedId(created.id);
      setDesign(created);
      setSelectedDiagramId(resolveSelectedDiagramId(created.diagrams));
      await loadList(created.id);
      return;
    }
    const previousDiagram = selectedDiagram;
    const updated = await apiRef.current.importExcalidrawArchitectureDiagram(design.id, {
      title: data.title,
      description: data.description,
      diagram_type: data.diagramType,
      payload: data.payload,
      replace_diagram_id: data.replaceDiagramId || null,
    });
    const full = await apiRef.current.getArchitectureDesign(updated.id, true);
    setDesign(full);
    setSelectedDiagramId(resolveSelectedDiagramId(full.diagrams, data.replaceDiagramId || undefined, previousDiagram));
    await loadList(full.id);
  };

  const updateDiagram = (next: ArchitectureDiagram) => {
    if (!design) return;
    let normalized = next;
    let nextEntities = design.entities;
    const currentDiagram = design.diagrams.find((item) => item.id === next.id) || null;
    const currentElementIds = new Set(canvasElements(currentDiagram).map((element) => element.id));
    const nextElements = canvasElements(next);
    const newPresetNodes = nextElements.filter((element) => (
      element.type !== 'arrow'
      && element.type !== 'text'
      && !element.linkedEntityId
      && !currentElementIds.has(element.id)
    ));

    if (newPresetNodes.length > 0) {
      nextEntities = [...design.entities];
      const linkedElements = nextElements.map((element) => {
        const newNodeIndex = newPresetNodes.findIndex((node) => node.id === element.id);
        if (newNodeIndex < 0) return element;
        const entityIndex = nextEntities.length;
        const entity: ArchitectureEntityDraft = {
          id: `entity_${Date.now()}_${newNodeIndex}`,
          name: element.text || `New ${element.displayType || element.architectureKind || 'entity'}`,
          entity_type: element.displayType || element.architectureKind || 'component',
          color: element.strokeColor || colorForEntityType(element.displayType || element.architectureKind),
          icon: (element.iconName as ArchitectureVisualIcon | null) || iconForEntityType(element.displayType || element.architectureKind),
          responsibility: '',
          boundaries: '',
          technologies: [],
          relationships: [],
          notes: '',
        };
        nextEntities.push(entity);
        return {
          ...element,
          linkedEntityId: entityRef(entity, entityIndex),
          displayType: entity.entity_type,
          architectureKind: entity.entity_type,
          iconName: element.iconName || iconForEntityType(entity.entity_type),
        };
      });
      normalized = withCanvasElements(next, linkedElements);
    }

    const nextInterfaces = syncInterfaceParticipantsFromDiagram(design.interfaces, normalized);

    patchDesign({
      entities: nextEntities,
      interfaces: nextInterfaces,
      diagrams: design.diagrams.map((item) => (item.id === normalized.id ? normalized : item)),
    });
  };

  const addDiagram = () => {
    if (!design || authoringLocked) return;
    const next = makeBlankDiagram(design.diagrams.length);
    patchDesign({ diagrams: [...design.diagrams, next] });
    setSelectedDiagramId(next.id || '');
  };

  const addEntityNodeToDiagram = (diagram: ArchitectureDiagram, entity: ArchitectureEntity, index: number): ArchitectureDiagram => {
    const ref = entityRef(entity, index);
    const elements = canvasElements(diagram);
    if (elements.some((element) => element.linkedEntityId === ref)) return diagram;
    const nodeCount = elements.filter((element) => element.type !== 'arrow').length;
    const visual = entity as ArchitectureEntityDraft;
    const node: ArchitectureCanvasElement = {
      id: nextCanvasId('entity_node'),
      type: 'rectangle',
      x: 80 + (nodeCount % 3) * 230,
      y: 80 + Math.floor(nodeCount / 3) * 150,
      width: 190,
      height: 86,
      text: entity.name || 'Unnamed entity',
      displayType: entity.entity_type || 'Entity',
      architectureKind: entity.entity_type || 'entity',
      strokeColor: visual.color || colorForEntityType(entity.entity_type),
      iconName: visual.icon || iconForEntityType(entity.entity_type),
      linkedEntityId: ref,
    };
    return withCanvasElements(diagram, [...elements, node]);
  };

  const addEntityToCurrentDiagram = (entity: ArchitectureEntity, index: number) => {
    if (!design || !selectedDiagram || authoringLocked) return;
    const nextDiagram = addEntityNodeToDiagram(selectedDiagram, entity, index);
    patchDesign({
      diagrams: design.diagrams.map((diagram) => (diagram.id === nextDiagram.id ? nextDiagram : diagram)),
    });
  };

  const addComponentToCurrentDiagram = (preset: ArchitectureComponentPreset) => {
    if (!design || !selectedDiagram || authoringLocked) return;
    const entity: ArchitectureEntityDraft = {
      id: nextCanvasId(preset.id),
      name: `New ${preset.label}`,
      entity_type: preset.entityType,
      color: preset.color,
      icon: preset.icon,
      responsibility: '',
      boundaries: '',
      technologies: [],
      relationships: [],
      notes: '',
    };
    const nextEntities = [...design.entities, entity];
    const nextIndex = nextEntities.length - 1;
    const nextDiagram = addEntityNodeToDiagram(selectedDiagram, entity, nextIndex);
    patchDesign({
      entities: nextEntities,
      diagrams: design.diagrams.map((diagram) => (diagram.id === nextDiagram.id ? nextDiagram : diagram)),
    });
  };

  const updateEntity = (index: number, patch: Partial<ArchitectureEntity>) => {
    if (!design || authoringLocked) return;
    const previous = design.entities[index];
    const previousRefs = entityRefsFor(previous, index);
    const nextEntities = design.entities.map((item, i) => (i === index ? { ...item, ...patch } : item));
    const next = nextEntities[index];
    const nextRef = entityRef(next, index);
    const nextDiagrams = design.diagrams.map((diagram) => {
      const elements = canvasElements(diagram);
      if (!elements.some((element) => previousRefs.includes(element.linkedEntityId || ''))) return diagram;
      return withCanvasElements(diagram, elements.map((element) => (
        previousRefs.includes(element.linkedEntityId || '') ? syncEntityElement(element, next, nextRef) : element
      )));
    });
    patchDesign({ entities: nextEntities, diagrams: nextDiagrams });
  };

  const updateInterface = (index: number, patch: Partial<ArchitectureInterface>) => {
    if (!design || authoringLocked) return;
    const previous = design.interfaces[index];
    const previousRefs = interfaceRefsFor(previous, index);
    const nextInterfaces = design.interfaces.map((item, i) => (i === index ? { ...item, ...patch } : item));
    const next = nextInterfaces[index];
    const nextRef = interfaceRef(next, index);
    const nextDiagrams = design.diagrams.map((diagram) => {
      const elements = canvasElements(diagram);
      if (!elements.some((element) => hasLinkedInterfaceRef(element, previousRefs))) return diagram;
      return withCanvasElements(diagram, elements.map((element) => {
        if (!hasLinkedInterfaceRef(element, previousRefs)) return element;
        return syncInterfaceElement(replaceLinkedInterfaceRefs(element, previousRefs, nextRef), next, nextRef);
      }));
    });
    patchDesign({ interfaces: nextInterfaces, diagrams: nextDiagrams });
  };

  const deleteEntity = (index: number) => {
    if (!design || authoringLocked) return;
    const entity = design.entities[index];
    const refs = entityRefsFor(entity, index);
    const nextInterfaces = design.interfaces.map((item) => ({
      ...item,
      participants: (item.participants || []).filter((participant) => !refs.includes(participant)),
    }));
    const nextDiagrams = design.diagrams.map((diagram) => {
      const elements = canvasElements(diagram);
      const removedNodeIds = elements.filter((element) => refs.includes(element.linkedEntityId || '')).map((element) => element.id);
      return withCanvasElements(diagram, elements.filter((element) => (
        !refs.includes(element.linkedEntityId || '')
        && !removedNodeIds.includes(element.sourceElementId || '')
        && !removedNodeIds.includes(element.targetElementId || '')
      )));
    });
    patchDesign({
      entities: design.entities.filter((_, i) => i !== index),
      interfaces: nextInterfaces,
      diagrams: nextDiagrams,
    });
    setEditingEntityIndex(null);
  };

  const deleteInterface = (index: number) => {
    if (!design || authoringLocked) return;
    const item = design.interfaces[index];
    const refs = interfaceRefsFor(item, index);
    const nextDiagrams = design.diagrams.map((diagram) => {
      const elements = canvasElements(diagram);
      return withCanvasElements(diagram, elements.map((element) => removeLinkedInterfaceRefs(element, refs)));
    });
    patchDesign({
      interfaces: design.interfaces.filter((_, i) => i !== index),
      diagrams: nextDiagrams,
    });
    setEditingInterfaceIndex(null);
  };

  const commitEntityDraft = () => {
    if (!design || !entityDraft || authoringLocked) return;
    const draft: ArchitectureEntityDraft = {
      ...entityDraft,
      color: entityDraft.color || colorForEntityType(entityDraft.entity_type),
      icon: entityDraft.icon || iconForEntityType(entityDraft.entity_type),
    };
    const nextEntities = [...design.entities, draft];
    const nextIndex = nextEntities.length - 1;
    const nextDiagrams = selectedDiagram
      ? design.diagrams.map((diagram) => (diagram.id === selectedDiagram.id ? addEntityNodeToDiagram(diagram, draft, nextIndex) : diagram))
      : design.diagrams;
    patchDesign({ entities: nextEntities, diagrams: nextDiagrams });
    setEntityDraft(null);
  };

  const commitInterfaceDraft = () => {
    if (!design || !interfaceDraft || authoringLocked) return;
    const nextInterfaces = [...design.interfaces, interfaceDraft];
    patchDesign({ interfaces: nextInterfaces });
    setInterfaceDraft(null);
  };

  const findEntityNode = (entity: ArchitectureEntity, index: number): ArchitectureCanvasElement | null => {
    if (!design) return null;
    const refs = [entity.id, entity.name, entityRef(entity, index)].filter(Boolean);
    const diagrams = selectedDiagram
      ? [selectedDiagram, ...design.diagrams.filter((diagram) => diagram.id !== selectedDiagram.id)]
      : design.diagrams;
    for (const diagram of diagrams) {
      const found = canvasElements(diagram).find((element) => refs.includes(element.linkedEntityId || ''));
      if (found) return found;
    }
    return null;
  };

  const entityVisual = (entity: ArchitectureEntity, index: number): { color: string; icon: ArchitectureVisualIcon } => {
    const node = findEntityNode(entity, index);
    const visual = entity as ArchitectureEntityDraft;
    return {
      color: node?.strokeColor || visual.color || colorForEntityType(entity.entity_type),
      icon: (node?.iconName as ArchitectureVisualIcon | undefined) || visual.icon || iconForEntityType(entity.entity_type),
    };
  };

  const updateEntityVisual = (index: number, patch: { color?: string; icon?: ArchitectureVisualIcon }) => {
    if (!design || authoringLocked) return;
    const entity = design.entities[index];
    const currentVisual = entity as ArchitectureEntityDraft;
    const nextEntities = design.entities.map((item, itemIndex) => {
      if (itemIndex !== index) return item;
      return {
        ...item,
        color: patch.color ?? currentVisual.color ?? colorForEntityType(item.entity_type),
        icon: patch.icon ?? currentVisual.icon ?? iconForEntityType(item.entity_type),
      } as ArchitectureEntityDraft;
    });
    const refs = [entity.id, entity.name, entityRef(entity, index)].filter(Boolean);
    const nextDiagrams = design.diagrams.map((diagram) => {
      const elements = canvasElements(diagram);
      if (!elements.some((element) => refs.includes(element.linkedEntityId || ''))) return diagram;
      return withCanvasElements(diagram, elements.map((element) => (
        refs.includes(element.linkedEntityId || '')
          ? {
              ...element,
              strokeColor: patch.color ?? element.strokeColor,
              iconName: patch.icon ?? element.iconName,
            }
          : element
      )));
    });
    patchDesign({ entities: nextEntities, diagrams: nextDiagrams });
  };

  const deleteEntityByRef = (ref: string) => {
    if (!design || authoringLocked) return;
    const index = design.entities.findIndex((entity, entityIndex) => (
      [entity.id, entity.name, entityRef(entity, entityIndex)].filter(Boolean).includes(ref)
    ));
    if (index >= 0) deleteEntity(index);
  };

  const duplicateEntity = (index: number) => {
    if (!design || authoringLocked) return;
    const source = design.entities[index];
    const visual = entityVisual(source, index);
    const entity: ArchitectureEntityDraft = {
      ...source,
      id: nextCanvasId('entity_copy'),
      name: `${source.name || 'Entity'} copy`,
      color: visual.color,
      icon: visual.icon,
    };
    const nextEntities = [...design.entities, entity];
    const nextIndex = nextEntities.length - 1;
    const nextDiagrams = selectedDiagram
      ? design.diagrams.map((diagram) => (diagram.id === selectedDiagram.id ? addEntityNodeToDiagram(diagram, entity, nextIndex) : diagram))
      : design.diagrams;
    patchDesign({ entities: nextEntities, diagrams: nextDiagrams });
    setEditingEntityIndex(nextIndex);
  };

  const duplicateInterface = (index: number) => {
    if (!design || authoringLocked) return;
    const source = design.interfaces[index];
    const item: ArchitectureInterface = {
      ...source,
      id: nextCanvasId('interface_copy'),
      name: `${source.name || 'Interface'} copy`,
      participants: [],
      direction: normalizeInterfaceDirection(source.direction),
    };
    const nextInterfaces = [...design.interfaces, item];
    const nextIndex = nextInterfaces.length - 1;
    patchDesign({ interfaces: nextInterfaces });
    setEditingInterfaceIndex(nextIndex);
  };

  return (
    <div className={`space-y-4 min-w-0 overflow-hidden ${expanded ? 'min-h-[70vh]' : ''}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 flex-wrap">
          {summaries.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setSelectedId(item.id)}
              className={`px-2.5 py-1 rounded text-xs flex items-center gap-1.5 ${
                selectedId === item.id
                  ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-200 font-medium'
                  : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
              }`}
            >
              <Boxes size={12} />
              {item.title}
              {item.inherited && (
                <span className="rounded bg-slate-200 px-1 py-0.5 text-[9px] font-medium text-slate-600 dark:bg-slate-700 dark:text-slate-200">
                  inherited
                </span>
              )}
            </button>
          ))}
          {!authoringLocked && (
            <button type="button" onClick={() => setNewTitle(newTitle ? '' : 'New architecture')} className="px-2 py-1 rounded text-xs text-cyan-600 hover:bg-cyan-50 dark:hover:bg-cyan-950/30 flex items-center gap-1">
              <Plus size={12} /> New
            </button>
          )}
        </div>
        <div className="flex items-center gap-1">
          {parentType === 'card' && specIdForCopy && (
            <button type="button" onClick={copyFromSpec} disabled={saving} className="p-1.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50" title="Copy from spec">
              <Copy size={15} />
            </button>
          )}
          {!authoringLocked && (
            <button type="button" onClick={() => setShowImport(true)} className="p-1.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800" title="Import Excalidraw">
              <FileUp size={15} />
            </button>
          )}
          <button type="button" onClick={refresh} className="p-1.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800" title="Refresh">
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {lockedProp && !cardSnapshotReadOnly && (
        <div className="px-3 py-2 rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 text-sm text-amber-800 dark:text-amber-200 flex items-center gap-2">
          <Shield size={15} />
          Spec architecture is locked
        </div>
      )}

      {cardSnapshotReadOnly && (
        <div className="px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 text-sm text-gray-600 dark:text-gray-300 flex items-center gap-2">
          <Shield size={15} />
          Card architecture snapshots are read-only
        </div>
      )}

      {selectedInheritedReadOnly && selectedSummary && (
        <div
          data-testid="architecture-inherited-origin"
          className="px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 text-sm text-slate-600 dark:text-slate-300 flex items-center gap-2"
        >
          <Shield size={15} />
          Read-only inherited from {inheritedSourceLabel(selectedSummary)}
        </div>
      )}

      {newTitle && !authoringLocked && (
        <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 bg-gray-50 dark:bg-gray-950 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs text-gray-500">Title</span>
              <input value={newTitle} onChange={(event) => setNewTitle(event.target.value)} className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100" />
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Global description</span>
              <input value={newDescription} onChange={(event) => setNewDescription(event.target.value)} className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100" />
            </label>
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setNewTitle('')} className="btn btn-secondary text-sm">Cancel</button>
            <button type="button" onClick={createDesign} disabled={!newTitle.trim() || !newDescription.trim() || creating} className="btn btn-primary text-sm disabled:opacity-50">
              {creating ? 'Creating...' : 'Create'}
            </button>
          </div>
        </div>
      )}

      {!design ? (
        <div className="text-center py-12 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg">
          <GitBranch size={30} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No architecture designs</p>
        </div>
      ) : (
        <div className={`grid grid-cols-[260px_minmax(0,1fr)_320px] gap-4 min-w-0 overflow-hidden ${expanded ? 'h-[calc(100vh-260px)]' : 'h-[calc(100vh-320px)]'} min-h-[520px]`}>
          <aside className="space-y-3 min-w-0 min-h-0 overflow-y-auto overflow-x-hidden pr-2 [scrollbar-gutter:stable]">
            <CollapsibleSection id="metadata" title="Architecture metadata" open={openPanels.metadata} onToggle={togglePanel}>
              <label className="block">
                <span className="text-xs text-gray-500 dark:text-gray-400">Name</span>
                <input
                  value={design.title}
                  onChange={(event) => patchDesign({ title: event.target.value })}
                  readOnly={authoringLocked}
                  className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
                />
              </label>
              <label className="block">
                <span className="text-xs text-gray-500 dark:text-gray-400">Description</span>
                <textarea
                  value={design.global_description}
                  onChange={(event) => patchDesign({ global_description: event.target.value })}
                  readOnly={authoringLocked}
                  rows={4}
                  className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none"
                />
              </label>
              <label className="block">
                <span className="text-xs text-gray-500 dark:text-gray-400">Version</span>
                <input
                  value={`v${design.version}`}
                  readOnly
                  className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-gray-50 dark:bg-gray-950 text-gray-500 dark:text-gray-400"
                />
              </label>
              <div className="flex gap-2 pt-1">
                {!authoringLocked && (
                  <button type="button" onClick={saveDesign} disabled={saving} className="btn btn-primary text-sm flex items-center gap-1 disabled:opacity-50">
                    <Save size={14} /> Save
                  </button>
                )}
                {!authoringLocked && (
                  <button type="button" onClick={deleteDesign} className="btn btn-secondary text-sm flex items-center gap-1">
                    <Trash2 size={14} /> Delete
                  </button>
                )}
              </div>
              <ArchitectureValidationPanel
                result={validation}
                loading={validating}
                error={validationError}
                onFocusElement={focusArchitectureElement}
                warningAcknowledged={warningAcknowledged}
                onWarningAcknowledgedChange={setWarningAcknowledged}
              />
            </CollapsibleSection>

            <CollapsibleSection id="components" title="Components" open={openPanels.components} onToggle={togglePanel}>
              <div className="flex gap-1 overflow-x-auto pb-1">
                {COMPONENT_SEGMENTS.map((segment) => (
                  <button
                    key={segment.id}
                    type="button"
                    onClick={() => setSelectedComponentSegmentId(segment.id)}
                    className={`px-2 py-1 rounded text-[11px] whitespace-nowrap ${
                      selectedComponentSegment.id === segment.id
                        ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-200'
                        : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
                    }`}
                  >
                    {segment.label}
                  </button>
                ))}
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                {selectedComponentSegment.items.map((item) => {
                  const ComponentIcon = iconComponentForName(item.icon);
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => addComponentToCurrentDiagram(item)}
                      disabled={authoringLocked}
                      title={`Add ${item.label} component`}
                      className="min-h-[58px] rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-950 px-1.5 py-2 text-[11px] font-medium text-gray-700 dark:text-gray-200 hover:border-cyan-400 hover:text-cyan-700 dark:hover:text-cyan-200 disabled:opacity-50 flex flex-col items-center justify-center gap-1"
                    >
                      <ComponentIcon size={16} style={{ color: item.color }} />
                      <span className="max-w-full truncate">{item.label}</span>
                    </button>
                  );
                })}
              </div>
            </CollapsibleSection>
          </aside>

          <main className="min-w-0 min-h-0 overflow-hidden flex flex-col gap-3">
            <div className="shrink-0 flex items-center gap-1 flex-wrap">
              {design.diagrams.map((diagram) => (
                <button
                  key={diagram.id || diagram.title}
                  type="button"
                  onClick={() => setSelectedDiagramId(diagram.id || '')}
                  className={`px-2.5 py-1 rounded text-xs ${selectedDiagram?.id === diagram.id ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-200' : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300'}`}
                >
                  {diagram.title}
                </button>
              ))}
              {!authoringLocked && (
                <button type="button" onClick={addDiagram} className="px-2 py-1 rounded text-xs text-cyan-600 hover:bg-cyan-50 dark:hover:bg-cyan-950/30 flex items-center gap-1">
                  <Plus size={12} /> Diagram
                </button>
              )}
            </div>
            {selectedDiagram && (
              <div className="shrink-0 grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-gray-500 dark:text-gray-400">Diagram title</span>
                  <input value={selectedDiagram.title} onChange={(event) => updateDiagram({ ...selectedDiagram, title: event.target.value })} readOnly={authoringLocked} className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100" />
                </label>
                <label className="block">
                  <span className="text-xs text-gray-500 dark:text-gray-400">Diagram description</span>
                  <input value={selectedDiagram.description || ''} onChange={(event) => updateDiagram({ ...selectedDiagram, description: event.target.value })} readOnly={authoringLocked} className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100" />
                </label>
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-hidden">
              <ArchitectureDiagramEditor
                diagram={selectedDiagram}
                entities={design.entities}
                interfaces={design.interfaces}
                mockups={availableMockups}
                readOnly={authoringLocked}
                onChange={updateDiagram}
                onDeleteLinkedEntity={deleteEntityByRef}
                focusElementId={focusRequest && focusRequest.diagramId === selectedDiagram?.id ? focusRequest.elementId : null}
                focusSignal={focusRequest?.signal || 0}
              />
            </div>
          </main>

          <aside className="space-y-3 min-w-0 min-h-0 overflow-y-auto overflow-x-hidden pr-2 [scrollbar-gutter:stable]">
            <CollapsibleSection
              id="entities"
              title="Entities"
              open={openPanels.entities}
              onToggle={togglePanel}
              action={!authoringLocked && (
                <button
                  type="button"
                  onClick={() => {
                    setEditingEntityIndex(null);
                    setEntityDraft((current) => current || makeBlankEntity());
                  }}
                  disabled={Boolean(entityDraft)}
                  className="text-xs text-cyan-600 flex items-center gap-1 disabled:opacity-50"
                >
                  <Plus size={12} /> Add
                </button>
              )}
            >
              {design.entities.length === 0 && !entityDraft && (
                <p className="text-xs text-gray-500 dark:text-gray-400 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg p-3">
                  Add entities to describe systems, actors, services, databases, or other architectural building blocks.
                </p>
              )}
              {entityDraft && (
                <div className="border border-cyan-200 dark:border-cyan-900 rounded-lg p-2 space-y-2 bg-cyan-50/50 dark:bg-cyan-950/20">
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Entity name</span>
                    <input value={entityDraft.name} onChange={(event) => setEntityDraft({ ...entityDraft, name: event.target.value })} className="mt-1 w-full px-2 py-1 text-sm font-medium border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                  </label>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Entity type</span>
                    <input value={entityDraft.entity_type || ''} onChange={(event) => setEntityDraft({ ...entityDraft, entity_type: event.target.value })} placeholder="service, actor, database, external system..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                  </label>
                  <div className="grid grid-cols-[72px_1fr] gap-2">
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Color</span>
                      <input
                        type="color"
                        value={entityDraft.color || colorForEntityType(entityDraft.entity_type)}
                        onChange={(event) => setEntityDraft({ ...entityDraft, color: event.target.value })}
                        className="mt-1 h-8 w-full rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950"
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Icon</span>
                      <select
                        value={entityDraft.icon || iconForEntityType(entityDraft.entity_type)}
                        onChange={(event) => setEntityDraft({ ...entityDraft, icon: event.target.value as ArchitectureVisualIcon })}
                        className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
                      >
                        {ENTITY_ICON_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                      </select>
                    </label>
                  </div>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Responsibility</span>
                    <textarea value={entityDraft.responsibility || ''} onChange={(event) => setEntityDraft({ ...entityDraft, responsibility: event.target.value })} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Boundary</span>
                      <input value={entityDraft.boundaries || ''} onChange={(event) => setEntityDraft({ ...entityDraft, boundaries: event.target.value })} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Technologies</span>
                      <input value={listToCsv(entityDraft.technologies)} onChange={(event) => setEntityDraft({ ...entityDraft, technologies: csvToList(event.target.value) })} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                  </div>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Relationships</span>
                    <input value={listToCsv(entityDraft.relationships)} onChange={(event) => setEntityDraft({ ...entityDraft, relationships: csvToList(event.target.value) })} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                  </label>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Notes</span>
                    <textarea value={entityDraft.notes || ''} onChange={(event) => setEntityDraft({ ...entityDraft, notes: event.target.value })} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                  </label>
                  <div className="flex justify-end gap-2">
                    <button type="button" onClick={() => setEntityDraft(null)} className="btn btn-secondary text-xs">Cancel</button>
                    <button type="button" onClick={commitEntityDraft} disabled={!entityDraft.name.trim()} className="btn btn-primary text-xs disabled:opacity-50">Add entity</button>
                  </div>
                </div>
              )}
              {design.entities.map((entity, index) => {
                const visual = entityVisual(entity, index);
                const EntityIcon = iconComponentForName(visual.icon);
                const editing = editingEntityIndex === index;

                if (!editing) {
                  return (
                    <div key={entity.id || index} className="border border-gray-200 dark:border-gray-700 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-900 flex items-center gap-2">
                      <span className="h-8 w-8 rounded border flex items-center justify-center shrink-0 bg-gray-50 dark:bg-gray-950" style={{ borderColor: visual.color, color: visual.color }}>
                        <EntityIcon size={15} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{entity.name || `Entity ${index + 1}`}</p>
                        <p className="text-[11px] uppercase text-gray-500 dark:text-gray-400 truncate">{entity.entity_type || 'Entity'}</p>
                      </div>
                      {!locked && (
                        <div className="flex items-center gap-1">
                          <button type="button" onClick={() => duplicateEntity(index)} className="p-1 text-gray-400 hover:text-cyan-500 rounded" title="Copy entity">
                            <Copy size={13} />
                          </button>
                          <button type="button" onClick={() => setEditingEntityIndex(index)} className="p-1 text-gray-400 hover:text-cyan-500 rounded" title="Edit entity">
                            <Pencil size={13} />
                          </button>
                          <button type="button" onClick={() => deleteEntity(index)} className="p-1 text-gray-400 hover:text-red-500 rounded" title="Delete entity">
                            <Trash2 size={13} />
                          </button>
                        </div>
                      )}
                    </div>
                  );
                }

                return (
                  <div key={entity.id || index} className="border border-gray-200 dark:border-gray-700 rounded-lg p-2 space-y-2 bg-white dark:bg-gray-900">
                    <div className="flex items-start gap-2">
                      <label className="block flex-1">
                        <span className="text-xs text-gray-500 dark:text-gray-400">Entity name</span>
                        <input value={entity.name} onChange={(event) => updateEntity(index, { name: event.target.value })} readOnly={locked} className="mt-1 w-full px-2 py-1 text-sm font-medium border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                      </label>
                      {!locked && (
                        <button
                          type="button"
                          onClick={() => deleteEntity(index)}
                          className="mt-5 p-1 text-gray-400 hover:text-red-500 rounded"
                          title="Delete entity"
                        >
                          <Trash2 size={13} />
                        </button>
                      )}
                    </div>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Entity type</span>
                      <input value={entity.entity_type || ''} onChange={(event) => updateEntity(index, { entity_type: event.target.value })} readOnly={locked} placeholder="service, actor, database, external system..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <div className="grid grid-cols-[72px_1fr] gap-2">
                      <label className="block">
                        <span className="text-xs text-gray-500 dark:text-gray-400">Color</span>
                        <input
                          type="color"
                          value={visual.color}
                          onChange={(event) => updateEntityVisual(index, { color: event.target.value })}
                          className="mt-1 h-8 w-full rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950"
                        />
                      </label>
                      <label className="block">
                        <span className="text-xs text-gray-500 dark:text-gray-400">Icon</span>
                        <select
                          value={visual.icon}
                          onChange={(event) => updateEntityVisual(index, { icon: event.target.value as ArchitectureVisualIcon })}
                          className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
                        >
                          {ENTITY_ICON_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                        </select>
                      </label>
                    </div>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Responsibility</span>
                      <textarea value={entity.responsibility || ''} onChange={(event) => updateEntity(index, { responsibility: event.target.value })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Boundary</span>
                      <input value={entity.boundaries || ''} onChange={(event) => updateEntity(index, { boundaries: event.target.value })} readOnly={locked} placeholder="domain, bounded context, trust zone..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Technologies</span>
                      <input value={listToCsv(entity.technologies)} onChange={(event) => updateEntity(index, { technologies: csvToList(event.target.value) })} readOnly={locked} placeholder="FastAPI, SQLite, React..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Relationships</span>
                      <input value={listToCsv(entity.relationships)} onChange={(event) => updateEntity(index, { relationships: csvToList(event.target.value) })} readOnly={locked} placeholder="uses Auth API, publishes event..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Notes</span>
                      <textarea value={entity.notes || ''} onChange={(event) => updateEntity(index, { notes: event.target.value })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    {!locked && (
                      <div className="flex justify-end gap-2">
                        <button type="button" onClick={() => setEditingEntityIndex(null)} className="btn btn-secondary text-xs">Done</button>
                        <button type="button" onClick={() => addEntityToCurrentDiagram(entity, index)} className="text-xs text-cyan-600 hover:text-cyan-500">
                          Add to diagram
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </CollapsibleSection>

            <CollapsibleSection
              id="interfaces"
              title="Interfaces"
              open={openPanels.interfaces}
              onToggle={togglePanel}
              action={!locked && (
                <button
                  type="button"
                  onClick={() => {
                    setEditingInterfaceIndex(null);
                    setInterfaceDraft((current) => current || makeBlankInterface());
                  }}
                  disabled={Boolean(interfaceDraft)}
                  className="text-xs text-cyan-600 flex items-center gap-1 disabled:opacity-50"
                >
                  <Plus size={12} /> Add
                </button>
              )}
            >
              {design.interfaces.length === 0 && !interfaceDraft && (
                <p className="text-xs text-gray-500 dark:text-gray-400 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg p-3">
                  Add interfaces to describe API calls, events, schemas, or contracts between entities.
                </p>
              )}
              {interfaceDraft && (
                <div className="border border-cyan-200 dark:border-cyan-900 rounded-lg p-2 space-y-2 bg-cyan-50/50 dark:bg-cyan-950/20">
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Interface name</span>
                    <input value={interfaceDraft.name} onChange={(event) => setInterfaceDraft({ ...interfaceDraft, name: event.target.value })} className="mt-1 w-full px-2 py-1 text-sm font-medium border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                  </label>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Description</span>
                    <textarea value={interfaceDraft.description || ''} onChange={(event) => setInterfaceDraft({ ...interfaceDraft, description: event.target.value })} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                  </label>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Endpoint / operation</span>
                    <input value={interfaceDraft.endpoint || ''} onChange={(event) => setInterfaceDraft({ ...interfaceDraft, endpoint: event.target.value })} placeholder="POST /orders, order.placed, SendMessage..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Direction</span>
                      <select
                        value={normalizeInterfaceDirection(interfaceDraft.direction)}
                        onChange={(event) => setInterfaceDraft({ ...interfaceDraft, direction: event.target.value })}
                        className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
                      >
                        {INTERFACE_DIRECTIONS.map((direction) => <option key={direction.value} value={direction.value}>{direction.label}</option>)}
                      </select>
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Protocol</span>
                      <input value={interfaceDraft.protocol || ''} onChange={(event) => setInterfaceDraft({ ...interfaceDraft, protocol: event.target.value })} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                  </div>
                  <label className="block">
                    <span className="text-xs text-gray-500 dark:text-gray-400">Contract type</span>
                    <input value={interfaceDraft.contract_type || ''} onChange={(event) => setInterfaceDraft({ ...interfaceDraft, contract_type: event.target.value })} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                  </label>
                  <div className="flex justify-end gap-2">
                    <button type="button" onClick={() => setInterfaceDraft(null)} className="btn btn-secondary text-xs">Cancel</button>
                    <button type="button" onClick={commitInterfaceDraft} disabled={!interfaceDraft.name.trim()} className="btn btn-primary text-xs disabled:opacity-50">Add interface</button>
                  </div>
                </div>
              )}
              {design.interfaces.map((item, index) => {
                const directionLabel = INTERFACE_DIRECTIONS.find((direction) => direction.value === normalizeInterfaceDirection(item.direction))?.label;
                const typeLabel = item.endpoint || item.protocol || item.contract_type || directionLabel || 'Interface';
                const editing = editingInterfaceIndex === index;

                if (!editing) {
                  return (
                    <div key={item.id || index} className="border border-gray-200 dark:border-gray-700 rounded-lg px-2 py-1.5 bg-white dark:bg-gray-900 flex items-center gap-2">
                      <span className="h-8 w-8 rounded border border-amber-500 text-amber-500 flex items-center justify-center shrink-0 bg-gray-50 dark:bg-gray-950">
                        <MessageSquare size={15} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{item.name || `Interface ${index + 1}`}</p>
                        <p className="text-[11px] uppercase text-gray-500 dark:text-gray-400 truncate">{typeLabel}</p>
                      </div>
                      {!locked && (
                        <div className="flex items-center gap-1">
                          <button type="button" onClick={() => duplicateInterface(index)} className="p-1 text-gray-400 hover:text-cyan-500 rounded" title="Copy interface">
                            <Copy size={13} />
                          </button>
                          <button type="button" onClick={() => setEditingInterfaceIndex(index)} className="p-1 text-gray-400 hover:text-cyan-500 rounded" title="Edit interface">
                            <Pencil size={13} />
                          </button>
                          <button type="button" onClick={() => deleteInterface(index)} className="p-1 text-gray-400 hover:text-red-500 rounded" title="Delete interface">
                            <Trash2 size={13} />
                          </button>
                        </div>
                      )}
                    </div>
                  );
                }

                return (
                  <div key={item.id || index} className="border border-gray-200 dark:border-gray-700 rounded-lg p-2 space-y-2 bg-white dark:bg-gray-900">
                    <div className="flex items-start gap-2">
                      <label className="block flex-1">
                        <span className="text-xs text-gray-500 dark:text-gray-400">Interface name</span>
                        <input value={item.name} onChange={(event) => updateInterface(index, { name: event.target.value })} readOnly={locked} className="mt-1 w-full px-2 py-1 text-sm font-medium border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                      </label>
                      {!locked && (
                        <button
                          type="button"
                          onClick={() => deleteInterface(index)}
                          className="mt-5 p-1 text-gray-400 hover:text-red-500 rounded"
                          title="Delete interface"
                        >
                          <Trash2 size={13} />
                        </button>
                      )}
                    </div>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Description</span>
                      <textarea value={item.description || ''} onChange={(event) => updateInterface(index, { description: event.target.value })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Endpoint / operation</span>
                      <input value={item.endpoint || ''} onChange={(event) => updateInterface(index, { endpoint: event.target.value })} readOnly={locked} placeholder="POST /orders, order.placed, SendMessage..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <div className="grid grid-cols-2 gap-2">
                      <label className="block">
                        <span className="text-xs text-gray-500 dark:text-gray-400">Direction</span>
                        <select
                          value={normalizeInterfaceDirection(item.direction)}
                          onChange={(event) => updateInterface(index, { direction: event.target.value })}
                          disabled={locked}
                          className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
                        >
                          {INTERFACE_DIRECTIONS.map((direction) => <option key={direction.value} value={direction.value}>{direction.label}</option>)}
                        </select>
                      </label>
                      <label className="block">
                        <span className="text-xs text-gray-500 dark:text-gray-400">Protocol</span>
                        <input value={item.protocol || ''} onChange={(event) => updateInterface(index, { protocol: event.target.value })} readOnly={locked} placeholder="REST, gRPC, event..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                      </label>
                    </div>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Contract type</span>
                      <input value={item.contract_type || ''} onChange={(event) => updateInterface(index, { contract_type: event.target.value })} readOnly={locked} placeholder="OpenAPI, JSON schema, protobuf..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Request schema</span>
                      <textarea value={schemaToText(item.request_schema)} onChange={(event) => updateInterface(index, { request_schema: textToSchema(event.target.value) })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs font-mono border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Response schema</span>
                      <textarea value={schemaToText(item.response_schema)} onChange={(event) => updateInterface(index, { response_schema: textToSchema(event.target.value) })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs font-mono border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Error contract</span>
                      <textarea value={contractToText(item.error_contract)} onChange={(event) => updateInterface(index, { error_contract: textToSchema(event.target.value) })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs font-mono border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Schema reference</span>
                      <input value={item.schema_ref || ''} onChange={(event) => updateInterface(index, { schema_ref: event.target.value })} readOnly={locked} placeholder="schema id, file path, URL..." className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100" />
                    </label>
                    <label className="block">
                      <span className="text-xs text-gray-500 dark:text-gray-400">Notes</span>
                      <textarea value={item.notes || ''} onChange={(event) => updateInterface(index, { notes: event.target.value })} readOnly={locked} rows={2} className="mt-1 w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none" />
                    </label>
                    {!locked && (
                      <div className="flex justify-end gap-2">
                        <button type="button" onClick={() => setEditingInterfaceIndex(null)} className="btn btn-secondary text-xs">Done</button>
                      </div>
                    )}
                  </div>
                );
              })}
            </CollapsibleSection>

            <CollapsibleSection id="screens" title="Screens / mockups" open={openPanels.screens} onToggle={togglePanel}>
              {availableMockups.length === 0 ? (
                <p className="text-xs text-gray-500 dark:text-gray-400 border border-dashed border-gray-300 dark:border-gray-700 rounded-lg p-3">
                  No mockup screens registered for this item.
                </p>
              ) : (
                <div className="space-y-1">
                  {availableMockups.map((mockup) => (
                    <div key={mockup.id} className="px-2 py-1.5 rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
                      <p className="text-xs font-medium text-gray-800 dark:text-gray-100 truncate">{mockup.title}</p>
                      <p className="text-[11px] uppercase text-gray-500 dark:text-gray-400">{mockup.screen_type}</p>
                    </div>
                  ))}
                </div>
              )}
            </CollapsibleSection>
          </aside>
        </div>
      )}

      <ExcalidrawImportDialog
        open={showImport}
        onClose={() => setShowImport(false)}
        onImport={importExcalidraw}
        replaceOptions={(design?.diagrams || []).filter((item) => item.id).map((item) => ({ id: item.id as string, title: item.title }))}
      />
    </div>
  );
}
