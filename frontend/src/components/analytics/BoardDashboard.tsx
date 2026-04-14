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
  Lightbulb,
  FileText,
  CheckSquare,
  Target,
  AlertTriangle,
  FlaskConical,
  Bug,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';

// ---------------------------------------------------------------------------
// Types matching backend responses
// ---------------------------------------------------------------------------

interface FunnelData {
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
  onSelectEntity: (type: 'ideation' | 'spec' | 'refinement', id: string, name: string) => void;
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

function SkeletonBlock({ className = '' }: { className?: string }) {
  return (
    <div className={`animate-pulse bg-gray-200 dark:bg-gray-700 rounded ${className}`} />
  );
}

function LoadingSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <div key={i} className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3">
            <SkeletonBlock className="h-3 w-20" />
            <SkeletonBlock className="h-8 w-16" />
            <SkeletonBlock className="h-3 w-24" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <SkeletonBlock className="h-4 w-40 mb-4" />
          <SkeletonBlock className="h-56" />
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <SkeletonBlock className="h-4 w-40 mb-4" />
          <SkeletonBlock className="h-56" />
        </div>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <SkeletonBlock className="h-4 w-32 mb-4" />
          <SkeletonBlock className="h-40" />
        </div>
        <div className="col-span-1 lg:col-span-2 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <SkeletonBlock className="h-4 w-40 mb-4" />
          <SkeletonBlock className="h-40" />
        </div>
      </div>
    </div>
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

  if (loading) return <LoadingSkeleton />;

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

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------------------------ */}
      {/* KPI Cards                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
        {/* Ideations */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center gap-1.5 mb-1">
            <Lightbulb className="w-4 h-4 text-amber-500" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Ideations</span>
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
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Scatter + Coverage Charts                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Scatter Completeness x Drift */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
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
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-2">
            Coverage by Spec
          </h3>
          <div className="flex items-center gap-4 mb-3 text-[10px] text-gray-500 dark:text-gray-400">
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-green-500 inline-block" /> Tests</span>
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-amber-500 inline-block" /> BR Coverage</span>
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-blue-500 inline-block" /> Contract Coverage</span>
          </div>
          {coverageBars.length > 0 ? (
            <div className="space-y-3 max-h-[260px] overflow-y-auto pr-1">
              {coverageBars.map((s) => (
                <div key={s.spec_id}>
                  <span className="text-xs text-gray-600 dark:text-gray-300 truncate block mb-1" title={s.title}>
                    {s.title}
                  </span>
                  <div className="space-y-0.5">
                    {/* Test coverage bar */}
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                        <div
                          className={`h-full rounded transition-all duration-500 ${coverageBarColor(s.pct)}`}
                          style={{ width: `${s.pct}%` }}
                        />
                      </div>
                      <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                        {s.pct}%
                      </span>
                    </div>
                    {/* BR coverage bar */}
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                        <div
                          className="h-full rounded transition-all duration-500 bg-amber-500"
                          style={{ width: `${s.fr_with_rules_pct ?? 0}%` }}
                        />
                      </div>
                      <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                        {s.fr_with_rules_pct ?? 0}%
                      </span>
                    </div>
                    {/* Contract coverage bar */}
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded overflow-hidden">
                        <div
                          className="h-full rounded transition-all duration-500 bg-blue-500"
                          style={{ width: `${s.fr_with_contracts_pct ?? 0}%` }}
                        />
                      </div>
                      <span className="w-10 text-[10px] font-medium text-gray-700 dark:text-gray-300 text-right shrink-0">
                        {s.fr_with_contracts_pct ?? 0}%
                      </span>
                    </div>
                  </div>
                </div>
              ))}
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
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
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
                    const entityType = activeTab === 'card' ? 'refinement' : activeTab;
                    return (
                      <tr
                        key={item.id}
                        onClick={() => onSelectEntity(entityType as 'ideation' | 'spec' | 'refinement', item.id, item.title)}
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
