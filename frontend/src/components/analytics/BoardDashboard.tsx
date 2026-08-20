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
  FlaskConical,
  Bug,
  Clock,
  Database,
  Download,
  HelpCircle,
  RefreshCw,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import { PulseLoader } from '@/components/shared/PulseLoader';

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

interface SprintsResponse {
  summary: {
    total_sprints: number;
    status_breakdown: Record<string, number>;
    avg_completion_rate: number | null;
    sprint_evaluation: {
      total_submitted: number;
      approve_rate: number | null;
      avg_overall_score: number | null;
    };
  };
  sprints: Array<{
    sprint_id: string;
    title: string;
    status: string;
    spec_id: string;
    total_cards: number;
    done_cards: number;
    completion_rate: number;
    card_status_breakdown: Record<string, number>;
    evaluations_count: number;
    last_evaluation: { overall_score: number | null; recommendation: string | null; evaluator_name: string | null; created_at: string | null } | null;
    task_validation_gate: {
      total_submitted: number;
      total_success: number;
      total_failed: number;
      rejection_reasons: Record<string, number>;
      first_pass_rate: number | null;
    };
    commitment: {
      state: 'available' | 'unavailable_legacy';
      baseline_ref: string | null;
      activated_at?: string;
      original_member_count?: number;
      current_member_count?: number;
      added_count?: number;
      removed_count?: number;
      unavailable_reason: string | null;
    };
  }>;
}

type BoardKgHealthState =
  | 'healthy'
  | 'at_risk'
  | 'backpressure'
  | 'recovery_needed'
  | 'quarantined';

type BoardKgResultState =
  | 'available'
  | 'restricted'
  | 'unavailable'
  | 'empty'
  | 'error';

interface BoardKgAnalyticsResponse {
  query_fingerprint: string;
  as_of: string;
  result_state: BoardKgResultState;
  health: {
    state: BoardKgHealthState;
    classification_reason: string;
    reason_codes: string[];
  };
  debt_domains: {
    result_state: BoardKgResultState;
    active_queue_count: number | null;
    technical_dlq_count: number | null;
    canonical_debt_count: number | null;
  };
  cognitive_effectiveness: {
    result_state: BoardKgResultState;
    cognitively_effective: boolean | null;
    denominator: number | null;
    attempted_count: number | null;
    persisted_count: number | null;
    technical_dlq_count: number | null;
    persistence_gap_count: number | null;
  };
}

interface CanonicalCoverageResponse {
  query_fingerprint: string;
  as_of: string;
  totals: {
    state: 'available' | 'not_applicable' | 'restricted' | 'unavailable' | 'inconsistent';
    applicable: number | null;
    covered: number | null;
    uncovered: number | null;
    skipped: number | null;
    value: number | null;
    n: number | null;
    reason: string | null;
  };
  coverage: Array<{
    obligation_type: string;
    counts: CanonicalCoverageResponse['totals'];
    rows: Array<{
      identity: { spec_id: string; obligation_id: string; edition: number };
      state: string;
      covered: boolean | null;
      skip: { state: string; effective: boolean; reason_code: string | null };
    }>;
  }>;
}

