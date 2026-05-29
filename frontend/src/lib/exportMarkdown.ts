/**
 * exportMarkdown.ts — Markdown export utilities for all Okto Pulse entities.
 *
 * Each entity type has a dedicated generator that compiles structured data
 * into a well-formatted Markdown string. Tasks resolve references from
 * their parent spec (TRs, BRs, FRs, ACs, test scenarios, API contracts,
 * decisions, KBs, mockups, and architecture designs).
 */

import type {
  Ideation,
  Refinement,
  Spec,
  Card,
  TestScenario,
  BusinessRule,
  ApiContract,
  IntegrationRequirement,
  ObservabilityRequirement,
  ScreenMockup,
  Story,
  TechnicalRequirement,
  SpecKnowledgeSummary,
  ConclusionEntry,
  ValidationEntry,
  ArchitectureWarningRecord,
  ArchitectureDesign,
  ArchitectureDiagram,
  ArchitectureEntity,
  ArchitectureInterface,
} from '@/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export type ExportWarningKind =
  | 'broken_link'
  | 'unresolved_reference'
  | 'architecture_warning'
  | 'asset_unavailable';

export type ExportWarningSeverity = 'info' | 'low' | 'medium' | 'high' | 'critical';

export interface ExportWarning {
  kind: ExportWarningKind;
  severity: ExportWarningSeverity;
  origin: string;
  source_ref?: string;
  message: string;
  impact?: string;
}

export type ExportWarningCandidate = {
  kind?: ExportWarningKind | string | null;
  severity?: ExportWarningSeverity | string | null;
  origin?: string | null;
  source_ref?: string | null;
  message?: string | null;
  impact?: string | null;
  code?: string | null;
  path?: string | null;
  diagram_id?: string | null;
  element_id?: string | null;
  entity_id?: string | null;
  node_ref?: string | null;
  suggested_fix?: string | null;
};

export interface ExportWarningCollectionInput {
  broken_links?: ExportWarningCandidate[] | null;
  unresolved_references?: ExportWarningCandidate[] | null;
  architecture_warnings?: ExportWarningCandidate[] | null;
  asset_warnings?: ExportWarningCandidate[] | null;
}

export interface ExportWarningCollector {
  add(warning: ExportWarningCandidate): void;
  addMany(warnings: ExportWarningCandidate[] | null | undefined): void;
  collect(input: ExportWarningCollectionInput | null | undefined): void;
  toArray(): ExportWarning[];
}

export interface ResolvedReferenceForExport {
  input: string | number;
  status: 'resolved' | 'unresolved';
  index?: number;
  id?: string;
  text?: string;
  warning?: ExportWarning;
}

const EXPORT_WARNING_SEVERITY_RANK: Record<ExportWarningSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
};

const EXPORT_WARNING_KINDS = new Set<ExportWarningKind>([
  'broken_link',
  'unresolved_reference',
  'architecture_warning',
  'asset_unavailable',
]);

const WARNING_BUCKETS: Array<[keyof ExportWarningCollectionInput, ExportWarningKind]> = [
  ['broken_links', 'broken_link'],
  ['unresolved_references', 'unresolved_reference'],
  ['architecture_warnings', 'architecture_warning'],
  ['asset_warnings', 'asset_unavailable'],
];

function cleanString(value: unknown): string | undefined {
  if (value == null) return undefined;
  const text = String(value).trim();
  return text || undefined;
}

function readableValue(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value, null, 2);
}

function normalizeWarningKind(value: unknown, fallback: ExportWarningKind): ExportWarningKind {
  const text = cleanString(value);
  return text && EXPORT_WARNING_KINDS.has(text as ExportWarningKind)
    ? text as ExportWarningKind
    : fallback;
}

function normalizeWarningSeverity(value: unknown): ExportWarningSeverity {
  const text = cleanString(value)?.toLowerCase();
  if (text === 'critical') return 'critical';
  if (text === 'high' || text === 'error') return 'high';
  if (text === 'low') return 'low';
  if (text === 'info' || text === 'notice') return 'info';
  return 'medium';
}

function warningSourceRef(candidate: ExportWarningCandidate): string | undefined {
  if (candidate.source_ref) return cleanString(candidate.source_ref);
  if (candidate.diagram_id && candidate.element_id) return `${candidate.diagram_id} / ${candidate.element_id}`;
  if (candidate.diagram_id && candidate.entity_id) return `${candidate.diagram_id} / ${candidate.entity_id}`;
  if (candidate.diagram_id && candidate.node_ref) return `${candidate.diagram_id} / ${candidate.node_ref}`;
  return cleanString(candidate.entity_id || candidate.node_ref || candidate.path);
}

function redactArchitectureRawPayloadTerms(value: string | undefined): string | undefined {
  let text = cleanString(value);
  if (!text) return undefined;
  const hasRawPayloadContext = /\badapter_payload\b|\badapterPayload\b/.test(text);
  text = text
    .replace(/\badapter_payload\b/g, 'diagram_payload')
    .replace(/\badapterPayload\b/g, 'diagramPayload')
    .replace(/appState/g, 'diagram_state');
  if (hasRawPayloadContext) {
    return text
      .replace(/\belements\b/g, 'diagram_items')
      .replace(/\bfiles\b/g, 'asset_refs');
  }
  return text
    .replace(/([.\[])\b(elements|files)\b/g, (_match, prefix: string, word: string) => (
      `${prefix}${word === 'elements' ? 'diagram_items' : 'asset_refs'}`
    ))
    .replace(/\b(elements|files)(?=[.\[])/g, (_match, word: string) => (
      word === 'elements' ? 'diagram_items' : 'asset_refs'
    ));
}

function normalizeExportWarning(
  candidate: ExportWarningCandidate,
  fallbackKind: ExportWarningKind,
): ExportWarning {
  const kind = normalizeWarningKind(candidate.kind, fallbackKind);
  const rawOrigin = cleanString(candidate.origin || candidate.code || candidate.path) || kind;
  const origin = kind === 'architecture_warning'
    ? redactArchitectureRawPayloadTerms(rawOrigin) || rawOrigin
    : rawOrigin;
  const rawSourceRef = warningSourceRef(candidate);
  const sourceRef = kind === 'architecture_warning'
    ? redactArchitectureRawPayloadTerms(rawSourceRef)
    : rawSourceRef;
  const rawMessage = cleanString(candidate.message || candidate.code) || `${kind} detected`;
  const rawImpact = cleanString(candidate.impact || candidate.suggested_fix);
  const message = kind === 'architecture_warning'
    ? redactArchitectureRawPayloadTerms(rawMessage) || rawMessage
    : rawMessage;
  const impact = kind === 'architecture_warning'
    ? redactArchitectureRawPayloadTerms(rawImpact)
    : rawImpact;
  return {
    kind,
    severity: normalizeWarningSeverity(candidate.severity),
    origin,
    ...(sourceRef ? { source_ref: sourceRef } : {}),
    message,
    ...(impact ? { impact } : {}),
  };
}

function warningSortKey(warning: ExportWarning): [number, string, string, string, string, string] {
  return [
    EXPORT_WARNING_SEVERITY_RANK[warning.severity],
    warning.kind,
    warning.origin,
    warning.source_ref || '',
    warning.message,
    warning.impact || '',
  ];
}

function compareExportWarnings(left: ExportWarning, right: ExportWarning): number {
  const a = warningSortKey(left);
  const b = warningSortKey(right);
  for (let i = 0; i < a.length; i += 1) {
    const delta = typeof a[i] === 'number'
      ? (a[i] as number) - (b[i] as number)
      : String(a[i]).localeCompare(String(b[i]));
    if (delta !== 0) return delta;
  }
  return 0;
}

function warningIdentity(warning: ExportWarning): string {
  return JSON.stringify([
    warning.kind,
    warning.severity,
    warning.origin,
    warning.source_ref || '',
    warning.message,
    warning.impact || '',
  ]);
}

export function collectExportWarnings(input: ExportWarningCollectionInput | null | undefined): ExportWarning[] {
  if (!input) return [];
  const byIdentity = new Map<string, ExportWarning>();
  for (const [bucket, kind] of WARNING_BUCKETS) {
    const warnings = input[bucket];
    if (!warnings?.length) continue;
    for (const warning of warnings) {
      const normalized = normalizeExportWarning(warning, kind);
      byIdentity.set(warningIdentity(normalized), normalized);
    }
  }
  return Array.from(byIdentity.values()).sort(compareExportWarnings);
}

export function createExportWarningCollector(): ExportWarningCollector {
  const candidates: ExportWarningCandidate[] = [];
  return {
    add(warning) {
      candidates.push(warning);
    },
    addMany(warnings) {
      if (warnings?.length) candidates.push(...warnings);
    },
    collect(input) {
      for (const warning of collectExportWarnings(input)) {
        candidates.push(warning);
      }
    },
    toArray() {
      return collectExportWarnings({ unresolved_references: candidates });
    },
  };
}

export function renderExportWarnings(warnings: ExportWarning[] | null | undefined): string {
  if (!warnings?.length) return '';
  const items = warnings
    .map((warning) => {
      const source = warning.source_ref ? ` (${warning.source_ref})` : '';
      const impact = warning.impact ? ` Impact: ${warning.impact}` : '';
      return `- **${warning.severity}** \`${warning.kind}\` from **${warning.origin}**${source}: ${warning.message}${impact}`;
    })
    .join('\n');
  return `## Export Warnings\n\n${items}\n\n`;
}

/** Create a filename-safe slug from a title. */
export function slugify(title: string, max = 50): string {
  return title
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '') // strip accents
    .replace(/[^a-z0-9\s-]/g, '')   // remove special chars
    .replace(/\s+/g, '-')           // spaces → hyphens
    .replace(/-+/g, '-')            // collapse hyphens
    .replace(/^-|-$/g, '')          // trim edges
    .slice(0, max);
}

function safeSlug(title: string | null | undefined, fallback = 'untitled', max = 50): string {
  return slugify(title || '', max) || fallback;
}

/** Sanitize a browser download filename for Markdown exports. */
export function sanitizeMarkdownFilename(filename: string, fallback = 'export.md'): string {
  const base = (filename || fallback)
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[._-]+|[._-]+$/g, '');
  const withName = base || fallback.replace(/\.md$/i, '');
  const withoutExtension = withName.replace(/\.md$/i, '').replace(/^[._-]+|[._-]+$/g, '') || 'export';
  return `${withoutExtension}.md`;
}

