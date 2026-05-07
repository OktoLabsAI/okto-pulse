import { useEffect, useMemo, useState } from 'react';
import {
  Archive,
  ArchiveRestore,
  BookOpen,
  ChevronRight,
  GitBranch,
  Link2,
  Plus,
  RefreshCw,
  Tags,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { SearchInput } from '@/components/shared/SearchInput';
import { openLineageGraph } from '@/components/traceability';
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

export function StoriesPanel({ boardId }: StoriesPanelProps) {
  const api = useDashboardApi();
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
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className="btn btn-secondary inline-flex items-center gap-1 text-sm disabled:opacity-50"
          >
            <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
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
              onClick={() => setTopicFormOpen((value) => !value)}
              className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-blue-500 dark:hover:bg-gray-700"
              title="New Topic"
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
                  disabled={!topicName.trim()}
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
              <button
                key={topic.id}
                type="button"
                onClick={() => setTopicFilter(topic.id)}
                className={`flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${
                  topicFilter === topic.id
                    ? 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-200'
                    : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700/60'
                }`}
              >
                <span className="truncate">{topic.name}</span>
                <span className="text-xs text-gray-400">{topic.story_count}</span>
              </button>
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
                  className="btn btn-primary inline-flex items-center gap-1 text-sm"
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
            <div className="space-y-2" data-testid="stories-list">
              {filteredStories.map((story) => (
                <div
                  key={story.id}
                  onClick={() => setSelectedStoryId(story.id)}
                  className={`group cursor-pointer rounded-xl border border-surface-200/80 bg-white p-4 transition-all duration-200 hover:border-accent-300 hover:shadow-card-hover dark:border-surface-700/40 dark:bg-surface-800/80 dark:hover:border-accent-600/40 dark:hover:shadow-card-dark-hover ${
                    story.archived ? 'opacity-55' : ''
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_COLORS[story.status]}`}>
                          {STORY_STATUS_LABELS[story.status]}
                        </span>
                        <span className="text-xs text-gray-400">
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
                        <div className="mt-2 grid grid-cols-1 gap-1 text-xs text-gray-500 dark:text-gray-400 md:grid-cols-3">
                          {story.actor && <span className="truncate">As: {story.actor}</span>}
                          {story.goal && <span className="truncate">Want: {story.goal}</span>}
                          {story.benefit && <span className="truncate">So: {story.benefit}</span>}
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
    </div>
  );
}
