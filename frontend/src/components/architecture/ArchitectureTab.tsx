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
  CreateArchitectureDesignRequest,
  ScreenMockup,
} from '@/types';
import { ArchitectureDiagramEditor } from './ArchitectureDiagramEditor';
import { ExcalidrawImportDialog } from './ExcalidrawImportDialog';

interface ArchitectureTabProps {
  parentType: ArchitectureParentType;
  parentId: string;
  specIdForCopy?: string | null;
  locked?: boolean;
  expanded?: boolean;
  screenMockups?: ScreenMockup[] | null;
  onChanged?: (designs: ArchitectureDesignSummary[]) => void;
}

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

type ArchitectureVisualIcon =
  | 'boxes'
  | 'server'
  | 'database'
  | 'message'
  | 'user'
  | 'network'
  | 'cloud'
  | 'cpu'
  | 'globe'
  | 'hard_drive'
  | 'lock'
  | 'monitor'
  | 'package'
  | 'smartphone'
  | 'terminal'
  | 'workflow';

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

const COMPONENT_SEGMENTS: ArchitectureComponentSegment[] = [
  {
    id: 'application',
    label: 'Application',
    items: [
      { id: 'web-app', label: 'Web App', entityType: 'Web App', icon: 'globe', color: '#0891b2' },
      { id: 'mobile-app', label: 'Mobile App', entityType: 'Mobile App', icon: 'smartphone', color: '#0891b2' },
      { id: 'desktop-ui', label: 'Desktop UI', entityType: 'Desktop UI', icon: 'monitor', color: '#0ea5e9' },
      { id: 'api', label: 'API', entityType: 'API', icon: 'server', color: '#0284c7' },
      { id: 'bff', label: 'BFF', entityType: 'BFF', icon: 'server', color: '#2563eb' },
      { id: 'worker', label: 'Worker', entityType: 'Worker', icon: 'cpu', color: '#7c3aed' },
      { id: 'job', label: 'Job', entityType: 'Job', icon: 'terminal', color: '#64748b' },
      { id: 'scheduler', label: 'Scheduler', entityType: 'Scheduler', icon: 'workflow', color: '#ca8a04' },
      { id: 'external-service', label: 'External', entityType: 'External Service', icon: 'network', color: '#7c3aed' },
    ],
  },
  {
    id: 'databases',
    label: 'Databases',
    items: [
      { id: 'postgresql', label: 'PostgreSQL', entityType: 'PostgreSQL', icon: 'database', color: '#16a34a' },
      { id: 'mysql', label: 'MySQL', entityType: 'MySQL', icon: 'database', color: '#16a34a' },
      { id: 'sqlite', label: 'SQLite', entityType: 'SQLite', icon: 'database', color: '#22c55e' },
      { id: 'mongodb', label: 'MongoDB', entityType: 'MongoDB', icon: 'database', color: '#15803d' },
      { id: 'redis', label: 'Redis', entityType: 'Redis', icon: 'database', color: '#dc2626' },
      { id: 'opensearch', label: 'Search', entityType: 'Search Index', icon: 'database', color: '#0f766e' },
      { id: 'cache', label: 'Cache', entityType: 'Cache', icon: 'hard_drive', color: '#65a30d' },
      { id: 'warehouse', label: 'Warehouse', entityType: 'Data Warehouse', icon: 'database', color: '#4f46e5' },
      { id: 'object-storage', label: 'Object Store', entityType: 'Object Storage', icon: 'hard_drive', color: '#ca8a04' },
    ],
  },
  {
    id: 'architecture',
    label: 'Architecture',
    items: [
      { id: 'service', label: 'Service', entityType: 'Service', icon: 'boxes', color: '#0891b2' },
      { id: 'component', label: 'Component', entityType: 'Component', icon: 'boxes', color: '#0ea5e9' },
      { id: 'module', label: 'Module', entityType: 'Module', icon: 'package', color: '#0284c7' },
      { id: 'adapter', label: 'Adapter', entityType: 'Adapter', icon: 'network', color: '#7c3aed' },
      { id: 'repository', label: 'Repository', entityType: 'Repository', icon: 'database', color: '#16a34a' },
      { id: 'gateway', label: 'Gateway', entityType: 'Gateway', icon: 'network', color: '#2563eb' },
      { id: 'boundary', label: 'Boundary', entityType: 'Boundary', icon: 'boxes', color: '#64748b' },
      { id: 'trust-zone', label: 'Trust Zone', entityType: 'Trust Zone', icon: 'lock', color: '#ca8a04' },
      { id: 'contract', label: 'Contract', entityType: 'Contract', icon: 'message', color: '#f59e0b' },
    ],
  },
  {
    id: 'events',
    label: 'Events',
    items: [
      { id: 'event-producer', label: 'Producer', entityType: 'Event Producer', icon: 'message', color: '#ca8a04' },
      { id: 'event-consumer', label: 'Consumer', entityType: 'Event Consumer', icon: 'message', color: '#ca8a04' },
      { id: 'event-store', label: 'Event Store', entityType: 'Event Store', icon: 'database', color: '#16a34a' },
      { id: 'event-processor', label: 'Processor', entityType: 'Event Processor', icon: 'workflow', color: '#7c3aed' },
      { id: 'webhook', label: 'Webhook', entityType: 'Webhook', icon: 'globe', color: '#0284c7' },
      { id: 'stream', label: 'Stream', entityType: 'Stream Processor', icon: 'workflow', color: '#0f766e' },
    ],
  },
  {
    id: 'pubsub',
    label: 'PubSub',
    items: [
      { id: 'topic', label: 'Topic', entityType: 'Topic', icon: 'message', color: '#ca8a04' },
      { id: 'queue', label: 'Queue', entityType: 'Queue', icon: 'message', color: '#ca8a04' },
      { id: 'exchange', label: 'Exchange', entityType: 'Exchange', icon: 'network', color: '#f59e0b' },
      { id: 'subscription', label: 'Subscription', entityType: 'Subscription', icon: 'message', color: '#d97706' },
      { id: 'dlq', label: 'DLQ', entityType: 'Dead Letter Queue', icon: 'message', color: '#ef4444' },
      { id: 'event-bus', label: 'Event Bus', entityType: 'Event Bus', icon: 'workflow', color: '#8b5cf6' },
    ],
  },
  {
    id: 'aws',
    label: 'AWS',
    items: [
      { id: 'aws-lambda', label: 'Lambda', entityType: 'AWS Lambda', icon: 'cloud', color: '#f59e0b' },
      { id: 'aws-ecs', label: 'ECS', entityType: 'AWS ECS', icon: 'cloud', color: '#f59e0b' },
      { id: 'aws-eks', label: 'EKS', entityType: 'AWS EKS', icon: 'cloud', color: '#f59e0b' },
      { id: 'aws-api-gateway', label: 'API GW', entityType: 'AWS API Gateway', icon: 'server', color: '#f59e0b' },
      { id: 'aws-s3', label: 'S3', entityType: 'AWS S3', icon: 'hard_drive', color: '#16a34a' },
      { id: 'aws-rds', label: 'RDS', entityType: 'AWS RDS', icon: 'database', color: '#2563eb' },
      { id: 'aws-dynamodb', label: 'DynamoDB', entityType: 'AWS DynamoDB', icon: 'database', color: '#2563eb' },
      { id: 'aws-sqs', label: 'SQS', entityType: 'AWS SQS', icon: 'message', color: '#ca8a04' },
      { id: 'aws-sns', label: 'SNS', entityType: 'AWS SNS', icon: 'message', color: '#ca8a04' },
      { id: 'aws-eventbridge', label: 'EventBridge', entityType: 'AWS EventBridge', icon: 'workflow', color: '#8b5cf6' },
      { id: 'aws-cloudfront', label: 'CloudFront', entityType: 'AWS CloudFront', icon: 'globe', color: '#0284c7' },
      { id: 'aws-cognito', label: 'Cognito', entityType: 'AWS Cognito', icon: 'lock', color: '#7c3aed' },
    ],
  },
  {
    id: 'azure',
    label: 'Azure',
    items: [
      { id: 'azure-functions', label: 'Functions', entityType: 'Azure Functions', icon: 'cloud', color: '#0284c7' },
      { id: 'azure-app-service', label: 'App Service', entityType: 'Azure App Service', icon: 'cloud', color: '#0284c7' },
      { id: 'azure-aks', label: 'AKS', entityType: 'Azure AKS', icon: 'cloud', color: '#0284c7' },
      { id: 'azure-apim', label: 'API Mgmt', entityType: 'Azure API Management', icon: 'server', color: '#2563eb' },
      { id: 'azure-blob', label: 'Blob', entityType: 'Azure Blob Storage', icon: 'hard_drive', color: '#16a34a' },
      { id: 'azure-sql', label: 'SQL DB', entityType: 'Azure SQL Database', icon: 'database', color: '#2563eb' },
      { id: 'azure-cosmos', label: 'Cosmos DB', entityType: 'Azure Cosmos DB', icon: 'database', color: '#7c3aed' },
      { id: 'azure-service-bus', label: 'Service Bus', entityType: 'Azure Service Bus', icon: 'message', color: '#ca8a04' },
      { id: 'azure-event-grid', label: 'Event Grid', entityType: 'Azure Event Grid', icon: 'workflow', color: '#8b5cf6' },
      { id: 'azure-entra', label: 'Entra ID', entityType: 'Azure Entra ID', icon: 'lock', color: '#7c3aed' },
    ],
  },
  {
    id: 'gcp',
    label: 'GCP',
    items: [
      { id: 'gcp-cloud-run', label: 'Cloud Run', entityType: 'GCP Cloud Run', icon: 'cloud', color: '#2563eb' },
      { id: 'gcp-functions', label: 'Functions', entityType: 'GCP Cloud Functions', icon: 'cloud', color: '#2563eb' },
      { id: 'gcp-gke', label: 'GKE', entityType: 'GCP GKE', icon: 'cloud', color: '#2563eb' },
      { id: 'gcp-api-gateway', label: 'API GW', entityType: 'GCP API Gateway', icon: 'server', color: '#0284c7' },
      { id: 'gcp-storage', label: 'Storage', entityType: 'GCP Cloud Storage', icon: 'hard_drive', color: '#16a34a' },
      { id: 'gcp-sql', label: 'Cloud SQL', entityType: 'GCP Cloud SQL', icon: 'database', color: '#2563eb' },
      { id: 'gcp-firestore', label: 'Firestore', entityType: 'GCP Firestore', icon: 'database', color: '#ca8a04' },
      { id: 'gcp-pubsub-topic', label: 'Topic', entityType: 'GCP Pub/Sub Topic', icon: 'message', color: '#ca8a04' },
      { id: 'gcp-pubsub-sub', label: 'Subscription', entityType: 'GCP Pub/Sub Subscription', icon: 'message', color: '#d97706' },
      { id: 'gcp-bigquery', label: 'BigQuery', entityType: 'GCP BigQuery', icon: 'database', color: '#4f46e5' },
    ],
  },
  {
    id: 'operations',
    label: 'Operations',
    items: [
      { id: 'auth', label: 'Auth', entityType: 'Authentication', icon: 'lock', color: '#7c3aed' },
      { id: 'secret-vault', label: 'Secrets', entityType: 'Secret Vault', icon: 'lock', color: '#475569' },
      { id: 'observability', label: 'Observability', entityType: 'Observability', icon: 'monitor', color: '#0f766e' },
      { id: 'logging', label: 'Logging', entityType: 'Logging', icon: 'terminal', color: '#64748b' },
      { id: 'metrics', label: 'Metrics', entityType: 'Metrics', icon: 'monitor', color: '#16a34a' },
      { id: 'pipeline', label: 'Pipeline', entityType: 'Pipeline', icon: 'workflow', color: '#0284c7' },
    ],
  },
];

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

