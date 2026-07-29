import { useId, useState, type ReactNode } from 'react';
import {
  BookOpen,
  GitBranch,
  Monitor,
  Plus,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { ArchitectureTab } from '@/components/architecture';
import { KnowledgeWorkspace } from '@/components/resources/KnowledgeWorkspace';
import { ResourceGateDisclosure } from '@/components/resources/ResourceGateDisclosure';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { useDashboardApi } from '@/services/api';
import type {
  Refinement,
  RefinementKnowledge,
} from '@/types';

type RefinementResourceTab = 'mockups' | 'knowledge' | 'architecture';

interface RefinementKnowledgeTabProps {
  refinementId: string;
  boardId: string;
  onCreated: (knowledge: RefinementKnowledge) => void;
  onDeleted: (knowledgeId: string) => void;
  onResourceChanged: () => void;
}

function RefinementKnowledgeTab({
  refinementId,
  boardId,
  onCreated,
  onDeleted,
  onResourceChanged,
}: RefinementKnowledgeTabProps) {
  const api = useDashboardApi();
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const [adding, setAdding] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newContent, setNewContent] = useState('');

  const refreshWorkspace = () => {
    setRefreshGeneration((current) => current + 1);
  };

  const handleAdd = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    try {
      const created = await api.createRefinementKnowledge(refinementId, {
        title: newTitle.trim(),
        description: newDesc.trim() || undefined,
        content: newContent.trim(),
      });
      toast.success('Knowledge added');
      setAdding(false);
      setNewTitle('');
      setNewDesc('');
      setNewContent('');
      onCreated(created);
      onResourceChanged();
      refreshWorkspace();
    } catch {
      toast.error('Failed to add knowledge');
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this knowledge base item?')) return false;
    try {
      await api.deleteRefinementKnowledge(refinementId, id);
      onDeleted(id);
      onResourceChanged();
      return true;
    } catch {
      toast.error('Failed to delete');
      return false;
    }
  };

  return (
    <div className="space-y-3">
      <KnowledgeWorkspace
        boardId={boardId}
        entityType="refinement"
        entityId={refinementId}
        refreshKey={refreshGeneration}
        loadFallbackDetail={(id) =>
          api.getRefinementKnowledge(refinementId, id)
        }
        onDelete={handleDelete}
      />
      {adding ? (
        <div className="space-y-2 rounded-lg border border-amber-200 bg-amber-50/50 p-3 dark:border-amber-700 dark:bg-amber-900/10">
          <input
            type="text"
            value={newTitle}
            onChange={(event) => setNewTitle(event.target.value)}
            placeholder="Title"
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700"
          />
          <input
            type="text"
            value={newDesc}
            onChange={(event) => setNewDesc(event.target.value)}
            placeholder="Description (optional)"
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700"
          />
          <textarea
            value={newContent}
            onChange={(event) => setNewContent(event.target.value)}
            placeholder="Content..."
            className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700"
            rows={6}
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="btn btn-secondary text-xs"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleAdd()}
              disabled={!newTitle.trim() || !newContent.trim()}
              className="btn btn-primary text-xs"
            >
              Add
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="flex items-center gap-1 text-sm text-amber-600 hover:text-amber-800 dark:text-amber-400 dark:hover:text-amber-300"
        >
          <Plus size={14} /> Add Knowledge
        </button>
      )}
    </div>
  );
}

interface RefinementResourcesPanelProps {
  refinement: Refinement;
  fallbackBoardId: string;
  expanded: boolean;
  onRefinementChanged: (refinement: Refinement) => void;
  onArchitectureChanged: (
    items: NonNullable<Refinement['architecture_designs']>,
  ) => void;
  onKnowledgeCreated: (knowledge: RefinementKnowledge) => void;
  onKnowledgeDeleted: (knowledgeId: string) => void;
}

export function RefinementResourcesPanel({
  refinement,
  fallbackBoardId,
  expanded,
  onRefinementChanged,
  onArchitectureChanged,
  onKnowledgeCreated,
  onKnowledgeDeleted,
}: RefinementResourcesPanelProps) {
  const api = useDashboardApi();
  const tabIdPrefix = useId();
  const [activeTab, setActiveTab] =
    useState<RefinementResourceTab>('mockups');
  const [resourceRevision, setResourceRevision] = useState(0);
  const boardId = refinement.board_id || fallbackBoardId;

  const noteResourceChanged = () => {
    setResourceRevision((current) => current + 1);
  };

  const tabs: {
    id: RefinementResourceTab;
    label: string;
    icon: ReactNode;
    count: number;
  }[] = [
    {
      id: 'mockups',
      label: 'Mockups',
      icon: <Monitor size={14} />,
      count: refinement.screen_mockups?.length || 0,
    },
    {
      id: 'knowledge',
      label: 'Knowledge',
      icon: <BookOpen size={14} />,
      count: refinement.knowledge_bases?.length || 0,
    },
    {
      id: 'architecture',
      label: 'Architecture',
      icon: <GitBranch size={14} />,
      count: refinement.architecture_designs?.length || 0,
    },
  ];

  return (
    <div className="space-y-4" data-testid="refinement-resources-panel">
      <ResourceGateDisclosure
        boardId={boardId}
        entityType="refinement"
        entityId={refinement.id}
        refreshKey={resourceRevision}
      />

      <AccessibleTabList
        idBase={`${tabIdPrefix}-resources`}
        ariaLabel="Refinement resource sections"
        items={tabs}
        value={activeTab}
        onValueChange={setActiveTab}
        variant="secondary"
        className="max-w-full"
      />

      <AccessibleTabPanel
        idBase={`${tabIdPrefix}-resources`}
        tabId="mockups"
        value={activeTab}
        mount="lazy-keep"
      >
          <MockupsTab
            screenMockups={refinement.screen_mockups}
            boardId={boardId}
            entityType="refinement"
            entityId={refinement.id}
            expanded={expanded}
            onUpdate={async (mockups) => {
              const updated = await api.updateRefinement(refinement.id, {
                screen_mockups: mockups,
              });
              onRefinementChanged(updated);
              noteResourceChanged();
            }}
          />
      </AccessibleTabPanel>

      <AccessibleTabPanel
        idBase={`${tabIdPrefix}-resources`}
        tabId="knowledge"
        value={activeTab}
        mount="lazy-keep"
      >
          <RefinementKnowledgeTab
            refinementId={refinement.id}
            boardId={boardId}
            onCreated={onKnowledgeCreated}
            onDeleted={onKnowledgeDeleted}
            onResourceChanged={noteResourceChanged}
          />
      </AccessibleTabPanel>

      <AccessibleTabPanel
        idBase={`${tabIdPrefix}-resources`}
        tabId="architecture"
        value={activeTab}
        mount="lazy-keep"
      >
          <ArchitectureTab
            parentType="refinement"
            parentId={refinement.id}
            boardId={boardId}
            entityType="refinement"
            entityId={refinement.id}
            expanded={expanded}
            screenMockups={refinement.screen_mockups || []}
            onChanged={(items) => {
              onArchitectureChanged(items);
              noteResourceChanged();
            }}
          />
      </AccessibleTabPanel>
    </div>
  );
}
