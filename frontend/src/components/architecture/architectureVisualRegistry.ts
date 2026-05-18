export type ArchitectureVisualIcon =
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

export type ArchitectureVisualTheme = 'light' | 'dark';
export type ArchitectureVisualSource = 'semantic' | 'custom' | 'fallback';

export interface ArchitectureComponentPreset {
  id: string;
  label: string;
  entityType: string;
  icon: ArchitectureVisualIcon;
  color: string;
}

export interface ArchitectureComponentSegment {
  id: string;
  label: string;
  items: ArchitectureComponentPreset[];
}

export interface ArchitectureVisualInput {
  elementType?: string | null;
  architectureKind?: string | null;
  displayType?: string | null;
  iconName?: string | null;
  strokeColor?: string | null;
  backgroundColor?: string | null;
  theme?: ArchitectureVisualTheme;
}

export interface ArchitectureVisualTokens {
  stroke: string;
  fill: string;
  text: string;
  mutedText: string;
  icon: ArchitectureVisualIcon;
  source: ArchitectureVisualSource;
  matchedType: string | null;
}

type VisualFamily =
  | 'component'
  | 'compute'
  | 'api'
  | 'database'
  | 'storage'
  | 'message'
  | 'workflow'
  | 'network'
  | 'security'
  | 'actor'
  | 'ui'
  | 'danger';

type ThemeScale = {
  light: string;
  dark: string;
};

interface FamilyPalette {
  stroke: ThemeScale;
  fill: ThemeScale;
  text: ThemeScale;
  mutedText: ThemeScale;
}

const ICONS = new Set<ArchitectureVisualIcon>([
  'boxes',
  'server',
  'database',
  'message',
  'user',
  'network',
  'cloud',
  'cpu',
  'globe',
  'hard_drive',
  'lock',
  'monitor',
  'package',
  'smartphone',
  'terminal',
  'workflow',
]);

const PALETTES: Record<VisualFamily, FamilyPalette> = {
  component: {
    stroke: { light: '#0891b2', dark: '#22d3ee' },
    fill: { light: '#ecfeff', dark: '#083344' },
    text: { light: '#0f172a', dark: '#ecfeff' },
    mutedText: { light: '#475569', dark: '#a5f3fc' },
  },
  compute: {
    stroke: { light: '#f59e0b', dark: '#fbbf24' },
    fill: { light: '#fffbeb', dark: '#451a03' },
    text: { light: '#1f2937', dark: '#fffbeb' },
    mutedText: { light: '#92400e', dark: '#fde68a' },
  },
  api: {
    stroke: { light: '#0284c7', dark: '#38bdf8' },
    fill: { light: '#f0f9ff', dark: '#082f49' },
    text: { light: '#0f172a', dark: '#f0f9ff' },
    mutedText: { light: '#075985', dark: '#bae6fd' },
  },
  database: {
    stroke: { light: '#2563eb', dark: '#60a5fa' },
    fill: { light: '#eff6ff', dark: '#172554' },
    text: { light: '#0f172a', dark: '#eff6ff' },
    mutedText: { light: '#1d4ed8', dark: '#bfdbfe' },
  },
  storage: {
    stroke: { light: '#16a34a', dark: '#4ade80' },
    fill: { light: '#f0fdf4', dark: '#052e16' },
    text: { light: '#0f172a', dark: '#f0fdf4' },
    mutedText: { light: '#15803d', dark: '#bbf7d0' },
  },
  message: {
    stroke: { light: '#ca8a04', dark: '#facc15' },
    fill: { light: '#fefce8', dark: '#422006' },
    text: { light: '#1f2937', dark: '#fefce8' },
    mutedText: { light: '#854d0e', dark: '#fef08a' },
  },
  workflow: {
    stroke: { light: '#8b5cf6', dark: '#c4b5fd' },
    fill: { light: '#f5f3ff', dark: '#2e1065' },
    text: { light: '#1f2937', dark: '#f5f3ff' },
    mutedText: { light: '#6d28d9', dark: '#ddd6fe' },
  },
  network: {
    stroke: { light: '#7c3aed', dark: '#a78bfa' },
    fill: { light: '#faf5ff', dark: '#3b0764' },
    text: { light: '#1f2937', dark: '#faf5ff' },
    mutedText: { light: '#6b21a8', dark: '#e9d5ff' },
  },
  security: {
    stroke: { light: '#7c3aed', dark: '#c084fc' },
    fill: { light: '#faf5ff', dark: '#3b0764' },
    text: { light: '#1f2937', dark: '#faf5ff' },
    mutedText: { light: '#6b21a8', dark: '#e9d5ff' },
  },
  actor: {
    stroke: { light: '#0f766e', dark: '#2dd4bf' },
    fill: { light: '#f0fdfa', dark: '#042f2e' },
    text: { light: '#0f172a', dark: '#f0fdfa' },
    mutedText: { light: '#0f766e', dark: '#99f6e4' },
  },
  ui: {
    stroke: { light: '#0ea5e9', dark: '#38bdf8' },
    fill: { light: '#f0f9ff', dark: '#082f49' },
    text: { light: '#0f172a', dark: '#f0f9ff' },
    mutedText: { light: '#0369a1', dark: '#bae6fd' },
  },
  danger: {
    stroke: { light: '#dc2626', dark: '#f87171' },
    fill: { light: '#fef2f2', dark: '#450a0a' },
    text: { light: '#1f2937', dark: '#fef2f2' },
    mutedText: { light: '#991b1b', dark: '#fecaca' },
  },
};

