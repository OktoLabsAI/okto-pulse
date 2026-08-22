import type {
  EntityExportManifestSection,
  EntityExportPreflight,
} from '@/types/entity-export';

export interface ExportReportMetric {
  key: string;
  label: string;
  value: number;
  maximum: number;
  direction?: 'higher' | 'lower';
  threshold?: number | null;
  justification?: string | null;
}

export interface ExportReportPinpoint {
  metric: string;
  target: string;
  target_id?: string | null;
  detail: string;
}

export type ExportReportBlock =
  | { kind: 'paragraph'; text: string }
  | { kind: 'key_values'; items: Array<{ key: string; value: string }> }
  | { kind: 'list'; ordered?: boolean; items: string[] }
  | { kind: 'table'; caption?: string; columns: string[]; rows: string[][] }
  | { kind: 'metrics'; metrics: ExportReportMetric[] }
  | { kind: 'pinpoints'; items: ExportReportPinpoint[] }
  | { kind: 'code'; language?: string; value: string }
  | { kind: 'details'; summary: string; open?: boolean; blocks: ExportReportBlock[] };

export interface ExportReportSection {
  id: string;
  title: string;
  summary?: string | null;
  state?: EntityExportManifestSection['state'];
  blocks: ExportReportBlock[];
}

/** Format-neutral projection consumed by every report renderer and preview. */
export interface ExportReportDocument {
  schema_version: 'export-report-document/v1';
  title: string;
  subtitle?: string | null;
  generated_at?: string | null;
  snapshot_fingerprint?: string | null;
  sections: ExportReportSection[];
}

function humanLabel(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function preflightLifecycle(preflight: EntityExportPreflight): string {
  return [
    preflight.identity.edition == null ? null : `Edition ${preflight.identity.edition}`,
    preflight.identity.version == null ? null : `Revision ${preflight.identity.version}`,
  ].filter(Boolean).join(' / ') || 'Not reported';
}

export function buildPreflightReportDocument(
  preflight: EntityExportPreflight,
  selectedSectionKeys: ReadonlySet<string>,
): ExportReportDocument {
  const visible = preflight.sections.filter(
    (section) => selectedSectionKeys.has(section.section_key),
  );
  const includedCount = visible.filter((item) => item.state === 'included').length;
  const resolvedCount = visible.filter(
    (item) => !['unavailable', 'error'].includes(item.state),
  ).length;
  const percentage = visible.length > 0
    ? Math.round((resolvedCount / visible.length) * 100)
    : 0;

  return {
    schema_version: 'export-report-document/v1',
    title: preflight.identity.title,
    subtitle: [
      `${humanLabel(preflight.identity.entity_type)} report`,
      preflight.scope === 'complete' ? 'Current and available history' : 'Current lifecycle edition',
      preflight.identity.status ? humanLabel(preflight.identity.status) : null,
      preflightLifecycle(preflight),
    ].filter(Boolean).join(' / '),
    sections: [
      {
        id: 'report-summary',
        title: 'Report summary',
        state: preflight.complete_for_actor ? 'included' : 'error',
        blocks: [
          {
            kind: 'key_values',
            items: [
              { key: 'Subject', value: humanLabel(preflight.identity.entity_type) },
              { key: 'Status', value: preflight.identity.status ? humanLabel(preflight.identity.status) : 'Not reported' },
              { key: 'Scope', value: preflight.scope === 'complete' ? 'Current and available history' : 'Current lifecycle edition' },
              { key: 'Lifecycle', value: preflightLifecycle(preflight) },
            ],
          },
          {
            kind: 'metrics',
            metrics: [
              {
                key: 'resolved-sections',
                label: 'Resolved sections',
                value: percentage,
                maximum: 100,
                direction: 'higher',
                justification: `${resolvedCount} of ${visible.length} selected sections are resolved; ${includedCount} contain records.`,
              },
            ],
          },
        ],
      },
      {
        id: 'content-manifest',
        title: 'Content manifest',
        summary: preflight.source_complete
          ? 'All report sources are visible to the current actor.'
          : 'The report is actor-limited; permission omissions remain explicit.',
        blocks: [
          {
            kind: 'table',
            caption: 'Selected report sections and their authoritative availability',
            columns: ['Section', 'State', 'Records', 'Reason'],
            rows: visible.map((section) => [
              section.label,
              section.state,
              section.total_count == null ? 'Not reported' : String(section.total_count),
              section.message || section.reason_code || 'No issue reported',
            ]),
          },
        ],
      },
      ...visible.map<ExportReportSection>((section) => ({
        id: `section-${section.section_key}`,
        title: section.label,
        state: section.state,
        summary: section.message || section.reason_code || null,
        blocks: [{
          kind: 'paragraph',
          text: section.state === 'included'
            ? `${section.total_count ?? 'Available'} record(s) are available in the generated report.`
            : `This section will be represented as ${section.state}; the report will not silently omit it.`,
        }],
      })),
    ],
  };
}

/** Stable semantic identity used by Markdown/HTML parity tests. */
export function reportSemanticKeys(document: ExportReportDocument): string[] {
  const keys: string[] = [];
  for (const section of document.sections) {
    keys.push(`section:${section.id}`);
    section.blocks.forEach((block, blockIndex) => {
      keys.push(`block:${section.id}:${blockIndex}:${block.kind}`);
      if (block.kind === 'metrics') {
        block.metrics.forEach((metric) => keys.push(`metric:${metric.key}`));
      }
      if (block.kind === 'pinpoints') {
        block.items.forEach((item, itemIndex) => keys.push(
          `pinpoint:${item.metric}:${item.target_id || itemIndex}`,
        ));
      }
    });
  }
  return keys;
}
