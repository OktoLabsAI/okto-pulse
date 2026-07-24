import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  Loader2,
  RefreshCw,
  Trash2,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { useDashboardApi } from '@/services/api';
import type {
  EffectiveResourceItem,
  EffectiveResourcesResponse,
  KnowledgeWorkspaceItem,
  ResourceGateEntityType,
} from '@/types';

interface KnowledgeWorkspaceProps {
  boardId: string;
  entityType: ResourceGateEntityType;
  entityId: string;
  refreshKey?: string | number;
  fallbackItems?: unknown[];
  loadFallbackDetail?: (resourceId: string) => Promise<unknown>;
  onDelete?: (resourceId: string) => Promise<void | boolean>;
  canDelete?: (item: KnowledgeWorkspaceItem) => boolean;
  className?: string;
}

const EMPTY_COUNTS = {
  unique_effective_count: 0,
  raw_attachment_count: 0,
  workspace_item_count: 0,
  unique_root_version_count: 0,
};
const EMPTY_FALLBACK_ITEMS: unknown[] = [];

function asText(value: unknown): string | null {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  for (const key of ['content', 'description', 'global_description', 'html_content']) {
    if (typeof record[key] === 'string') return record[key] as string;
  }
  if (record.resource && typeof record.resource === 'object') {
    return asText(record.resource);
  }
  try {
    return `\`\`\`json\n${JSON.stringify(record, null, 2)}\n\`\`\``;
  } catch {
    return null;
  }
}

function legacyItem(item: EffectiveResourceItem, index: number): KnowledgeWorkspaceItem {
  const resource = item.resource && typeof item.resource === 'object'
    ? item.resource as Record<string, unknown>
    : item as Record<string, unknown>;
  const resourceId = String(item.resource_id || item.id || resource.id || `legacy-${index}`);
  const rootId = String(
    item.ref?.root_resource_id
      || item.root_id
      || item.root_source_kb_id
      || item.source_kb_id
      || resource.root_id
      || resource.root_source_kb_id
      || resource.source_kb_id
      || resourceId,
  );
  const revision = item.resource_version
    ?? item.ref?.source_revision
    ?? resource.resource_version
    ?? resource.source_revision
    ?? null;
  const version = revision === null || revision === undefined || revision === ''
    ? null
    : String(revision);
  const canonicalId = String(
    item.canonical_unique_resource_id || `knowledge_base:${rootId}`,
  );
  const source = String(item.source || resource.source || '');
  const copiedFromSpec = source.startsWith('copied_from_spec:');
  const hasInlineBody = Boolean(
    item.resource
    || typeof resource.content === 'string'
    || typeof resource.html_content === 'string'
    || typeof resource.global_description === 'string',
  );
  return {
    resource_type: 'knowledge_base',
    canonical_unique_resource_id: canonicalId,
    versioned_projection_id: `${canonicalId}@${version || 'legacy'}`,
    root_id: rootId,
    resource_version: version,
    representative_resource_id: resourceId,
    title: String(resource.title || item.title || 'Knowledge resource'),
    attachment_kind: item.attachment_kind || null,
    inherited: item.inherited,
    grandfathered: version === null,
    stale: Boolean(item.ref?.knowledge_assignment_stale),
    superseded: Boolean(item.superseded),
    provenance: {
      source_entity_type: item.source_entity_type
        ?? item.provenance?.source_entity_type
        ?? (copiedFromSpec ? 'spec' : null),
      source_entity_id: item.source_entity_id ?? item.provenance?.source_entity_id ?? null,
      source_entity_title: item.source_entity_title ?? item.provenance?.source_entity_title ?? null,
      origin_class: item.ref?.origin_class ?? null,
      source_revision: version,
      source_content_sha256: null,
    },
    physical_attachments: [],
    detail_cursor: '',
    relevance_links: [],
    ...(hasInlineBody ? { body: resource } : {}),
  };
}

function normalizedItems(response: EffectiveResourcesResponse): KnowledgeWorkspaceItem[] {
  if (Array.isArray(response.items)) {
    return response.items.filter((item) => item.resource_type === 'knowledge_base');
  }
  return (response.resources?.knowledge_base || []).map(legacyItem);
}