export function markdownFilenameForSpec(spec: Pick<Spec, 'title' | 'version'>): string {
  return sanitizeMarkdownFilename(`spec_${safeSlug(spec.title)}_v${spec.version ?? 1}.md`);
}

export function markdownFilenameForCard(card: Pick<Card, 'title' | 'card_type'>): string {
  const type = card.card_type === 'bug'
    ? 'bug'
    : card.card_type === 'test'
      ? 'test'
      : 'task';
  return sanitizeMarkdownFilename(`${type}_${safeSlug(card.title)}.md`);
}

/** Format an ISO date string to YYYY-MM-DD HH:mm */
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Trigger browser download of a text file. */
export function downloadMarkdown(content: string, filename: string): void {
  const safeFilename = sanitizeMarkdownFilename(filename);
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = safeFilename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function formatScalar(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.map(formatScalar).filter(Boolean).join(', ');
  return `\n\`\`\`json\n${readableValue(value)}\n\`\`\``;
}

function formatListValue(value: unknown): string {
  const formatted = formatScalar(value).trim();
  return formatted || 'N/A';
}

function renderJsonBlock(title: string, value: unknown): string {
  if (value == null) return '';
  return `**${title}:**\n\`\`\`json\n${readableValue(value)}\n\`\`\`\n\n`;
}

function renderKeyValues(rows: Array<[string, unknown]>): string {
  const filtered = rows
    .map(([key, value]) => [key, formatScalar(value).trim()] as [string, string])
    .filter(([, value]) => Boolean(value));
  if (!filtered.length) return '';
  return filtered.map(([key, value]) => `- **${key}:** ${value}`).join('\n') + '\n\n';
}

/** Add a section only if content is non-empty. */
function section(heading: string, body: unknown, level = 2): string {
  const formatted = formatScalar(body).trim();
  if (!formatted) return '';
  const prefix = '#'.repeat(level);
  return `${prefix} ${heading}\n\n${formatted}\n\n`;
}

/** Render a string[] as a numbered list. */
function numberedList(items: (string | TechnicalRequirement)[]): string {
  return items
    .map((item, i) => {
      const text = typeof item === 'string' ? item : item.text || readableValue(item);
      return `${i + 1}. ${text}`;
    })
    .join('\n');
}

/** Render a string[] as bullets. */
function bulletList(items: unknown[]): string {
  return items.map(item => `- ${formatListValue(item)}`).join('\n');
}

/** Render metadata table at the top of the document. */
function metaTable(rows: [string, string][]): string {
  const filtered = rows.filter(([, v]) => !!v);
  if (filtered.length === 0) return '';
  return filtered.map(([k, v]) => `| **${k}** | ${v} |`).join('\n') + '\n\n';
}

function criterionText(item: unknown): string {
  if (typeof item === 'string') return item;
  if (item && typeof item === 'object') {
    const record = item as Record<string, unknown>;
    return cleanString(record.text || record.value || record.title || record.description || record.id) || readableValue(item);
  }
  return readableValue(item);
}

function criterionId(item: unknown): string | undefined {
  if (!item || typeof item !== 'object') return undefined;
  return cleanString((item as Record<string, unknown>).id);
}

function criterionLabel(index: number, text: string): string {
  return `AC${index}: ${text}`;
}

function parseCriterionIndex(value: string | number): number | undefined {
  if (typeof value === 'number' && Number.isInteger(value)) {
    if (value === 0) return 1;
    return value > 0 ? value : undefined;
  }
  const text = String(value).trim();
  const match = text.match(/^AC[-\s_]*(\d+)$/i) || text.match(/^(\d+)$/);
  if (!match) return undefined;
  const parsed = Number.parseInt(match[1], 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
}

export function resolveLinkedCriteriaForExport(
  linkedCriteria: Array<string | number> | null | undefined,
  acceptanceCriteria: unknown[] | null | undefined,
): ResolvedReferenceForExport[] {
  if (!linkedCriteria?.length) return [];
  const criteria = acceptanceCriteria || [];
  const criteriaRefs = criteria.map((item, index) => ({
    id: criterionId(item),
    index: index + 1,
    text: criterionText(item),
  }));

  return linkedCriteria.map((input) => {
    const raw = String(input).trim();
    const parsedIndex = parseCriterionIndex(input);
    const byIndex = parsedIndex ? criteriaRefs[parsedIndex - 1] : undefined;
    const byId = criteriaRefs.find((item) => item.id && item.id === raw);
    const byText = criteriaRefs.find((item) => item.text === raw || criterionLabel(item.index, item.text) === raw);
    const resolved = byIndex || byId || byText;
    if (resolved) {
      return {
        input,
        status: 'resolved',
        index: resolved.index,
        ...(resolved.id ? { id: resolved.id } : {}),
        text: resolved.text,
      };
    }

    const warning: ExportWarning = {
      kind: 'unresolved_reference',
      severity: 'medium',
      origin: 'linked_criteria',
      source_ref: raw,
      message: `Linked acceptance criterion could not be resolved: ${raw}`,
    };
    return {
      input,
      status: 'unresolved',
      warning,
    };
  });
}

// ---------------------------------------------------------------------------
// Mockups
// ---------------------------------------------------------------------------

function hasMockupVisualReference(mockup: ScreenMockup): boolean {
  const record = mockup as unknown as Record<string, unknown>;
  return Boolean(
    cleanString(record.preview_ref) ||
    cleanString(record.render_ref) ||
    cleanString(record.asset_ref) ||
    cleanString(record.image_url) ||
    record.render_metadata
  );
}

function renderMockups(
  mockups: ScreenMockup[] | null | undefined,
  warningCollector?: ExportWarningCollector,
): string {
  if (!mockups?.length) return '';
  const items = mockups.map((m, i) => {
    let entry = `### ${i + 1}. ${m.title || 'Untitled mockup'}\n\n`;
    if (m.description) entry += `${m.description}\n\n`;
    if (m.screen_type) entry += `**Type:** ${m.screen_type}\n\n`;
    if (m.origin_id) entry += `**Origin:** ${m.origin_id}\n\n`;
    if (m.annotations?.length) {
      entry += `**Annotations:**\n${m.annotations.map((annotation) => `- ${annotation.text}`).join('\n')}\n\n`;
    }
    if (hasMockupVisualReference(m)) {
      entry += `*[Visual render/reference available in Okto Pulse UI]*\n`;
    } else {
      warningCollector?.add({
        kind: 'asset_unavailable',
        severity: 'medium',
        origin: `mockup:${m.id || m.title || i + 1}`,
        source_ref: m.id,
        message: 'Mockup visual render is unavailable in the export context.',
        impact: 'Markdown includes the structured mockup summary only.',
      });
      entry += `*[No visual render/reference available; structured summary retained]*\n`;
    }
    return entry;
  }).join('\n');
  return `## Screen Mockups\n\n${items}\n`;
}

// ---------------------------------------------------------------------------
// Architecture Designs
// ---------------------------------------------------------------------------

function structuredArchitectureWarnings(design: any): ArchitectureWarningRecord[] {
  if (Array.isArray(design?.structured_warnings)) return design.structured_warnings;
  if (Array.isArray(design?.validation?.structured_warnings)) return design.validation.structured_warnings;
  if (Array.isArray(design?.validation_result?.structured_warnings)) return design.validation_result.structured_warnings;
  return [];
}

function renderArchitectureConnectivityWarnings(design: any, warningCollector?: ExportWarningCollector): string {
  const warnings = collectExportWarnings({
    architecture_warnings: structuredArchitectureWarnings(design),
  });
  if (!warnings.length) return '';
  warningCollector?.addMany(warnings);

  const entries = warnings.map((warning) => {
    const location = warning.source_ref || warning.origin;
    return [
      `- **Code:** \`${warning.origin}\``,
      `  **Location:** \`${location}\``,
      `  **Suggested fix:** ${warning.impact || warning.message}`,
    ].join('\n');
  }).join('\n');

  return `#### Connectivity and Coverage Warnings\n\n${entries}\n\n`;
}

function renderArchitectureMermaidWarnings(warnings: ExportWarningCandidate[] | null | undefined): string {
  const normalized = collectExportWarnings({ architecture_warnings: warnings });
  if (!normalized.length) return '';

  const entries = normalized.map((warning) => {
    const location = warning.source_ref || warning.origin;
    return [
      `- **Code:** \`${warning.origin}\``,
      `  **Location:** \`${location}\``,
      `  **Message:** ${warning.message}`,
      `  **Impact:** ${warning.impact || 'Mermaid projection is partial; complementary architecture metadata is retained.'}`,
    ].join('\n');
  }).join('\n');

  return `#### Mermaid Conversion Warnings\n\n${entries}\n\n`;
}

function hasDiagramVisualReference(diagram: any): boolean {
  return Boolean(
    cleanString(diagram?.preview_ref) ||
    cleanString(diagram?.render_ref) ||
    diagram?.render_metadata
  );
}

export type ArchitectureMermaidRenderedFrom =
  | 'diagram_connections'
  | 'entity_interface_fallback'
  | 'empty';

export interface ArchitectureMermaidRenderResult {
  mermaid: string;
  warnings: ExportWarningCandidate[];
  metadata: {
    renderedFrom: ArchitectureMermaidRenderedFrom;
    diagramIds: string[];
    designId?: string;
    designVersion?: number;
    sourceHash?: string;
  };
}

type ArchitectureMermaidNode = {
  entity: ArchitectureEntity;
  key: string;
  id: string;
  label: string;
  sortKey: string;
};

type ArchitectureMermaidEdge = {
  sourceKey: string;
  targetKey: string;
  label: string;
  sortKey: string;
};

const MERMAID_ID_PATTERN = /^[A-Za-z][A-Za-z0-9_]*$/;
const MERMAID_RESERVED_IDS = new Set([
  'end',
  'subgraph',
  'graph',
  'flowchart',
  'style',
  'linkStyle',
  'classDef',
  'class',
  'click',
  'call',
  'href',
]);

export function sanitizeMermaidId(value: unknown, fallback = 'node'): string {
  const normalized = String(value ?? '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^A-Za-z0-9_]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  const fallbackId = String(fallback || 'node')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^A-Za-z0-9_]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '') || 'node';
  const candidate = normalized || fallbackId;
  const withLeadingLetter = /^[A-Za-z]/.test(candidate) ? candidate : `n_${candidate}`;
  const sanitized = withLeadingLetter.replace(/_+/g, '_');
  const safe = MERMAID_RESERVED_IDS.has(sanitized) ? `n_${sanitized}` : sanitized;
  return MERMAID_ID_PATTERN.test(safe) ? safe : 'node';
}

export function escapeMermaidLabel(value: unknown): string {
  return String(value ?? 'Unnamed')
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/`+/g, "'")
    .replace(/&/g, '#amp;')
    .replace(/"/g, '#quot;')
    .replace(/</g, '#lt;')
    .replace(/>/g, '#gt;')
    .replace(/\[/g, '#91;')
    .replace(/\]/g, '#93;')
    .replace(/\{/g, '#123;')
    .replace(/\}/g, '#125;')
    .trim() || 'Unnamed';
}

function architectureSortValue(value: unknown): string {
  return cleanString(value)?.toLowerCase() || '';
}

function architectureEntityKey(entity: ArchitectureEntity, index: number): string {
  return cleanString(entity.id) || cleanString(entity.name) || `entity:${index + 1}`;
}

function architectureInterfaceKey(itf: ArchitectureInterface, index: number): string {
  return cleanString(itf.id) || cleanString(itf.name) || `interface:${index + 1}`;
}

function sortArchitectureEntities(entities: ArchitectureEntity[] | null | undefined): ArchitectureEntity[] {
  return [...(entities || [])].sort((left, right) => {
    const leftRecord = left as unknown as Record<string, unknown>;
    const rightRecord = right as unknown as Record<string, unknown>;
    const leftOrder = Number(leftRecord.order_index ?? leftRecord.order ?? Number.POSITIVE_INFINITY);
    const rightOrder = Number(rightRecord.order_index ?? rightRecord.order ?? Number.POSITIVE_INFINITY);
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    return architectureSortValue(left.id || left.name)
      .localeCompare(architectureSortValue(right.id || right.name));
  });
}

function sortArchitectureInterfaces(interfaces: ArchitectureInterface[] | null | undefined): ArchitectureInterface[] {
  return [...(interfaces || [])].sort((left, right) => {
    const leftRecord = left as unknown as Record<string, unknown>;
    const rightRecord = right as unknown as Record<string, unknown>;
    const leftOrder = Number(leftRecord.order_index ?? leftRecord.order ?? Number.POSITIVE_INFINITY);
    const rightOrder = Number(rightRecord.order_index ?? rightRecord.order ?? Number.POSITIVE_INFINITY);
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    return architectureSortValue(left.id || left.name)
      .localeCompare(architectureSortValue(right.id || right.name));
  });
}

function sortArchitectureDiagrams(diagrams: ArchitectureDiagram[] | null | undefined): ArchitectureDiagram[] {
  return [...(diagrams || [])].sort((left, right) => {
    const leftOrder = Number(left.order_index ?? Number.POSITIVE_INFINITY);
    const rightOrder = Number(right.order_index ?? Number.POSITIVE_INFINITY);
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    return architectureSortValue(left.id || left.title)
      .localeCompare(architectureSortValue(right.id || right.title));
  });
}

function uniqueMermaidId(raw: unknown, used: Set<string>, fallback: string): string {
  const base = sanitizeMermaidId(raw, fallback);
  if (!used.has(base)) {
    used.add(base);
    return base;
  }
  let suffix = 2;
  while (used.has(`${base}_${suffix}`)) suffix += 1;
  const id = `${base}_${suffix}`;
  used.add(id);
  return id;
}

function buildArchitectureMermaidNodes(entities: ArchitectureEntity[]): ArchitectureMermaidNode[] {
  const usedIds = new Set<string>();
  return entities.map((entity, index) => {
    const key = architectureEntityKey(entity, index);
    const label = cleanString(entity.name) || cleanString(entity.id) || `Entity ${index + 1}`;
    return {
      entity,
      key,
      id: uniqueMermaidId(key, usedIds, `entity_${index + 1}`),
      label,
      sortKey: `${String(index).padStart(5, '0')}:${architectureSortValue(key)}`,
    };
  });
}

function recordString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = cleanString(record[key]);
    if (value) return value;
  }
  return undefined;
}

function nestedRecordString(record: Record<string, unknown>, containerKeys: string[], keys: string[]): string | undefined {
  for (const containerKey of containerKeys) {
    const nested = record[containerKey];
    if (!nested || typeof nested !== 'object' || Array.isArray(nested)) continue;
    const value = recordString(nested as Record<string, unknown>, keys);
    if (value) return value;
  }
  return undefined;
}

function firstRecordArrayString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (!Array.isArray(value)) continue;
    const first = value.map(cleanString).find(Boolean);
    if (first) return first;
  }
  return undefined;
}

function resolveArchitectureEntityKey(ref: unknown, entityKeys: Map<string, string>): string | undefined {
  const raw = cleanString(ref);
  if (!raw) return undefined;
  return entityKeys.get(raw) || entityKeys.get(raw.toLowerCase());
}

function buildEntityLookup(nodes: ArchitectureMermaidNode[]): Map<string, string> {
  const lookup = new Map<string, string>();
  for (const node of nodes) {
    const aliases = [
      node.key,
      node.entity.id,
      node.entity.name,
    ].map(cleanString).filter(Boolean) as string[];
    for (const alias of aliases) {
      lookup.set(alias, node.key);
      lookup.set(alias.toLowerCase(), node.key);
    }
  }
  return lookup;
}

function diagramPayloadElements(payload: unknown): Record<string, unknown>[] {
  if (Array.isArray(payload)) {
    return payload.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
  }
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    const record = payload as Record<string, unknown>;
    const elements = record.elements;
    if (Array.isArray(elements)) {
      return elements.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
    }
    const nodes = Array.isArray(record.nodes) ? record.nodes : [];
    const edges = Array.isArray(record.edges) ? record.edges : [];
    return [...nodes, ...edges].filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
  }
  return [];
}

function linkedEntityFromElement(element: Record<string, unknown>): string | undefined {
  return recordString(element, [
    'entity_id',
    'entityId',
    'architecture_entity_id',
    'architectureEntityId',
    'linked_entity_id',
    'linkedEntityId',
  ])
    || firstRecordArrayString(element, ['entity_ids', 'entityIds', 'linked_entity_ids', 'linkedEntityIds'])
    || nestedRecordString(element, ['customData', 'data', 'metadata'], [
      'entity_id',
      'entityId',
      'architecture_entity_id',
      'architectureEntityId',
      'linked_entity_id',
      'linkedEntityId',
    ]);
}

function linkedInterfaceFromElement(element: Record<string, unknown>): string | undefined {
  return recordString(element, [
    'interface_id',
    'interfaceId',
    'architecture_interface_id',
    'architectureInterfaceId',
    'linked_interface_id',
    'linkedInterfaceId',
  ])
    || firstRecordArrayString(element, ['interface_ids', 'interfaceIds', 'linked_interface_ids', 'linkedInterfaceIds'])
    || nestedRecordString(element, ['customData', 'data', 'metadata'], [
      'interface_id',
      'interfaceId',
      'architecture_interface_id',
      'architectureInterfaceId',
      'linked_interface_id',
      'linkedInterfaceId',
    ]);
}

function boundElementId(element: Record<string, unknown>, keys: string[]): string | undefined {
  const direct = recordString(element, keys);
  if (direct) return direct;
  for (const key of keys) {
    const binding = element[key];
    if (binding && typeof binding === 'object' && !Array.isArray(binding)) {
      const bound = recordString(binding as Record<string, unknown>, ['elementId', 'element_id', 'id']);
      if (bound) return bound;
    }
  }
  return undefined;
}

function extractDiagramEdges(
  design: ArchitectureDesign,
  nodes: ArchitectureMermaidNode[],
  interfaces: ArchitectureInterface[],
): ArchitectureMermaidEdge[] {
  const entityLookup = buildEntityLookup(nodes);
  const interfaceByKey = new Map<string, ArchitectureInterface>();
  interfaces.forEach((itf, index) => {
    const key = architectureInterfaceKey(itf, index);
    if (itf.id) {
      interfaceByKey.set(itf.id, itf);
      interfaceByKey.set(itf.id.toLowerCase(), itf);
    }
    if (itf.name) {
      interfaceByKey.set(itf.name, itf);
      interfaceByKey.set(itf.name.toLowerCase(), itf);
    }
    interfaceByKey.set(key, itf);
    interfaceByKey.set(key.toLowerCase(), itf);
  });

  const edges: ArchitectureMermaidEdge[] = [];
  for (const diagram of sortArchitectureDiagrams(design.diagrams)) {
    const elementToEntityKey = new Map<string, string>();
    const elements = diagramPayloadElements(diagram.adapter_payload);
    for (const element of elements) {
      const elementId = cleanString(element.id);
      const entityKey = resolveArchitectureEntityKey(linkedEntityFromElement(element), entityLookup);
      if (elementId && entityKey) elementToEntityKey.set(elementId, entityKey);
    }

    elements.forEach((element, index) => {
      const sourceEntityRef = recordString(element, ['source_entity_id', 'sourceEntityId', 'from_entity_id', 'fromEntityId'])
        || nestedRecordString(element, ['customData', 'data', 'metadata'], ['source_entity_id', 'sourceEntityId', 'from_entity_id', 'fromEntityId']);
      const targetEntityRef = recordString(element, ['target_entity_id', 'targetEntityId', 'to_entity_id', 'toEntityId'])
        || nestedRecordString(element, ['customData', 'data', 'metadata'], ['target_entity_id', 'targetEntityId', 'to_entity_id', 'toEntityId']);
      const sourceElementId = boundElementId(element, ['source', 'source_id', 'sourceId', 'sourceElementId', 'start', 'startBinding', 'start_binding']);
      const targetElementId = boundElementId(element, ['target', 'target_id', 'targetId', 'targetElementId', 'end', 'endBinding', 'end_binding']);
      const sourceKey = resolveArchitectureEntityKey(sourceEntityRef, entityLookup)
        || (sourceElementId ? elementToEntityKey.get(sourceElementId) : undefined);
      const targetKey = resolveArchitectureEntityKey(targetEntityRef, entityLookup)
        || (targetElementId ? elementToEntityKey.get(targetElementId) : undefined);
      if (!sourceKey || !targetKey || sourceKey === targetKey) return;
      const interfaceRef = linkedInterfaceFromElement(element);
      const linkedInterface = interfaceRef ? interfaceByKey.get(interfaceRef) || interfaceByKey.get(interfaceRef.toLowerCase()) : undefined;
      const label = cleanString(linkedInterface?.name)
        || cleanString(recordString(element, ['label', 'text', 'name']))
        || cleanString(element.type)
        || `connection ${index + 1}`;
      edges.push({
        sourceKey,
        targetKey,
        label,
        sortKey: [
          diagram.order_index ?? Number.POSITIVE_INFINITY,
          diagram.id || diagram.title || '',
          index,
          sourceKey,
          targetKey,
          label,
        ].join(':'),
      });
    });
  }
  return edges;
}

function extractInterfaceFallbackEdges(
  interfaces: ArchitectureInterface[],
  nodes: ArchitectureMermaidNode[],
): ArchitectureMermaidEdge[] {
  const entityLookup = buildEntityLookup(nodes);
  const edges: ArchitectureMermaidEdge[] = [];
  interfaces.forEach((itf, index) => {
    const record = itf as unknown as Record<string, unknown>;
    const participants = Array.isArray(itf.participants)
      ? itf.participants.map((participant) => resolveArchitectureEntityKey(participant, entityLookup)).filter(Boolean) as string[]
      : [];
    const explicitSource = recordString(record, ['source_entity_id', 'sourceEntityId', 'from_entity_id', 'fromEntityId']);
    const explicitTarget = recordString(record, ['target_entity_id', 'targetEntityId', 'to_entity_id', 'toEntityId']);
    let sourceKey = resolveArchitectureEntityKey(explicitSource, entityLookup) || participants[0];
    let targetKey = resolveArchitectureEntityKey(explicitTarget, entityLookup) || participants[1];
    if (cleanString(itf.direction)?.toLowerCase() === 'target_to_source') {
      [sourceKey, targetKey] = [targetKey, sourceKey];
    }
    if (!sourceKey || !targetKey || sourceKey === targetKey) return;
    const key = architectureInterfaceKey(itf, index);
    edges.push({
      sourceKey,
      targetKey,
      label: cleanString(itf.name) || cleanString(itf.endpoint) || key,
      sortKey: `${String(index).padStart(5, '0')}:${architectureSortValue(key)}`,
    });
  });
  return edges;
}

function uniqueMermaidEdges(edges: ArchitectureMermaidEdge[]): ArchitectureMermaidEdge[] {
  const byKey = new Map<string, ArchitectureMermaidEdge>();
  for (const edge of edges) {
    byKey.set(`${edge.sourceKey}\u0000${edge.targetKey}\u0000${edge.label}`, edge);
  }
  return Array.from(byKey.values()).sort((left, right) => left.sortKey.localeCompare(right.sortKey));
}

function architectureWarning(
  design: ArchitectureDesign,
  code: string,
  message: string,
  impact: string,
): ExportWarningCandidate {
  return {
    kind: 'architecture_warning',
    severity: 'medium',
    origin: code,
    source_ref: cleanString(design.id) || cleanString(design.title),
    message,
    impact,
    code,
  };
}

function architectureSourceHash(design: ArchitectureDesign): string | undefined {
  const direct = cleanString((design as unknown as Record<string, unknown>).content_hash);
  if (direct) return direct;
  const hashes = sortArchitectureDiagrams(design.diagrams)
    .map((diagram) => cleanString(diagram.content_hash))
    .filter(Boolean) as string[];
  return hashes.length ? hashes.join('|') : undefined;
}

export function renderArchitectureMermaid(design: ArchitectureDesign): ArchitectureMermaidRenderResult {
  const diagrams = sortArchitectureDiagrams(design.diagrams);
  const diagramIds = diagrams.map((diagram, index) => cleanString(diagram.id) || cleanString(diagram.title) || `diagram:${index + 1}`);
  const sourceHash = architectureSourceHash(design);
  const baseMetadata = {
    diagramIds,
    ...(cleanString(design.id) ? { designId: cleanString(design.id) } : {}),
    ...(design.version != null ? { designVersion: Number(design.version) } : {}),
    ...(sourceHash ? { sourceHash } : {}),
  };
  const entities = sortArchitectureEntities(design.entities);
  if (!entities.length) {
    return {
      mermaid: '',
      warnings: [
        architectureWarning(
          design,
          'architecture_not_renderable',
          'Architecture Design has no entities to render as Mermaid.',
          'Markdown export omits the Mermaid block and retains complementary architecture metadata.',
        ),
      ],
      metadata: {
        renderedFrom: 'empty',
        ...baseMetadata,
      },
    };
  }

  const nodes = buildArchitectureMermaidNodes(entities);
  const interfaces = sortArchitectureInterfaces(design.interfaces);
  const diagramEdges = uniqueMermaidEdges(extractDiagramEdges(design, nodes, interfaces));
  const fallbackEdges = diagramEdges.length
    ? []
    : uniqueMermaidEdges(extractInterfaceFallbackEdges(interfaces, nodes));
  const edges = diagramEdges.length ? diagramEdges : fallbackEdges;
  const nodeByKey = new Map(nodes.map((node) => [node.key, node]));
  const lines = [
    'flowchart TD',
    ...nodes.map((node) => `  ${node.id}["${escapeMermaidLabel(node.label)}"]`),
    ...edges.map((edge) => {
      const source = nodeByKey.get(edge.sourceKey);
      const target = nodeByKey.get(edge.targetKey);
      if (!source || !target) return '';
      return `  ${source.id} -- "${escapeMermaidLabel(edge.label)}" --> ${target.id}`;
    }).filter(Boolean),
  ];
  const warnings = edges.length
    ? []
    : [
      architectureWarning(
        design,
        'relationships_not_reconstructable',
        'Architecture Design entities were rendered, but no deterministic relationship edges could be reconstructed.',
        'Markdown export emits a node-only Mermaid block and retains complementary interface/diagram metadata.',
      ),
    ];

  return {
    mermaid: `${lines.join('\n')}\n`,
    warnings,
    metadata: {
      renderedFrom: diagramEdges.length ? 'diagram_connections' : 'entity_interface_fallback',
      ...baseMetadata,
    },
  };
}

function renderArchitectureDesigns(
  designs: any[] | null | undefined,
  warningCollector?: ExportWarningCollector,
): string {
  if (!designs?.length) return '';
  const items = designs.map((design, i) => {
    let entry = `### ${i + 1}. ${design.title || 'Untitled architecture'}\n\n`;
    if (design.global_description) entry += `${design.global_description}\n\n`;
    if (design.parent_type) entry += `**Source:** ${design.parent_type}${design.source_title ? ` — ${design.source_title}` : ''}\n\n`;
    if (design.version != null) entry += `**Version:** v${design.version}\n\n`;
    const renderedMermaid = renderArchitectureMermaid(design as ArchitectureDesign);
    warningCollector?.addMany(renderedMermaid.warnings);
    if (renderedMermaid.mermaid) {
      entry += `#### Mermaid\n\n`;
      entry += `\`\`\`mermaid\n${renderedMermaid.mermaid}\`\`\`\n\n`;
    }
    entry += renderArchitectureMermaidWarnings(renderedMermaid.warnings);
    if (design.entities?.length) {
      entry += `#### Entities\n\n`;
      entry += design.entities.map((entity: any) => {
        const parts = [
          `- **${entity.name || entity.title || entity.id || 'Unnamed entity'}**`,
          entity.entity_type ? `type=${entity.entity_type}` : '',
          entity.responsibility ? `responsibility=${entity.responsibility}` : '',
          entity.boundaries ? `boundaries=${entity.boundaries}` : '',
          entity.technologies?.length ? `technologies=${entity.technologies.join(', ')}` : '',
        ].filter(Boolean);
        return parts.join(' — ');
      }).join('\n');
      entry += `\n\n`;
    }
    if (design.interfaces?.length) {
      entry += `#### Interfaces\n\n`;
      entry += design.interfaces.map((itf: any) => {
        const parts = [
          `- **${itf.name || itf.label || itf.id || 'Unnamed interface'}**`,
          itf.protocol ? `protocol=${itf.protocol}` : '',
          itf.endpoint ? `endpoint=${itf.endpoint}` : '',
          itf.description ? `description=${itf.description}` : '',
        ].filter(Boolean);
        return parts.join(' — ');
      }).join('\n');
      entry += `\n\n`;
    }
    if (design.diagrams_count != null) entry += `**Diagrams:** ${design.diagrams_count}\n\n`;
    if (design.diagrams?.length) {
      entry += `#### Diagrams\n\n`;
      entry += design.diagrams.map((diagram: any, diagramIndex: number) => {
        const sourceRef = `architecture_design:${design.id || design.title || i + 1}:diagram:${diagram.id || diagramIndex + 1}`;
        if (!hasDiagramVisualReference(diagram)) {
          warningCollector?.add({
            kind: 'asset_unavailable',
            severity: 'medium',
            origin: sourceRef,
            source_ref: diagram.id || diagram.title,
            message: 'Architecture diagram visual render is unavailable in the export context.',
            impact: 'Markdown includes diagram metadata and payload references only.',
          });
        }
        const parts = [
          `- **${diagram.title || diagram.id || 'Untitled diagram'}**`,
          diagram.diagram_type ? `type=${diagram.diagram_type}` : '',
          diagram.format ? `format=${diagram.format}` : '',
          diagram.preview_ref ? `preview=${diagram.preview_ref}` : '',
          diagram.adapter_payload_ref ? `payload_ref=${diagram.adapter_payload_ref}` : '',
          diagram.content_hash ? `hash=${diagram.content_hash}` : '',
        ].filter(Boolean);
        return parts.join(' — ');
      }).join('\n');
      entry += `\n\n`;
    }
    entry += renderArchitectureConnectivityWarnings(design, warningCollector);
    return entry;
  }).join('\n');
  return `## Architecture Designs\n\n${items}\n`;
}

// ---------------------------------------------------------------------------
// Q&A
// ---------------------------------------------------------------------------

function renderQA(items: { question: string; answer?: string | null; asked_by?: string | null; answered_by?: string | null }[]): string {
  if (!items?.length) return '';
  const entries = items.map((q, i) => {
    let entry = `### Q${i + 1}: ${q.question}\n\n`;
    if (q.asked_by) entry += `*Asked by: ${q.asked_by}*\n\n`;
    if (q.answer) {
      entry += `**A:** ${q.answer}\n\n`;
      if (q.answered_by) entry += `*Answered by: ${q.answered_by}*\n\n`;
    } else {
      entry += `*Unanswered*\n\n`;
    }
    return entry;
  }).join('');
  return `## Q&A\n\n${entries}`;
}

// ---------------------------------------------------------------------------
// Test Scenarios
// ---------------------------------------------------------------------------

function renderTestScenarios(
  scenarios: TestScenario[] | null | undefined,
  criteria?: string[] | null,
  warningCollector?: ExportWarningCollector,
): string {
  if (!scenarios?.length) return '';
  const items = scenarios.map((ts, i) => {
    let entry = `### ${i + 1}. ${ts.title}\n\n`;
    if (ts.scenario_type) entry += `**Type:** ${ts.scenario_type}\n\n`;
    entry += `- **Given:** ${ts.given}\n`;
    entry += `- **When:** ${ts.when}\n`;
    entry += `- **Then:** ${ts.then}\n\n`;
    if (ts.linked_criteria?.length && criteria?.length) {
      const resolvedCriteria = resolveLinkedCriteriaForExport(ts.linked_criteria, criteria);
      const names = resolvedCriteria.map((resolved) => {
        if (resolved.status === 'resolved') {
          return `  - ${criterionLabel(resolved.index || 0, resolved.text || readableValue(resolved.input))}`;
        }
        if (resolved.warning) warningCollector?.add(resolved.warning);
        return `  - Unresolved: ${readableValue(resolved.input)} (see Export Warnings)`;
      });
      entry += `**Linked criteria:**\n${names.join('\n')}\n\n`;
    } else if (ts.linked_criteria?.length) {
      const names = ts.linked_criteria.map((criterion) => `  - ${readableValue(criterion)}`);
      entry += `**Linked criteria:**\n${names.join('\n')}\n\n`;
    }
    if (ts.notes) entry += `**Notes:** ${ts.notes}\n\n`;
    return entry;
  }).join('');
  return `## Test Scenarios\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Technical Requirements
// ---------------------------------------------------------------------------

function renderTechnicalRequirements(requirements: (string | TechnicalRequirement)[] | null | undefined): string {
  if (!requirements?.length) return '';
  const items = requirements.map((item, i) => {
    if (typeof item === 'string') return `${i + 1}. ${item}`;
    let entry = `${i + 1}. ${item.text || readableValue(item)}`;
    const details = renderKeyValues([
      ['ID', item.id],
      ['Status', item.status],
      ['Linked task IDs', item.linked_task_ids?.join(', ')],
      ['Notes', item.notes],
    ]);
    if (details) entry += `\n${details.trimEnd()}`;
    return entry;
  }).join('\n');
  return `## Technical Requirements\n\n${items}\n\n`;
}

// ---------------------------------------------------------------------------
// Business Rules
// ---------------------------------------------------------------------------

function renderBusinessRules(rules: BusinessRule[] | null | undefined, frs?: string[] | null): string {
  if (!rules?.length) return '';
  const items = rules.map((br, i) => {
    let entry = `### ${i + 1}. ${br.title}\n\n`;
    entry += renderKeyValues([
      ['ID', br.id],
      ['Status', br.status],
    ]);
    if (br.rule) entry += `**Rule:** ${formatScalar(br.rule)}\n\n`;
    entry += renderKeyValues([
      ['When', br.when],
      ['Then', br.then],
      ['Linked task IDs', br.linked_task_ids?.join(', ')],
    ]);
    if (br.linked_requirements?.length && frs?.length) {
      const names = br.linked_requirements
        .map(r => frs.indexOf(r) >= 0 ? `FR${frs.indexOf(r) + 1}: ${r}` : r)
        .map(r => `  - ${r}`);
      entry += `**Linked requirements:**\n${names.join('\n')}\n\n`;
    }
    if (br.notes) entry += `**Notes:** ${br.notes}\n\n`;
    return entry;
  }).join('');
  return `## Business Rules\n\n${items}`;
}

// ---------------------------------------------------------------------------
// API Contracts
// ---------------------------------------------------------------------------

function renderApiContracts(contracts: ApiContract[] | null | undefined): string {
  if (!contracts?.length) return '';
  const items = contracts.map((ac, i) => {
    let entry = `### ${i + 1}. ${ac.method} ${ac.path}\n\n`;
    entry += `\`${ac.method} ${ac.path}\`\n\n`;
    entry += renderKeyValues([
      ['ID', ac.id],
      ['Status', ac.status],
    ]);
    if (ac.description) entry += `${ac.description}\n\n`;
    entry += renderJsonBlock('Request', ac.request_body);
    entry += renderJsonBlock('Response (success)', ac.response_success);
    entry += renderJsonBlock('Response (errors)', ac.response_errors);
    entry += renderKeyValues([
      ['Linked requirements', ac.linked_requirements?.join(', ')],
      ['Linked business rules', ac.linked_rules?.join(', ')],
      ['Linked task IDs', ac.linked_task_ids?.join(', ')],
      ['Notes', ac.notes],
    ]);
    return entry;
  }).join('');
  return `## API Contracts\n\n${items}`;
}

function renderIntegrationRequirements(requirements: IntegrationRequirement[] | null | undefined): string {
  if (!requirements?.length) return '';
  const items = requirements.map((item, i) => {
    let entry = `### ${i + 1}. ${item.title || item.id || 'Integration Requirement'}\n\n`;
    entry += renderKeyValues([
      ['ID', item.id],
      ['Status', item.status],
      ['Type', item.integration_type],
    ]);
    if (item.description) entry += `${item.description}\n\n`;
    entry += renderKeyValues([
      ['Provider', item.provider],
      ['Consumer', item.consumer],
      ['Endpoint/topic/procedure', item.endpoint ? `\`${item.endpoint}\`` : null],
      ['Method/action', item.method],
      ['Contract ref', item.contract_ref],
    ]);
    entry += renderJsonBlock('Data contract', item.data_contract);
    if (item.linked_requirements?.length) entry += `**Linked requirements:** ${item.linked_requirements.join(', ')}\n\n`;
    if (item.linked_api_contracts?.length) entry += `**Linked API contracts:** ${item.linked_api_contracts.join(', ')}\n\n`;
    if (item.linked_task_ids?.length) entry += `**Linked task IDs:** ${item.linked_task_ids.join(', ')}\n\n`;
    if (item.notes) entry += `**Notes:** ${item.notes}\n\n`;
    return entry;
  }).join('');
  return `## Integration Requirements\n\n${items}`;
}

function renderObservabilityRequirements(requirements: ObservabilityRequirement[] | null | undefined): string {
  if (!requirements?.length) return '';
  const items = requirements.map((item, i) => {
    let entry = `### ${i + 1}. ${item.title || item.id || 'Observability Requirement'}\n\n`;
    entry += renderKeyValues([
      ['ID', item.id],
      ['Status', item.status],
      ['Signal', item.signal_type],
    ]);
    if (item.description) entry += `${item.description}\n\n`;
    entry += renderKeyValues([
      ['Target', item.target],
      ['Metric/query/dashboard', item.metric_name],
      ['Threshold', item.threshold],
      ['Severity', item.severity],
      ['Owner', item.owner],
    ]);
    if (item.linked_requirements?.length) entry += `\n**Linked requirements:** ${item.linked_requirements.join(', ')}\n\n`;
    if (item.linked_integration_requirements?.length) entry += `**Linked integration requirements:** ${item.linked_integration_requirements.join(', ')}\n\n`;
    if (item.linked_task_ids?.length) entry += `**Linked task IDs:** ${item.linked_task_ids.join(', ')}\n\n`;
    if (item.notes) entry += `**Notes:** ${item.notes}\n\n`;
    return entry;
  }).join('');
  return `## Observability Requirements\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------

function renderDecisions(decisions: any[] | null | undefined): string {
  if (!decisions?.length) return '';
  const items = decisions.map((decision, i) => {
    let entry = `### ${i + 1}. ${decision.title || decision.id || 'Decision'}\n\n`;
    entry += renderKeyValues([
      ['ID', decision.id],
      ['Status', decision.status],
    ]);
    if (decision.context) entry += `**Context:** ${formatScalar(decision.context)}\n\n`;
    if (decision.rationale) entry += `${decision.rationale}\n\n`;
    if (decision.decision) entry += `${decision.decision}\n\n`;
    if (decision.alternatives?.length) entry += `**Alternatives:** ${decision.alternatives.join(', ')}\n\n`;
    if (decision.alternatives_considered?.length) entry += `**Alternatives considered:** ${decision.alternatives_considered.join(', ')}\n\n`;
    if (decision.supersedes_decision_id) entry += `**Supersedes:** ${decision.supersedes_decision_id}\n\n`;
    if (decision.linked_requirements?.length) entry += `**Linked requirements:** ${decision.linked_requirements.join(', ')}\n\n`;
    if (decision.linked_task_ids?.length) entry += `**Linked task IDs:** ${decision.linked_task_ids.join(', ')}\n\n`;
    if (decision.notes) entry += `**Notes:** ${decision.notes}\n\n`;
    return entry;
  }).join('');
  return `## Decisions\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Resolved References
// ---------------------------------------------------------------------------

function renderResolvedReferences(refs: any | null | undefined): string {
  if (!refs) return '';
  let md = `## Resolved References\n\n`;
  const renderResolvedList = (title: string, items: any[], label: (item: any) => string) => {
    if (!items?.length) return '';
    const rows = items.map((item) => {
      const source = item.reference_type ? ` (${item.reference_type}${item.source_title ? ` from ${item.source_title}` : ''})` : '';
      return `- ${label(item)}${source}`;
    }).join('\n');
    return `### ${title}\n\n${rows}\n\n`;
  };
  md += renderResolvedList('Knowledge Bases', refs.knowledge_bases || [], (item) => item.title || item.id);
  md += renderResolvedList('Mockups', refs.screen_mockups || [], (item) => item.title || item.id);
  md += renderResolvedList('Architecture Designs', refs.architecture_designs || [], (item) => item.title || item.id);
  md += renderResolvedList('Functional Requirements', refs.functional_requirements || [], (item) => item.text || item.value || item.id);
  md += renderResolvedList('Technical Requirements', refs.technical_requirements || [], (item) => item.text || item.value || item.id);
  md += renderResolvedList('Acceptance Criteria', refs.acceptance_criteria || [], (item) => item.text || item.value || item.id);
  md += renderResolvedList('Business Rules', refs.business_rules || [], (item) => item.title || item.rule || item.id);
  md += renderResolvedList('API Contracts', refs.api_contracts || [], (item) => `${item.method || ''} ${item.path || item.title || item.id}`.trim());
  md += renderResolvedList('Integration Requirements', refs.integration_requirements || [], (item) => item.title || item.id);
  md += renderResolvedList('Observability Requirements', refs.observability_requirements || [], (item) => item.title || item.id);
  md += renderResolvedList('Decisions', refs.decisions || [], (item) => item.title || item.id);
  return md === `## Resolved References\n\n` ? '' : md;
}

