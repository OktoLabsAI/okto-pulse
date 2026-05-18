import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const srcRoot = path.resolve(currentDir, '../../..');

function readSrc(relativePath: string): string {
  return readFileSync(path.join(srcRoot, relativePath), 'utf8');
}

describe('guided help anchors', () => {
  it('declares stable data-tour-id anchors on the MVP surfaces', () => {
    const anchors: Array<[string, string]> = [
      ['board.tabs', 'App.tsx'],
      ['board.refresh', 'components/layout/Header.tsx'],
      ['specs.resources.tabs', 'components/specs/SpecModal.tsx'],
      ['tasks.validation.column', 'components/kanban/KanbanColumn.tsx'],
      ['kg.discovery.search', 'components/knowledge/GlobalSearchView.tsx'],
      ['metrics.overview.summary', 'components/analytics/OverviewDashboard.tsx'],
      ['agents.modal.entry', 'components/layout/Header.tsx'],
      ['help.guided_tours', 'components/layout/Header.tsx'],
    ];

    for (const [anchor, file] of anchors) {
      const source = readSrc(file);
      expect(source).toContain('data-tour-id');
      expect(source).toContain(anchor);
    }
  });

  it('keeps the tour registry independent from test selectors', () => {
    const registry = readSrc('components/guided-help/registry.ts');

    expect(registry).not.toContain('data-testid');
    expect(registry).toContain("anchor: 'board.tabs'");
    expect(registry).toContain("anchor: 'tasks.validation.column'");
  });
});
