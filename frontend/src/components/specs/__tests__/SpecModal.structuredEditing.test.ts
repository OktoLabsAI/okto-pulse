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

  it('routes report export through the shared server-rendered dialog', () => {
    expect(source).toContain("import { EntityExportButton } from '@/components/export'");
    expect(source).toContain('<EntityExportButton');
    expect(source).toContain('entityType="spec"');
    expect(source).not.toContain('downloadMarkdown(');
    expect(source).not.toContain('exportSpec(');
  });

  it('drives status actions from allowed_transitions instead of a local status flow map', () => {
    const loadBlock = sourceBlock(
      'const loadAllowedTransitions = useCallback',
      'const loadSpec = async',
    );
    const statusFlowBlock = sourceBlock('{/* Status flow */}', '{/* Provenance breadcrumb */}');

    expect(loadBlock).toContain('api.getAllowedTransitions');
    expect(loadBlock).toContain("entity_type: 'spec'");
    expect(statusFlowBlock).toContain('statusFlowStatuses');
    expect(statusFlowBlock).toContain('handleMoveSpec(status)');
    expect(source).not.toContain('const getNextStatuses');
    expect(source).not.toContain('Record<SpecStatus, SpecStatus[]>');
  });
});