// ---------------------------------------------------------------------------
// Knowledge Bases
// ---------------------------------------------------------------------------

function renderKnowledgeBases(kbs: (SpecKnowledgeSummary | { title: string; content?: string; source_type?: string })[]): string {
  if (!kbs?.length) return '';
  const items = kbs.map((kb, i) => {
    let entry = `### ${i + 1}. ${kb.title}\n\n`;
    if ('source_type' in kb && kb.source_type) entry += `**Source:** ${kb.source_type}\n\n`;
    if ('content' in kb && kb.content) entry += `${kb.content}\n\n`;
    return entry;
  }).join('');
  return `## Knowledge Base\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Card-specific helpers
// ---------------------------------------------------------------------------

function cardTypeLabel(card: Card): string {
  if (card.card_type === 'bug') return 'Bug';
  if (card.card_type === 'test') return 'Test';
  return 'Task';
}

function renderCardRef(item: unknown): string {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return formatListValue(item);
  const record = item as Record<string, unknown>;
  const title = cleanString(record.title || record.name || record.id) || 'Card';
  const details = [
    cleanString(record.status) ? `status=${record.status}` : '',
    cleanString(record.id) && record.id !== title ? `id=${record.id}` : '',
  ].filter(Boolean);
  return details.length ? `${title} (${details.join(', ')})` : title;
}

function renderCardDependencies(card: Card): string {
  const record = card as unknown as Record<string, unknown>;
  const dependencyGroups: Array<[string, unknown]> = [
    ['Depends On', record.depends_on || record.dependencies],
    ['Dependents', record.dependents],
  ];
  let body = '';
  for (const [title, value] of dependencyGroups) {
    if (!Array.isArray(value) || value.length === 0) continue;
    body += `### ${title}\n\n${value.map((item) => `- ${renderCardRef(item)}`).join('\n')}\n\n`;
  }
  return body ? `## Dependencies\n\n${body}` : '';
}

