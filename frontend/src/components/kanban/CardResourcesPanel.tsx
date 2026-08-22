import {
  useEffect,
  useId,
  useMemo,
  useState,
  type ChangeEvent,
  type ReactNode,
} from 'react';
import {
  BookOpen,
  Download,
  GitBranch,
  Monitor,
  Paperclip,
  Trash2,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { ArchitectureTab } from '@/components/architecture';
import { ResourceGateDisclosure } from '@/components/resources/ResourceGateDisclosure';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { useDashboardApi } from '@/services/api';
import type { Card } from '@/types';

import { CardKnowledgeTab } from './CardKnowledgeTab';

type CardResourceTab =
  | 'mockups'
  | 'knowledge'
  | 'architecture'
  | 'attachments';

interface CardResourcesPanelProps {
  card: Card;
  expanded: boolean;
  specKnowledgeBases: {
    id: string;
    title: string;
    description?: string | null;
    content: string;
    mime_type?: string;
    root_source_kb_id?: string | null;
    governance?: Record<string, unknown>;
  }[];
  canReadMockups: boolean;
  canReadKnowledge: boolean;
  canReadArchitecture: boolean;
  canReadAttachments: boolean;
  canUploadAttachments: boolean;
  canDeleteAttachments: boolean;
  onCardChanged: (card: Card) => void;
  onSubjectChanged: () => void;
  onBusyChange: (busy: boolean) => void;
}

function AttachmentsPanel({
  card,
  canUpload,
  canDelete,
  onCardChanged,
  onResourceChanged,
}: {
  card: Card;
  canUpload: boolean;
  canDelete: boolean;
  onCardChanged: (card: Card) => void;
  onResourceChanged: () => void;
}) {
  const api = useDashboardApi();

  const handleFileUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;

    try {
      const attachment = await api.uploadAttachment(
        card.board_id,
        card.id,
        file,
      );
      onCardChanged({
        ...card,
        attachments: [...card.attachments, attachment],
      });
      onResourceChanged();
      toast.success('Attachment uploaded');
    } catch {
      toast.error('Failed to upload attachment');
    }
  };

  const handleDelete = async (attachmentId: string) => {
    if (!confirm('Delete this attachment?')) return;
    try {
      await api.deleteAttachment(card.board_id, card.id, attachmentId);
      onCardChanged({
        ...card,
        attachments: card.attachments.filter(
          (attachment) => attachment.id !== attachmentId,
        ),
      });
      onResourceChanged();
      toast.success('Attachment deleted');
    } catch {
      toast.error('Failed to delete attachment');
    }
  };

  return (
    <div className="space-y-3" data-testid="card-attachments-panel">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-100">
            Attachments
          </h3>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Supporting files attached directly to this card.
          </p>
        </div>
        {canUpload && (
          <label className="btn btn-secondary cursor-pointer text-xs">
            <input
              type="file"
              className="hidden"
              onChange={(event) => void handleFileUpload(event)}
            />
            + Add attachment
          </label>
        )}
      </div>

      {card.attachments.length > 0 ? (
        <div className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200 dark:divide-gray-700 dark:border-gray-700">
          {card.attachments.map((attachment) => (
            <div
              key={attachment.id}
              className="flex items-center gap-2 bg-white p-3 dark:bg-gray-900/20"
            >
              <Paperclip
                size={15}
                className="shrink-0 text-gray-400"
                aria-hidden="true"
              />
              <span className="min-w-0 flex-1 truncate text-sm text-gray-700 dark:text-gray-200">
                {attachment.original_filename}
              </span>
              <button
                type="button"
                onClick={async () => {
                  try {
                    await api.downloadAttachment(
                      card.board_id,
                      card.id,
                      attachment.id,
                      attachment.original_filename,
                    );
                  } catch {
                    toast.error('Failed to download attachment');
                  }
                }}
                className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-blue-600 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-blue-300"
                aria-label={`Download ${attachment.original_filename}`}
              >
                <Download size={15} />
              </button>
              {canDelete && (
                <button
                  type="button"
                  onClick={() => void handleDelete(attachment.id)}
                  className="rounded p-1 text-gray-500 hover:bg-red-50 hover:text-red-600 dark:text-gray-400 dark:hover:bg-red-900/20 dark:hover:text-red-300"
                  aria-label={`Delete ${attachment.original_filename}`}
                >
                  <Trash2 size={15} />
                </button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="rounded-lg border border-dashed border-gray-300 px-4 py-8 text-center text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
          No attachments yet.
        </div>
      )}
    </div>
  );
}

export function CardResourcesPanel({
  card,
  expanded,
  specKnowledgeBases,
  canReadMockups,
  canReadKnowledge,
  canReadArchitecture,
  canReadAttachments,
  canUploadAttachments,
  canDeleteAttachments,
  onCardChanged,
  onSubjectChanged,
  onBusyChange,
}: CardResourcesPanelProps) {
  const api = useDashboardApi();
  const idBase = useId();
  const [activeTab, setActiveTab] = useState<CardResourceTab>('mockups');
  const [resourceRevision, setResourceRevision] = useState(0);

  const tabs = useMemo<{
    id: CardResourceTab;
    label: string;
    icon: ReactNode;
    count: number;
  }[]>(() => [
    ...(canReadMockups
      ? [{
          id: 'mockups' as const,
          label: 'Mockups',
          icon: <Monitor size={14} />,
          count: card.screen_mockups?.length || 0,
        }]
      : []),
    ...(canReadKnowledge
      ? [{
          id: 'knowledge' as const,
          label: 'Knowledge',
          icon: <BookOpen size={14} />,
          count: card.knowledge_bases?.length || 0,
        }]
      : []),
    ...(canReadArchitecture
      ? [{
          id: 'architecture' as const,
          label: 'Architecture',
          icon: <GitBranch size={14} />,
          count: card.architecture_designs?.length || 0,
        }]
      : []),
    ...(canReadAttachments
      ? [{
          id: 'attachments' as const,
          label: 'Attachments',
          icon: <Paperclip size={14} />,
          count: card.attachments.length,
        }]
      : []),
  ], [
    canReadArchitecture,
    canReadAttachments,
    canReadKnowledge,
    canReadMockups,
    card.architecture_designs?.length,
    card.attachments.length,
    card.knowledge_bases?.length,
    card.screen_mockups?.length,
  ]);

  useEffect(() => {
    if (!tabs.some((tab) => tab.id === activeTab) && tabs[0]) {
      setActiveTab(tabs[0].id);
    }
  }, [activeTab, tabs]);

  const noteResourceChanged = () => {
    setResourceRevision((current) => current + 1);
    onSubjectChanged();
  };

  return (
    <div className="space-y-4" data-testid="card-resources-panel">
      <ResourceGateDisclosure
        boardId={card.board_id}
        entityType="card"
        entityId={card.id}
        refreshKey={resourceRevision}
      />

      <AccessibleTabList
        idBase={`${idBase}-card-resources`}
        ariaLabel="Card resource sections"
        items={tabs}
        value={activeTab}
        onValueChange={setActiveTab}
        variant="secondary"
        className="max-w-full"
      />

      {canReadMockups && (
        <AccessibleTabPanel
          idBase={`${idBase}-card-resources`}
          tabId="mockups"
          value={activeTab}
          mount="lazy-keep"
        >
          <MockupsTab
            screenMockups={card.screen_mockups}
            boardId={card.board_id}
            entityType="card"
            entityId={card.id}
            expanded={expanded}
          />
        </AccessibleTabPanel>
      )}

      {canReadKnowledge && (
        <AccessibleTabPanel
          idBase={`${idBase}-card-resources`}
          tabId="knowledge"
          value={activeTab}
          mount="lazy-keep"
        >
          <CardKnowledgeTab
            card={card}
            specKnowledgeBases={specKnowledgeBases}
            readOnly={card.status === 'rejected'}
            onUpdate={async () => {
              const updated = await api.getCard(card.id).catch(() => null);
              if (updated) {
                onCardChanged(updated);
              }
              noteResourceChanged();
            }}
            onBusyChange={onBusyChange}
          />
        </AccessibleTabPanel>
      )}

      {canReadArchitecture && (
        <AccessibleTabPanel
          idBase={`${idBase}-card-resources`}
          tabId="architecture"
          value={activeTab}
          mount="lazy-keep"
        >
          <ArchitectureTab
            parentType="card"
            parentId={card.id}
            boardId={card.board_id}
            entityType="card"
            entityId={card.id}
            specIdForCopy={card.spec_id}
            locked={card.status === 'rejected'}
            expanded={expanded}
            screenMockups={card.screen_mockups || []}
            onChanged={(items) => {
              onCardChanged({ ...card, architecture_designs: items });
              noteResourceChanged();
            }}
          />
        </AccessibleTabPanel>
      )}

      {canReadAttachments && (
        <AccessibleTabPanel
          idBase={`${idBase}-card-resources`}
          tabId="attachments"
          value={activeTab}
          mount="lazy-keep"
        >
          <AttachmentsPanel
            card={card}
            canUpload={canUploadAttachments}
            canDelete={canDeleteAttachments}
            onCardChanged={onCardChanged}
            onResourceChanged={noteResourceChanged}
          />
        </AccessibleTabPanel>
      )}
    </div>
  );
}
