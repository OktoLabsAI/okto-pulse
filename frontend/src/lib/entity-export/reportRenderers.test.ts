import { describe, expect, it } from 'vitest';

import {
  reportSemanticKeys,
  type ExportReportDocument,
} from './reportDocument';
import { renderReportHtml } from './renderHtml';
import { renderReportMarkdown } from './renderMarkdown';
import { assertPassiveStandaloneHtml } from './security';

const document: ExportReportDocument = {
  schema_version: 'export-report-document/v1',
  title: 'Export <script>alert(1)</script> | report',
  subtitle: 'Same facts, two formats',
  snapshot_fingerprint: 'sha256:abc',
  sections: [{
    id: 'validation',
    title: 'Validation | current',
    state: 'included',
    blocks: [
      {
        kind: 'metrics',
        metrics: [{
          key: 'clarity',
          label: 'Clarity <img src=x onerror=alert(1)>',
          value: 92,
          maximum: 100,
          threshold: 80,
          justification: 'Readable | actionable',
        }],
      },
      {
        kind: 'pinpoints',
        items: [{
          metric: 'decidability',
          target: 'AC-1: must scale',
          target_id: 'ac_1',
          detail: '# must not become a heading',
        }],
      },
      {
        kind: 'table',
        columns: ['Section', 'Reason'],
        rows: [['Policy | compliance', 'line 1\nline 2']],
      },
      {
        kind: 'code',
        language: 'json',
        value: '{"literal":"```"}',
      },
    ],
  }],
};

describe('canonical export report renderers', () => {
  it('renders passive, standalone HTML and escapes all report content', () => {
    const html = renderReportHtml(document);
    expect(() => assertPassiveStandaloneHtml(html)).not.toThrow();
    expect(html).toContain('Content-Security-Policy');
    expect(html).not.toMatch(/<script\b/i);
    expect(html).not.toContain('<img');
    expect(html).not.toMatch(/https?:\/\//i);
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('data-semantic-key="section:validation"');
    expect(html).toContain('<svg');
    expect(html).toContain('@media print');
  });

  it('hardens Markdown tables, headings and dynamic code fences', () => {
    const markdown = renderReportMarkdown(document);
    expect(markdown).toContain('Validation \\| current');
    expect(markdown).toContain('Policy \\| compliance');
    expect(markdown).toContain('line 1<br>line 2');
    expect(markdown).toContain('\\# must not become a heading');
    expect(markdown).toContain('````json');
  });

  it('provides one semantic identity for both renderers', () => {
    expect(reportSemanticKeys(document)).toEqual([
      'section:validation',
      'block:validation:0:metrics',
      'metric:clarity',
      'block:validation:1:pinpoints',
      'pinpoint:decidability:ac_1',
      'block:validation:2:table',
      'block:validation:3:code',
    ]);
  });

  it.each([
    '<script>alert(1)</script>',
    '<iframe sandbox src="data:text/html;base64,PGgxPk1vY2t1cDwvaDE+"></iframe>',
    '<img src="https://example.invalid/a.png">',
    '<div onclick="alert(1)">x</div>',
    '<style>@import "https://example.invalid/a.css"</style>',
  ])('blocks active server HTML: %s', (html) => {
    expect(() => assertPassiveStandaloneHtml(html)).toThrow(/blocked/);
  });
});
