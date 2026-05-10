import { useEffect, useMemo, useState } from 'react';
import {
  Archive,
  ArchiveRestore,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  Clock,
  Download,
  GitBranch,
  Link2,
  Maximize2,
  Minimize2,
  RefreshCw,
  Save,
  Search,
  X,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import { MockupsTab } from '@/components/specs/MockupsTab';
import { EditableField } from '@/components/shared/EditableField';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { openLineageGraph } from '@/components/traceability';
import { downloadMarkdown, exportStory, slugify } from '@/lib/exportMarkdown';
import type {
  IdeationStatus,
  IdeationSummary,
  ScreenMockup,
  Story,
  StoryStatus,
  TopicSummary,
  UpdateStoryRequest,
} from '@/types';
import {
  IDEATION_STATUS_LABELS,
  STORY_STATUSES,
  STORY_STATUS_LABELS,
} from '@/types';

interface StoryModalProps {
  boardId: string;
  storyId?: string | null;
  topics: TopicSummary[];
  initialTopicId?: string | null;
  onClose: () => void;
  onChanged: () => void;
}

type StoryModalTab = 'details' | 'mockups' | 'links' | 'activity';

const STATUS_STYLES: Record<StoryStatus, string> = {
  draft: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  triage: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  ready: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  converted: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
};

const TAB_LABELS: Record<StoryModalTab, string> = {
  details: 'Details',
  mockups: 'Mockups',
  links: 'Links',
  activity: 'Activity',
};

const EDITABLE_IDEATION_STATUSES: IdeationStatus[] = ['draft', 'review', 'approved', 'evaluating'];

function normalizeOptional(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function parseLabels(value: string): string[] | undefined {
  const labels = value
    .split(',')
    .map((label) => label.trim())
    .filter(Boolean);
  return labels.length > 0 ? labels : undefined;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '-';
  return new Date(value).toLocaleString();
}

function formatLabels(labelsValue: string): string[] {
  return parseLabels(labelsValue) || [];
}

export function StoryModal({
  boardId,
  storyId,
  topics,
  initialTopicId,
  onClose,
  onChanged,
}: StoryModalProps) {
  const api = useDashboardApi();
  const [story, setStory] = useState<Story | null>(null);
  const [activeTab, setActiveTab] = useState<StoryModalTab>('details');
  const [loading, setLoading] = useState(Boolean(storyId));
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [editableIdeations, setEditableIdeations] = useState<IdeationSummary[]>([]);
  const [loadingIdeations, setLoadingIdeations] = useState(false);
  const [ideationSearch, setIdeationSearch] = useState('');
  const [selectedIdeationId, setSelectedIdeationId] = useState('');
  const [ideationSelectorOpen, setIdeationSelectorOpen] = useState(false);

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [topicId, setTopicId] = useState(initialTopicId || topics[0]?.id || '');
  const [actor, setActor] = useState('');
  const [goal, setGoal] = useState('');
  const [benefit, setBenefit] = useState('');
  const [labels, setLabels] = useState('');
  const [status, setStatus] = useState<StoryStatus>('draft');
  const [mockups, setMockups] = useState<ScreenMockup[]>([]);

  const existing = Boolean(storyId);

  const syncStoryState = (data: Story) => {
    setStory(data);
    setTitle(data.title);
    setDescription(data.description);
    setTopicId(data.topic_id);
    setActor(data.actor || '');
    setGoal(data.goal || '');
    setBenefit(data.benefit || '');
    setLabels((data.labels || []).join(', '));
    setStatus(data.status);
    setMockups(data.screen_mockups || []);
  };

  useEffect(() => {
    if (!storyId) {
      setTopicId(initialTopicId || topics[0]?.id || '');
      return;
    }

    let active = true;
    setLoading(true);
    api.getStory(storyId)
      .then((data) => {
        if (!active) return;
        syncStoryState(data);
      })
      .catch(() => toast.error('Failed to load story'))
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [storyId, initialTopicId, topics]);

  const handleRefresh = async () => {
    if (!storyId) return;
    setLoading(true);
    try {
      const data = await api.getStory(storyId);
      syncStoryState(data);
    } catch {
      toast.error('Failed to refresh story');
    } finally {
      setLoading(false);
    }
  };

  const canSave = title.trim().length > 0 && description.trim().length > 0 && topicId.length > 0;
  const hasIdeationLink = (story?.ideation_links?.length || 0) > 0;
  const canConvert = story?.status === 'ready' && !hasIdeationLink;

  const topicName = useMemo(() => {
    return topics.find((topic) => topic.id === topicId)?.name || story?.topic?.name || 'No topic';
  }, [story?.topic?.name, topicId, topics]);

  const linkedIdeationIds = useMemo(() => {
    return new Set((story?.ideation_links || []).map((link) => link.ideation_id));
  }, [story?.ideation_links]);

  const linkedIdeationKey = useMemo(() => {
    return Array.from(linkedIdeationIds).sort().join('|');
  }, [linkedIdeationIds]);

  useEffect(() => {
    if (activeTab !== 'links' || !story) return;
    if (hasIdeationLink) {
      setEditableIdeations([]);
      setSelectedIdeationId('');
      setIdeationSearch('');
      setIdeationSelectorOpen(false);
      return;
    }

    let active = true;
    setLoadingIdeations(true);
    Promise.all(
      EDITABLE_IDEATION_STATUSES.map((statusValue) => api.listIdeations(boardId, statusValue, false))
    )
      .then((groups) => {
        if (!active) return;
        const byId = new Map<string, IdeationSummary>();
        for (const ideation of groups.flat()) {
          if (ideation.archived || linkedIdeationIds.has(ideation.id)) continue;
          byId.set(ideation.id, ideation);
        }
        setEditableIdeations(Array.from(byId.values()));
      })
      .catch(() => toast.error('Failed to load ideations'))
      .finally(() => {
        if (active) setLoadingIdeations(false);
      });

    return () => {
      active = false;
    };
  }, [activeTab, boardId, hasIdeationLink, linkedIdeationKey, story?.id]);

  const filteredIdeations = useMemo(() => {
    const query = ideationSearch.trim().toLowerCase();
    if (!query) return editableIdeations;
    return editableIdeations.filter((ideation) => {
      const haystack = [
        ideation.title,
        ideation.id,
        ideation.status,
        ideation.problem_statement || '',
        ideation.description || '',
      ].join(' ').toLowerCase();
      return haystack.includes(query);
    });
  }, [editableIdeations, ideationSearch]);

  const selectedIdeation = useMemo(() => {
    return editableIdeations.find((ideation) => ideation.id === selectedIdeationId) || null;
  }, [editableIdeations, selectedIdeationId]);

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      if (story) {
        let updated = await api.updateStory(story.id, {
          title: title.trim(),
          description: description.trim(),
          topic_id: topicId,
          actor: normalizeOptional(actor) || null,
          goal: normalizeOptional(goal) || null,
          benefit: normalizeOptional(benefit) || null,
          labels: parseLabels(labels),
          screen_mockups: mockups,
        });
        if (status !== story.status) {
          updated = await api.moveStory(story.id, { status });
        }
        syncStoryState(updated);
        toast.success('Story updated');
      } else {
        const created = await api.createStory(boardId, {
          title: title.trim(),
          description: description.trim(),
          topic_id: topicId,
          actor: normalizeOptional(actor),
          goal: normalizeOptional(goal),
          benefit: normalizeOptional(benefit),
          labels: parseLabels(labels),
          status,
          screen_mockups: mockups,
        });
        syncStoryState(created);
        toast.success('Story created');
      }
      onChanged();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save story';
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const updateExistingStory = async (patch: UpdateStoryRequest) => {
    if (!story) return;
    try {
      const updated = await api.updateStory(story.id, patch);
      syncStoryState(updated);
      onChanged();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to update story';
      toast.error(message);
      throw err;
    }
  };

  const saveRequiredText = async (
    value: string,
    field: 'title' | 'description',
    label: string
  ) => {
    const next = value.trim();
    if (!next) {
      const message = `${label} is required`;
      toast.error(message);
      throw new Error(message);
    }
    await updateExistingStory(field === 'title' ? { title: next } : { description: next });
  };

  const saveOptionalText = async (field: 'actor' | 'goal' | 'benefit', value: string) => {
    const next = normalizeOptional(value) || null;
    const patch: UpdateStoryRequest =
      field === 'actor' ? { actor: next } :
      field === 'goal' ? { goal: next } :
      { benefit: next };
    await updateExistingStory(patch);
  };

  const saveLabels = async (value: string) => {
    await updateExistingStory({ labels: formatLabels(value) });
  };

  const handleMockupsUpdate = async (next: ScreenMockup[]) => {
    if (!story) {
      setMockups(next);
      return;
    }
    const updated = await api.updateStory(story.id, { screen_mockups: next });
    setStory(updated);
    setMockups(updated.screen_mockups || []);
    onChanged();
  };

  const handleArchive = async () => {
    if (!story) return;
    setSaving(true);
    try {
      const updated = story.archived
        ? await api.restoreStory(story.id)
        : await api.archiveStory(story.id);
      setStory(updated);
      toast.success(updated.archived ? 'Story archived' : 'Story restored');
      onChanged();
    } catch {
      toast.error('Failed');
    } finally {
      setSaving(false);
    }
  };

  const handleLinkIdeation = async () => {
    if (!story || !selectedIdeationId) return;
    setSaving(true);
    try {
      const updated = await api.linkStoryToIdeation(story.id, selectedIdeationId);
      setStory(updated);
      setSelectedIdeationId('');
      setIdeationSearch('');
      setIdeationSelectorOpen(false);
      toast.success('Ideation linked');
      onChanged();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to link ideation';
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const handleOpenLineage = () => {
    if (!story) return;
    openLineageGraph('story', story.id);
  };

  const handleDownloadMarkdown = () => {
    if (!story) return;
    const filename = `story-${slugify(story.title || story.id)}.md`;
    downloadMarkdown(exportStory(story), filename);
  };

  const handleConvert = async () => {
    if (!story || !canConvert) return;
    setSaving(true);
    try {
      const response = await api.convertStories(boardId, {
        story_ids: [story.id],
        title: story.title,
        mockup_ids: (story.screen_mockups || []).map((mockup) => mockup.id),
      });
      const updated = await api.getStory(story.id);
      setStory(updated);
      setStatus(updated.status);
      toast.success(`Ideation created (${response.propagated_mockups} mockups)`);
      onChanged();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to convert story';
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const renderMetadataControls = () => (
    <>
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Topic *</label>
        <select
          value={topicId}
          onChange={(event) => setTopicId(event.target.value)}
          className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
        >
          {topics.map((topic) => (
            <option key={topic.id} value={topic.id}>{topic.name}</option>
          ))}
        </select>
      </div>
      <div>
        <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Status</label>
        <select
          value={status}
          onChange={(event) => setStatus(event.target.value as StoryStatus)}
          className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
        >
          {STORY_STATUSES.map((item) => (
            <option key={item} value={item}>{STORY_STATUS_LABELS[item]}</option>
          ))}
        </select>
      </div>
    </>
  );

  const renderExistingDetails = () => (
    <div className="space-y-5" data-testid="story-details-read-view">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">Title</h4>
          <EditableField
            value={title}
            onSave={(value) => saveRequiredText(value, 'title', 'Title')}
            renderView={(value) => <p className="text-sm font-medium text-gray-900 dark:text-white">{value}</p>}
            placeholder="No title"
          />
        </div>
        {renderMetadataControls()}
      </div>

      <div data-testid="story-description-field">
        <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">Description</h4>
        <EditableField
          value={description}
          onSave={(value) => saveRequiredText(value, 'description', 'Description')}
          multiline
          renderView={(value) => <MarkdownContent content={value} />}
          placeholder="No description"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div>
          <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">Actor</h4>
          <EditableField
            value={actor}
            onSave={(value) => saveOptionalText('actor', value)}
            placeholder="No actor"
          />
        </div>
        <div>
          <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">Goal</h4>
          <EditableField
            value={goal}
            onSave={(value) => saveOptionalText('goal', value)}
            placeholder="No goal"
          />
        </div>
        <div className="md:col-span-2">
          <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">Benefit</h4>
          <EditableField
            value={benefit}
            onSave={(value) => saveOptionalText('benefit', value)}
            placeholder="No benefit"
          />
        </div>
      </div>

      <div>
        <h4 className="mb-1 text-sm font-semibold text-gray-700 dark:text-gray-300">Labels</h4>
        <EditableField
          value={labels}
          onSave={saveLabels}
          renderView={(value) => {
            const parsed = formatLabels(value);
            return parsed.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {parsed.map((label) => (
                  <span
                    key={label}
                    className="rounded bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                  >
                    {label}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-sm text-gray-400 dark:text-gray-500 italic">No labels</p>
            );
          }}
          placeholder="No labels"
        />
      </div>
    </div>
  );

  const renderCreateDetails = () => (
    <div className="space-y-4">
      {topics.length === 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700 dark:border-amber-900/50 dark:bg-amber-950/40 dark:text-amber-300">
          Create a Topic before saving a Story.
        </div>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Title *</label>
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        {renderMetadataControls()}
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Actor</label>
          <input
            value={actor}
            onChange={(event) => setActor(event.target.value)}
            placeholder="As a..."
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Goal</label>
          <input
            value={goal}
            onChange={(event) => setGoal(event.target.value)}
            placeholder="I want..."
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Benefit</label>
          <input
            value={benefit}
            onChange={(event) => setBenefit(event.target.value)}
            placeholder="So that..."
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Description *</label>
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={5}
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-gray-400">Labels</label>
          <input
            value={labels}
            onChange={(event) => setLabels(event.target.value)}
            placeholder="ux, onboarding"
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
      </div>
    </div>
  );

  const renderDetails = () => (
    <div className="space-y-4">
      {story ? renderExistingDetails() : renderCreateDetails()}
    </div>
  );

  const renderLinks = () => {
    if (!story) {
      return <div className="py-10 text-center text-sm text-gray-500 dark:text-gray-400">Save the Story before linking Ideations.</div>;
    }

    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="text-sm font-semibold text-gray-900 dark:text-white">
              {story.ideation_links.length === 1 ? 'Linked Ideation' : 'Linked Ideations'}
            </h4>
            {story.ideation_links.length > 0 && (
              <button
                type="button"
                onClick={() => openLineageGraph('story', story.id)}
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-cyan-600 hover:bg-cyan-50 dark:text-cyan-300 dark:hover:bg-cyan-900/20"
              >
                <GitBranch size={13} />
                Lineage
              </button>
            )}
          </div>
          {story.ideation_links.length === 0 ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">No links yet</p>
          ) : (
            <div className="space-y-2">
              {story.ideation_links.map((link) => (
                <div key={link.id} className="flex items-center justify-between rounded-lg bg-gray-50 px-3 py-2 text-sm dark:bg-gray-800/70">
                  <span className="font-mono text-xs text-gray-600 dark:text-gray-300">{link.ideation_id}</span>
                  <span className="text-xs text-gray-400">{formatDate(link.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {hasIdeationLink ? (
          <div className="rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-3 text-sm text-cyan-800 dark:border-cyan-900/50 dark:bg-cyan-950/30 dark:text-cyan-200">
            This Story already has its Ideation link. A Story can link to only one Ideation.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_auto]">
            <div className="relative">
              <Search
                size={15}
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
              />
              <input
                value={ideationSearch}
                onChange={(event) => {
                  setIdeationSearch(event.target.value);
                  setSelectedIdeationId('');
                  setIdeationSelectorOpen(true);
                }}
                onFocus={() => setIdeationSelectorOpen(true)}
                placeholder="Search editable ideations..."
                className="w-full rounded-lg border border-gray-300 bg-white py-2 pl-9 pr-10 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
              />
              <button
                type="button"
                onClick={() => setIdeationSelectorOpen((open) => !open)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                title="Show editable ideations"
              >
                <ChevronDown size={15} />
              </button>
              {ideationSelectorOpen && (
                <div className="absolute z-20 mt-2 max-h-72 w-full overflow-auto rounded-lg border border-gray-200 bg-white p-1 shadow-xl dark:border-gray-700 dark:bg-surface-800">
                  {loadingIdeations ? (
                    <div className="px-3 py-2 text-sm text-gray-500 dark:text-gray-400">Loading ideations...</div>
                  ) : filteredIdeations.length === 0 ? (
                    <div className="px-3 py-2 text-sm text-gray-500 dark:text-gray-400">No editable ideations found</div>
                  ) : (
                    filteredIdeations.map((ideation) => (
                      <button
                        key={ideation.id}
                        type="button"
                        onClick={() => {
                          setSelectedIdeationId(ideation.id);
                          setIdeationSearch(ideation.title);
                          setIdeationSelectorOpen(false);
                        }}
                        className="flex w-full items-start justify-between gap-3 rounded-md px-3 py-2 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-700/60"
                      >
                        <span className="min-w-0">
                          <span className="block truncate font-medium text-gray-900 dark:text-white">
                            {ideation.title}
                          </span>
                          <span className="mt-0.5 block truncate font-mono text-[11px] text-gray-500 dark:text-gray-400">
                            {ideation.id}
                          </span>
                        </span>
                        <span className="shrink-0 rounded-full bg-blue-100 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                          {IDEATION_STATUS_LABELS[ideation.status]}
                        </span>
                      </button>
                    ))
                  )}
                </div>
              )}
              {selectedIdeation && (
                <div className="mt-2 rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-600 dark:bg-gray-800/70 dark:text-gray-300">
                  <span className="font-medium text-gray-900 dark:text-white">{selectedIdeation.title}</span>
                  <span className="ml-2 text-gray-400">{selectedIdeation.id.slice(0, 8)}</span>
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={handleLinkIdeation}
              disabled={!selectedIdeationId || saving}
              className="btn btn-secondary inline-flex items-center justify-center gap-1 text-sm disabled:opacity-50"
            >
              <Link2 size={15} />
              Link
            </button>
          </div>
        )}

        <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-3 dark:border-blue-900/50 dark:bg-blue-950/30">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-blue-900 dark:text-blue-100">Create Ideation</div>
              <div className="text-xs text-blue-700 dark:text-blue-300">Requires Ready status and no existing Ideation link.</div>
            </div>
            <button
              type="button"
              onClick={handleConvert}
              disabled={!canConvert || saving}
              className="btn btn-primary inline-flex items-center gap-1 text-sm disabled:opacity-50"
            >
              <ArrowRight size={15} />
              Convert
            </button>
          </div>
        </div>
      </div>
    );
  };

  const renderActivity = () => (
    <div className="space-y-3">
      <div className="rounded-lg border border-gray-200 p-3 text-sm dark:border-gray-700">
        <div className="flex items-center gap-2 text-gray-900 dark:text-white">
          <Clock size={15} className="text-gray-400" />
          Created
        </div>
        <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatDate(story?.created_at)}</div>
      </div>
      <div className="rounded-lg border border-gray-200 p-3 text-sm dark:border-gray-700">
        <div className="flex items-center gap-2 text-gray-900 dark:text-white">
          <CheckCircle2 size={15} className="text-gray-400" />
          Updated
        </div>
        <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">{formatDate(story?.updated_at)}</div>
      </div>
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div
        className={`flex flex-col overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-surface-800 ${
          expanded
            ? 'h-[min(900px,96vh)] w-[min(1400px,98vw)]'
            : 'h-[min(780px,92vh)] w-[min(980px,96vw)]'
        }`}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <BookOpen size={18} className="text-blue-500" />
              <h3 className="truncate text-lg font-semibold text-gray-900 dark:text-white">
                {existing ? title || 'Story' : 'New Story'}
              </h3>
              <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}>
                {STORY_STATUS_LABELS[status]}
              </span>
            </div>
            <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">{topicName}</div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {story && (
              <>
                <button
                  type="button"
                  onClick={handleOpenLineage}
                  className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-cyan-600 dark:hover:bg-gray-700 dark:hover:text-cyan-300"
                  title="Open lineage graph"
                >
                  <GitBranch size={18} />
                </button>
                <button
                  type="button"
                  onClick={handleDownloadMarkdown}
                  className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-blue-600 dark:hover:bg-gray-700 dark:hover:text-blue-300"
                  title="Download Markdown"
                >
                  <Download size={18} />
                </button>
                <button
                  type="button"
                  onClick={handleRefresh}
                  disabled={loading}
                  className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                  title="Refresh"
                >
                  <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
                </button>
              </>
            )}
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              title={expanded ? 'Collapse' : 'Expand'}
            >
              {expanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              title="Close"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="flex gap-1 border-b border-gray-200 px-5 dark:border-gray-700">
          {(Object.keys(TAB_LABELS) as StoryModalTab[]).map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`border-b-2 px-3 py-3 text-sm font-medium transition-colors ${
                activeTab === tab
                  ? 'border-blue-500 text-blue-600 dark:text-blue-300'
                  : 'border-transparent text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'
              }`}
            >
              {TAB_LABELS[tab]}
              {tab === 'mockups' && mockups.length > 0 && (
                <span className="ml-1 rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-300">
                  {mockups.length}
                </span>
              )}
              {tab === 'links' && story && story.ideation_links.length > 0 && (
                <span className="ml-1 rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-300">
                  {story.ideation_links.length}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-auto p-5">
          {loading ? (
            <div className="py-12 text-center text-sm text-gray-500 dark:text-gray-400">Loading story...</div>
          ) : (
            <>
              {activeTab === 'details' && renderDetails()}
              {activeTab === 'mockups' && (
                <MockupsTab
                  screenMockups={mockups}
                  expanded
                  onUpdate={handleMockupsUpdate}
                />
              )}
              {activeTab === 'links' && renderLinks()}
              {activeTab === 'activity' && renderActivity()}
            </>
          )}
        </div>

        <div className="flex items-center justify-between border-t border-gray-200 px-5 py-4 dark:border-gray-700">
          <div>
            {story && (
              <button
                type="button"
                onClick={handleArchive}
                disabled={saving}
                className="inline-flex items-center gap-1 text-sm text-red-600 hover:text-red-700 disabled:opacity-50 dark:text-red-400 dark:hover:text-red-300"
              >
                {story.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
                {story.archived ? 'Restore story' : 'Archive story'}
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onClose} className="btn btn-secondary text-sm">Close</button>
            <button
              type="button"
              onClick={handleSave}
              disabled={!canSave || saving || loading}
              className="btn btn-primary inline-flex items-center gap-1 text-sm disabled:opacity-50"
            >
              <Save size={15} />
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