function renderCardAttachments(card: Card): string {
  if (!card.attachments?.length) return '';
  const rows = card.attachments.map((attachment) => {
    const name = attachment.original_filename || attachment.filename || attachment.id;
    return `- **${name}** (${attachment.mime_type || 'unknown'}, ${attachment.size ?? 0} bytes, uploaded ${fmtDate(attachment.created_at)})`;
  }).join('\n');
  return `## Attachments\n\n${rows}\n\n`;
}

function renderTestCardDetails(card: Card): string {
  if (card.card_type !== 'test' && !card.test_scenario_ids?.length) return '';
  let body = `## Test Details\n\n`;
  if (card.test_scenario_ids?.length) {
    body += `**Linked test scenario IDs:**\n${card.test_scenario_ids.map((id) => `- ${id}`).join('\n')}\n\n`;
  } else {
    body += `No linked test scenarios.\n\n`;
  }
  return body;
}

// ---------------------------------------------------------------------------
// Entity Generators
// ---------------------------------------------------------------------------

/** Generate Markdown for a Story. */
export function exportStory(story: Story): string {
  let md = `# ${story.title}\n\n`;

  md += metaTable([
    ['Status', story.status],
    ['Topic', story.topic?.name || story.topic_id || ''],
    ['Actor', story.actor || ''],
    ['Goal', story.goal || ''],
    ['Benefit', story.benefit || ''],
    ['Created', fmtDate(story.created_at)],
    ['Updated', fmtDate(story.updated_at)],
    ['Labels', story.labels?.join(', ') || ''],
  ]);

  md += section('Description', story.description);

  if (story.ideation_links?.length) {
    const rows = story.ideation_links
      .map((link) => `- ${link.ideation_id}${link.created_at ? ` (linked ${fmtDate(link.created_at)})` : ''}`)
      .join('\n');
    md += `## Linked Ideation\n\n${rows}\n\n`;
  }

  md += renderMockups(story.screen_mockups);

  return md;
}

