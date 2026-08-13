import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const MODALS = [
  ['stories/StoryModal.tsx', 'story'],
  ['ideations/IdeationModal.tsx', 'ideation'],
  ['refinements/RefinementModal.tsx', 'refinement'],
  ['specs/SpecModal.tsx', 'spec'],
  ['sprints/SprintModal.tsx', 'sprint'],
  ['kanban/CardModal.tsx', 'card'],
] as const;

describe('entity report export wiring', () => {
  it.each(MODALS)('%s uses the shared server-rendered report action', (relativePath, entityType) => {
    const source = readFileSync(join(process.cwd(), 'src/components', relativePath), 'utf8');

    expect(source).toContain("import { EntityExportButton } from '@/components/export'");
    expect(source).toContain('<EntityExportButton');
    expect(source).toContain(`entityType="${entityType}"`);
    expect(source).not.toContain("from '@/lib/exportMarkdown'");
    expect(source).not.toContain('downloadMarkdown(');
  });
});