interface CoverageSpec {
  spec_id: string;
  title: string;
  total_ac: number;
  covered_ac: number;
  total_scenarios: number;
  scenario_status_counts: Record<string, number>;
  business_rules_count: number;
  api_contracts_count: number;
  fr_with_rules_pct: number;
  fr_with_contracts_pct: number;
  // Spec 233eaad3: 4 novos campos vindo do spec_coverage_summary —
  // refletem cancelled-card filter (gates e dashboard usam mesma fonte).
  decisions_coverage_pct?: number;
  decisions_total?: number;
  tr_task_linkage_pct?: number;
  trs_total?: number;
  ir_task_linkage_pct?: number;
  irs_total?: number;
  irs_linked?: number;
  irs_uncovered_ids?: string[];
  skip_ir_coverage?: boolean;
  or_task_linkage_pct?: number;
  ors_total?: number;
  ors_linked?: number;
  ors_uncovered_ids?: string[];
  skip_or_coverage?: boolean;
  // Bug 6f152627: AC/FR coverage explícitos para o painel nível 2.
  ac_coverage_pct?: number;
  fr_coverage_pct?: number;
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
  onSelectEntity: (type: 'ideation' | 'spec' | 'refinement' | 'card', id: string, name: string) => void;
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

function coverageBarColor(pct: number): string {
  if (pct >= 95) return 'bg-green-500';
  if (pct >= 80) return 'bg-amber-500';
  return 'bg-red-500';
}

function scatterDotColor(completeness: number, drift: number): string {
  // Green quadrant: high completeness + low drift
  if (completeness >= 70 && drift <= 25) return '#22c55e';
  return '#ef4444';
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

export function BoardDashboard({ boardId, from, to, onSelectEntity }: BoardDashboardProps) {
  const api = useDashboardApi();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [funnel, setFunnel] = useState<FunnelData | null>(null);
  const [quality, setQuality] = useState<QualityPoint[]>([]);
  const [coverage, setCoverage] = useState<CoverageSpec[]>([]);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [validations, setValidations] = useState<ValidationsResponse | null>(null);
  const [sprints, setSprints] = useState<SprintsResponse | null>(null);
  const [kgAnalytics, setKgAnalytics] = useState<BoardKgAnalyticsResponse | null>(null);
  const [kgLoading, setKgLoading] = useState(true);
  const [kgError, setKgError] = useState<string | null>(null);
  const [kgRetry, setKgRetry] = useState(0);
  const [kgExporting, setKgExporting] = useState(false);
  const [canonicalCoverage, setCanonicalCoverage] = useState<CanonicalCoverageResponse | null>(null);
  const [canonicalCoverageError, setCanonicalCoverageError] = useState<string | null>(null);
  const [canonicalCoverageLoading, setCanonicalCoverageLoading] = useState(true);
  const [canonicalCoverageRetry, setCanonicalCoverageRetry] = useState(0);
  const [canonicalCoverageExporting, setCanonicalCoverageExporting] = useState(false);
  const [entities, setEntities] = useState<Record<EntityTab, EntityListResponse | null>>({
    spec: null,
    ideation: null,
    card: null,
  });

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
      api.getBoardAnalyticsCoverage(boardId, from, to),
      api.getBoardAnalyticsAgents(boardId, from, to),
      api.getBoardAnalyticsValidations(boardId, from, to),
      api.getBoardAnalyticsSprints(boardId, from, to),
    ])
      .then(([funnelRes, qualityRes, coverageRes, agentsRes, validationsRes, sprintsRes]) => {
        if (cancelled) return;
        setFunnel(funnelRes as FunnelData);
        // Quality endpoint now returns {conclusion_reported, validation_reported}.
        // Prefer validation data; fall back to conclusions when absent.
        const q = qualityRes as QualityResponse;
        setQuality(q.validation_reported.length > 0 ? q.validation_reported : q.conclusion_reported);
        setCoverage(coverageRes as CoverageSpec[]);
        setAgents(agentsRes as AgentRow[]);
        setValidations(validationsRes as ValidationsResponse);
        setSprints(sprintsRes as SprintsResponse);
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
    api.getBoardKgAnalytics(boardId, from, to)
      .then((payload) => {
        if (!cancelled) setKgAnalytics(payload as BoardKgAnalyticsResponse);
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
    setCanonicalCoverageLoading(true);
    setCanonicalCoverageError(null);
    api.getCanonicalBoardCoverage(boardId, from, to)
      .then((payload) => {
        if (!cancelled) setCanonicalCoverage(payload as CanonicalCoverageResponse);
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

    // Coverage: % of specs that have at least one test scenario
    const specsWithTests = coverage.filter((s) => s.total_scenarios > 0).length;
    const coberturaPct = coverage.length > 0
      ? Math.round((specsWithTests / coverage.length) * 100)
      : 0;

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
      coberturaPct,
    };
  }, [funnel, quality, coverage]);

  // Sorted entity items for current tab
  const sortedEntities = useMemo(() => {
    const current = entities[activeTab];
    if (!current) return [];
    return [...current.items].sort((a, b) => (a.title || '').localeCompare(b.title || ''));
  }, [entities, activeTab]);

  // Coverage bars sorted by coverage %
  const coverageBars = useMemo(() => {
    return [...coverage]
      .map((s) => {
        const pct = s.total_ac > 0 ? Math.round((s.covered_ac / s.total_ac) * 100) : 0;
        return { ...s, pct };
      })
      .sort((a, b) => b.pct - a.pct);
  }, [coverage]);

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
  const hasIrCoverageMetrics = coverage.some((s) =>
    s.irs_total !== undefined || s.ir_task_linkage_pct !== undefined || s.skip_ir_coverage === true
  );
  const hasOrCoverageMetrics = coverage.some((s) =>
    s.ors_total !== undefined || s.or_task_linkage_pct !== undefined || s.skip_or_coverage === true
  );

  return (
    <div className="space-y-6">
      <section
        aria-labelledby="board-kg-analytics-heading"
        className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <Database className="w-4 h-4 text-indigo-500" />
              <h3 id="board-kg-analytics-heading" className="text-sm font-semibold text-gray-700 dark:text-gray-200">
                Board KG Analytics
              </h3>
            </div>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Canonical health, operational debt and cognitive effectiveness. Availability is reported separately from health.
            </p>
          </div>
          <button
            type="button"
            disabled={kgExporting || kgLoading || kgAnalytics === null}
            onClick={async () => {
              if (kgExporting) return;
              setKgExporting(true);
              try {
                await api.exportBoardKgAnalyticsCsv(boardId, from, to);
              } catch (err) {
                setKgError(err instanceof Error ? err.message : 'KG Analytics export failed');
              } finally {
                setKgExporting(false);
              }
            }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md border border-gray-200 dark:border-gray-600 disabled:opacity-50"
          >
            <Download className="w-3.5 h-3.5" />
            {kgExporting ? 'Exporting…' : 'Complete CSV'}
          </button>
        </div>

        {kgLoading && (
          <p className="mt-4 text-xs text-gray-500" role="status">Loading KG Analytics…</p>
        )}
        {!kgLoading && kgError && (
          <div className="mt-4 flex items-center justify-between gap-3 rounded-md bg-red-50 dark:bg-red-900/20 px-3 py-2" role="alert">
            <span className="text-xs text-red-700 dark:text-red-300">{kgError}</span>
            <button
              type="button"
              onClick={() => setKgRetry((value) => value + 1)}
              className="inline-flex items-center gap-1 text-xs font-medium text-red-700 dark:text-red-300"
            >
              <RefreshCw className="w-3.5 h-3.5" /> Retry
            </button>
          </div>
        )}
        {!kgLoading && !kgError && kgAnalytics && (
          <div className="mt-4 space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-gray-500 dark:text-gray-400">Health</span>
              <span className="rounded-full bg-indigo-100 dark:bg-indigo-900/40 px-2 py-0.5 text-xs font-medium text-indigo-700 dark:text-indigo-300">
                {kgAnalytics.health.state}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400">Result</span>
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                kgAnalytics.result_state === 'available'
                  ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                  : 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300'
              }`}>
                {kgAnalytics.result_state}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {kgAnalytics.health.classification_reason}
              </span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3" aria-label="KG analytics facts">
              {[
                ['Active queue', kgAnalytics.debt_domains.active_queue_count],
                ['Technical DLQ', kgAnalytics.debt_domains.technical_dlq_count],
                ['Canonical debt', kgAnalytics.debt_domains.canonical_debt_count],
                ['Cognitive denominator', kgAnalytics.cognitive_effectiveness.denominator],
                ['Persistence gaps', kgAnalytics.cognitive_effectiveness.persistence_gap_count],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-md bg-gray-50 dark:bg-gray-900/40 p-3">
                  <p className="text-[10px] uppercase text-gray-400">{label}</p>
                  <p className="mt-1 text-lg font-semibold text-gray-800 dark:text-gray-100">
                    {value === null ? 'Unavailable' : value}
                  </p>
                </div>
              ))}
            </div>
            <p className="text-[10px] text-gray-400">
              as_of {kgAnalytics.as_of} · query {kgAnalytics.query_fingerprint.slice(0, 12)}…
            </p>
          </div>
        )}
      </section>

      <section
        aria-labelledby="canonical-coverage-heading"
        className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 id="canonical-coverage-heading" className="text-sm font-semibold text-gray-700 dark:text-gray-200">
              Canonical Coverage &amp; Traceability
            </h3>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Factual coverage keeps governed skips, unavailable authority and ineligible historical evidence separate.
            </p>
          </div>
          <button
            type="button"
            disabled={canonicalCoverageExporting || canonicalCoverageLoading || canonicalCoverage === null}
            onClick={async () => {
              if (canonicalCoverageExporting) return;
              setCanonicalCoverageExporting(true);
              try {
                await api.exportCanonicalBoardCoverageCsv(boardId, from, to);
              } catch (err) {
                setCanonicalCoverageError(err instanceof Error ? err.message : 'Coverage export failed');
              } finally {
                setCanonicalCoverageExporting(false);
              }
            }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md border border-gray-200 dark:border-gray-600 disabled:opacity-50"
          >
            <Download className="w-3.5 h-3.5" />
            {canonicalCoverageExporting ? 'Exporting…' : 'Complete CSV'}
          </button>
        </div>
        {canonicalCoverageLoading && (
          <p className="mt-4 text-xs text-gray-500" role="status">Loading canonical coverage…</p>
        )}
        {!canonicalCoverageLoading && canonicalCoverageError && (
          <div className="mt-4 flex items-center justify-between rounded-md bg-red-50 dark:bg-red-900/20 px-3 py-2" role="alert">
            <span className="text-xs text-red-700 dark:text-red-300">{canonicalCoverageError}</span>
            <button
              type="button"
              onClick={() => setCanonicalCoverageRetry((value) => value + 1)}
              className="inline-flex items-center gap-1 text-xs text-red-700 dark:text-red-300"
            >
              <RefreshCw className="w-3.5 h-3.5" /> Retry
            </button>
          </div>
        )}
        {!canonicalCoverageLoading && !canonicalCoverageError && canonicalCoverage && (
          <div className="mt-4 space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              {[
                ['Applicable', canonicalCoverage.totals.applicable],
                ['Covered', canonicalCoverage.totals.covered],
                ['Uncovered', canonicalCoverage.totals.uncovered],
                ['Skipped', canonicalCoverage.totals.skipped],
                ['Coverage', canonicalCoverage.totals.value === null ? null : `${Math.round(canonicalCoverage.totals.value * 100)}%`],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-md bg-gray-50 dark:bg-gray-900/40 p-3">
                  <p className="text-[10px] uppercase text-gray-400">{label}</p>
                  <p className="mt-1 text-lg font-semibold text-gray-800 dark:text-gray-100">
                    {value === null ? canonicalCoverage.totals.state : value}
                  </p>
                </div>
              ))}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-[10px] uppercase text-gray-400">
                    <th className="py-2">Obligation</th>
                    <th className="py-2 text-right">Applicable</th>
                    <th className="py-2 text-right">Covered</th>
                    <th className="py-2 text-right">Uncovered</th>
                    <th className="py-2 text-right">Skipped</th>
                  </tr>
                </thead>
                <tbody>
                  {canonicalCoverage.coverage.map((group) => (
                    <tr key={group.obligation_type} className="border-b border-gray-100 dark:border-gray-700/50">
                      <td className="py-2 font-medium">{group.obligation_type}</td>
                      <td className="py-2 text-right">{group.counts.applicable ?? group.counts.state}</td>
                      <td className="py-2 text-right">{group.counts.covered ?? '—'}</td>
                      <td className="py-2 text-right">{group.counts.uncovered ?? '—'}</td>
                      <td className="py-2 text-right">{group.counts.skipped ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[10px] text-gray-400">
              as_of {canonicalCoverage.as_of} · query {canonicalCoverage.query_fingerprint.slice(0, 12)}…
            </p>
          </div>
        )}
      </section>

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

        {/* Coverage */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <FlaskConical className="w-4 h-4 text-emerald-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Coverage</span>
            <DashboardMetricHelp
              label="Coverage"
              description="Shows objective spec coverage. Open the chart to inspect AC, FR, TR, IR, OR, and decision coverage per spec."
              targetId="analytics-coverage-by-spec"
            />
          </div>
          <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">
            {kpis.coberturaPct}%
          </span>
          <p className="text-[10px] text-gray-400 dark:text-gray-500 mt-0.5">specs with tests</p>
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
      {/* Scatter + Coverage Charts                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
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

        {/* Coverage by Spec (Tests, Rules, Contracts) */}
        <div id="analytics-coverage-by-spec" className="scroll-mt-20 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-2">
            Coverage by Spec
          </h3>
          <div className="flex items-center gap-4 mb-3 text-[10px] text-gray-500 dark:text-gray-400 flex-wrap">
            <span className="flex items-center gap-1" title="Acceptance Criteria covered by Test Scenarios"><span className="w-2 h-2 rounded-full bg-green-500 inline-block" /> AC</span>
            <span className="flex items-center gap-1" title="Functional Requirements covered by Business Rules"><span className="w-2 h-2 rounded-full bg-amber-500 inline-block" /> FR</span>
            <span className="flex items-center gap-1" title="Technical Requirements with active linked tasks"><span className="w-2 h-2 rounded-full bg-purple-500 inline-block" /> TRs</span>
            {hasIrCoverageMetrics && (
              <span className="flex items-center gap-1" title="Integration Requirements with active linked tasks"><span className="w-2 h-2 rounded-full bg-sky-500 inline-block" /> IRs</span>
            )}
            {hasOrCoverageMetrics && (
              <span className="flex items-center gap-1" title="Observability Requirements with active linked tasks"><span className="w-2 h-2 rounded-full bg-teal-500 inline-block" /> ORs</span>
            )}
            <span className="flex items-center gap-1" title="Decisions with active linked tasks"><span className="w-2 h-2 rounded-full bg-indigo-500 inline-block" /> Decisions</span>
          </div>
          {coverageBars.length > 0 ? (
            <div className="space-y-3 max-h-[260px] overflow-y-auto pr-1">
              {coverageBars.map((s) => {
                const acPct = s.ac_coverage_pct ?? s.pct;
                const frPct = s.fr_coverage_pct ?? s.fr_with_rules_pct ?? 0;
                const trPct = s.tr_task_linkage_pct ?? 0;
                const irPct = s.ir_task_linkage_pct ?? 0;
                const orPct = s.or_task_linkage_pct ?? 0;
                const irSkipped = s.skip_ir_coverage === true;
                const orSkipped = s.skip_or_coverage === true;
                const decPct = s.decisions_coverage_pct ?? 0;
                return (
                  <div key={s.spec_id}>
                    <span className="text-xs text-gray-600 dark:text-gray-300 truncate block mb-1" title={s.title}>
                      {s.title}
                    </span>
                    <div className="space-y-0.5">
                      {/* AC coverage bar */}
                      <div className="flex items-center gap-2" title={`ACs: ${s.covered_ac}/${s.total_ac} covered by Test Scenarios`}>
                        <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                          <div
                            className={`h-full rounded transition-all duration-500 ${coverageBarColor(acPct)}`}
                            style={{ width: `${acPct}%` }}
                          />
                        </div>
                        <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                          {acPct}%
                        </span>
                      </div>
                      {/* FR coverage bar */}
                      <div className="flex items-center gap-2" title={`FRs covered by Business Rules`}>
                        <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                          <div
                            className="h-full rounded transition-all duration-500 bg-amber-500"
                            style={{ width: `${frPct}%` }}
                          />
                        </div>
                        <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                          {frPct}%
                        </span>
                      </div>
                      {/* TR coverage bar (spec 233eaad3) */}
                      <div className="flex items-center gap-2" title={`TRs: ${s.trs_total ?? 0} total`}>
                        <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                          <div
                            className="h-full rounded transition-all duration-500 bg-purple-500"
                            style={{ width: `${trPct}%` }}
                          />
                        </div>
                        <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                          {trPct}%
                        </span>
                      </div>
                      {/* IR coverage bar */}
                      {hasIrCoverageMetrics && (
                        <div
                          className="flex items-center gap-2"
                          title={irSkipped ? 'IR coverage skipped by coverage calculator' : `IRs: ${s.irs_linked ?? 0}/${s.irs_total ?? 0} linked to active tasks`}
                        >
                          <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                            <div
                              className={`h-full rounded transition-all duration-500 ${irSkipped ? 'bg-gray-400 dark:bg-gray-500' : 'bg-sky-500'}`}
                              style={{ width: `${irSkipped ? 100 : irPct}%` }}
                            />
                          </div>
                          <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                            {irSkipped ? 'skip' : `${irPct}%`}
                          </span>
                        </div>
                      )}
                      {/* OR coverage bar */}
                      {hasOrCoverageMetrics && (
                        <div
                          className="flex items-center gap-2"
                          title={orSkipped ? 'OR coverage skipped by coverage calculator' : `ORs: ${s.ors_linked ?? 0}/${s.ors_total ?? 0} linked to active tasks`}
                        >
                          <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                            <div
                              className={`h-full rounded transition-all duration-500 ${orSkipped ? 'bg-gray-400 dark:bg-gray-500' : 'bg-teal-500'}`}
                              style={{ width: `${orSkipped ? 100 : orPct}%` }}
                            />
                          </div>
                          <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                            {orSkipped ? 'skip' : `${orPct}%`}
                          </span>
                        </div>
                      )}
                      {/* Decisions coverage bar (spec 233eaad3) */}
                      <div className="flex items-center gap-2" title={`Decisions: ${s.decisions_total ?? 0} total`}>
                        <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                          <div
                            className="h-full rounded transition-all duration-500 bg-indigo-500"
                            style={{ width: `${decPct}%` }}
                          />
                        </div>
                        <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                          {decPct}%
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="h-56 flex items-center justify-center text-sm text-gray-400 dark:text-gray-500">
              No specs with acceptance criteria
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

      {/* ------------------------------------------------------------------ */}
      {/* Sprints panel                                                      */}
      {/* ------------------------------------------------------------------ */}
      {sprints && sprints.summary.total_sprints > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200">
              Sprints
            </h3>
            <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
              <span>{sprints.summary.total_sprints} total</span>
              <span>·</span>
              <span>avg completion: {sprints.summary.avg_completion_rate !== null ? `${sprints.summary.avg_completion_rate}%` : '--'}</span>
              {sprints.summary.sprint_evaluation.total_submitted > 0 && (
                <>
                  <span>·</span>
                  <span>eval approve: {sprints.summary.sprint_evaluation.approve_rate !== null ? `${sprints.summary.sprint_evaluation.approve_rate}%` : '--'}</span>
                </>
              )}
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[10px] uppercase text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <th className="py-2 font-medium">Sprint</th>
                  <th className="py-2 font-medium text-center">Status</th>
                  <th className="py-2 font-medium text-center">Cards</th>
                  <th className="py-2 font-medium text-center">Completion</th>
                  <th className="py-2 font-medium text-center">Commitment</th>
                  <th className="py-2 font-medium text-center">Task Gate</th>
                  <th className="py-2 font-medium text-center">Last Eval</th>
                </tr>
              </thead>
              <tbody>
                {sprints.sprints.map((sp) => (
                  <tr key={sp.sprint_id} className="border-b border-gray-100 dark:border-gray-700/50">
                    <td className="py-2 truncate max-w-[250px]" title={sp.title}>{sp.title}</td>
                    <td className="py-2 text-center">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                        sp.status === 'active' ? 'bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300' :
                        sp.status === 'closed' ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300' :
                        sp.status === 'review' ? 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300' :
                        'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                      }`}>{sp.status}</span>
                    </td>
                    <td className="py-2 text-center text-gray-600 dark:text-gray-400">
                      {sp.done_cards}/{sp.total_cards}
                    </td>
                    <td className="py-2 text-center">
                      <div className="flex items-center justify-center gap-1.5">
                        <div className="w-16 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                          <div className="h-full bg-indigo-500" style={{ width: `${sp.completion_rate}%` }} />
                        </div>
                        <span className="text-[10px] font-medium">{sp.completion_rate}%</span>
                      </div>
                    </td>
                    <td className="py-2 text-center">
                      {sp.commitment.state === 'available' ? (
                        <span
                          className="text-[10px] text-gray-600 dark:text-gray-300"
                          title={`Baseline ${sp.commitment.baseline_ref}`}
                        >
                          {sp.commitment.original_member_count} original · {sp.commitment.added_count} added · {sp.commitment.removed_count} removed
                        </span>
                      ) : (
                        <span
                          className="text-[10px] text-amber-600 dark:text-amber-300"
                          title={sp.commitment.unavailable_reason || 'Activation baseline unavailable'}
                        >
                          unavailable legacy
                        </span>
                      )}
                    </td>
                    <td className="py-2 text-center">
                      {sp.task_validation_gate.total_submitted > 0 ? (
                        <span className="text-[10px]">
                          <span className="text-green-600 dark:text-green-400">{sp.task_validation_gate.total_success}</span>
                          /
                          <span className="text-red-500 dark:text-red-400">{sp.task_validation_gate.total_failed}</span>
                        </span>
                      ) : (
                        <span className="text-[10px] text-gray-400">—</span>
                      )}
                    </td>
                    <td className="py-2 text-center">
                      {sp.last_evaluation ? (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                          sp.last_evaluation.recommendation === 'approve'
                            ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                            : 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                        }`}>
                          {sp.last_evaluation.recommendation} ({sp.last_evaluation.overall_score}%)
                        </span>
                      ) : (
                        <span className="text-[10px] text-gray-400">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
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