function itemTitle(item: KnowledgeWorkspaceItem): string {
  return item.title || item.root_id || item.representative_resource_id || 'Knowledge resource';
}

function sourceLabel(item: KnowledgeWorkspaceItem): string | null {
  const type = item.provenance.source_entity_type;
  const source = item.provenance.source_entity_title || item.provenance.source_entity_id;
  if (!type && !source) return null;
  return [type, source].filter(Boolean).join(': ');
}

function relevanceLabel(link: Record<string, unknown>): string {
  if (link.entity_id) {
    const entityType = typeof link.entity_type === 'string'
      ? link.entity_type.replace(/_/g, ' ')
      : 'entity';
    return `${entityType}: ${String(link.entity_id)}`;
  }
  const label = link.label
    || link.title
    || link.requirement_id
    || link.target_id
    || link.assignment_id
    || link.mode;
  return label ? String(label) : 'linked';
}

function bodyOmissionLabel(reason: string | undefined): string {
  if (reason === 'body_unavailable') return 'Content is unavailable for this historical resource.';
  if (reason === 'body_size_limit') return 'Content exceeds the per-item detail budget.';
  if (reason === 'response_budget') return 'Content was omitted to keep the response within its budget.';
  return 'Content metadata is available; expand the item to load its detail.';
}

class StaleKnowledgeWorkspaceRequest extends Error {
  constructor() {
    super('stale Knowledge Workspace request');
    this.name = 'StaleKnowledgeWorkspaceRequest';
  }
}

function responseMatchesEntity(
  response: EffectiveResourcesResponse,
  boardId: string,
  entityType: ResourceGateEntityType,
  entityId: string,
): boolean {
  return (
    (!response.board_id || response.board_id === boardId)
    && (!response.entity_type || response.entity_type === entityType)
    && (!response.entity_id || response.entity_id === entityId)
  );
}