/** Generate Markdown for an Ideation. */
export function exportIdeation(ideation: Ideation): string {
  let md = `# ${ideation.title}\n\n`;

  md += metaTable([
    ['Status', ideation.status],
    ['Version', `v${ideation.version}`],
    ['Complexity', ideation.complexity || ''],
    ['Assignee', ideation.assignee_id || ''],
    ['Created', fmtDate(ideation.created_at)],
    ['Updated', fmtDate(ideation.updated_at)],
    ['Labels', ideation.labels?.join(', ') || ''],
  ]);

  md += section('Description', ideation.description);
  md += section('Problem Statement', ideation.problem_statement);
  md += section('Proposed Approach', ideation.proposed_approach);

  // Scope assessment
  if (ideation.scope_assessment) {
    const sa = ideation.scope_assessment as Record<string, unknown>;
    let table = '| Dimension | Score | Justification |\n|-----------|-------|---------------|\n';
    for (const dim of ['domains', 'ambiguity', 'dependencies']) {
      const score = sa[dim] ?? '-';
      const just = sa[`${dim}_justification`] ?? '';
      table += `| **${dim.charAt(0).toUpperCase() + dim.slice(1)}** | ${score}/5 | ${just} |\n`;
    }
    md += `## Scope Assessment\n\n${table}\n`;
  }

  md += renderMockups(ideation.screen_mockups);
  md += renderArchitectureDesigns(ideation.architecture_designs);
  md += renderQA(ideation.qa_items || []);

  return md;
}

