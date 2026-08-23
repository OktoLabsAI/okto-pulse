import { useEffect, useState, useMemo } from 'react';
import {
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
  Cell,
} from 'recharts';
import {
  BookOpen,
  Lightbulb,
  FileText,
  CheckSquare,
  Target,
  AlertTriangle,
  Bug,
  Clock,
  Download,
  HelpCircle,
  RefreshCw,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { PulseLoader } from '@/components/shared/PulseLoader';
import { CanonicalCoveragePanel } from './CanonicalCoveragePanel';
import { DeliveryForecastPanel } from './DeliveryForecastPanel';
import { FlowHealthSummary } from './FlowHealthSummary';
import { KgEffectivenessPanel } from './KgEffectivenessPanel';
import { mergeBoardKgAnalyticsPages } from './kgEffectivenessPagination';
import type { CanonicalCoverageQueryState } from './canonicalCoverageQueryState';
import type {
  BoardKgAnalyticsResponse,
  CanonicalCoverageResponse,
  FlowHealthResponse,
} from './analyticsCanonicalTypes';
import type {
  DeliveryForecastResponse,
  SprintAnalyticsResponse,
} from './analyticsDeliveryTypes';

// ---------------------------------------------------------------------------
// Types matching backend responses
// ---------------------------------------------------------------------------

interface FunnelData {
  stories?: number;
  stories_converted?: number;
  story_conversion_pct?: number;
  story_ideation_links?: number;
  story_status_breakdown?: Record<string, number>;
  stories_by_topic?: Array<{ topic_id: string; topic: string; stories: number }>;
  ideations: number;
  ideations_done: number;
  refinements: number;
  specs: number;
  specs_done: number;
  sprints: number;
  cards: number;
  cards_impl: number;
  cards_test: number;
  cards_bug: number;
  done: number;
  rules_count: number;
  contracts_count: number;
  specs_with_rules: number;
  specs_with_contracts: number;
  spec_status_breakdown: Record<string, number>;
  sprint_status_breakdown: Record<string, number>;
  card_status_breakdown: Record<string, number>;
  bugs_total: number;
  bugs_open: number;
  bugs_by_severity: { critical: number; major: number; minor: number };
  avg_cycle_hours: number | null;
}

interface QualityPoint {
  card_id: string;
  title: string;
  card_type?: string;
  completeness: number;
  drift: number;
  confidence?: number;
  outcome?: string;
}

interface QualityResponse {
  conclusion_reported: QualityPoint[];
  validation_reported: QualityPoint[];
}

interface ValidationsResponse {
  spec_validation_gate: {
    total_submitted: number;
    total_success: number;
    total_failed: number;
    success_rate: number | null;
    avg_attempts_per_spec: number | null;
    avg_scores: { completeness: number | null; assertiveness: number | null; ambiguity: number | null };
    rejection_reasons: { completeness_below: number; assertiveness_below: number; ambiguity_above: number; reject_recommendation: number };
    specs_with_validation: number;
    per_spec: Array<{
      spec_id: string;
      title: string;
      status: string;
      attempts: number;
      last_outcome: string | null;
      last_completeness: number | null;
      last_assertiveness: number | null;
      last_ambiguity: number | null;
      success_count: number;
      failed_count: number;
      rejection_reasons: Record<string, number>;
      current_validation_id: string | null;
    }>;
  };
  task_validation_gate: {
    total_submitted: number;
    total_success: number;
    total_failed: number;
    success_rate: number | null;
    avg_attempts_per_card: number | null;
    first_pass_rate: number | null;
    avg_scores: { confidence: number | null; completeness: number | null; drift: number | null };
    rejection_reasons: { confidence_below: number; completeness_below: number; drift_above: number; reject_recommendation: number };
    cards_with_validation: number;
    per_card: Array<{
      card_id: string;
      title: string;
      card_type: string;
      spec_id: string | null;
      sprint_id: string | null;
      status: string;
      attempts: number;
      last_outcome: string | null;
      last_confidence: number | null;
      last_completeness: number | null;
      last_drift: number | null;
      success_count: number;
      failed_count: number;
      rejection_reasons: Record<string, number>;
    }>;
  };
  spec_evaluation: {
    total_submitted: number;
    approve_rate: number | null;
    avg_overall_score: number | null;
    specs_with_evaluation: number;
  };
  sprint_evaluation: {
    total_submitted: number;
    approve_rate: number | null;
    avg_overall_score: number | null;
    sprints_with_evaluation: number;
  };
}

interface SpecReadinessResponse {
  query_fingerprint: string;
  as_of: string;
  specs: Array<{
    spec_id: string;
    edition: number;
    validation: {
      state: string;
      measures: {
        confidence: number | null;
        clarity: number | null;
        assertiveness: number | null;
        decidability: number | null;
        ambiguity: number | null;
      };
      attempts: number;
      lifecycle_ready: boolean | null;
    };
    lifecycle: { spec_pending_validation: boolean | null };
  }>;
}

interface PolicyResourceReadinessResponse {
  query_fingerprint: string;
  as_of: string;
  specs: Array<{
    spec_id: string;
    edition: number;
    policy: {
      totals: {
        native_pass: number;
        blocking_pending: number;
        blocking_failed: number;
        stale: number;
        inconsistent: number;
      };
    };
    resources: {
      l1: Array<{ resource_type: string; state: string }>;
      l2: Array<{
        resource_type: string;
        state: string;
        covered_only_by_cancelled_task: boolean | null;
      }>;
      covered_only_by_cancelled_task: number;
    };
  }>;
}

interface AgentRow {
  actor_id: string;
  actor_name: string;
  total_cards: number;
  done_cards: number;
  avg_completeness: number | null;
  avg_drift: number | null;
}

interface EntityItem {
  id: string;
  title: string;
  status: string | null;
  // ideation
  refinement_count?: number;
  spec_count?: number;
  complexity?: string | null;
  // spec
  ac_count?: number;
  scenario_count?: number;
  card_count?: number;
  rules_count?: number;
  contracts_count?: number;
  // card
  completeness?: number | null;
  drift?: number | null;
  is_test?: boolean;
}

interface EntityListResponse {
  total: number;
  offset: number;
  limit: number;
  items: EntityItem[];
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface BoardDashboardProps {
  boardId: string;
  from: string;
  to: string;
  onSelectEntity: (type: 'ideation' | 'spec' | 'refinement' | 'sprint' | 'card', id: string, name: string) => void;
  onOpenFlowHealth?: () => void;
  onOpenCanonicalCoverage?: (query: CanonicalCoverageQueryState) => void;
  onOpenKgEffectiveness?: () => void;
  onOpenDeliveryIntelligence?: () => void;
}

// ---------------------------------------------------------------------------
// Color helpers
// ---------------------------------------------------------------------------

function completenessColor(v: number | null): string {
  if (v === null) return 'text-gray-400 dark:text-gray-500';
  if (v >= 90) return 'text-green-600 dark:text-green-400';
  if (v >= 70) return 'text-blue-600 dark:text-blue-400';
  if (v >= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function completenessBg(v: number | null): string {
  if (v === null) return 'bg-gray-100 dark:bg-gray-700';
  if (v >= 90) return 'bg-green-50 dark:bg-green-900/30';
  if (v >= 70) return 'bg-blue-50 dark:bg-blue-900/30';
  if (v >= 50) return 'bg-amber-50 dark:bg-amber-900/30';
  return 'bg-red-50 dark:bg-red-900/30';
}

function driftColor(v: number | null): string {
  if (v === null) return 'text-gray-400 dark:text-gray-500';
  if (v <= 10) return 'text-green-600 dark:text-green-400';
  if (v <= 25) return 'text-blue-600 dark:text-blue-400';
  if (v <= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function driftBg(v: number | null): string {
  if (v === null) return 'bg-gray-100 dark:bg-gray-700';
  if (v <= 10) return 'bg-green-50 dark:bg-green-900/30';
  if (v <= 25) return 'bg-blue-50 dark:bg-blue-900/30';
  if (v <= 50) return 'bg-amber-50 dark:bg-amber-900/30';
  return 'bg-red-50 dark:bg-red-900/30';
}

function formatCycleTime(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

function scatterDotColor(completeness: number, drift: number): string {
  // Green quadrant: high completeness + low drift
  if (completeness >= 70 && drift <= 25) return '#22c55e';
  return '#ef4444';
}

type AnalyticsEntityCatalogKind = 'spec' | 'card';

function analyticsEntityCatalogKey(kind: string, id: string): string {
  return `${kind}:${id}`;
}

// ---------------------------------------------------------------------------
// Skeleton
// ---------------------------------------------------------------------------



function DashboardMetricHelp({ label, description, targetId }: { label: string; description: string; targetId: string }) {
  const openDetail = () => {
    document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <span className="relative group inline-flex">
      <button
        type="button"
        aria-label={`${label} help`}
        title={`${description} Open details.`}
        onClick={openDetail}
        className="inline-flex h-4 w-4 items-center justify-center rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        <HelpCircle className="h-3.5 w-3.5" />
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute right-0 top-5 z-20 w-56 rounded-md border border-gray-200 bg-white px-2 py-1.5 text-[11px] font-normal leading-snug text-gray-600 opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
      >
        {description}
        <span className="mt-1 block font-medium text-blue-600 dark:text-blue-400">Open detail</span>
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Scatter tooltip
// ---------------------------------------------------------------------------

interface ScatterTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: QualityPoint }>;
}

function ScatterTooltipContent({ active, payload }: ScatterTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const data = payload[0].payload;
  return (
    <div className="bg-gray-800 text-white text-xs px-3 py-2 rounded-lg shadow-lg max-w-xs">
      <p className="font-medium truncate">{data.title}</p>
      <p className="text-gray-300 mt-0.5">
        Completeness: {data.completeness}% &middot; Drift: {data.drift}%
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Entity table tab type
// ---------------------------------------------------------------------------

type EntityTab = 'spec' | 'ideation' | 'card';

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function BoardDashboard({
  boardId,
  from,
  to,
  onSelectEntity,
  onOpenFlowHealth = () => {},
  onOpenCanonicalCoverage,
  onOpenKgEffectiveness,
  onOpenDeliveryIntelligence,
}: BoardDashboardProps) {
  const api = useDashboardApi();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [funnel, setFunnel] = useState<FunnelData | null>(null);
  const [quality, setQuality] = useState<QualityPoint[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [validations, setValidations] = useState<ValidationsResponse | null>(null);
  const [sprints, setSprints] = useState<SprintAnalyticsResponse | null>(null);
  const [deliveryForecast, setDeliveryForecast] = useState<DeliveryForecastResponse | null>(null);
  const [deliveryForecastLoading, setDeliveryForecastLoading] = useState(true);
  const [deliveryForecastError, setDeliveryForecastError] = useState<string | null>(null);
  const [deliveryForecastExportError, setDeliveryForecastExportError] = useState<string | null>(null);
  const [deliveryForecastRetry, setDeliveryForecastRetry] = useState(0);
  const [deliveryForecastExporting, setDeliveryForecastExporting] = useState(false);
  const [kgAnalytics, setKgAnalytics] = useState<BoardKgAnalyticsResponse | null>(null);
  const [kgLoading, setKgLoading] = useState(true);
  const [kgError, setKgError] = useState<string | null>(null);
  const [kgExportError, setKgExportError] = useState<string | null>(null);
  const [kgRetry, setKgRetry] = useState(0);
  const [kgExporting, setKgExporting] = useState(false);
  const [canonicalCoverage, setCanonicalCoverage] = useState<CanonicalCoverageResponse | null>(null);
  const [canonicalCoverageError, setCanonicalCoverageError] = useState<string | null>(null);
  const [canonicalCoverageExportError, setCanonicalCoverageExportError] = useState<string | null>(null);
  const [canonicalCoverageLoading, setCanonicalCoverageLoading] = useState(true);
  const [canonicalCoverageRetry, setCanonicalCoverageRetry] = useState(0);
  const [canonicalCoverageExporting, setCanonicalCoverageExporting] = useState(false);
  const [flowHealth, setFlowHealth] = useState<FlowHealthResponse | null>(null);
  const [flowHealthError, setFlowHealthError] = useState<string | null>(null);
  const [flowHealthLoading, setFlowHealthLoading] = useState(true);
  const [flowHealthRetry, setFlowHealthRetry] = useState(0);
  const [readiness, setReadiness] = useState<{
    spec: SpecReadinessResponse;
    policyResource: PolicyResourceReadinessResponse;
  } | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [readinessExportError, setReadinessExportError] = useState<{ kind: 'spec' | 'policy-resource'; message: string } | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(true);
  const [readinessRetry, setReadinessRetry] = useState(0);
  const [readinessExporting, setReadinessExporting] = useState<'spec' | 'policy-resource' | null>(null);
  const [entities, setEntities] = useState<Record<EntityTab, EntityListResponse | null>>({
    spec: null,
    ideation: null,
    card: null,
  });
  const [entityTitleCatalog, setEntityTitleCatalog] = useState<Record<string, string>>({});

  const [activeTab, setActiveTab] = useState<EntityTab>('spec');
  const [entitySearch, setEntitySearch] = useState('');
  const [entityPage, setEntityPage] = useState(0);
  const PAGE_SIZE = 50;

  // Load data
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([
      api.getBoardAnalyticsFunnel(boardId, from, to),
      api.getBoardAnalyticsQuality(boardId, from, to),
      api.getBoardAnalyticsAgents(boardId, from, to),
      api.getBoardAnalyticsValidations(boardId, from, to),
      api.getBoardAnalyticsSprints(boardId, from, to),
    ])
      .then(([funnelRes, qualityRes, agentsRes, validationsRes, sprintsRes]) => {
        if (cancelled) return;
        setFunnel(funnelRes as FunnelData);
        // Quality endpoint now returns {conclusion_reported, validation_reported}.
        // Prefer validation data; fall back to conclusions when absent.
        const q = qualityRes as QualityResponse;
        setQuality(q.validation_reported.length > 0 ? q.validation_reported : q.conclusion_reported);
        setAgents(agentsRes as AgentRow[]);
        setValidations(validationsRes as ValidationsResponse);
        setSprints(sprintsRes);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load board analytics');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to]);

  // KG Analytics has an independent lifecycle: failure here must not hide the
  // usable funnel, coverage, validation or Sprint panels.
  useEffect(() => {
    let cancelled = false;
    setKgLoading(true);
    setKgError(null);
    const loadAllKgPages = async () => {
      const pages: BoardKgAnalyticsResponse[] = [];
      const seenCursors = new Set<string>();
      let cursor: string | null = null;
      do {
        const payload = await api.getBoardKgAnalytics(boardId, from, to, {
          cursor,
          limit: 500,
        });
        if (!payload) return null;
        pages.push(payload);
        const nextCursor = payload.next_cursor;
        if (nextCursor && seenCursors.has(nextCursor)) {
          throw new Error('KG analytics pagination returned a repeated cursor.');
        }
        if (nextCursor) seenCursors.add(nextCursor);
        cursor = nextCursor;
      } while (cursor && !cancelled);
      return mergeBoardKgAnalyticsPages(pages);
    };
    loadAllKgPages()
      .then((payload) => {
        if (!cancelled) setKgAnalytics(payload);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setKgError(err instanceof Error ? err.message : 'Failed to load KG analytics');
        }
      })
      .finally(() => {
        if (!cancelled) setKgLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, kgRetry]);

  useEffect(() => {
    let cancelled = false;
    setDeliveryForecastLoading(true);
    setDeliveryForecastError(null);
    api.getBoardDeliveryForecast(boardId, from, to)
      .then((payload) => {
        if (!cancelled) setDeliveryForecast(payload);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setDeliveryForecast(null);
          setDeliveryForecastError(err instanceof Error ? err.message : 'Delivery forecast is unavailable.');
        }
      })
      .finally(() => {
        if (!cancelled) setDeliveryForecastLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, deliveryForecastRetry]);

  useEffect(() => {
    let cancelled = false;
    setCanonicalCoverageLoading(true);
    setCanonicalCoverageError(null);
    api.getCanonicalBoardCoverage(boardId, from, to)
      .then((payload) => {
        if (!cancelled) setCanonicalCoverage(payload);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setCanonicalCoverageError(err instanceof Error ? err.message : 'Failed to load canonical coverage');
        }
      })
      .finally(() => {
        if (!cancelled) setCanonicalCoverageLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, canonicalCoverageRetry]);

  useEffect(() => {
    let cancelled = false;
    setFlowHealthLoading(true);
    setFlowHealthError(null);
    api.getBoardFlowHealth(boardId, from, to)
      .then((payload) => {
        if (!cancelled) setFlowHealth(payload);
      })
      .catch((err: unknown) => {
        if (!cancelled) setFlowHealthError(err instanceof Error ? err.message : 'Failed to load Flow Health');
      })
      .finally(() => {
        if (!cancelled) setFlowHealthLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, flowHealthRetry]);

  useEffect(() => {
    let cancelled = false;
    setReadinessLoading(true);
    setReadinessError(null);
    Promise.all([api.getSpecReadiness(boardId, from, to), api.getPolicyResourceReadiness(boardId, from, to)])
      .then(([spec, policyResource]) => {
        if (!cancelled) {
          setReadiness({
            spec: spec as SpecReadinessResponse,
            policyResource: policyResource as PolicyResourceReadinessResponse
          });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setReadinessError(err instanceof Error ? err.message : 'Failed to load readiness');
      })
      .finally(() => {
        if (!cancelled) setReadinessLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, readinessRetry]);

  // Canonical Flow Health and readiness projections intentionally expose only
  // stable entity identities. Resolve human-readable titles through the
  // paginated analytics catalog and fail soft to those identities when either
  // catalog is unavailable.
  useEffect(() => {
    let cancelled = false;
    const CATALOG_PAGE_SIZE = 200;
    setEntityTitleCatalog({});

    const loadCatalog = async (kind: AnalyticsEntityCatalogKind): Promise<EntityItem[]> => {
      const items: EntityItem[] = [];
      let offset = 0;

      while (!cancelled) {
        const response = await api.getBoardAnalyticsEntities(
          boardId,
          kind,
          undefined,
          undefined,
          offset,
          CATALOG_PAGE_SIZE,
        ) as EntityListResponse;
        if (cancelled) return [];

        const pageItems = Array.isArray(response.items) ? response.items : [];
        items.push(...pageItems);
        const nextOffset = offset + pageItems.length;
        const total = Number.isInteger(response.total) && response.total >= 0
          ? response.total
          : nextOffset;
        if (pageItems.length === 0 || nextOffset <= offset || nextOffset >= total) break;
        offset = nextOffset;
      }

      return items;
    };

    Promise.allSettled([loadCatalog('spec'), loadCatalog('card')]).then((results) => {
      if (cancelled) return;
      const nextCatalog: Record<string, string> = {};
      results.forEach((result, index) => {
        if (result.status !== 'fulfilled') return;
        const kind: AnalyticsEntityCatalogKind = index === 0 ? 'spec' : 'card';
        result.value.forEach((item) => {
          const title = typeof item.title === 'string' ? item.title.trim() : '';
          if (item.id && title) {
            nextCatalog[analyticsEntityCatalogKey(kind, item.id)] = title;
          }
        });
      });
      setEntityTitleCatalog(nextCatalog);
    });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId]);

  // Load entities separately — responds to tab, search, page changes
  useEffect(() => {
    const search = entitySearch || undefined;
    api.getBoardAnalyticsEntities(boardId, activeTab, from, to, entityPage * PAGE_SIZE, PAGE_SIZE, search)
      .then((res) => {
        setEntities((prev) => ({ ...prev, [activeTab]: res as EntityListResponse }));
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId, from, to, activeTab, entitySearch, entityPage]);

  // ---------------------------------------------------------------------------
  // Derived KPI values
  // ---------------------------------------------------------------------------

  const kpis = useMemo(() => {
    if (!funnel) return null;

    const ideationsDonePct = funnel.ideations > 0
      ? Math.round(((funnel.ideations_done || 0) / funnel.ideations) * 100)
      : 0;

    const specsDonePct = funnel.specs > 0
      ? Math.round(((funnel.specs_done || 0) / funnel.specs) * 100)
      : 0;

    const tasksDonePct = funnel.cards > 0
      ? Math.round((funnel.done / funnel.cards) * 100)
      : 0;

    // Avg completude and drift from quality data
    const compVals = quality.map((q) => q.completeness);
    const driftVals = quality.map((q) => q.drift);
    const avgCompleteness = compVals.length > 0
      ? Math.round(compVals.reduce((a, b) => a + b, 0) / compVals.length)
      : null;
    const avgDrift = driftVals.length > 0
      ? Math.round(driftVals.reduce((a, b) => a + b, 0) / driftVals.length)
      : null;

    return {
      stories: funnel.stories || 0,
      storiesConvertedPct: Math.round(funnel.story_conversion_pct || 0),
      ideations: funnel.ideations,
      ideationsDonePct,
      specs: funnel.specs,
      specsDonePct,
      tasks: funnel.cards,
      tasksDonePct,
      avgCompleteness,
      avgDrift,
    };
  }, [funnel, quality]);

  // Sorted entity items for current tab
  const sortedEntities = useMemo(() => {
    const current = entities[activeTab];
    if (!current) return [];
    return [...current.items].sort((a, b) => (a.title || '').localeCompare(b.title || ''));
  }, [entities, activeTab]);

  const exportReadiness = async (kind: 'spec' | 'policy-resource') => {
    if (readinessExporting !== null) return;
    setReadinessExporting(kind);
    setReadinessExportError(null);
    try {
      await api.exportReadinessCsv(boardId, kind, from, to);
    } catch (err) {
      setReadinessExportError({
        kind,
        message: err instanceof Error ? err.message : 'Readiness export failed',
      });
    } finally {
      setReadinessExporting(null);
    }
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  if (loading) return <PulseLoader size="lg" label="Loading board analytics..." className="py-20" />;

  if (error) {
    return (
      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-6 text-center">
        <AlertTriangle className="w-6 h-6 text-red-500 mx-auto mb-2" />
        <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
      </div>
    );
  }

  if (!kpis) return null;

  const tabLabels: Record<EntityTab, string> = { spec: 'Specs', ideation: 'Ideations', card: 'Tasks' };
  const readinessSummary = readiness
    ? {
        specs: readiness.spec.specs.length,
        current: readiness.spec.specs.filter((item) => item.validation.state === 'current').length,
        ready: readiness.spec.specs.filter((item) => item.validation.lifecycle_ready === true).length,
        pending: readiness.spec.specs.filter((item) => item.lifecycle.spec_pending_validation === true).length,
        nativePass: readiness.policyResource.specs.reduce((total, item) => total + item.policy.totals.native_pass, 0),
        policyPending: readiness.policyResource.specs.reduce((total, item) => total + item.policy.totals.blocking_pending, 0),
        policyFailed: readiness.policyResource.specs.reduce((total, item) => total + item.policy.totals.blocking_failed, 0),
        resourcesProvided: readiness.policyResource.specs.reduce((total, item) => total + item.resources.l1.filter((resource) => resource.state === 'provided').length, 0),
        resourcesMissing: readiness.policyResource.specs.reduce((total, item) => total + item.resources.l1.filter((resource) => resource.state === 'missing').length, 0),
        cancelledOnly: readiness.policyResource.specs.reduce((total, item) => total + item.resources.covered_only_by_cancelled_task, 0)
      }
    : null;

  const governedAnalyticsSections = (
    <>
      <KgEffectivenessPanel
        data={kgAnalytics}
        loading={kgLoading}
        error={kgError}
        exportError={kgExportError}
        exporting={kgExporting}
        from={from}
        to={to}
        mode="compact"
        onOpenFullView={onOpenKgEffectiveness}
        onRetry={() => setKgRetry((value) => value + 1)}
        onExport={async () => {
          if (kgExporting) return;
          setKgExporting(true);
          setKgExportError(null);
          try {
            await api.exportBoardKgAnalyticsCsv(boardId, from, to);
          } catch (err) {
            setKgExportError(err instanceof Error ? err.message : 'KG effectiveness export failed');
          } finally {
            setKgExporting(false);
          }
        }}
      />

      <CanonicalCoveragePanel
        data={canonicalCoverage}
        loading={canonicalCoverageLoading}
        error={canonicalCoverageError}
        exportError={canonicalCoverageExportError}
        exporting={canonicalCoverageExporting}
        from={from}
        to={to}
        specTitles={Object.fromEntries(
          Object.entries(entityTitleCatalog)
            .filter(([key]) => key.startsWith('spec:'))
            .map(([key, value]) => [key.slice('spec:'.length), value]),
        )}
        onRetry={() => setCanonicalCoverageRetry((value) => value + 1)}
        onExport={async () => {
          if (canonicalCoverageExporting) return;
          setCanonicalCoverageExporting(true);
          setCanonicalCoverageExportError(null);
          try {
            await api.exportCanonicalBoardCoverageCsv(boardId, from, to);
          } catch (err) {
            setCanonicalCoverageExportError(err instanceof Error ? err.message : 'Coverage export failed');
          } finally {
            setCanonicalCoverageExporting(false);
          }
        }}
        onOpenSpec={(specId, title) => onSelectEntity('spec', specId, title)}
        onOpenFullView={onOpenCanonicalCoverage}
        viewMode="summary"
      />

      <FlowHealthSummary
        data={flowHealth}
        loading={flowHealthLoading}
        error={flowHealthError}
        from={from}
        to={to}
        onRetry={() => setFlowHealthRetry((value) => value + 1)}
        onOpenFullView={onOpenFlowHealth}
        subjectTitles={entityTitleCatalog}
        onOpenSubject={(type, id, title) => {
          const normalized = type === 'task' ? 'card' : type;
          if (normalized === 'spec' || normalized === 'card' || normalized === 'ideation' || normalized === 'refinement') {
            onSelectEntity(normalized, id, title);
          }
        }}
      />

      <section aria-labelledby="readiness-heading" className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 id="readiness-heading" className="text-sm font-semibold text-gray-700 dark:text-gray-200">
              Spec &amp; Policy Readiness
            </h3>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Current-edition validation, native policy outcomes and governed L1/L2 resource evidence.</p>
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            {(['spec', 'policy-resource'] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                disabled={readinessExporting !== null || readinessLoading || readiness === null}
                onClick={() => void exportReadiness(kind)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md border border-gray-200 dark:border-gray-600 disabled:opacity-50"
              >
                <Download className="w-3.5 h-3.5" />
                {readinessExporting === kind ? 'Exporting…' : kind === 'spec' ? 'Spec CSV' : 'Policy/resource CSV'}
              </button>
            ))}
          </div>
        </div>
        {readinessLoading && (
          <p className="mt-4 text-xs text-gray-500" role="status">
            Loading readiness…
          </p>
        )}
        {!readinessLoading && readinessError && (
          <div className="mt-4 flex items-center justify-between rounded-md bg-red-50 dark:bg-red-900/20 px-3 py-2" role="alert">
            <span className="text-xs text-red-700 dark:text-red-300">{readinessError}</span>
            <button type="button" onClick={() => setReadinessRetry((value) => value + 1)} className="inline-flex items-center gap-1 text-xs text-red-700 dark:text-red-300">
              <RefreshCw className="w-3.5 h-3.5" /> Retry
            </button>
          </div>
        )}
        {readinessExportError && (
          <div className="mt-4 flex items-center justify-between rounded-md bg-red-50 px-3 py-2 dark:bg-red-900/20" role="alert">
            <span className="text-xs text-red-700 dark:text-red-300">CSV export failed: {readinessExportError.message}</span>
            <button type="button" disabled={readinessExporting !== null} onClick={() => void exportReadiness(readinessExportError.kind)} className="inline-flex items-center gap-1 text-xs font-semibold text-red-700 disabled:opacity-50 dark:text-red-300">
              <Download className="h-3.5 w-3.5" /> Retry export
            </button>
          </div>
        )}
        {!readinessLoading && !readinessError && readiness && readinessSummary && (
          <div className="mt-4 space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3" aria-label="Readiness facts">
              {[
                ['Ready specs', `${readinessSummary.ready}/${readinessSummary.specs}`],
                ['Pending validation', readinessSummary.pending],
                ['Native policy pass', readinessSummary.nativePass],
                ['Resources provided', readinessSummary.resourcesProvided],
                ['Resources missing', readinessSummary.resourcesMissing]
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-md bg-gray-50 dark:bg-gray-900/40 p-3">
                  <p className="text-[10px] uppercase text-gray-400">{label}</p>
                  <p className="mt-1 text-lg font-semibold text-gray-800 dark:text-gray-100">{value}</p>
                </div>
              ))}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-[10px] uppercase text-gray-400">
                    <th className="py-2">Spec</th>
                    <th>Edition</th>
                    <th>Validation</th>
                    <th>Attempts</th>
                    <th>Policy</th>
                    <th>Resources L1</th>
                    <th>Cancelled-only</th>
                  </tr>
                </thead>
                <tbody>
                  {readiness.spec.specs.map((spec) => {
                    const policy = readiness.policyResource.specs.find((item) => item.spec_id === spec.spec_id && item.edition === spec.edition);
                    const provided = policy?.resources.l1.filter((item) => item.state === 'provided').length ?? 0;
                    const specTitle = entityTitleCatalog[analyticsEntityCatalogKey('spec', spec.spec_id)];
                    return (
                      <tr key={`${spec.spec_id}:${spec.edition}`} className="border-b border-gray-100 dark:border-gray-700/50">
                        <td className="py-2 font-medium" title={specTitle ? spec.spec_id : undefined}>
                          {specTitle ?? spec.spec_id}
                        </td>
                        <td>{spec.edition}</td>
                        <td>{spec.validation.lifecycle_ready === true ? 'ready' : spec.validation.state}</td>
                        <td>{spec.validation.attempts}</td>
                        <td>{policy ? `${policy.policy.totals.native_pass} pass / ${policy.policy.totals.blocking_pending} pending / ${policy.policy.totals.blocking_failed} failed` : 'unavailable'}</td>
                        <td>{policy ? `${provided}/${policy.resources.l1.length}` : '—'}</td>
                        <td>{policy?.resources.covered_only_by_cancelled_task ?? '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="text-[10px] text-gray-400">
              current {readinessSummary.current} · policy pending {readinessSummary.policyPending} · policy failed {readinessSummary.policyFailed} · cancelled-only resources {readinessSummary.cancelledOnly} · as_of {readiness.spec.as_of} · query {readiness.spec.query_fingerprint.slice(0, 12)}…
            </p>
          </div>
        )}
      </section>
    </>
  );

  return (
    <div className="space-y-6">

      {/* ------------------------------------------------------------------ */}
      {/* KPI Cards                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-9 gap-4">
        {/* Stories */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <BookOpen className="w-4 h-4 text-blue-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Stories</span>
            <DashboardMetricHelp
              label="Stories"
              description="Stories are intake items for requirements. The conversion badge shows how many became ideations in this board."
              targetId="analytics-entity-drilldown"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">{kpis.stories}</span>
          <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300">
            {kpis.storiesConvertedPct}% conv
          </span>
        </div>

        {/* Ideations */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <Lightbulb className="w-4 h-4 text-amber-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Ideations</span>
            <DashboardMetricHelp
              label="Ideations"
              description="Ideations represent explored problem spaces. The done badge shows ideations completed in the board lifecycle."
              targetId="analytics-entity-drilldown"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">{kpis.ideations}</span>
          <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300">
            {kpis.ideationsDonePct}% done
          </span>
        </div>

        {/* Specs */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <FileText className="w-4 h-4 text-blue-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Specs</span>
            <DashboardMetricHelp
              label="Specs"
              description="Specs are validated execution contracts. The done badge shows specs fully delivered."
              targetId="analytics-entity-drilldown"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">{kpis.specs}</span>
          <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300">
            {kpis.specsDonePct}% done
          </span>
        </div>

        {/* Tasks */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <CheckSquare className="w-4 h-4 text-violet-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Tasks</span>
            <DashboardMetricHelp
              label="Tasks"
              description="Tasks count implementation, test, and bug cards. The done badge shows completed cards."
              targetId="analytics-entity-drilldown"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">{kpis.tasks}</span>
          <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-violet-100 dark:bg-violet-900/40 text-violet-700 dark:text-violet-300">
            {kpis.tasksDonePct}% done
          </span>
        </div>

        {/* Completeness */}
        <div className={`rounded-lg border border-gray-200 dark:border-gray-700 p-4 ${completenessBg(kpis.avgCompleteness)}`}>
          <div className="flex items-center gap-1.5 mb-1">
            <Target className="w-4 h-4 text-gray-400" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Completeness</span>
            <DashboardMetricHelp
              label="Completeness"
              description="Average validator-reported completeness for validated task work in this date range."
              targetId="analytics-quality-scatter"
            />
          </div>
          <span className={`text-2xl font-bold ${completenessColor(kpis.avgCompleteness)}`}>
            {kpis.avgCompleteness !== null ? `${kpis.avgCompleteness}%` : '--'}
          </span>
        </div>

        {/* Drift */}
        <div className={`rounded-lg border border-gray-200 dark:border-gray-700 p-4 ${driftBg(kpis.avgDrift)}`}>
          <div className="flex items-center gap-1.5 mb-1">
            <AlertTriangle className="w-4 h-4 text-gray-400" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Drift</span>
            <DashboardMetricHelp
              label="Drift"
              description="Average validator-reported drift from the intended task scope. Lower drift is better."
              targetId="analytics-quality-scatter"
            />
          </div>
          <span className={`text-2xl font-bold ${driftColor(kpis.avgDrift)}`}>
            {kpis.avgDrift !== null ? `${kpis.avgDrift}%` : '--'}
          </span>
        </div>

        {/* Bugs */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <Bug className="w-4 h-4 text-red-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Bugs</span>
            <DashboardMetricHelp
              label="Bugs"
              description="Bug cards tracked on this board. The open badge shows bugs not yet completed."
              targetId="analytics-entity-drilldown"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">{funnel?.bugs_total ?? 0}</span>
          <span className={`ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium ${
            (funnel?.bugs_open ?? 0) > 0
              ? 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
              : 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
          }`}>
            {funnel?.bugs_open ?? 0} open
          </span>
        </div>

        {/* Cycle Time */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <Clock className="w-4 h-4 text-gray-400" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Cycle Time</span>
            <DashboardMetricHelp
              label="Cycle Time"
              description="Average elapsed time for done tasks in the selected date range."
              targetId="analytics-entity-drilldown"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">
            {funnel?.avg_cycle_hours != null ? formatCycleTime(funnel.avg_cycle_hours) : '--'}
          </span>
          <p className="text-[10px] text-gray-400 dark:text-gray-500 mt-0.5">avg done tasks</p>
        </div>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Canonical coverage lives above; this chart is quality-only.         */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-1 gap-4">
        {/* Scatter Completeness x Drift */}
        <div id="analytics-quality-scatter" className="scroll-mt-20 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-4">
            Completeness x Drift
          </h3>
          {quality.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <ScatterChart margin={{ top: 10, right: 10, bottom: 10, left: 0 }}>
                <XAxis
                  type="number"
                  dataKey="completeness"
                  name="Completeness"
                  domain={[0, 100]}
                  tick={{ fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                  label={{ value: 'Completeness %', position: 'insideBottom', offset: -5, fontSize: 10 }}
                />
                <YAxis
                  type="number"
                  dataKey="drift"
                  name="Drift"
                  domain={[0, 100]}
                  tick={{ fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                  width={35}
                  label={{ value: 'Drift %', angle: -90, position: 'insideLeft', fontSize: 10 }}
                />
                <ReferenceLine x={70} stroke="#9ca3af" strokeDasharray="4 4" />
                <ReferenceLine y={25} stroke="#9ca3af" strokeDasharray="4 4" />
                <Tooltip content={<ScatterTooltipContent />} />
                <Scatter data={quality} fill="#8884d8">
                  {quality.map((entry, index) => (
                    <Cell
                      key={`cell-${index}`}
                      fill={scatterDotColor(entry.completeness, entry.drift)}
                    />
                  ))}
                </Scatter>
              </ScatterChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-56 flex items-center justify-center text-sm text-gray-400 dark:text-gray-500">
              No completed tasks with quality data
            </div>
          )}
        </div>

      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Agent Ranking + Entity Table                                       */}
      {/* ------------------------------------------------------------------ */}
      <div id="analytics-entity-drilldown" className="scroll-mt-20 grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Agent Ranking (1/3) */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-4">
            Agent Ranking
          </h3>
          {agents.length > 0 ? (
            <div className="space-y-3">
              {agents.map((a, idx) => {
                const medal = idx === 0 ? '\u{1F947}' : idx === 1 ? '\u{1F948}' : idx === 2 ? '\u{1F949}' : `${idx + 1}.`;
                return (
                  <div key={a.actor_id} className="flex items-start gap-2">
                    <span className="text-sm shrink-0 w-6 text-center">{medal}</span>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-medium text-gray-800 dark:text-gray-100 truncate">
                        {a.actor_name || a.actor_id}
                      </p>
                      <p className="text-[9px] text-gray-400 truncate">{a.actor_id}</p>
                      <div className="flex items-center gap-3 mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">
                        <span>{a.total_cards} tasks</span>
                        <span className={completenessColor(a.avg_completeness)}>
                          C: {a.avg_completeness !== null ? `${a.avg_completeness}%` : '--'}
                        </span>
                        <span className={driftColor(a.avg_drift)}>
                          D: {a.avg_drift !== null ? `${a.avg_drift}%` : '--'}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="h-32 flex items-center justify-center text-sm text-gray-400 dark:text-gray-500">
              No agents with activity
            </div>
          )}
        </div>

        {/* Entity Table (2/3) */}
        <div className="col-span-1 lg:col-span-2 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          {/* Search + Tabs */}
          <div className="mb-3">
            <input
              type="text"
              value={entitySearch}
              onChange={(e) => { setEntitySearch(e.target.value); setEntityPage(0); }}
              placeholder="Search by title..."
              className="w-full px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-900 text-gray-900 dark:text-white outline-none focus:ring-1 focus:ring-blue-400"
            />
          </div>
          <div className="flex items-center gap-1 mb-4 border-b border-gray-200 dark:border-gray-700">
            {(['spec', 'ideation', 'card'] as EntityTab[]).map((tab) => (
              <button
                key={tab}
                onClick={() => { setActiveTab(tab); setEntityPage(0); }}
                className={`px-3 py-1.5 text-xs font-medium border-b-2 transition-colors ${
                  activeTab === tab
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                {tabLabels[tab]}
              </button>
            ))}
          </div>

          {/* Table */}
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <th className="pb-2 font-medium">Title</th>
                  <th className="pb-2 font-medium">Status</th>
                  {activeTab === 'spec' && (
                    <>
                      <th className="pb-2 font-medium text-center">Tasks</th>
                      <th className="pb-2 font-medium text-center">ACs</th>
                      <th className="pb-2 font-medium text-center">Tests</th>
                      <th className="pb-2 font-medium text-center">Rules</th>
                      <th className="pb-2 font-medium text-center">Contracts</th>
                    </>
                  )}
                  {activeTab === 'ideation' && (
                    <>
                      <th className="pb-2 font-medium text-center">Refinements</th>
                      <th className="pb-2 font-medium text-center">Specs</th>
                    </>
                  )}
                  {activeTab === 'card' && (
                    <>
                      <th className="pb-2 font-medium text-center">Type</th>
                      <th className="pb-2 font-medium text-center">Compl.</th>
                      <th className="pb-2 font-medium text-center">Drift</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {sortedEntities.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="py-8 text-center text-gray-400 dark:text-gray-500">
                      No items found
                    </td>
                  </tr>
                ) : (
                  sortedEntities.map((item) => {
                    return (
                      <tr
                        key={item.id}
                        onClick={() => onSelectEntity(activeTab, item.id, item.title)}
                        className="border-b border-gray-100 dark:border-gray-700/50 hover:bg-gray-50 dark:hover:bg-gray-700/30 cursor-pointer transition-colors"
                      >
                        <td className="py-2 pr-2 max-w-[200px] truncate text-gray-800 dark:text-gray-100 font-medium">
                          {item.title}
                        </td>
                        <td className="py-2 pr-2">
                          <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
                            {item.status || '--'}
                          </span>
                        </td>
                        {activeTab === 'spec' && (
                          <>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.card_count ?? 0}</td>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.ac_count ?? 0}</td>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.scenario_count ?? 0}</td>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.rules_count ?? 0}</td>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.contracts_count ?? 0}</td>
                          </>
                        )}
                        {activeTab === 'ideation' && (
                          <>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.refinement_count ?? 0}</td>
                            <td className="py-2 text-center text-gray-600 dark:text-gray-300">{item.spec_count ?? 0}</td>
                          </>
                        )}
                        {activeTab === 'card' && (
                          <>
                            <td className="py-2 text-center">
                              <span className={`text-xs font-medium ${
                                (item as any).card_type === 'bug' ? 'text-red-500' :
                                item.is_test ? 'text-cyan-600 dark:text-cyan-400' :
                                'text-gray-500'
                              }`}>
                                {(item as any).card_type === 'bug' ? 'Bug' : item.is_test ? 'Test' : 'Impl'}
                              </span>
                            </td>
                            <td className={`py-2 text-center font-medium ${completenessColor(item.completeness ?? null)}`}>
                              {item.completeness !== null && item.completeness !== undefined ? `${item.completeness}%` : '--'}
                            </td>
                            <td className={`py-2 text-center font-medium ${driftColor(item.drift ?? null)}`}>
                              {item.drift !== null && item.drift !== undefined ? `${item.drift}%` : '--'}
                            </td>
                          </>
                        )}
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>

            {/* Pagination */}
            {entities[activeTab] && (
              <div className="flex items-center justify-between mt-3 pt-3 border-t border-gray-100 dark:border-gray-700">
                <span className="text-xs text-gray-400">
                  {entities[activeTab].total} total · page {entityPage + 1} of {Math.max(1, Math.ceil((entities[activeTab].total || 1) / PAGE_SIZE))}
                </span>
                <div className="flex gap-1">
                  <button
                    onClick={() => setEntityPage(p => Math.max(0, p - 1))}
                    disabled={entityPage === 0}
                    className="px-2 py-1 text-xs rounded bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 disabled:opacity-30"
                  >
                    ← Prev
                  </button>
                  <button
                    onClick={() => setEntityPage(p => p + 1)}
                    disabled={sortedEntities.length < PAGE_SIZE}
                    className="px-2 py-1 text-xs rounded bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 disabled:opacity-30"
                  >
                    Next →
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Validation Gates panel                                             */}
      {/* ------------------------------------------------------------------ */}
      {validations && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-4">
            Validation Gates
          </h3>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Spec Validation Gate */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-xs font-bold text-violet-700 dark:text-violet-300 uppercase tracking-wide">
                  Spec Validation Gate
                </h4>
                <span className="text-[10px] text-gray-500">
                  {validations.spec_validation_gate.total_submitted} submitted · {validations.spec_validation_gate.specs_with_validation} specs
                </span>
              </div>
              <div className="grid grid-cols-4 gap-2 mb-3">
                <MiniStat label="success rate" value={validations.spec_validation_gate.success_rate} unit="%" />
                <MiniStat label="avg complete" value={validations.spec_validation_gate.avg_scores.completeness} unit="%" />
                <MiniStat label="avg assert" value={validations.spec_validation_gate.avg_scores.assertiveness} unit="%" />
                <MiniStat label="avg ambig" value={validations.spec_validation_gate.avg_scores.ambiguity} unit="%" invert />
              </div>
              <RejectionReasonsBar reasons={validations.spec_validation_gate.rejection_reasons} color="violet" />
              {validations.spec_validation_gate.per_spec.length > 0 && (
                <div className="mt-3 max-h-48 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-white dark:bg-gray-800">
                      <tr className="text-left text-[10px] uppercase text-gray-400 border-b border-gray-200 dark:border-gray-700">
                        <th className="py-1 font-medium">Spec</th>
                        <th className="py-1 font-medium text-center">Attempts</th>
                        <th className="py-1 font-medium text-center">Last</th>
                      </tr>
                    </thead>
                    <tbody>
                      {validations.spec_validation_gate.per_spec.slice(0, 10).map((s) => (
                        <tr key={s.spec_id} className="border-b border-gray-100 dark:border-gray-700/50">
                          <td className="py-1.5 truncate max-w-[180px]" title={s.title}>{s.title}</td>
                          <td className="py-1.5 text-center text-gray-600 dark:text-gray-400">{s.attempts}</td>
                          <td className="py-1.5 text-center">
                            {s.last_outcome && (
                              <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                                s.last_outcome === 'success'
                                  ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                                  : 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                              }`}>
                                {s.last_outcome}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Task Validation Gate */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-xs font-bold text-blue-700 dark:text-blue-300 uppercase tracking-wide">
                  Task Validation Gate
                </h4>
                <span className="text-[10px] text-gray-500">
                  {validations.task_validation_gate.total_submitted} submitted · {validations.task_validation_gate.cards_with_validation} cards
                </span>
              </div>
              <div className="grid grid-cols-4 gap-2 mb-3">
                <MiniStat label="success rate" value={validations.task_validation_gate.success_rate} unit="%" />
                <MiniStat label="avg conf" value={validations.task_validation_gate.avg_scores.confidence} unit="%" />
                <MiniStat label="avg complete" value={validations.task_validation_gate.avg_scores.completeness} unit="%" />
                <MiniStat label="avg drift" value={validations.task_validation_gate.avg_scores.drift} unit="%" invert />
              </div>
              <RejectionReasonsBar reasons={validations.task_validation_gate.rejection_reasons} color="blue" />
              {validations.task_validation_gate.per_card.length > 0 && (
                <div className="mt-3 max-h-48 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-white dark:bg-gray-800">
                      <tr className="text-left text-[10px] uppercase text-gray-400 border-b border-gray-200 dark:border-gray-700">
                        <th className="py-1 font-medium">Card</th>
                        <th className="py-1 font-medium text-center">Attempts</th>
                        <th className="py-1 font-medium text-center">Last</th>
                      </tr>
                    </thead>
                    <tbody>
                      {validations.task_validation_gate.per_card.slice(0, 10).map((c) => (
                        <tr key={c.card_id} className="border-b border-gray-100 dark:border-gray-700/50">
                          <td className="py-1.5 truncate max-w-[180px]" title={c.title}>{c.title}</td>
                          <td className="py-1.5 text-center text-gray-600 dark:text-gray-400">{c.attempts}</td>
                          <td className="py-1.5 text-center">
                            {c.last_outcome && (
                              <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                                c.last_outcome === 'success' || c.last_outcome === 'pass'
                                  ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                                  : 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                              }`}>
                                {c.last_outcome}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {governedAnalyticsSections}

      <DeliveryForecastPanel
        sprints={sprints}
        forecast={deliveryForecast}
        forecastLoading={deliveryForecastLoading}
        forecastError={deliveryForecastError}
        forecastExportError={deliveryForecastExportError}
        forecastExporting={deliveryForecastExporting}
        from={from}
        to={to}
        compact
        onOpenFullView={onOpenDeliveryIntelligence}
        onRetryForecast={() => setDeliveryForecastRetry((value) => value + 1)}
        onExportForecast={async () => {
          if (deliveryForecastExporting) return;
          setDeliveryForecastExporting(true);
          setDeliveryForecastExportError(null);
          try {
            await api.exportBoardDeliveryForecastCsv(boardId, from, to);
          } catch (err) {
            setDeliveryForecastExportError(err instanceof Error ? err.message : 'Delivery forecast export failed');
          } finally {
            setDeliveryForecastExporting(false);
          }
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Validation gate helpers
// ---------------------------------------------------------------------------

function MiniStat({ label, value, unit, invert = false }: { label: string; value: number | null; unit?: string; invert?: boolean }) {
  const colorClass = (() => {
    if (value === null) return 'text-gray-400';
    if (invert) {
      if (value <= 20) return 'text-green-600 dark:text-green-400';
      if (value <= 50) return 'text-amber-600 dark:text-amber-400';
      return 'text-red-600 dark:text-red-400';
    }
    if (value >= 80) return 'text-green-600 dark:text-green-400';
    if (value >= 60) return 'text-blue-600 dark:text-blue-400';
    if (value >= 40) return 'text-amber-600 dark:text-amber-400';
    return 'text-red-600 dark:text-red-400';
  })();

  return (
    <div className="bg-gray-50 dark:bg-gray-900/40 rounded p-2">
      <div className="text-[9px] uppercase text-gray-400 dark:text-gray-500 truncate">{label}</div>
      <div className={`text-sm font-bold ${colorClass}`}>
        {value !== null ? `${value}${unit ?? ''}` : '--'}
      </div>
    </div>
  );
}

const REASON_LABEL_MAP: Record<string, string> = {
  completeness_below: 'completeness',
  assertiveness_below: 'assertiveness',
  ambiguity_above: 'ambiguity',
  confidence_below: 'confidence',
  drift_above: 'drift',
  reject_recommendation: 'rejected',
};

function RejectionReasonsBar({ reasons, color }: { reasons: Record<string, number>; color: 'violet' | 'blue' }) {
  const entries = Object.entries(reasons).filter(([, v]) => v > 0);
  if (entries.length === 0) {
    return (
      <div className="text-[10px] text-gray-400 dark:text-gray-500 italic">
        No rejections recorded
      </div>
    );
  }
  const total = entries.reduce((acc, [, v]) => acc + (v as number), 0);
  const barColor = color === 'violet' ? 'bg-violet-500' : 'bg-blue-500';

  return (
    <div>
      <div className="text-[10px] text-gray-500 dark:text-gray-400 mb-1">
        Rejection reasons ({total} total, multi-count)
      </div>
      <div className="flex flex-col gap-1">
        {entries.sort(([, a], [, b]) => (b as number) - (a as number)).map(([reason, count]) => {
          const pct = total > 0 ? ((count as number) / total) * 100 : 0;
          return (
            <div key={reason} className="flex items-center gap-2">
              <span className="w-24 text-[10px] text-gray-600 dark:text-gray-400 shrink-0">
                {REASON_LABEL_MAP[reason] ?? reason}
              </span>
              <div className="flex-1 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                <div className={`h-full ${barColor}`} style={{ width: `${pct}%` }} />
              </div>
              <span className="text-[10px] font-medium text-gray-600 dark:text-gray-400 w-8 text-right">
                {String(count)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