export function KnowledgeWorkspace({
  boardId,
  entityType,
  entityId,
  refreshKey,
  fallbackItems,
  loadFallbackDetail,
  onDelete,
  canDelete,
  className = '',
}: KnowledgeWorkspaceProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  const fallbackItemsRef = useRef(fallbackItems || EMPTY_FALLBACK_ITEMS);
  fallbackItemsRef.current = fallbackItems || EMPTY_FALLBACK_ITEMS;
  const contextKey = `${boardId}\u001f${entityType}\u001f${entityId}`;
  const contextKeyRef = useRef(contextKey);
  contextKeyRef.current = contextKey;
  const requestEpochRef = useRef(0);
  const consumedSummaryCursorsRef = useRef(new Set<string>());
  const loadingMoreRef = useRef(false);
  const [items, setItems] = useState<KnowledgeWorkspaceItem[]>([]);
  const [counts, setCounts] = useState(EMPTY_COUNTS);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, KnowledgeWorkspaceItem>>({});
  const detailsRef = useRef<Record<string, KnowledgeWorkspaceItem>>({});
  const [detailLoadingIds, setDetailLoadingIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  const cacheDetail = useCallback((
    projectionId: string,
    detail: KnowledgeWorkspaceItem,
  ) => {
    detailsRef.current = {
      ...detailsRef.current,
      [projectionId]: detail,
    };
    setDetails(detailsRef.current);
  }, []);

  const applyPage = useCallback((
    response: EffectiveResourcesResponse,
    append: boolean,
    requestedCursor: string | null = null,
  ) => {
    let pageItems = normalizedItems(response);
    const resolvedFallbackItems = fallbackItemsRef.current;
    if (!append && !Array.isArray(response.items) && resolvedFallbackItems.length > 0) {
      const fallback = resolvedFallbackItems.map((item, index) => legacyItem(
        item as unknown as EffectiveResourceItem,
        index,
      ));
      const seen = new Set<string>();
      pageItems = [...fallback, ...pageItems].filter((item) => {
        if (seen.has(item.versioned_projection_id)) return false;
        seen.add(item.versioned_projection_id);
        return true;
      });
    }
    setItems((current) => {
      const seen = new Set<string>();
      const combined = append ? [...current, ...pageItems] : pageItems;
      return combined.filter((item) => {
        if (seen.has(item.versioned_projection_id)) return false;
        seen.add(item.versioned_projection_id);
        return true;
      });
    });
    const candidateCursor = response.next_cursor || null;
    const repeatedCursor = Boolean(
      candidateCursor
      && (
        candidateCursor === requestedCursor
        || consumedSummaryCursorsRef.current.has(candidateCursor)
      )
    );
    setNextCursor(repeatedCursor ? null : candidateCursor);
    if (repeatedCursor) {
      toast.error('Knowledge Workspace pagination stopped after a repeated cursor.');
    }
    setCounts({
      unique_effective_count: response.unique_effective_count
        ?? Number(response.lineage_counts?.unique_effective_count || pageItems.length),
      raw_attachment_count: response.raw_attachment_count
        ?? Number(response.lineage_counts?.raw_attachment_count || pageItems.length),
      workspace_item_count: response.workspace_item_count
        ?? response.total_count
        ?? pageItems.length,
      unique_root_version_count: response.unique_root_version_count
        ?? response.total_count
        ?? pageItems.length,
    });
  }, []);

  const loadFirstPage = useCallback(async () => {
    const requestEpoch = requestEpochRef.current + 1;
    requestEpochRef.current = requestEpoch;
    const requestContext = contextKey;
    consumedSummaryCursorsRef.current.clear();
    loadingMoreRef.current = false;
    setLoading(true);
    setLoadingMore(false);
    setError(null);
    setItems([]);
    setCounts(EMPTY_COUNTS);
    setNextCursor(null);
    setExpandedId(null);
    detailsRef.current = {};
    setDetails({});
    setDetailLoadingIds(new Set());
    try {
      const response = await apiRef.current.getEffectiveResources(
        boardId,
        entityType,
        entityId,
        { profile: 'summary', limit: 25 },
      );
      if (
        requestEpochRef.current !== requestEpoch
        || contextKeyRef.current !== requestContext
      ) return;
      if (!responseMatchesEntity(response, boardId, entityType, entityId)) {
        throw new Error('Knowledge Workspace response belongs to a different entity.');
      }
      applyPage(response, false);
    } catch (loadError) {
      if (
        requestEpochRef.current !== requestEpoch
        || contextKeyRef.current !== requestContext
      ) return;
      setItems([]);
      setCounts(EMPTY_COUNTS);
      setNextCursor(null);
      setError(loadError instanceof Error ? loadError.message : 'Failed to load Knowledge Workspace.');
    } finally {
      if (
        requestEpochRef.current === requestEpoch
        && contextKeyRef.current === requestContext
      ) {
        setLoading(false);
      }
    }
  }, [applyPage, boardId, contextKey, entityId, entityType]);

  useEffect(() => {
    void loadFirstPage();
    return () => {
      requestEpochRef.current += 1;
      loadingMoreRef.current = false;
    };
  }, [loadFirstPage, refreshKey]);

  const loadMore = async () => {
    if (!nextCursor || loadingMoreRef.current) return;
    const requestEpoch = requestEpochRef.current;
    const requestContext = contextKey;
    const requestedCursor = nextCursor;
    if (consumedSummaryCursorsRef.current.has(requestedCursor)) {
      setNextCursor(null);
      toast.error('Knowledge Workspace pagination stopped after a repeated cursor.');
      return;
    }
    consumedSummaryCursorsRef.current.add(requestedCursor);
    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      const response = await apiRef.current.getEffectiveResources(
        boardId,
        entityType,
        entityId,
        { profile: 'summary', cursor: requestedCursor, limit: 25 },
      );
      if (
        requestEpochRef.current !== requestEpoch
        || contextKeyRef.current !== requestContext
      ) return;
      if (!responseMatchesEntity(response, boardId, entityType, entityId)) {
        throw new Error('Knowledge Workspace page belongs to a different entity.');
      }
      applyPage(response, true, requestedCursor);
    } catch (loadError) {
      if (
        requestEpochRef.current !== requestEpoch
        || contextKeyRef.current !== requestContext
      ) return;
      consumedSummaryCursorsRef.current.delete(requestedCursor);
      toast.error(loadError instanceof Error ? loadError.message : 'Failed to load more resources.');
    } finally {
      if (
        requestEpochRef.current === requestEpoch
        && contextKeyRef.current === requestContext
      ) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
    }
  };

  const ensureDetail = async (item: KnowledgeWorkspaceItem): Promise<KnowledgeWorkspaceItem> => {
    const requestEpoch = requestEpochRef.current;
    const requestContext = contextKey;
    const isCurrentRequest = () => (
      requestEpochRef.current === requestEpoch
      && contextKeyRef.current === requestContext
    );
    const cached = detailsRef.current[item.versioned_projection_id];
    if (cached) return cached;
    if (item.body !== undefined) {
      if (!isCurrentRequest()) throw new StaleKnowledgeWorkspaceRequest();
      cacheDetail(item.versioned_projection_id, item);
      return item;
    }
    if (!item.detail_cursor) {
      if (loadFallbackDetail && item.representative_resource_id) {
        setDetailLoadingIds((current) => new Set(current).add(item.versioned_projection_id));
        try {
          const body = await loadFallbackDetail(item.representative_resource_id);
          if (!isCurrentRequest()) throw new StaleKnowledgeWorkspaceRequest();
          const detail = { ...item, body, body_omitted_reason: undefined };
          cacheDetail(item.versioned_projection_id, detail);
          return detail;
        } finally {
          if (isCurrentRequest()) {
            setDetailLoadingIds((current) => {
              const next = new Set(current);
              next.delete(item.versioned_projection_id);
              return next;
            });
          }
        }
      }
      if (!isCurrentRequest()) throw new StaleKnowledgeWorkspaceRequest();
      cacheDetail(item.versioned_projection_id, item);
      return item;
    }
    setDetailLoadingIds((current) => new Set(current).add(item.versioned_projection_id));
    try {
      const response = await apiRef.current.getEffectiveResources(
        boardId,
        entityType,
        entityId,
        { profile: 'detail', cursor: item.detail_cursor },
      );
      if (!isCurrentRequest()) throw new StaleKnowledgeWorkspaceRequest();
      if (!responseMatchesEntity(response, boardId, entityType, entityId)) {
        throw new Error('Knowledge detail response belongs to a different entity.');
      }
      const detail = normalizedItems(response).find(
        (candidate) => (
          candidate.versioned_projection_id === item.versioned_projection_id
        ),
      );
      if (!detail) {
        throw new Error('Knowledge detail response did not match the requested resource.');
      }
      cacheDetail(item.versioned_projection_id, detail);
      return detail;
    } finally {
      if (isCurrentRequest()) {
        setDetailLoadingIds((current) => {
          const next = new Set(current);
          next.delete(item.versioned_projection_id);
          return next;
        });
      }
    }
  };

  const toggle = async (item: KnowledgeWorkspaceItem) => {
    if (expandedId === item.versioned_projection_id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(item.versioned_projection_id);
    try {
      await ensureDetail(item);
    } catch (detailError) {
      if (!(detailError instanceof StaleKnowledgeWorkspaceRequest)) {
        toast.error(detailError instanceof Error ? detailError.message : 'Failed to load resource detail.');
      }
    }
  };

  const download = async (item: KnowledgeWorkspaceItem) => {
    try {
      let detail = detailsRef.current[item.versioned_projection_id] || item;
      if (detail.body === undefined) detail = await ensureDetail(item);
      const content = asText(detail.body);
      if (!content) {
        toast.error(bodyOmissionLabel(detail.body_omitted_reason));
        return;
      }
      const title = itemTitle(detail);
      const safeTitle = title.replace(/[^A-Za-z0-9._-]+/g, '_') || 'knowledge';
      const safeVersion = detail.resource_version
        ? String(detail.resource_version).replace(/[^A-Za-z0-9._-]+/g, '_')
        : '';
      const filename = `${safeTitle}${safeVersion ? `_v${safeVersion}` : ''}.md`;
      const blob = new Blob([`# ${title}\n\n${content}\n`], {
        type: 'text/markdown;charset=utf-8',
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (detailError) {
      if (!(detailError instanceof StaleKnowledgeWorkspaceRequest)) {
        toast.error(detailError instanceof Error ? detailError.message : 'Failed to download resource.');
      }
    }
  };

  const copyBodyReference = async (item: KnowledgeWorkspaceItem) => {
    const detail = detailsRef.current[item.versioned_projection_id] || item;
    const bodyRef = detail.body_ref || item.body_ref;
    if (!bodyRef) return;
    const reference = `${bodyRef.resource_type}:${bodyRef.resource_id || item.root_id}`;
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error('Clipboard access is unavailable.');
      }
      await navigator.clipboard.writeText(reference);
      toast.success('Knowledge source reference copied.');
    } catch (copyError) {
      toast.error(copyError instanceof Error ? copyError.message : 'Failed to copy source reference.');
    }
  };

  const remove = async (item: KnowledgeWorkspaceItem) => {
    const resourceId = item.representative_resource_id;
    if (!onDelete || !resourceId) return;
    setDeletingId(item.versioned_projection_id);
    try {
      const deleted = await onDelete(resourceId);
      if (deleted !== false) {
        await loadFirstPage();
      }
    } finally {
      setDeletingId(null);
    }
  };

  const visibleItems = useMemo(() => {
    const seen = new Set<string>();
    return items.filter((item) => {
      if (seen.has(item.versioned_projection_id)) return false;
      seen.add(item.versioned_projection_id);
      return true;
    });
  }, [items]);

  return (
    <section
      className={`space-y-3 ${className}`}
      aria-label="Knowledge Workspace"
      data-testid="knowledge-workspace"
    >
      <div className="rounded-xl border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-900/40">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white">
              <BookOpen size={15} className="text-amber-500" />
              Knowledge Workspace
            </h3>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Version-aware, read-only projection from ResourceLineage.v2.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void loadFirstPage()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            ['Logical roots', counts.unique_effective_count],
            ['Physical links', counts.raw_attachment_count],
            ['Workspace rows', counts.workspace_item_count],
            ['Root versions', counts.unique_root_version_count],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-lg bg-white px-2 py-1.5 dark:bg-gray-950">
              <div className="text-[10px] uppercase tracking-wide text-gray-400">{label}</div>
              <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">{value}</div>
            </div>
          ))}
        </div>
      </div>

      {loading && (
        <div className="flex items-center justify-center gap-2 py-6 text-sm text-gray-500" role="status">
          <Loader2 size={15} className="animate-spin" />
          Loading Knowledge Workspace…
        </div>
      )}

      {!loading && error && (
        <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300" role="alert">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span className="flex-1">{error}</span>
          <button type="button" onClick={() => void loadFirstPage()} className="font-medium underline">
            Retry
          </button>
        </div>
      )}

      {!loading && !error && visibleItems.length === 0 && (
        <div className="rounded-lg border border-dashed border-gray-300 py-6 text-center dark:border-gray-700">
          <BookOpen size={28} className="mx-auto mb-2 text-gray-300 dark:text-gray-600" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No knowledge bases</p>
          <p className="mt-1 text-xs text-gray-400">Choose an explicit reference or snapshot below when one is relevant.</p>
          <p className="mt-1 text-[10px] text-violet-500">Knowledge is advisory; an empty workspace does not block delivery.</p>
        </div>
      )}

      {!loading && visibleItems.map((item) => {
        const detail = details[item.versioned_projection_id] || item;
        const expanded = expandedId === item.versioned_projection_id;
        const body = asText(detail.body);
        const source = sourceLabel(item);
        const deletable = Boolean(
          onDelete
          && item.representative_resource_id
          && !item.inherited
          && (canDelete ? canDelete(item) : true),
        );
        return (
          <article
            key={item.versioned_projection_id}
            className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700"
            data-testid={`knowledge-workspace-item-${item.versioned_projection_id}`}
          >
            <div className="flex items-center gap-2 bg-white p-2.5 dark:bg-gray-900">
              <button
                type="button"
                onClick={() => void toggle(item)}
                className="flex min-w-0 flex-1 items-center gap-2 text-left"
                aria-expanded={expanded}
                data-testid={`kb-row-${item.representative_resource_id || item.versioned_projection_id}`}
              >
                <BookOpen size={14} className="shrink-0 text-amber-500" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-gray-900 dark:text-white">
                    {itemTitle(item)}
                  </span>
                  <span className="block truncate text-[10px] text-gray-400">
                    {item.canonical_unique_resource_id}
                  </span>
                </span>
                {source && (
                  <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-600 dark:bg-slate-800 dark:text-slate-200">
                    from {source}
                  </span>
                )}
              </button>
              <div className="flex flex-wrap items-center justify-end gap-1">
                <span className="rounded bg-indigo-50 px-1.5 py-0.5 text-[9px] text-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-200">
                  {item.resource_version || 'legacy'}
                </span>
                {item.inherited && (
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-600 dark:bg-slate-800 dark:text-slate-200">inherited</span>
                )}
                {item.grandfathered && (
                  <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[9px] text-amber-800 dark:bg-amber-950/50 dark:text-amber-200">grandfathered</span>
                )}
                {item.stale && (
                  <span className="rounded bg-orange-100 px-1.5 py-0.5 text-[9px] text-orange-800 dark:bg-orange-950/50 dark:text-orange-200">stale</span>
                )}
                {item.superseded && (
                  <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[9px] text-gray-600 dark:bg-gray-700 dark:text-gray-200">superseded</span>
                )}
                <button
                  type="button"
                  onClick={() => void download(item)}
                  className="p-1 text-gray-400 hover:text-emerald-600"
                  aria-label={`Download ${itemTitle(item)}`}
                  data-testid={`kb-download-${item.representative_resource_id || item.versioned_projection_id}`}
                >
                  <Download size={13} />
                </button>
                {deletable && (
                  <button
                    type="button"
                    onClick={() => void remove(item)}
                    disabled={deletingId === item.versioned_projection_id}
                    className="p-1 text-gray-400 hover:text-red-500 disabled:opacity-50"
                    aria-label={`Delete ${itemTitle(item)}`}
                  >
                    {deletingId === item.versioned_projection_id
                      ? <Loader2 size={13} className="animate-spin" />
                      : <Trash2 size={13} />}
                  </button>
                )}
                <button type="button" onClick={() => void toggle(item)} className="p-1 text-gray-400" aria-label={expanded ? 'Collapse detail' : 'Expand detail'}>
                  {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </button>
              </div>
            </div>
            {expanded && (
              <div className="space-y-2 border-t border-gray-100 bg-gray-50/60 px-3 py-2 dark:border-gray-700 dark:bg-gray-950/40">
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-gray-500 dark:text-gray-400">
                  {source && <span>Source: {source}</span>}
                  <span>{item.physical_attachments.length || 1} physical attachment(s)</span>
                  <span>{item.relevance_links.length} relevance link(s)</span>
                </div>
                {item.relevance_links.length > 0 && (
                  <div className="flex flex-wrap gap-1" aria-label="Relevance links">
                    {item.relevance_links.map((link, index) => (
                      <span key={`${relevanceLabel(link)}-${index}`} className="rounded bg-violet-100 px-1.5 py-0.5 text-[10px] text-violet-700 dark:bg-violet-950/50 dark:text-violet-200">
                        {relevanceLabel(link)}
                      </span>
                    ))}
                  </div>
                )}
                {detailLoadingIds.has(item.versioned_projection_id) ? (
                  <div className="flex items-center gap-2 py-3 text-xs text-gray-500">
                    <Loader2 size={13} className="animate-spin" />
                    Loading detail…
                  </div>
                ) : body ? (
                  <div className="max-h-80 overflow-y-auto rounded-lg bg-white p-3 dark:bg-gray-900">
                    <MarkdownContent content={body} className="text-xs" />
                  </div>
                ) : (
                  <div className="rounded-lg bg-white p-3 text-xs text-gray-500 dark:bg-gray-900 dark:text-gray-400">
                    <p>{bodyOmissionLabel(detail.body_omitted_reason)}</p>
                    {(detail.body_ref || item.body_ref) && (
                      <button
                        type="button"
                        onClick={() => void copyBodyReference(item)}
                        className="mt-2 inline-flex items-center gap-1 font-medium text-violet-600 hover:underline dark:text-violet-300"
                      >
                        <Copy size={12} />
                        Copy source reference
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </article>
        );
      })}

      {!loading && nextCursor && (
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={loadingMore}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-gray-200 py-2 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-900"
        >
          {loadingMore && <Loader2 size={14} className="animate-spin" />}
          Load more
        </button>
      )}
    </section>
  );
}