/** Generate Markdown for a Refinement. */
export function exportRefinement(refinement: Refinement): string {
  let md = `# ${refinement.title}\n\n`;

  md += metaTable([
    ['Status', refinement.status],
    ['Version', `v${refinement.version}`],
    ['Assignee', refinement.assignee_id || ''],
    ['Created', fmtDate(refinement.created_at)],
    ['Updated', fmtDate(refinement.updated_at)],
    ['Labels', refinement.labels?.join(', ') || ''],
  ]);

  md += section('Description', refinement.description);

  if (refinement.in_scope?.length) {
    md += `## In Scope\n\n${bulletList(refinement.in_scope)}\n\n`;
  }
  if (refinement.out_of_scope?.length) {
    md += `## Out of Scope\n\n${bulletList(refinement.out_of_scope)}\n\n`;
  }

  md += section('Analysis', refinement.analysis);

  if (refinement.decisions?.length) {
    md += `## Decisions\n\n${numberedList(refinement.decisions)}\n\n`;
  }

  md += renderKnowledgeBases(refinement.knowledge_bases || []);
  md += renderMockups(refinement.screen_mockups);
  md += renderArchitectureDesigns(refinement.architecture_designs);
  md += renderQA(refinement.qa_items || []);

  return md;
}

/** Generate Markdown for a Sprint with parent spec context. */
export function exportSprint(sprint: any, parentSpec: any): string {
  let md = `# Sprint: ${sprint.title}\n\n`;

  md += metaTable([
    ['Status', sprint.status],
    ['Version', `v${sprint.version}`],
    ['Spec Version', `v${sprint.spec_version}`],
    ['Spec', parentSpec?.title || sprint.spec_id || 'N/A'],
    ...(sprint.start_date ? [['Start Date', sprint.start_date] as [string, string]] : []),
    ...(sprint.end_date ? [['End Date', sprint.end_date] as [string, string]] : []),
  ]);

  if (sprint.objective) md += `## Objective\n\n${sprint.objective}\n\n`;
  if (sprint.expected_outcome) md += `## Expected Outcome\n\n${sprint.expected_outcome}\n\n`;
  if (sprint.description) md += `## Description\n\n${sprint.description}\n\n`;

  // Progress
  const cards = sprint.cards || [];
  const done = cards.filter((c: any) => c.status === 'done').length;
  const total = cards.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  md += `## Progress\n\n**${pct}%** complete (${done}/${total} cards done)\n\n`;

  // Cards
  if (cards.length > 0) {
    md += `## Cards\n\n| Title | Status | Type |\n|-------|--------|------|\n`;
    for (const c of cards) {
      md += `| ${c.title} | ${c.status} | ${c.card_type || 'normal'} |\n`;
    }
    md += '\n';
  }

  // Scoped Test Scenarios
  if (sprint.test_scenario_ids?.length && parentSpec?.test_scenarios?.length) {
    const scoped = parentSpec.test_scenarios.filter((ts: any) =>
      sprint.test_scenario_ids.includes(ts.id) ||
      ts.linked_task_ids?.some((id: string) => cards.some((c: any) => c.id === id))
    );
    if (scoped.length > 0) {
      md += `## Scoped Test Scenarios\n\n`;
      for (const ts of scoped) {
        md += `### ${ts.title}\n- **Given:** ${ts.given}\n- **When:** ${ts.when}\n- **Then:** ${ts.then}\n- **Status:** ${ts.status}\n\n`;
      }
    }
  }

  // Scoped Business Rules
  if (sprint.business_rule_ids?.length && parentSpec?.business_rules?.length) {
    const scoped = parentSpec.business_rules.filter((br: any) =>
      sprint.business_rule_ids.includes(br.id) ||
      br.linked_task_ids?.some((id: string) => cards.some((c: any) => c.id === id))
    );
    if (scoped.length > 0) {
      md += `## Scoped Business Rules\n\n`;
      for (const br of scoped) {
        md += `### ${br.title}\n- **When:** ${br.when}\n- **Then:** ${br.then}\n\n`;
      }
    }
  }

  // Evaluations
  if (sprint.evaluations?.length) {
    md += `## Evaluations\n\n`;
    for (const ev of sprint.evaluations) {
      md += `### ${ev.evaluator_name} — ${ev.recommendation} (${ev.overall_score}/100)\n`;
      if (ev.overall_justification) md += `${ev.overall_justification}\n`;
      md += '\n';
    }
  }

  // Labels
  if (sprint.labels?.length) {
    md += `## Labels\n\n${sprint.labels.join(', ')}\n\n`;
  }

  return md;
}

