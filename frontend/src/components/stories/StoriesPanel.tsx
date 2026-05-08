import { useEffect, useMemo, useState } from 'react';
import {
  Archive,
  ArchiveRestore,
  BookOpen,
  ChevronRight,
  GitMerge,
  GitBranch,
  Link2,
  MoreHorizontal,
  Pencil,
  Plus,
  Tags,
  Trash2,
  X,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { SearchInput } from '@/components/shared/SearchInput';
import { ViewModeToggle } from '@/components/shared/ViewModeToggle';
import { openLineageGraph } from '@/components/traceability';
import { usePermissions } from '@/hooks/usePermissions';
import { useViewMode } from '@/hooks/useViewMode';
import { sanitizePreview } from '@/lib/sanitizePreview';
import type { StoryStatus, StorySummary, TopicSummary } from '@/types';
import { STORY_STATUS_LABELS } from '@/types';
import { StoryModal } from './StoryModal';

interface StoriesPanelProps {
  boardId: string;
}

const STATUS_FILTERS: Array<{ value: '' | StoryStatus; label: string }> = [
  { value: '', label: 'All' },
  { value: 'draft', label: 'Draft' },
  { value: 'triage', label: 'Triage' },
  { value: 'ready', label: 'Ready' },
  { value: 'converted', label: 'Converted' },
];

const STATUS_COLORS: Record<StoryStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  triage: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  ready: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  converted: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
};

function topicActiveCount(topic: TopicSummary): number {
  return topic.active_count ?? topic.story_count ?? 0;
}

function topicArchivedCount(topic: TopicSummary): number {
  return topic.archived_count ?? 0;
}

function topicTotalCount(topic: TopicSummary): number {
  return topic.total_associated_count ?? topicActiveCount(topic) + topicArchivedCount(topic);
}

function topicErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return 'Topic operation failed';
  try {
    const detail = JSON.parse(error.message);
    if (detail?.code === 'topic_not_empty') {
      return `Topic has ${detail.active_count ?? 0} active and ${detail.archived_count ?? 0} archived Stories. Merge or move them before deleting.`;
    }
    return detail?.detail || detail?.error || error.message;
  } catch {
    return error.message;
  }
}