function ArchitectureValidationPanel({
  result,
  loading,
  error,
}: {
  result: ArchitectureDesignValidationResult | null;
  loading: boolean;
  error: string | null;
}) {
  const issues = result?.issues || [];
  const warnings = result?.warnings || [];
  if (!error && issues.length === 0 && warnings.length === 0) return null;

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
            {warnings.length > 0 && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-700 dark:bg-amber-900/60 dark:text-amber-100">
                {warnings.length} warning{warnings.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {error && <p className="mt-1 text-red-700 dark:text-red-300">{error}</p>}
          {(issues.length > 0 || warnings.length > 0) && (
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
              {warnings.length > 0 && (
                <div>
                  <div className="mb-1 font-medium text-amber-800 dark:text-amber-200">Warnings</div>
                  <ul className="space-y-1">
                    {warnings.map((item) => (
                      <li key={item} className="rounded border border-amber-200 bg-white/70 px-2 py-1 dark:border-amber-900/70 dark:bg-gray-950/50">
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
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
  const value = (type || '').toLowerCase();
  if (value.includes('db') || value.includes('database') || value.includes('repository') || value.includes('store')) return '#16a34a';
  if (value.includes('queue') || value.includes('event') || value.includes('message') || value.includes('interface')) return '#ca8a04';
  if (value.includes('external') || value.includes('adapter')) return '#7c3aed';
  if (value.includes('api') || value.includes('server')) return '#0284c7';
  return '#0891b2';
}

function iconForEntityType(type: string | null | undefined): ArchitectureVisualIcon {
  const value = (type || '').toLowerCase();
  if (value.includes('db') || value.includes('database') || value.includes('repository') || value.includes('store')) return 'database';
  if (value.includes('queue') || value.includes('event') || value.includes('message') || value.includes('interface')) return 'message';
  if (value.includes('actor') || value.includes('user')) return 'user';
  if (value.includes('cloud') || value.includes('saas')) return 'cloud';
  if (value.includes('security') || value.includes('auth') || value.includes('identity')) return 'lock';
  if (value.includes('web') || value.includes('browser')) return 'globe';
  if (value.includes('mobile')) return 'smartphone';
  if (value.includes('job') || value.includes('cli') || value.includes('worker')) return 'terminal';
  if (value.includes('workflow') || value.includes('pipeline')) return 'workflow';
  if (value.includes('network') || value.includes('adapter') || value.includes('external')) return 'network';
  if (value.includes('api') || value.includes('server') || value.includes('runtime')) return 'server';
  return 'boxes';
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

export function ArchitectureTab({ parentType, parentId, specIdForCopy, locked = false, expanded = false, screenMockups = [], onChanged }: ArchitectureTabProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  const onChangedRef = useRef(onChanged);
  const [summaries, setSummaries] = useState<ArchitectureDesignSummary[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [design, setDesign] = useState<ArchitectureDesign | null>(null);
  const [selectedDiagramId, setSelectedDiagramId] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [validation, setValidation] = useState<ArchitectureDesignValidationResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
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
  const availableMockups = screenMockups || [];

  const selectedDiagram = useMemo(
    () => design?.diagrams.find((item) => item.id === selectedDiagramId) || design?.diagrams[0] || null,
    [design, selectedDiagramId],
  );
  const selectedComponentSegment = COMPONENT_SEGMENTS.find((segment) => segment.id === selectedComponentSegmentId) || COMPONENT_SEGMENTS[0];

  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  useEffect(() => {
    onChangedRef.current = onChanged;
  }, [onChanged]);

  useEffect(() => {
    setEntityDraft(null);
    setInterfaceDraft(null);
    setEditingEntityIndex(null);
    setEditingInterfaceIndex(null);
  }, [selectedId]);

  const loadList = useCallback(async (preferredSelectedId?: string) => {
    setLoading(true);
    try {
      const data = await apiRef.current.listArchitectureDesigns(parentType, parentId);
      setSummaries(data);
      onChangedRef.current?.(data);
      setSelectedId((current) => {
        const requested = preferredSelectedId || current;
        if (requested && data.some((item) => item.id === requested)) return requested;
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

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    if (!selectedId) {
      setDesign(null);
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
  }, [selectedId]);

  useEffect(() => {
    if (!design) {
      setValidation(null);
      setValidationError(null);
      setValidating(false);
      return undefined;
    }

    const payload: CreateArchitectureDesignRequest = {
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
    if (!newTitle.trim() || !newDescription.trim()) return;
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
    if (!design) return;
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
      });
      const full = await apiRef.current.getArchitectureDesign(updated.id, true);
      setSelectedId(full.id);
      setDesign(full);
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
    if (!design || locked || !confirm('Delete this architecture design?')) return;
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
    if (locked) return;
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
    if (!design || locked) return;
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
    if (!design || !selectedDiagram) return;
    const nextDiagram = addEntityNodeToDiagram(selectedDiagram, entity, index);
    patchDesign({
      diagrams: design.diagrams.map((diagram) => (diagram.id === nextDiagram.id ? nextDiagram : diagram)),
    });
  };

  const addComponentToCurrentDiagram = (preset: ArchitectureComponentPreset) => {
    if (!design || !selectedDiagram || locked) return;
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
    if (!design) return;
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
    if (!design) return;
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
    if (!design) return;
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
    if (!design) return;
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
    if (!design || !entityDraft) return;
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
    if (!design || !interfaceDraft) return;
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
    if (!design) return;
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
    if (!design) return;
    const index = design.entities.findIndex((entity, entityIndex) => (
      [entity.id, entity.name, entityRef(entity, entityIndex)].filter(Boolean).includes(ref)
    ));
    if (index >= 0) deleteEntity(index);
  };

  const duplicateEntity = (index: number) => {
    if (!design) return;
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
    if (!design) return;
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
            </button>
          ))}
          {!locked && (
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
          {!locked && (
            <button type="button" onClick={() => setShowImport(true)} className="p-1.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800" title="Import Excalidraw">
              <FileUp size={15} />
            </button>
          )}
          <button type="button" onClick={refresh} className="p-1.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800" title="Refresh">
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {locked && (
        <div className="px-3 py-2 rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 text-sm text-amber-800 dark:text-amber-200 flex items-center gap-2">
          <Shield size={15} />
          Spec architecture is locked
        </div>
      )}

      {newTitle && !locked && (
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
                  readOnly={locked}
                  className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100"
                />
              </label>
              <label className="block">
                <span className="text-xs text-gray-500 dark:text-gray-400">Description</span>
                <textarea
                  value={design.global_description}
                  onChange={(event) => patchDesign({ global_description: event.target.value })}
                  readOnly={locked}
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
                {!locked && (
                  <button type="button" onClick={saveDesign} disabled={saving} className="btn btn-primary text-sm flex items-center gap-1 disabled:opacity-50">
                    <Save size={14} /> Save
                  </button>
                )}
                {!locked && (
                  <button type="button" onClick={deleteDesign} className="btn btn-secondary text-sm flex items-center gap-1">
                    <Trash2 size={14} /> Delete
                  </button>
                )}
              </div>
              <ArchitectureValidationPanel result={validation} loading={validating} error={validationError} />
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
                      disabled={locked}
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
              {!locked && (
                <button type="button" onClick={addDiagram} className="px-2 py-1 rounded text-xs text-cyan-600 hover:bg-cyan-50 dark:hover:bg-cyan-950/30 flex items-center gap-1">
                  <Plus size={12} /> Diagram
                </button>
              )}
            </div>
            {selectedDiagram && (
              <div className="shrink-0 grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-gray-500 dark:text-gray-400">Diagram title</span>
                  <input value={selectedDiagram.title} onChange={(event) => updateDiagram({ ...selectedDiagram, title: event.target.value })} readOnly={locked} className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100" />
                </label>
                <label className="block">
                  <span className="text-xs text-gray-500 dark:text-gray-400">Diagram description</span>
                  <input value={selectedDiagram.description || ''} onChange={(event) => updateDiagram({ ...selectedDiagram, description: event.target.value })} readOnly={locked} className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100" />
                </label>
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-hidden">
              <ArchitectureDiagramEditor
                diagram={selectedDiagram}
                entities={design.entities}
                interfaces={design.interfaces}
                mockups={availableMockups}
                readOnly={locked}
                onChange={updateDiagram}
                onDeleteLinkedEntity={deleteEntityByRef}
              />
            </div>
          </main>

          <aside className="space-y-3 min-w-0 min-h-0 overflow-y-auto overflow-x-hidden pr-2 [scrollbar-gutter:stable]">
            <CollapsibleSection
              id="entities"
              title="Entities"
              open={openPanels.entities}
              onToggle={togglePanel}
              action={!locked && (
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