/** Generate Markdown for a Spec. */
export function exportSpec(spec: Spec): string {
  const warningCollector = createExportWarningCollector();
  let md = `# ${spec.title}\n\n`;

  md += metaTable([
    ['Status', spec.status],
    ['Version', `v${spec.version}`],
    ['Assignee', spec.assignee_id || ''],
    ['Created', fmtDate(spec.created_at)],
    ['Updated', fmtDate(spec.updated_at)],
    ['Labels', spec.labels?.join(', ') || ''],
  ]);

  md += section('Description', spec.description);
  md += section('Context', spec.context);

  let body = '';

  if (spec.functional_requirements?.length) {
    body += `## Functional Requirements\n\n${numberedList(spec.functional_requirements)}\n\n`;
  }
  body += renderTechnicalRequirements(spec.technical_requirements);
  if (spec.acceptance_criteria?.length) {
    body += `## Acceptance Criteria\n\n${numberedList(spec.acceptance_criteria)}\n\n`;
  }

  body += renderResolvedReferences((spec as any).resolved_references);
  body += renderTestScenarios(spec.test_scenarios, spec.acceptance_criteria, warningCollector);
  body += renderBusinessRules(spec.business_rules, spec.functional_requirements);
  body += renderApiContracts(spec.api_contracts);
  body += renderIntegrationRequirements(spec.integration_requirements);
  body += renderObservabilityRequirements(spec.observability_requirements);
  body += renderDecisions(spec.decisions);
  body += renderKnowledgeBases(spec.knowledge_bases || []);
  body += renderMockups(spec.screen_mockups, warningCollector);
  body += renderArchitectureDesigns(spec.architecture_designs, warningCollector);
  body += renderQA(spec.qa_items || []);

  md += renderExportWarnings(warningCollector.toArray());
  md += body;

  return md;
}

