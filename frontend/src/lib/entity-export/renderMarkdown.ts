import type {
  ExportReportBlock,
  ExportReportDocument,
  ExportReportSection,
} from './reportDocument';

const MARKDOWN_INLINE_CHARACTERS = new Set('`*_{}[]<>#+.!|');

function inline(value: unknown): string {
  return String(value ?? '')
    .replace(/\\/g, '\\\\')
    .split('')
    .map((character) => MARKDOWN_INLINE_CHARACTERS.has(character) ? `\\${character}` : character)
    .join('')
    .replace(/\r?\n/g, '<br>');
}

function tableCell(value: unknown): string {
  return inline(value);
}

function prose(value: string): string {
  return value.split(/\r?\n/).map((line) => inline(line)).join('<br>');
}

function codeFence(value: string): string {
  const runs = value.match(/`+/g) ?? [];
  const longest = runs.reduce((max, run) => Math.max(max, run.length), 0);
  return '`'.repeat(Math.max(3, longest + 1));
}

function renderBlock(block: ExportReportBlock, depth = 0): string {
  switch (block.kind) {
    case 'paragraph':
      return `${prose(block.text)}\n\n`;
    case 'key_values':
      return `${block.items.map((item) => `- **${inline(item.key)}:** ${inline(item.value)}`).join('\n')}\n\n`;
    case 'list':
      return `${block.items.map((item, index) => `${block.ordered ? `${index + 1}.` : '-'} ${inline(item)}`).join('\n')}\n\n`;
    case 'table': {
      if (block.columns.length === 0) return '';
      const caption = block.caption ? `*${inline(block.caption)}*\n\n` : '';
      const header = `| ${block.columns.map(tableCell).join(' | ')} |`;
      const divider = `| ${block.columns.map(() => '---').join(' | ')} |`;
      const rows = block.rows.map((row) => (
        `| ${block.columns.map((_, index) => tableCell(row[index] ?? '')).join(' | ')} |`
      )).join('\n');
      return `${caption}${header}\n${divider}${rows ? `\n${rows}` : ''}\n\n`;
    }
    case 'metrics':
      return `${block.metrics.map((metric) => {
        const threshold = metric.threshold == null
          ? ''
          : ` · ${metric.direction === 'lower' ? 'maximum' : 'minimum'} ${metric.threshold}`;
        const justification = metric.justification
          ? `\n  - Justification: ${inline(metric.justification)}`
          : '';
        return `- **${inline(metric.label)}:** ${metric.value}/${metric.maximum}${threshold}${justification}`;
      }).join('\n')}\n\n`;
    case 'pinpoints':
      return `${block.items.map((item) => {
        const identity = item.target_id ? ` (${inline(item.target_id)})` : '';
        return `- **${inline(item.metric)}** — ${inline(item.target)}${identity}\n  - ${inline(item.detail)}`;
      }).join('\n')}\n\n`;
    case 'code': {
      const fence = codeFence(block.value);
      const language = String(block.language || '').replace(/[^A-Za-z0-9_-]/g, '');
      return `${fence}${language}\n${block.value}\n${fence}\n\n`;
    }
    case 'details': {
      const heading = '#'.repeat(Math.min(6, 4 + depth));
      return `${heading} ${inline(block.summary)}\n\n${block.blocks.map((item) => renderBlock(item, depth + 1)).join('')}`;
    }
  }
}

function renderSection(section: ExportReportSection): string {
  const status = section.state ? ` · ${inline(section.state)}` : '';
  const summary = section.summary ? `${prose(section.summary)}\n\n` : '';
  return `## ${inline(section.title)}${status}\n\n${summary}${section.blocks.map((block) => renderBlock(block)).join('')}`;
}

export function renderReportMarkdown(document: ExportReportDocument): string {
  const metadata = [
    document.subtitle ? `> ${inline(document.subtitle)}` : '',
    document.generated_at ? `> Generated: ${inline(document.generated_at)}` : '',
    document.snapshot_fingerprint
      ? `> Snapshot: \`${inline(document.snapshot_fingerprint)}\``
      : '',
  ].filter(Boolean).join('\n');
  const toc = document.sections.map((section) => `- ${inline(section.title)}`).join('\n');
  return [
    `# ${inline(document.title)}`,
    metadata,
    `## Contents\n\n${toc}`,
    document.sections.map(renderSection).join(''),
  ].filter(Boolean).join('\n\n').trimEnd() + '\n';
}