export const ARCHITECTURE_COMPONENT_SEGMENTS: ArchitectureComponentSegment[] = [
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

const PRESET_LOOKUP = new Map<string, ArchitectureComponentPreset>();
const PRESET_ALIASES: Record<string, string[]> = {
  api: ['rest api', 'http api', 'backend api'],
  bff: ['backend for frontend'],
  worker: ['background worker', 'async worker'],
  scheduler: ['cron', 'scheduled job'],
  'external-service': ['third party', 'external system'],
  postgresql: ['postgres', 'pgsql'],
  sqlite: ['sqlite db'],
  mongodb: ['mongo', 'mongo db'],
  opensearch: ['open search', 'search index'],
  warehouse: ['data warehouse', 'analytics warehouse'],
  'object-storage': ['object store', 'blob store'],
  service: ['microservice', 'backend service'],
  repository: ['repo', 'data repository'],
  gateway: ['api gateway', 'edge gateway'],
  'trust-zone': ['trust boundary'],
  contract: ['api contract', 'data contract'],
  webhook: ['web hook'],
  stream: ['stream', 'stream processor'],
  topic: ['pubsub topic', 'pub/sub topic'],
  queue: ['message queue'],
  dlq: ['dead letter queue', 'dead-letter queue'],
  'event-bus': ['eventbus', 'event bridge'],
  'aws-lambda': ['lambda', 'aws function', 'aws functions'],
  'aws-api-gateway': ['api gateway', 'apigateway', 'aws api gw', 'api gw'],
  'aws-s3': ['s3', 'simple storage service'],
  'aws-rds': ['rds', 'relational database service'],
  'aws-dynamodb': ['dynamodb', 'dynamo db'],
  'aws-sqs': ['sqs', 'simple queue service'],
  'aws-sns': ['sns', 'simple notification service'],
  'aws-eventbridge': ['eventbridge', 'event bridge'],
  'aws-cloudfront': ['cloudfront', 'cloud front', 'cdn'],
  'aws-cognito': ['cognito', 'user pool', 'identity pool'],
  'azure-functions': ['azure function'],
  'azure-app-service': ['app service', 'azure web app'],
  'azure-aks': ['aks', 'azure kubernetes service'],
  'azure-apim': ['apim', 'api management', 'azure api management'],
  'azure-blob': ['blob', 'blob storage', 'azure storage'],
  'azure-sql': ['azure sql', 'sql database'],
  'azure-cosmos': ['cosmos', 'cosmos db'],
  'azure-service-bus': ['service bus'],
  'azure-event-grid': ['event grid'],
  'azure-entra': ['entra', 'entra id', 'azure ad'],
  'gcp-functions': ['cloud functions'],
  'gcp-cloud-run': ['cloud run'],
  'gcp-gke': ['gke', 'google kubernetes engine'],
  'gcp-api-gateway': ['gcp api gateway', 'google api gateway'],
  'gcp-storage': ['cloud storage', 'gcs', 'google cloud storage'],
  'gcp-sql': ['cloud sql', 'google cloud sql'],
  'gcp-firestore': ['firestore'],
  'gcp-pubsub-topic': ['pubsub topic', 'pub/sub topic', 'google pubsub topic'],
  'gcp-pubsub-sub': ['pubsub subscription', 'pub/sub subscription', 'google pubsub subscription'],
  'gcp-bigquery': ['bigquery', 'big query'],
  auth: ['authentication', 'identity provider'],
  'secret-vault': ['secrets', 'vault', 'secret manager'],
  observability: ['monitoring', 'telemetry'],
  logging: ['logs'],
  pipeline: ['ci cd', 'ci/cd', 'deployment pipeline'],
};

for (const segment of ARCHITECTURE_COMPONENT_SEGMENTS) {
  for (const preset of segment.items) {
    [
      preset.id,
      preset.label,
      preset.entityType,
      ...(PRESET_ALIASES[preset.id] || []),
    ].forEach((key) => {
      const normalized = normalizeKey(key);
      if (!PRESET_LOOKUP.has(normalized)) {
        PRESET_LOOKUP.set(normalized, preset);
      }
    });
  }
}

function normalizeKey(value: string | null | undefined): string {
  return (value || '')
    .toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ');
}

function parseHexColor(value: string | null | undefined): [number, number, number] | null {
  if (!value) return null;
  const trimmed = value.trim().toLowerCase();
  const short = /^#([0-9a-f]{3})$/.exec(trimmed);
  if (short) {
    return short[1].split('').map((part) => parseInt(part + part, 16)) as [number, number, number];
  }
  const long = /^#([0-9a-f]{6})$/.exec(trimmed);
  if (!long) return null;
  return [
    parseInt(long[1].slice(0, 2), 16),
    parseInt(long[1].slice(2, 4), 16),
    parseInt(long[1].slice(4, 6), 16),
  ];
}

function luminance([red, green, blue]: [number, number, number]): number {
  const [r, g, b] = [red, green, blue].map((channel) => {
    const value = channel / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(colorA: string, colorB: string): number | null {
  const parsedA = parseHexColor(colorA);
  const parsedB = parseHexColor(colorB);
  if (!parsedA || !parsedB) return null;
  const lumA = luminance(parsedA);
  const lumB = luminance(parsedB);
  const light = Math.max(lumA, lumB);
  const dark = Math.min(lumA, lumB);
  return (light + 0.05) / (dark + 0.05);
}

export function isArchitectureVisualIcon(value: string | null | undefined): value is ArchitectureVisualIcon {
  return Boolean(value && ICONS.has(value as ArchitectureVisualIcon));
}

export function findArchitecturePreset(type: string | null | undefined): ArchitectureComponentPreset | null {
  const key = normalizeKey(type);
  if (!key) return null;
  if (PRESET_LOOKUP.has(key)) return PRESET_LOOKUP.get(key) || null;
  for (const [candidate, preset] of PRESET_LOOKUP.entries()) {
    if (key.includes(candidate) || candidate.includes(key)) return preset;
  }
  return null;
}

function familyForType(type: string | null | undefined): VisualFamily {
  const value = normalizeKey(type);
  if (!value) return 'component';
  if (value.includes('dlq') || value.includes('dead letter') || value.includes('redis')) return 'danger';
  if (value.includes('lambda') || value.includes('ecs') || value.includes('eks') || value.includes('function') || value.includes('run') || value.includes('worker') || value.includes('processor')) return 'compute';
  if (value.includes('api') || value.includes('bff') || value.includes('gateway') || value.includes('server') || value.includes('webhook')) return 'api';
  if (value.includes('db') || value.includes('database') || value.includes('postgres') || value.includes('mysql') || value.includes('mongo') || value.includes('repository') || value.includes('warehouse') || value.includes('search')) return 'database';
  if (value.includes('storage') || value.includes('store') || value.includes('s3') || value.includes('blob') || value.includes('cache')) return 'storage';
  if (value.includes('queue') || value.includes('topic') || value.includes('message') || value.includes('sns') || value.includes('sqs') || value.includes('interface') || value.includes('contract')) return 'message';
  if (value.includes('event') || value.includes('stream') || value.includes('workflow') || value.includes('pipeline') || value.includes('scheduler')) return 'workflow';
  if (value.includes('external') || value.includes('adapter') || value.includes('network') || value.includes('exchange')) return 'network';
  if (value.includes('security') || value.includes('auth') || value.includes('identity') || value.includes('cognito') || value.includes('entra') || value.includes('vault') || value.includes('trust')) return 'security';
  if (value.includes('actor') || value.includes('user')) return 'actor';
  if (value.includes('ui') || value.includes('mobile') || value.includes('desktop') || value.includes('web app') || value.includes('browser')) return 'ui';
  return 'component';
}

function isDefaultFill(value: string | null | undefined): boolean {
  if (!value) return true;
  return ['#ecfeff', '#e0f2fe', '#ffffff', '#fff', 'white', 'transparent'].includes(value.trim().toLowerCase());
}

function isSafeStroke(color: string, theme: ArchitectureVisualTheme): boolean {
  const canvas = theme === 'dark' ? '#020617' : '#ffffff';
  const ratio = contrastRatio(color, canvas);
  return ratio !== null && ratio >= 3;
}

function isSafeFill(color: string, text: string): boolean {
  const ratio = contrastRatio(color, text);
  return ratio !== null && ratio >= 4.5;
}

export function colorForArchitectureType(type: string | null | undefined): string {
  const preset = findArchitecturePreset(type);
  if (preset) return preset.color;
  return PALETTES[familyForType(type)].stroke.light;
}

export function iconForArchitectureType(type: string | null | undefined): ArchitectureVisualIcon {
  const preset = findArchitecturePreset(type);
  if (preset) return preset.icon;
  const family = familyForType(type);
  if (family === 'compute') return 'cpu';
  if (family === 'api') return 'server';
  if (family === 'database') return 'database';
  if (family === 'storage') return 'hard_drive';
  if (family === 'message') return 'message';
  if (family === 'workflow') return 'workflow';
  if (family === 'network') return 'network';
  if (family === 'security') return 'lock';
  if (family === 'actor') return 'user';
  if (family === 'ui') return 'monitor';
  if (family === 'danger') return 'message';
  return 'boxes';
}

export function resolveArchitectureVisualStyle(input: ArchitectureVisualInput): ArchitectureVisualTokens {
  const theme = input.theme || 'light';
  const semanticType = input.displayType || input.architectureKind;
  const preset = findArchitecturePreset(semanticType);
  const family = familyForType(preset?.entityType || semanticType);
  const palette = PALETTES[family];
  const explicitIcon = isArchitectureVisualIcon(input.iconName) ? input.iconName : null;
  const icon = explicitIcon || preset?.icon || iconForArchitectureType(semanticType);
  const isEdge = input.elementType === 'arrow' || normalizeKey(input.architectureKind).includes('relationship');
  const baseStroke = isEdge ? PALETTES.network.stroke[theme] : palette.stroke[theme];
  const baseFill = isEdge ? 'transparent' : palette.fill[theme];
  const baseText = palette.text[theme];
  let source: ArchitectureVisualSource = preset || semanticType ? 'semantic' : 'fallback';
  let stroke = baseStroke;
  let fill = baseFill;
  let rejectedCustomFill = false;

  if (input.strokeColor && isSafeStroke(input.strokeColor, theme)) {
    stroke = input.strokeColor;
    source = 'custom';
  } else if (input.strokeColor) {
    source = 'fallback';
  }

  if (!isEdge && input.backgroundColor && !isDefaultFill(input.backgroundColor)) {
    if (isSafeFill(input.backgroundColor, baseText)) {
      fill = input.backgroundColor;
      source = 'custom';
    } else {
      source = 'fallback';
      rejectedCustomFill = true;
    }
  }

  if (rejectedCustomFill && (preset || semanticType)) {
    stroke = baseStroke;
  }

  return {
    stroke,
    fill,
    text: baseText,
    mutedText: palette.mutedText[theme],
    icon,
    source,
    matchedType: preset?.entityType || semanticType || null,
  };
}
