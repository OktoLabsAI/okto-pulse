import { IdeationsPanel } from '@/components/ideations';
import { KanbanBoard } from '@/components/kanban';
import { RefinementsPanel } from '@/components/refinements';
import { SpecsPanel } from '@/components/specs';
import { SprintsPanel } from '@/components/sprints';
import { StoriesPanel } from '@/components/stories';

export type StageTabId =
  | 'stories'
  | 'ideations'
  | 'refinements'
  | 'specs'
  | 'sprints'
  | 'tasks';

interface BoardStageContentProps {
  activeTab: StageTabId;
  boardId: string;
  refreshKey: number;
}

/**
 * Keeps refresh behavior per panel while guaranteeing that board-local UI
 * state cannot leak when the active board changes.
 */
export function BoardStageContent({
  activeTab,
  boardId,
  refreshKey,
}: BoardStageContentProps) {
  return (
    <>
      {activeTab === 'stories' && (
        <StoriesPanel key={boardId} boardId={boardId} refreshKey={refreshKey} />
      )}
      {activeTab === 'ideations' && (
        <IdeationsPanel key={`${boardId}:${refreshKey}`} boardId={boardId} />
      )}
      {activeTab === 'refinements' && (
        <RefinementsPanel key={`${boardId}:${refreshKey}`} boardId={boardId} />
      )}
      {activeTab === 'specs' && (
        <SpecsPanel key={`${boardId}:${refreshKey}`} boardId={boardId} />
      )}
      {activeTab === 'sprints' && (
        <SprintsPanel key={`${boardId}:${refreshKey}`} boardId={boardId} />
      )}
      {activeTab === 'tasks' && (
        <KanbanBoard key={boardId} boardId={boardId} refreshKey={refreshKey} />
      )}
    </>
  );
}