export function StoriesPanel({ boardId }: StoriesPanelProps) {
  const api = useDashboardApi();
  const permissions = usePermissions(boardId);
  const [topics, setTopics] = useState<TopicSummary[]>([]);
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'' | StoryStatus>('');
  const [topicFilter, setTopicFilter] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedStoryId, setSelectedStoryId] = useState<string | null>(null);
  const [topicFormOpen, setTopicFormOpen] = useState(false);
  const [topicName, setTopicName] = useState('');
  const [topicDescription, setTopicDescription] = useState('');
  const [topicMenuOpen, setTopicMenuOpen] = useState<string | null>(null);
  const [editingTopic, setEditingTopic] = useState<TopicSummary | null>(null);
  const [editTopicName, setEditTopicName] = useState('');
  const [editTopicDescription, setEditTopicDescription] = useState('');
  const [mergeSourceTopic, setMergeSourceTopic] = useState<TopicSummary | null>(null);
  const [mergeTargetId, setMergeTargetId] = useState('');
  const [mergeConfirmed, setMergeConfirmed] = useState(false);
  const [topicActionError, setTopicActionError] = useState('');
  const [topicActionBusy, setTopicActionBusy] = useState(false);
  const { viewMode, setViewMode } = useViewMode('stories', 'list');
  const canCreateTopic = permissions.has('topic.entity.create');
  const canEditTopic = permissions.has('topic.entity.edit_fields');
  const canArchiveTopic = permissions.has('topic.entity.archive');
  const canRestoreTopic = permissions.has('topic.entity.restore');
  const canDeleteTopic = permissions.has('topic.entity.delete');
  const canMergeTopic = permissions.has('topic.entity.merge');

  const canChangeTopicLifecycle = (topic: TopicSummary) => (topic.archived ? canRestoreTopic : canArchiveTopic);
  const hasTopicActions = (topic: TopicSummary) =>
    canEditTopic || canMergeTopic || canDeleteTopic || canChangeTopicLifecycle(topic);

  useEffect(() => {
    load();
  }, [boardId, statusFilter, topicFilter, showArchived]);

  const load = async () => {
    setLoading(true);
    try {
      const [topicData, storyData] = await Promise.all([
        api.listTopics(boardId, showArchived),
        api.listStories(boardId, {
          status: statusFilter || undefined,
          topicId: topicFilter || undefined,
          includeArchived: showArchived,
        }),
      ]);
      setTopics(topicData);
      setStories(storyData);
    } catch {
      toast.error('Failed to load stories');
    } finally {
      setLoading(false);
    }
  };

  const filteredStories = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) return stories;
    return stories.filter((story) => {
      const haystack = [
        story.title,
        story.description,
        story.actor || '',
        story.goal || '',
        story.benefit || '',
        ...(story.labels || []),
      ].join(' ').toLowerCase();
      return haystack.includes(query);
    });
  }, [searchQuery, stories]);

  const selectedTopic = topics.find((topic) => topic.id === topicFilter) || null;

  const createTopic = async () => {
    if (!topicName.trim()) return;
    if (!canCreateTopic) {
      toast.error('Missing permission: topic.entity.create');
      return;
    }
    try {
      const topic = await api.createTopic(boardId, {
        name: topicName.trim(),
        description: topicDescription.trim() || undefined,
      });
      setTopicName('');
      setTopicDescription('');
      setTopicFormOpen(false);
      setTopicFilter(topic.id);
      toast.success('Topic created');
      await load();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to create topic';
      toast.error(message);
    }
  };

  const openTopicEditor = (topic: TopicSummary) => {
    setTopicMenuOpen(null);
    setTopicActionError('');
    setEditingTopic(topic);
    setEditTopicName(topic.name);
    setEditTopicDescription(topic.description || '');
  };

  const saveTopic = async () => {
    if (!editingTopic || !editTopicName.trim()) return;
    if (!canEditTopic) {
      setTopicActionError('Missing permission: topic.entity.edit_fields');
      return;
    }
    setTopicActionBusy(true);
    setTopicActionError('');
    try {
      await api.updateTopic(editingTopic.id, {
        name: editTopicName.trim(),
        description: editTopicDescription.trim() || null,
      });
      toast.success('Topic updated');
      await load();
      setEditingTopic(null);
    } catch (err) {
      const message = topicErrorMessage(err);
      setTopicActionError(message);
      toast.error(message);
    } finally {
      setTopicActionBusy(false);
    }
  };

  const toggleTopicArchive = async () => {
    if (!editingTopic) return;
    const requiredPermission = editingTopic.archived ? 'topic.entity.restore' : 'topic.entity.archive';
    if (!canChangeTopicLifecycle(editingTopic)) {
      setTopicActionError(`Missing permission: ${requiredPermission}`);
      return;
    }
    setTopicActionBusy(true);
    setTopicActionError('');
    try {
      const archived = !editingTopic.archived;
      await api.updateTopic(editingTopic.id, { archived });
      toast.success(archived ? 'Topic archived' : 'Topic restored');
      await load();
      setEditingTopic(null);
    } catch (err) {
      const message = topicErrorMessage(err);
      setTopicActionError(message);
      toast.error(message);
    } finally {
      setTopicActionBusy(false);
    }
  };

  const deleteTopic = async () => {
    if (!editingTopic) return;
    if (!canDeleteTopic) {
      setTopicActionError('Missing permission: topic.entity.delete');
      return;
    }
    setTopicActionBusy(true);
    setTopicActionError('');
    try {
      await api.deleteTopic(editingTopic.id);
      if (topicFilter === editingTopic.id) setTopicFilter('');
      toast.success('Topic deleted');
      await load();
      setEditingTopic(null);
    } catch (err) {
      const message = topicErrorMessage(err);
      setTopicActionError(message);
      toast.error(message);
    } finally {
      setTopicActionBusy(false);
    }
  };

  const openMergeModal = (topic: TopicSummary) => {
    if (!canMergeTopic) {
      toast.error('Missing permission: topic.entity.merge');
      return;
    }
    const firstTarget = topics.find((candidate) => candidate.id !== topic.id && !candidate.archived);
    setTopicMenuOpen(null);
    setTopicActionError('');
    setMergeSourceTopic(topic);
    setMergeTargetId(firstTarget?.id || '');
    setMergeConfirmed(false);
  };

  const mergeTopic = async () => {
    if (!mergeSourceTopic || !mergeTargetId || !mergeConfirmed) return;
    if (!canMergeTopic) {
      setTopicActionError('Missing permission: topic.entity.merge');
      return;
    }
    setTopicActionBusy(true);
    setTopicActionError('');
    try {
      const result = await api.mergeTopics(mergeSourceTopic.id, mergeTargetId);
      if (topicFilter === mergeSourceTopic.id) setTopicFilter(result.target.id);
      toast.success(`Merged ${result.moved_count} Stories`);
      await load();
      setMergeSourceTopic(null);
    } catch (err) {
      const message = topicErrorMessage(err);
      setTopicActionError(message);
      toast.error(message);
    } finally {
      setTopicActionBusy(false);
    }
  };

  const openCreateStory = () => {
    if (topics.length === 0) {
      setTopicFormOpen(true);
      return;
    }
    setCreateOpen(true);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Stories</h2>
          <span className="text-sm text-gray-400">
            ({filteredStories.length}
            {searchQuery ? ` of ${stories.length}` : ''})
          </span>
        </div>
        <div className="flex items-center gap-2">
          <SearchInput
            value={searchQuery}
            onChange={setSearchQuery}
            placeholder="Search stories..."
            testId="stories-search"
          />
          <ViewModeToggle value={viewMode} onChange={setViewMode} testId="stories-view-mode" />
          <button
            type="button"
            onClick={openCreateStory}
            className="btn btn-primary inline-flex items-center gap-1 text-sm"
          >
            <Plus size={16} />
            New Story
          </button>
        </div>
      </div>

      <div className="mb-4 flex flex-wrap gap-1.5">
        {STATUS_FILTERS.map((filter) => (
          <button
            key={filter.value || 'all'}
            type="button"
            onClick={() => setStatusFilter(filter.value)}
            className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
              statusFilter === filter.value
                ? 'bg-accent-500 text-white shadow-sm'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
            }`}
          >
            {filter.label}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setShowArchived((value) => !value)}
          className={`ml-2 rounded-full px-2.5 py-1 text-xs transition-colors ${
            showArchived
              ? 'bg-amber-500 text-white'
              : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400'
          }`}
        >
          {showArchived ? 'Hide archived' : 'Show archived'}
        </button>
      </div>

      <div className="grid flex-1 min-h-0 grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
        <aside className="min-h-0 overflow-auto rounded-xl border border-surface-200/80 bg-white p-3 dark:border-surface-700/40 dark:bg-surface-800/80">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white">
              <Tags size={15} className="text-blue-500" />
              Topics
            </div>
            <button
              type="button"
              onClick={() => canCreateTopic && setTopicFormOpen((value) => !value)}
              disabled={!canCreateTopic}
              className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-blue-500 disabled:cursor-not-allowed disabled:opacity-40 dark:hover:bg-gray-700"
              title={canCreateTopic ? 'New Topic' : 'Missing permission: topic.entity.create'}
            >
              <Plus size={15} />
            </button>
          </div>

          {topicFormOpen && (
            <div className="mb-3 space-y-2 rounded-lg border border-blue-200 bg-blue-50 p-2 dark:border-blue-900/50 dark:bg-blue-950/30">
              <input
                value={topicName}
                onChange={(event) => setTopicName(event.target.value)}
                placeholder="Topic name"
                className="w-full rounded-md border border-blue-200 bg-white px-2 py-1.5 text-sm text-gray-900 dark:border-blue-800 dark:bg-gray-900 dark:text-white"
              />
              <input
                value={topicDescription}
                onChange={(event) => setTopicDescription(event.target.value)}
                placeholder="Description"
                className="w-full rounded-md border border-blue-200 bg-white px-2 py-1.5 text-sm text-gray-900 dark:border-blue-800 dark:bg-gray-900 dark:text-white"
              />
              <div className="flex justify-end gap-1">
                <button
                  type="button"
                  onClick={() => setTopicFormOpen(false)}
                  className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-white/70 dark:hover:bg-gray-800"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={createTopic}
                  disabled={!topicName.trim() || !canCreateTopic}
                  className="rounded-md bg-blue-600 px-2 py-1 text-xs font-semibold text-white disabled:opacity-50"
                >
                  Save
                </button>
              </div>
            </div>
          )}

          <div className="space-y-1">
            <button
              type="button"
              onClick={() => setTopicFilter('')}
              className={`flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${
                topicFilter === ''
                  ? 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-200'
                  : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700/60'
              }`}
            >
              <span>All topics</span>
              <span className="text-xs text-gray-400">{stories.length}</span>
            </button>
            {topics.map((topic) => (
              <div
                key={topic.id}
                className={`relative flex items-center rounded-lg transition-colors ${
                  topicFilter === topic.id
                    ? 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-200'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700/60'
                }`}
              >
                <button
                  type="button"
                  onClick={() => setTopicFilter(topic.id)}
                  className="flex min-w-0 flex-1 items-center justify-between gap-2 px-2.5 py-2 text-left text-sm"
                >
                  <span className="min-w-0 truncate">
                    {topic.name}
                    {topic.archived && (
                      <span className="ml-1 rounded bg-gray-200 px-1 py-0.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                        archived
                      </span>
                    )}
                  </span>
                  <span className="text-xs text-gray-400">{topic.story_count}</span>
                </button>
                {hasTopicActions(topic) && (
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      setTopicMenuOpen(topicMenuOpen === topic.id ? null : topic.id);
                    }}
                    className="mr-1 rounded-md p-1 text-gray-400 hover:bg-white/70 hover:text-blue-500 dark:hover:bg-gray-800"
                    title={`Topic actions: ${topic.name}`}
                  >
                    <MoreHorizontal size={15} />
                  </button>
                )}
                {topicMenuOpen === topic.id && (
                  <div className="absolute right-1 top-9 z-20 w-52 rounded-lg border border-surface-200 bg-white p-1 shadow-xl dark:border-surface-700 dark:bg-surface-900">
                    {canEditTopic && (
                      <button
                        type="button"
                        onClick={() => openTopicEditor(topic)}
                        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-surface-800"
                      >
                        <Pencil size={14} />
                        Edit details
                      </button>
                    )}
                    {canMergeTopic && (
                      <button
                        type="button"
                        onClick={() => openMergeModal(topic)}
                        disabled={topics.filter((candidate) => candidate.id !== topic.id && !candidate.archived).length === 0}
                        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-surface-800"
                      >
                        <GitMerge size={14} />
                        Merge into...
                      </button>
                    )}
                    {(canChangeTopicLifecycle(topic) || canDeleteTopic) && (
                      <button
                        type="button"
                        onClick={() => openTopicEditor(topic)}
                        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-surface-800"
                      >
                        {topic.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                        {topic.archived ? 'Restore / delete' : 'Archive / delete'}
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </aside>

        <section className="min-h-0 overflow-auto">
          {loading ? (
            <div className="py-12 text-center text-sm text-gray-500 dark:text-gray-400">Loading stories...</div>
          ) : topics.length === 0 ? (
            <div className="flex min-h-[300px] items-center justify-center rounded-xl border border-dashed border-surface-300 dark:border-surface-700">
              <div className="text-center">
                <Tags size={36} className="mx-auto mb-3 text-gray-300 dark:text-gray-600" />
                <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">No topics yet</p>
                <button
                  type="button"
                  onClick={() => setTopicFormOpen(true)}
                  disabled={!canCreateTopic}
                  className="btn btn-primary inline-flex items-center gap-1 text-sm disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Plus size={15} />
                  New Topic
                </button>
              </div>
            </div>
          ) : filteredStories.length === 0 ? (
            <div className="flex min-h-[300px] items-center justify-center rounded-xl border border-dashed border-surface-300 dark:border-surface-700">
              <div className="text-center">
                <BookOpen size={36} className="mx-auto mb-3 text-gray-300 dark:text-gray-600" />
                <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">
                  {searchQuery ? `No results for "${searchQuery}"` : 'No stories here'}
                </p>
                <button
                  type="button"
                  onClick={openCreateStory}
                  className="btn btn-primary inline-flex items-center gap-1 text-sm"
                >
                  <Plus size={15} />
                  New Story
                </button>
              </div>
            </div>
          ) : (
            <div
              className={`animate-list ${
                viewMode === 'grid'
                  ? 'grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3'
                  : 'space-y-2'
              }`}
              data-testid={`stories-${viewMode}`}
            >
              {filteredStories.map((story) => (
                <div
                  key={story.id}
                  onClick={() => setSelectedStoryId(story.id)}
                  className={`group h-full cursor-pointer overflow-hidden rounded-xl border border-surface-200/80 bg-white p-4 transition-all duration-200 hover:border-accent-300 hover:shadow-card-hover dark:border-surface-700/40 dark:bg-surface-800/80 dark:hover:border-accent-600/40 dark:hover:shadow-card-dark-hover ${
                    story.archived ? 'opacity-55' : ''
                  }`}
                >
                  <div className="flex min-w-0 items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        <span className={`inline-flex max-w-full shrink-0 items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_COLORS[story.status]}`}>
                          {STORY_STATUS_LABELS[story.status]}
                        </span>
                        <span className="min-w-0 max-w-full truncate text-xs text-gray-400">
                          {topics.find((topic) => topic.id === story.topic_id)?.name || selectedTopic?.name || 'Topic'}
                        </span>
                        {story.archived && (
                          <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                            archived
                          </span>
                        )}
                      </div>
                      <h3 className="truncate text-sm font-semibold text-gray-900 dark:text-white">{story.title}</h3>
                      <p className="mt-1 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                        {sanitizePreview(story.description)}
                      </p>
                      {(story.actor || story.goal || story.benefit) && (
                        <div
                          className={`mt-2 text-xs text-gray-500 dark:text-gray-400 ${
                            viewMode === 'grid'
                              ? 'space-y-1'
                              : 'grid grid-cols-1 gap-1 md:grid-cols-3'
                          }`}
                        >
                          {story.actor && <span className="block min-w-0 truncate">As: {story.actor}</span>}
                          {story.goal && <span className="block min-w-0 truncate">Want: {story.goal}</span>}
                          {story.benefit && <span className="block min-w-0 truncate">So: {story.benefit}</span>}
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      {story.screen_mockups && story.screen_mockups.length > 0 && (
                        <span className="inline-flex items-center gap-1 rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                          {story.screen_mockups.length} mockups
                        </span>
                      )}
                      {story.ideation_links.length > 0 && (
                        <span className="inline-flex items-center gap-1 rounded bg-cyan-100 px-1.5 py-0.5 text-[10px] font-medium text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300">
                          <Link2 size={10} />
                          {story.ideation_links.length}
                        </span>
                      )}
                      {story.ideation_links.length > 0 && (
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            openLineageGraph('story', story.id);
                          }}
                          className="rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-cyan-500 group-hover:opacity-100 dark:hover:bg-gray-700"
                          title="Open lineage graph"
                        >
                          <GitBranch size={14} />
                        </button>
                      )}
                      {story.archived ? <ArchiveRestore size={14} className="text-gray-300" /> : <Archive size={14} className="text-gray-300" />}
                      <ChevronRight size={16} className="text-gray-300 transition-colors group-hover:text-blue-500 dark:text-gray-600" />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {createOpen && (
        <StoryModal
          boardId={boardId}
          topics={topics}
          initialTopicId={topicFilter || topics[0]?.id}
          onClose={() => setCreateOpen(false)}
          onChanged={() => {
            load();
            setCreateOpen(false);
          }}
        />
      )}

      {selectedStoryId && (
        <StoryModal
          boardId={boardId}
          storyId={selectedStoryId}
          topics={topics}
          onClose={() => setSelectedStoryId(null)}
          onChanged={load}
        />
      )}

      {editingTopic && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-surface-700 bg-white shadow-2xl dark:bg-surface-900">
            <div className="flex items-center justify-between border-b border-surface-200 px-5 py-4 dark:border-surface-700">
              <div className="min-w-0">
                <h3 className="truncate text-lg font-semibold text-gray-900 dark:text-white">Edit Topic</h3>
                <p className="text-sm text-gray-500 dark:text-gray-400">Lifecycle changes affect only the Topic grouping.</p>
              </div>
              <button
                type="button"
                onClick={() => setEditingTopic(null)}
                className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-surface-800"
                title="Close"
              >
                <X size={18} />
              </button>
            </div>
            <div className="flex-1 space-y-4 overflow-auto p-5">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Name</span>
                  <input
                    value={editTopicName}
                    onChange={(event) => setEditTopicName(event.target.value)}
                    disabled={!canEditTopic}
                    className="w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-surface-700 dark:bg-surface-950 dark:text-white"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Status</span>
                  <div className="rounded-lg border border-surface-300 px-3 py-2 text-sm dark:border-surface-700">
                    {editingTopic.archived ? 'Archived' : 'Active'}
                  </div>
                </label>
              </div>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Description</span>
                <textarea
                  value={editTopicDescription}
                  onChange={(event) => setEditTopicDescription(event.target.value)}
                  disabled={!canEditTopic}
                  rows={4}
                  className="w-full resize-none rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-surface-700 dark:bg-surface-950 dark:text-white"
                />
              </label>

              <div className="rounded-lg border border-surface-200 bg-surface-50 p-4 dark:border-surface-700 dark:bg-surface-950">
                <h4 className="mb-3 text-sm font-semibold text-gray-900 dark:text-white">Impact summary</h4>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="rounded-lg bg-white p-3 dark:bg-surface-900">
                    <div className="text-xs text-gray-500">Active Stories</div>
                    <div className="text-xl font-semibold text-gray-900 dark:text-white">{topicActiveCount(editingTopic)}</div>
                  </div>
                  <div className="rounded-lg bg-white p-3 dark:bg-surface-900">
                    <div className="text-xs text-gray-500">Archived Stories</div>
                    <div className="text-xl font-semibold text-gray-900 dark:text-white">{topicArchivedCount(editingTopic)}</div>
                  </div>
                  <div className="rounded-lg bg-white p-3 dark:bg-surface-900">
                    <div className="text-xs text-gray-500">Delete gate</div>
                    <div className="text-sm font-semibold text-gray-900 dark:text-white">
                      {topicTotalCount(editingTopic) === 0 ? 'Allowed' : 'Blocked'}
                    </div>
                  </div>
                </div>
              </div>

              {topicActionError && (
                <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300">
                  {topicActionError}
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-surface-200 px-5 py-4 dark:border-surface-700">
              <button
                type="button"
                onClick={deleteTopic}
                disabled={topicActionBusy || !canDeleteTopic || topicTotalCount(editingTopic) > 0}
                className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:text-red-300 dark:hover:bg-red-950/30"
                title={
                  !canDeleteTopic
                    ? 'Missing permission: topic.entity.delete'
                    : topicTotalCount(editingTopic) > 0
                      ? 'Delete requires zero active and archived Stories'
                      : 'Delete Topic'
                }
              >
                <Trash2 size={15} />
                Delete topic
              </button>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={toggleTopicArchive}
                  disabled={topicActionBusy || !canChangeTopicLifecycle(editingTopic)}
                  className="inline-flex items-center gap-2 rounded-lg border border-surface-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-surface-700 dark:text-gray-200 dark:hover:bg-surface-800"
                  title={
                    canChangeTopicLifecycle(editingTopic)
                      ? undefined
                      : `Missing permission: ${editingTopic.archived ? 'topic.entity.restore' : 'topic.entity.archive'}`
                  }
                >
                  {editingTopic.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
                  {editingTopic.archived ? 'Restore' : 'Archive'}
                </button>
                <button
                  type="button"
                  onClick={saveTopic}
                  disabled={topicActionBusy || !canEditTopic || !editTopicName.trim()}
                  className="btn btn-primary text-sm"
                  title={canEditTopic ? undefined : 'Missing permission: topic.entity.edit_fields'}
                >
                  Save changes
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {mergeSourceTopic && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-surface-700 bg-white shadow-2xl dark:bg-surface-900">
            <div className="flex items-center justify-between border-b border-surface-200 px-5 py-4 dark:border-surface-700">
              <div className="min-w-0">
                <h3 className="truncate text-lg font-semibold text-gray-900 dark:text-white">Merge Topics</h3>
                <p className="text-sm text-gray-500 dark:text-gray-400">Stories move to the target; Story-Ideation lineage remains intact.</p>
              </div>
              <button
                type="button"
                onClick={() => setMergeSourceTopic(null)}
                className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-surface-800"
                title="Close"
              >
                <X size={18} />
              </button>
            </div>
            <div className="flex-1 space-y-4 overflow-auto p-5">
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <span className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Source Topic</span>
                  <div className="rounded-lg border border-surface-300 px-3 py-2 text-sm font-medium dark:border-surface-700">
                    {mergeSourceTopic.name}
                  </div>
                </div>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Target Topic</span>
                  <select
                    value={mergeTargetId}
                    onChange={(event) => setMergeTargetId(event.target.value)}
                    className="w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-surface-700 dark:bg-surface-950 dark:text-white"
                  >
                    <option value="">Select target</option>
                    {topics
                      .filter((topic) => topic.id !== mergeSourceTopic.id && !topic.archived)
                      .map((topic) => (
                        <option key={topic.id} value={topic.id}>
                          {topic.name} ({topicTotalCount(topic)} Stories)
                        </option>
                      ))}
                  </select>
                </label>
              </div>

              <div className="rounded-lg border border-surface-200 bg-surface-50 p-4 dark:border-surface-700 dark:bg-surface-950">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h4 className="text-sm font-semibold text-gray-900 dark:text-white">Preview</h4>
                  <span className="rounded-full bg-cyan-100 px-2 py-0.5 text-xs font-medium text-cyan-700 dark:bg-cyan-900/40 dark:text-cyan-300">
                    {topicTotalCount(mergeSourceTopic)} Stories will move
                  </span>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="rounded-lg bg-white p-3 dark:bg-surface-900">
                    <div className="text-xs text-gray-500">Active</div>
                    <div className="text-xl font-semibold text-gray-900 dark:text-white">{topicActiveCount(mergeSourceTopic)}</div>
                  </div>
                  <div className="rounded-lg bg-white p-3 dark:bg-surface-900">
                    <div className="text-xs text-gray-500">Archived</div>
                    <div className="text-xl font-semibold text-gray-900 dark:text-white">{topicArchivedCount(mergeSourceTopic)}</div>
                  </div>
                  <div className="rounded-lg bg-white p-3 dark:bg-surface-900">
                    <div className="text-xs text-gray-500">Source after merge</div>
                    <div className="text-sm font-semibold text-gray-900 dark:text-white">Archived</div>
                  </div>
                </div>
              </div>

              <label className="flex items-start gap-3 rounded-lg border border-surface-200 bg-white p-3 text-sm text-gray-700 dark:border-surface-700 dark:bg-surface-950 dark:text-gray-200">
                <input
                  type="checkbox"
                  checked={mergeConfirmed}
                  onChange={(event) => setMergeConfirmed(event.target.checked)}
                  className="mt-1"
                />
                <span>I understand the source Topic will be archived and its Stories will point to the target Topic.</span>
              </label>

              {topicActionError && (
                <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300">
                  {topicActionError}
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 border-t border-surface-200 px-5 py-4 dark:border-surface-700">
              <button
                type="button"
                onClick={() => setMergeSourceTopic(null)}
                className="rounded-lg border border-surface-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-surface-700 dark:text-gray-200 dark:hover:bg-surface-800"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={mergeTopic}
                disabled={topicActionBusy || !canMergeTopic || !mergeTargetId || !mergeConfirmed}
                className="btn btn-primary inline-flex items-center gap-2 text-sm"
                title={canMergeTopic ? undefined : 'Missing permission: topic.entity.merge'}
              >
                <GitMerge size={15} />
                Merge topics
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
