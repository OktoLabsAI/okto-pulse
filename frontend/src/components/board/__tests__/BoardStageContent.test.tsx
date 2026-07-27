import { render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { BoardStageContent, type StageTabId } from '../BoardStageContent';

const probe = vi.hoisted(() => ({ nextMount: 0 }));

interface ProbeProps {
  boardId: string;
  refreshKey?: number;
}

function PanelProbe({
  boardId,
  refreshKey,
  name,
}: ProbeProps & { name: StageTabId }) {
  const [mountId] = useState(() => ++probe.nextMount);
  return (
    <div data-testid={`${name}-probe`}>
      {mountId}:{boardId}:{refreshKey ?? 'none'}
    </div>
  );
}

vi.mock('@/components/stories', () => ({
  StoriesPanel: (props: ProbeProps) => <PanelProbe {...props} name="stories" />,
}));
vi.mock('@/components/ideations', () => ({
  IdeationsPanel: (props: ProbeProps) => <PanelProbe {...props} name="ideations" />,
}));
vi.mock('@/components/refinements', () => ({
  RefinementsPanel: (props: ProbeProps) => <PanelProbe {...props} name="refinements" />,
}));
vi.mock('@/components/specs', () => ({
  SpecsPanel: (props: ProbeProps) => <PanelProbe {...props} name="specs" />,
}));
vi.mock('@/components/sprints', () => ({
  SprintsPanel: (props: ProbeProps) => <PanelProbe {...props} name="sprints" />,
}));
vi.mock('@/components/kanban', () => ({
  KanbanBoard: (props: ProbeProps) => <PanelProbe {...props} name="tasks" />,
}));

function mountId(tab: StageTabId): string {
  return screen.getByTestId(`${tab}-probe`).textContent?.split(':')[0] ?? '';
}

describe('BoardStageContent', () => {
  it.each(['stories', 'tasks'] as const)(
    'remounts %s on board change but preserves its refresh contract',
    (activeTab) => {
      const { rerender } = render(
        <BoardStageContent activeTab={activeTab} boardId="board-1" refreshKey={0} />,
      );
      const initialMount = mountId(activeTab);

      rerender(<BoardStageContent activeTab={activeTab} boardId="board-1" refreshKey={1} />);
      expect(mountId(activeTab)).toBe(initialMount);
      expect(screen.getByTestId(`${activeTab}-probe`)).toHaveTextContent('board-1:1');

      rerender(<BoardStageContent activeTab={activeTab} boardId="board-2" refreshKey={1} />);
      expect(mountId(activeTab)).not.toBe(initialMount);
    },
  );

  it.each(['ideations', 'refinements', 'specs', 'sprints'] as const)(
    'remounts %s on both refresh and board change',
    (activeTab) => {
      const { rerender } = render(
        <BoardStageContent activeTab={activeTab} boardId="board-1" refreshKey={0} />,
      );
      const initialMount = mountId(activeTab);

      rerender(<BoardStageContent activeTab={activeTab} boardId="board-1" refreshKey={1} />);
      const refreshedMount = mountId(activeTab);
      expect(refreshedMount).not.toBe(initialMount);

      rerender(<BoardStageContent activeTab={activeTab} boardId="board-2" refreshKey={1} />);
      expect(mountId(activeTab)).not.toBe(refreshedMount);
    },
  );
});
