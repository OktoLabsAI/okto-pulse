import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, CircleSlash, Database, GitBranch, Monitor, RefreshCw, RotateCcw } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type {
  ResourceGateEntityType,
  ResourceGateResource,
  ResourceGateResourceType,
  ResourceGateSummary as ResourceGateSummaryType,
} from '@/types';

interface ResourceGateSummaryProps {
  boardId: string;
  entityType: ResourceGateEntityType;
  entityId: string;
  compact?: boolean;
  onChanged?: (summary: ResourceGateSummaryType) => void;
}

const RESOURCE_LABELS: Record<ResourceGateResourceType, string> = {
  architecture: 'Architecture',
  mockup: 'Mockup',
  knowledge_base: 'Knowledge Base',
};

const RESOURCE_ICONS: Record<ResourceGateResourceType, ReactNode> = {
  architecture: <GitBranch size={15} />,
  mockup: <Monitor size={15} />,
  knowledge_base: <Database size={15} />,
};

const STATE_META: Record<ResourceGateResource['state'], { label: string; badge: string; icon: ReactNode }> = {
  provided: {
    label: 'Provided',
    badge: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/35 dark:text-emerald-300',
    icon: <CheckCircle2 size={13} />,
  },
  not_applicable: {
    label: 'N/A',
    badge: 'bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-200',
    icon: <CircleSlash size={13} />,
  },
  missing: {
    label: 'Missing',
    badge: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
    icon: <AlertTriangle size={13} />,
  },
};

function resourceCounts(resource: ResourceGateResource): string {
  const direct = resource.direct_count || 0;
  const inherited = resource.inherited_count || 0;
  if (!direct && !inherited) return 'No resources linked';
  const parts = [];
  if (direct) parts.push(`${direct} direct`);
  if (inherited) parts.push(`${inherited} inherited`);
  return parts.join(' + ');
}

function naDescription(resource: ResourceGateResource): string | null {
  const mark = resource.na_mark;
  if (!mark) return null;
  if (mark.justification) return mark.justification;
  return 'Marked as not applicable';
}

export function ResourceGateSummary({
  boardId,
  entityType,
  entityId,
  compact = false,
  onChanged,
}: ResourceGateSummaryProps) {
  const api = useDashboardApi();
  const [summary, setSummary] = useState<ResourceGateSummaryType | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyType, setBusyType] = useState<ResourceGateResourceType | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getResourceGateSummary(boardId, entityType, entityId);
      setSummary(data);
      onChanged?.(data);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to load Resource Gate');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [boardId, entityType, entityId]);

  const byType = useMemo(() => {
    const map = new Map<ResourceGateResourceType, ResourceGateResource>();
    summary?.resources.forEach((resource) => map.set(resource.resource_type, resource));
    return map;
  }, [summary]);

  const markNotApplicable = async (resourceType: ResourceGateResourceType) => {
    setBusyType(resourceType);
    try {
      const result = await api.markResourceNotApplicable(boardId, entityType, entityId, {
        resource_type: resourceType,
        source_channel: 'ui',
        justification: drafts[resourceType]?.trim() || undefined,
      });
      setSummary(result.summary);
      onChanged?.(result.summary);
      setDrafts((current) => ({ ...current, [resourceType]: '' }));
      toast.success(`${RESOURCE_LABELS[resourceType]} marked N/A`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to mark N/A');
    } finally {
      setBusyType(null);
    }
  };

  const clearNotApplicable = async (resourceType: ResourceGateResourceType) => {
    setBusyType(resourceType);
    try {
      const result = await api.clearResourceNotApplicable(boardId, entityType, entityId, resourceType, {
        source_channel: 'ui',
        reason: 'Cleared from dashboard Resource Gate summary',
      });
      setSummary(result.summary);
      onChanged?.(result.summary);
      toast.success(`${RESOURCE_LABELS[resourceType]} N/A cleared`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to clear N/A');
    } finally {
      setBusyType(null);
    }
  };

  if (loading && !summary) {
    return (
      <section className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50/80 dark:bg-gray-900/30 p-3">
        <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
          <RefreshCw size={14} className="animate-spin" />
          Loading Resource Gate...
        </div>
      </section>
    );
  }

  const resources: ResourceGateResourceType[] = ['architecture', 'mockup', 'knowledge_base'];

  return (
    <section data-testid="resource-gate-summary" className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900/30">
      <div className="flex items-center justify-between gap-3 border-b border-gray-100 dark:border-gray-700 px-3 py-2.5">
        <div>
          <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-100">Resource Gate</h4>
          <p className="text-xs text-gray-500 dark:text-gray-400">Architecture, Mockup and Knowledge Base readiness</p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          title="Refresh Resource Gate"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      <div className={compact ? 'divide-y divide-gray-100 dark:divide-gray-800' : 'grid gap-2 p-3'}>
        {resources.map((resourceType) => {
          const resource = byType.get(resourceType);
          if (!resource) return null;
          const meta = STATE_META[resource.state];
          const disabled = busyType === resourceType;
          return (
            <div
              key={resourceType}
              data-testid={`resource-gate-row-${resourceType}`}
              className={`${compact ? 'px-3 py-3' : 'rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50/70 dark:bg-gray-950/30 p-3'} space-y-2`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-2 min-w-0">
                  <span className="mt-0.5 text-gray-500 dark:text-gray-400">{RESOURCE_ICONS[resourceType]}</span>
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-gray-800 dark:text-gray-100">{RESOURCE_LABELS[resourceType]}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400">{resourceCounts(resource)}</div>
                  </div>
                </div>
                <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${meta.badge}`}>
                  {meta.icon}
                  {meta.label}
                </span>
              </div>

              {resource.state === 'missing' && (
                <div className="flex flex-col gap-2 sm:flex-row">
                  <input
                    value={drafts[resourceType] || ''}
                    onChange={(event) => setDrafts((current) => ({ ...current, [resourceType]: event.target.value }))}
                    placeholder="Optional N/A reason"
                    className="min-w-0 flex-1 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 px-2.5 py-1.5 text-xs text-gray-800 dark:text-gray-100 placeholder:text-gray-400"
                  />
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => void markNotApplicable(resourceType)}
                    className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-600 disabled:opacity-50"
                  >
                    <CircleSlash size={13} />
                    Mark N/A
                  </button>
                </div>
              )}

              {resource.state === 'not_applicable' && (
                <div className="flex items-start justify-between gap-3 rounded-lg bg-slate-100 dark:bg-slate-800/80 px-2.5 py-2">
                  <p className="text-xs text-slate-600 dark:text-slate-300">{naDescription(resource)}</p>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => void clearNotApplicable(resourceType)}
                    className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300 disabled:opacity-50"
                  >
                    <RotateCcw size={13} />
                    Clear
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