/** Generate Markdown for a Card/Task, resolving spec references. */
export function exportCard(card: Card, spec?: Spec | null): string {
  const warningCollector = createExportWarningCollector();
  const isBug = card.card_type === 'bug';
  const typeLabel = cardTypeLabel(card);
  let md = `# ${isBug ? '[BUG] ' : ''}${card.title}\n\n`;

  md += metaTable([
    ['Status', card.status],
    ['Priority', card.priority !== 'none' ? card.priority : ''],
    ['Type', typeLabel],
    ['Assignee', card.assignee_id || ''],
    ['Created', fmtDate(card.created_at)],
    ['Updated', fmtDate(card.updated_at)],
    ['Due date', card.due_date ? fmtDate(card.due_date) : ''],
    ['Labels', card.labels?.join(', ') || ''],
  ]);

  md += section('Description', card.description);
  md += section('Details', card.details);

  let body = '';

  body += renderCardDependencies(card);
  body += renderTestCardDetails(card);

  // Bug-specific fields
  if (isBug) {
    let bugSection = `## Bug Details\n\n`;
    bugSection += `**Severity:** ${card.severity || 'unknown'}\n\n`;
    if (card.origin_task_id) bugSection += `**Origin task:** ${card.origin_task_id}\n\n`;
    if (card.expected_behavior) bugSection += `### Expected Behavior\n\n${card.expected_behavior}\n\n`;
    if (card.observed_behavior) bugSection += `### Observed Behavior\n\n${card.observed_behavior}\n\n`;
    if (card.steps_to_reproduce) bugSection += `### Steps to Reproduce\n\n${card.steps_to_reproduce}\n\n`;
    if (card.action_plan) bugSection += `### Action Plan\n\n${card.action_plan}\n\n`;
    if (card.linked_test_task_ids?.length) {
      bugSection += `**Linked test tasks:** ${card.linked_test_task_ids.join(', ')}\n\n`;
    }
    body += bugSection;
  }

  // Conclusions
  if (card.conclusions?.length) {
    const entries = card.conclusions.map((c: ConclusionEntry, i: number) => {
      let e = `### Conclusion ${i + 1}\n\n${c.text}\n\n`;
      if (c.author_id) e += `*Author: ${c.author_id} | ${fmtDate(c.created_at)}*\n\n`;
      if (c.completeness != null) e += `**Completeness:** ${c.completeness}%${c.completeness_justification ? ` — ${c.completeness_justification}` : ''}\n\n`;
      if (c.drift != null) e += `**Drift:** ${c.drift}%${c.drift_justification ? ` — ${c.drift_justification}` : ''}\n\n`;
      if (c.source) e += `**Source:** ${c.source}\n\n`;
      if (c.validation_id) e += `**Validation ID:** ${c.validation_id}\n\n`;
      return e;
    }).join('');
    body += `## Conclusions\n\n${entries}`;
  }

  // Validations
  if (card.validations?.length) {
    const entries = card.validations.map((v: ValidationEntry, i: number) => {
      let e = `### Validation ${i + 1} — ${v.verdict === 'pass' ? 'PASSED' : 'FAILED'}\n\n`;
      e += `| Metric | Score |\n|--------|-------|\n`;
      e += `| Confidence | ${v.confidence} |\n`;
      e += `| Completeness | ${v.completeness} |\n`;
      e += `| Drift | ${v.drift} |\n\n`;
      if (v.summary) e += `**Summary:** ${v.summary}\n\n`;
      e += `*Reviewer: ${v.evaluator_id} | ${fmtDate(v.created_at)}*\n\n`;
      return e;
    }).join('');
    body += `## Validations\n\n${entries}`;
  }

  // Dependencies
  // Note: Card type doesn't include dependency data directly; we show what we have
  // from comments/QA context

  const explicitResolvedRefs = (card as any).resolved_references;
  body += renderResolvedReferences(explicitResolvedRefs);

  // Resolved spec context
  if (spec && !explicitResolvedRefs) {
    body += `---\n\n## Spec Context: ${spec.title}\n\n`;

    // Linked test scenarios (resolve from IDs)
    let linkedScenarios: TestScenario[] = [];
    if (card.test_scenario_ids?.length && spec.test_scenarios?.length) {
      linkedScenarios = spec.test_scenarios.filter(ts => card.test_scenario_ids!.includes(ts.id));
      if (linkedScenarios.length) {
        body += renderTestScenarios(linkedScenarios, spec.acceptance_criteria, warningCollector);
      }
      const foundIds = new Set(linkedScenarios.map((ts) => ts.id));
      for (const scenarioId of card.test_scenario_ids) {
        if (!foundIds.has(scenarioId)) {
          warningCollector.add({
            kind: 'broken_link',
            severity: 'medium',
            origin: `card:${card.id}:test_scenario_ids`,
            source_ref: scenarioId,
            message: `Linked test scenario could not be resolved: ${scenarioId}`,
          });
        }
      }
    } else if (card.test_scenario_ids?.length) {
      for (const scenarioId of card.test_scenario_ids) {
        warningCollector.add({
          kind: 'unresolved_reference',
          severity: 'medium',
          origin: `card:${card.id}:test_scenario_ids`,
          source_ref: scenarioId,
          message: `Linked test scenario was exported as raw id because parent spec context is unavailable: ${scenarioId}`,
        });
      }
    }

    if (spec.functional_requirements?.length) {
      body += `## Functional Requirements\n\n${numberedList(spec.functional_requirements)}\n\n`;
    }
    body += renderTechnicalRequirements(spec.technical_requirements);
    if (spec.acceptance_criteria?.length) {
      body += `## Acceptance Criteria\n\n${numberedList(spec.acceptance_criteria)}\n\n`;
    }

    const linkedBusinessRules = (spec.business_rules || []).filter((item: any) => item.linked_task_ids?.includes(card.id));
    const linkedContracts = (spec.api_contracts || []).filter((item: any) => item.linked_task_ids?.includes(card.id));
    const linkedIRs = (spec.integration_requirements || []).filter((item: any) => item.linked_task_ids?.includes(card.id));
    const linkedORs = (spec.observability_requirements || []).filter((item: any) => item.linked_task_ids?.includes(card.id));
    const linkedDecisions = (spec.decisions || []).filter((item: any) => item.linked_task_ids?.includes(card.id));
    body += renderBusinessRules(
      linkedBusinessRules.length ? linkedBusinessRules : spec.business_rules,
      spec.functional_requirements
    );
    body += renderApiContracts(linkedContracts.length ? linkedContracts : spec.api_contracts);
    body += renderIntegrationRequirements(linkedIRs.length ? linkedIRs : spec.integration_requirements);
    body += renderObservabilityRequirements(linkedORs.length ? linkedORs : spec.observability_requirements);
    body += renderDecisions(linkedDecisions.length ? linkedDecisions : spec.decisions);
    body += renderKnowledgeBases(spec.knowledge_bases || []);
    body += renderMockups(spec.screen_mockups, warningCollector);
    body += renderArchitectureDesigns(spec.architecture_designs, warningCollector);
  }

  // Card-own knowledge bases
  if (card.knowledge_bases?.length) {
    body += `## Card Knowledge Bases\n\n`;
    for (const kb of card.knowledge_bases) {
      body += `### ${kb.title}${kb.source === 'spec' ? ' (from spec)' : ''}\n\n`;
      body += `${kb.content}\n\n`;
    }
  }

  body += renderMockups(card.screen_mockups, warningCollector);
  body += renderArchitectureDesigns(card.architecture_designs, warningCollector);
  body += renderCardAttachments(card);
  body += renderQA(card.qa_items || []);

  // Comments
  if (card.comments?.length) {
    const entries = card.comments.map(c =>
      `**${c.author_id || 'Unknown'}** (${fmtDate(c.created_at)}):\n\n${c.content}\n`
    ).join('\n---\n\n');
    body += `## Comments\n\n${entries}\n`;
  }

  md += renderExportWarnings(warningCollector.toArray());
  md += body;

  return md;
}
