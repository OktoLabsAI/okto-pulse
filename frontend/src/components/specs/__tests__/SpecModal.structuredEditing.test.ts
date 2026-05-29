import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const source = readFileSync(join(process.cwd(), 'src/components/specs/SpecModal.tsx'), 'utf8');

function sourceBlock(startMarker: string, endMarker: string) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return source.slice(start, end);
}

describe('SpecModal structured entity editing', () => {
  it('routes object collection edits through atomic structured entity calls', () => {
    const block = sourceBlock('const syncStructuredCollection = async', 'const boardSettings =');

    expect(block).toContain('api.createSpecEntity');
    expect(block).toContain('api.updateSpecEntity');
    expect(block).toContain('api.operateSpecEntity');
    expect(block).toContain('applyImpactAwareOperation');
    expect(block).toContain('reloadSpecAfterStructuredEdit');
    expect(block).not.toContain('api.updateSpec(');
  });

  it('uses impact preview and acknowledgement for destructive structured operations', () => {
    const block = sourceBlock('const applyImpactAwareOperation = async', 'const syncTextEntityList = async');

    expect(block).toContain('api.previewSpecEntityImpact');
    expect(block).toContain('ack_token: ackToken');
    expect(block).toContain("result.error_code === 'impact_ack_required'");
    expect(block).toContain('api.operateSpecEntity');
    expect(block).not.toContain('api.updateSpec(');
  });

  it('routes Markdown download through sanitized filename helper without write APIs', () => {
    const block = sourceBlock('const md = exportSpec', "} catch {");

    expect(block).toContain('exportSpec');
    expect(block).toContain('downloadMarkdown(md, markdownFilenameForSpec(spec))');
    expect(block).not.toContain('api.updateSpec(');
    expect(block).not.toContain('api.createSpecEntity');
    expect(block).not.toContain('api.updateSpecEntity');
    expect(block).not.toContain('api.operateSpecEntity');
  });
});
