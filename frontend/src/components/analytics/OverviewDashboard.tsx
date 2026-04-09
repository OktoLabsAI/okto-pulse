import { useEffect, useState, useMemo } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import {
  TrendingUp,
  TrendingDown,
  Minus,
  Lightbulb,
  FileText,
  CheckSquare,
  Target,
  AlertTriangle,
  Scale,
  Globe,
  Bug,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';

// ---------------------------------------------------------------------------
// Types matching backend GET /analytics/overview response
// ---------------------------------------------------------------------------

interface FunnelData {
  ideations: number;
  refinements: number;
  specs: number;
  cards: number;
  done: number;
}

interface VelocityWeek {
  week: string;
  impl: number;
  test: number;
}

interface BoardStat {
  board_id: string;
  board_name: string;
  ideations: number;
  refinements: number;
  specs: number;
  cards: number;
  cards_done: number;
  bugs: number;
}

interface OverviewData {
  total_ideations: number;
  ideations_done: number;
  total_specs: number;
  specs_done: number;
  specs_with_tests: number;
  total_business_rules: number;
  total_api_contracts: number;
  specs_with_rules: number;
  specs_with_contracts: number;
  total_cards_impl: number;
  total_cards_test: number;
  avg_completeness: number | null;
  avg_drift: number | null;
  funnel: FunnelData;
  velocity: VelocityWeek[];
  boards: BoardStat[];
  // Bug metrics
  total_bugs: number;
  bugs_open: number;
  bugs_done: number;
  bugs_by_severity: { critical: number; major: number; minor: number };
  bug_rate_per_spec: { spec_id: string; spec_title: string; total_tasks: number; bugs: number; rate: number }[];
  avg_triage_hours: number | null;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface OverviewDashboardProps {
  from: string;
  to: string;
  onSelectBoard: (boardId: string, boardName: string) => void;
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

function completenessTag(v: number | null): string {
  if (v === null) return 'bg-gray-200 dark:bg-gray-600 text-gray-600 dark:text-gray-300';
  if (v >= 90) return 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300';
  if (v >= 70) return 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300';
  if (v >= 50) return 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300';
  return 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300';
}

// driftTag kept available for future board-card badges
// function driftTag(v: number | null): string { ... }

// ---------------------------------------------------------------------------
// Skeleton
// ---------------------------------------------------------------------------

function SkeletonBlock({ className = '' }: { className?: string }) {
  return (
    <div className={`animate-pulse bg-gray-200 dark:bg-gray-700 rounded ${className}`} />
  );
}

function KpiSkeleton() {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3">
      <SkeletonBlock className="h-3 w-20" />
      <SkeletonBlock className="h-8 w-16" />
      <SkeletonBlock className="h-3 w-24" />
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="space-y-6">
      {/* KPI row */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
        {[1, 2, 3, 4, 5].map((i) => (
          <KpiSkeleton key={i} />
        ))}
      </div>
      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <SkeletonBlock className="h-4 w-40 mb-4" />
          <SkeletonBlock className="h-48" />
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <SkeletonBlock className="h-4 w-40 mb-4" />
          <SkeletonBlock className="h-48" />
        </div>
      </div>
      {/* Boards row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3"
          >
            <SkeletonBlock className="h-4 w-32" />
            <SkeletonBlock className="h-3 w-48" />
            <SkeletonBlock className="h-3 w-40" />
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Funnel bar component
// ---------------------------------------------------------------------------

function FunnelBar({
  label,
  count,
  maxCount,
  conversionPct,
  colors,
  splitImpl,
  splitTest,
  splitBug,
}: {
  label: string;
  count: number;
  maxCount: number;
  conversionPct: number | null;
  colors: string;
  splitImpl?: number;
  splitTest?: number;
  splitBug?: number;
}) {
  const pct = maxCount > 0 ? (count / maxCount) * 100 : 0;
  const hasSegments = splitImpl !== undefined && splitTest !== undefined;

  const implPct = hasSegments && count > 0 ? (splitImpl / count) * pct : 0;
  const testPct = hasSegments && count > 0 ? (splitTest / count) * pct : 0;
  const bugPct = hasSegments && splitBug && count > 0 ? (splitBug / count) * pct : 0;

  return (
    <div className="flex items-center gap-3">
      <span className="w-28 text-xs font-medium text-gray-600 dark:text-gray-300 text-right shrink-0">
        {label}
      </span>
      <div className="flex-1 h-7 bg-gray-100 dark:bg-gray-700 rounded-md overflow-hidden relative">
        {hasSegments ? (
          <div className="flex h-full">
            <div
              className="bg-violet-500 h-full transition-all duration-500"
              style={{ width: `${implPct}%` }}
            />
            <div
              className="bg-emerald-500 h-full transition-all duration-500"
              style={{ width: `${testPct}%` }}
            />
            {bugPct > 0 && (
              <div
                className="bg-red-500 h-full transition-all duration-500"
                style={{ width: `${bugPct}%` }}
              />
            )}
          </div>
        ) : (
          <div
            className={`h-full transition-all duration-500 ${colors}`}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
      <div className="w-24 shrink-0 flex items-center gap-1.5">
        <span className="text-sm font-semibold text-gray-800 dark:text-gray-100">{count}</span>
        {conversionPct !== null && (
          <span className="text-[10px] text-gray-400 dark:text-gray-500">
            ({conversionPct.toFixed(0)}%)
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Trend indicator
// ---------------------------------------------------------------------------

function TrendIndicator({ value }: { value: number | null }) {
  if (value === null) return <Minus className="w-3.5 h-3.5 text-gray-400" />;
  if (value > 0) return <TrendingUp className="w-3.5 h-3.5 text-green-500" />;
  if (value < 0) return <TrendingDown className="w-3.5 h-3.5 text-red-500" />;
  return <Minus className="w-3.5 h-3.5 text-gray-400" />;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function OverviewDashboard({ from, to, onSelectBoard }: OverviewDashboardProps) {
  const api = useDashboardApi();
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    api
      .getAnalyticsOverview(from, to)
      .then((resp: OverviewData) => {
        if (!cancelled) setData(resp);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load analytics');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to]);

  // Derived values
  const totalCards = data ? data.total_cards_impl + data.total_cards_test : 0;
  const donePct = data && totalCards > 0 ? ((data.funnel.done / totalCards) * 100) : 0;

  const ideationsDonePct = useMemo(() => {
    if (!data || data.total_ideations === 0) return 0;
    return Math.round(((data.ideations_done || 0) / data.total_ideations) * 100);
  }, [data]);

  const specsDonePct = useMemo(() => {
    if (!data || data.total_specs === 0) return 0;
    return Math.round(((data.specs_done || 0) / data.total_specs) * 100);
  }, [data]);

  const specsWithTestsPct = useMemo(() => {
    if (!data || data.total_specs === 0) return 0;
    return Math.round(((data.specs_with_tests || 0) / data.total_specs) * 100);
  }, [data]);

  // Velocity data formatted for Recharts
  const velocityChartData = useMemo(() => {
    if (!data) return [];
    return data.velocity.map((w) => ({
      ...w,
      label: formatWeekLabel(w.week),
    }));
  }, [data]);

  // Funnel conversion percentages
  const funnelConversions = useMemo(() => {
    if (!data) return { ref: null, spec: null, card: null, done: null };
    const f = data.funnel;
    return {
      ref: f.ideations > 0 ? (f.refinements / f.ideations) * 100 : null,
      spec: f.refinements > 0 ? (f.specs / f.refinements) * 100 : null,
      card: f.specs > 0 ? (f.cards / f.specs) * 100 : null,
      done: f.cards > 0 ? (f.done / f.cards) * 100 : null,
    };
  }, [data]);

  if (loading) return <LoadingSkeleton />;

  if (error) {
    return (
      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-6 text-center">
        <AlertTriangle className="w-6 h-6 text-red-500 mx-auto mb-2" />
        <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className="space-y-6">
      {/* ------------------------------------------------------------------ */}
      {/* KPI Cards                                                          */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-4">
        {/* Total Ideations */}
        <KpiCard
          icon={<Lightbulb className="w-4 h-4 text-amber-500" />}
          title="Total Ideations"
          value={data.total_ideations}
          badge={`${ideationsDonePct}% done`}
          badgeColor="bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300"
        />

        {/* Specs */}
        <KpiCard
          icon={<FileText className="w-4 h-4 text-blue-500" />}
          title="Specs"
          value={data.total_specs}
          badge={`${specsDonePct}% with tasks`}
          badgeColor="bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300"
          extra={
            <span className="text-[10px] text-gray-400 dark:text-gray-500">
              {specsWithTestsPct}% with tests
            </span>
          }
        />

        {/* Tasks */}
        <KpiCard
          icon={<CheckSquare className="w-4 h-4 text-violet-500" />}
          title="Tasks"
          value={totalCards}
          extra={
            <div className="flex items-center gap-3 mt-0.5">
              <span className="flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400">
                <span className="w-2 h-2 rounded-full bg-violet-500 inline-block" />
                Impl: {data.total_cards_impl}
              </span>
              <span className="flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400">
                <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block" />
                Tests: {data.total_cards_test}
              </span>
            </div>
          }
        />

        {/* Business Rules */}
        <KpiCard
          icon={<Scale className="w-4 h-4 text-orange-500" />}
          title="Business Rules"
          value={data.total_business_rules ?? 0}
          badge={`${data.specs_with_rules ?? 0} specs`}
          badgeColor="bg-orange-100 dark:bg-orange-900/40 text-orange-700 dark:text-orange-300"
        />

        {/* API Contracts */}
        <KpiCard
          icon={<Globe className="w-4 h-4 text-cyan-500" />}
          title="API Contracts"
          value={data.total_api_contracts ?? 0}
          badge={`${data.specs_with_contracts ?? 0} specs`}
          badgeColor="bg-cyan-100 dark:bg-cyan-900/40 text-cyan-700 dark:text-cyan-300"
        />

        {/* Bugs */}
        <KpiCard
          icon={<Bug className="w-4 h-4 text-red-500" />}
          title="Bugs"
          value={data.total_bugs ?? 0}
          badge={`${data.bugs_open ?? 0} open`}
          badgeColor={
            (data.bugs_open ?? 0) > 0
              ? 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
              : 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
          }
        />

        {/* Avg Completeness */}
        <div
          className={`rounded-lg border border-gray-200 dark:border-gray-700 p-4 ${completenessBg(data.avg_completeness)}`}
        >
          <div className="flex items-center gap-1.5 mb-1">
            <Target className="w-4 h-4 text-gray-400" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Avg Completeness
            </span>
          </div>
          <div className="flex items-end gap-2">
            <span className={`text-2xl font-bold ${completenessColor(data.avg_completeness)}`}>
              {data.avg_completeness !== null ? `${data.avg_completeness}%` : '--'}
            </span>
            <TrendIndicator value={data.avg_completeness} />
          </div>
        </div>

        {/* Avg Drift */}
        <div
          className={`rounded-lg border border-gray-200 dark:border-gray-700 p-4 ${driftBg(data.avg_drift)}`}
        >
          <div className="flex items-center gap-1.5 mb-1">
            <AlertTriangle className="w-4 h-4 text-gray-400" />
            <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Avg Drift
            </span>
          </div>
          <div className="flex items-end gap-2">
            <span className={`text-2xl font-bold ${driftColor(data.avg_drift)}`}>
              {data.avg_drift !== null ? `${data.avg_drift}%` : '--'}
            </span>
            <TrendIndicator value={data.avg_drift !== null ? -data.avg_drift : null} />
          </div>
        </div>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Funnel + Velocity Charts                                           */}
      {/* ------------------------------------------------------------------ */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Funnel */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-4">
            Conversion Funnel
          </h3>
          <div className="space-y-3">
            <FunnelBar
              label="Ideations"
              count={data.funnel.ideations}
              maxCount={data.funnel.ideations}
              conversionPct={null}
              colors="bg-amber-400"
            />
            <FunnelBar
              label="Refinements"
              count={data.funnel.refinements}
              maxCount={data.funnel.ideations}
              conversionPct={funnelConversions.ref}
              colors="bg-orange-400"
            />
            <FunnelBar
              label="Specs"
              count={data.funnel.specs}
              maxCount={data.funnel.ideations}
              conversionPct={funnelConversions.spec}
              colors="bg-blue-400"
            />
            <FunnelBar
              label="Tasks"
              count={data.funnel.cards}
              maxCount={data.funnel.ideations}
              conversionPct={funnelConversions.card}
              colors=""
              splitImpl={data.total_cards_impl}
              splitTest={data.total_cards_test}
              splitBug={data.total_bugs}
            />
            <FunnelBar
              label="Done"
              count={data.funnel.done}
              maxCount={data.funnel.ideations}
              conversionPct={funnelConversions.done}
              colors="bg-green-500"
            />
          </div>
          <div className="flex gap-3 mt-3 items-center justify-between">
            <div className="flex gap-3">
              <span className="flex items-center gap-1 text-[10px] text-gray-400"><span className="w-2.5 h-2.5 rounded-sm bg-violet-500" /> Implementation</span>
              <span className="flex items-center gap-1 text-[10px] text-gray-400"><span className="w-2.5 h-2.5 rounded-sm bg-emerald-500" /> Tests</span>
              {(data.total_bugs ?? 0) > 0 && (
                <span className="flex items-center gap-1 text-[10px] text-gray-400"><span className="w-2.5 h-2.5 rounded-sm bg-red-500" /> Bugs</span>
              )}
            </div>
            <p className="text-[10px] text-gray-400 dark:text-gray-500">
              {donePct.toFixed(0)}% overall completion
            </p>
          </div>
        </div>

        {/* Velocity Chart */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-4">
            Velocity (cards done / week)
          </h3>
          {velocityChartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={velocityChartData}>
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                  width={30}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'var(--color-gray-800, #1f2937)',
                    border: 'none',
                    borderRadius: '0.5rem',
                    color: '#fff',
                    fontSize: '12px',
                  }}
                />
                <Legend
                  verticalAlign="bottom"
                  iconType="circle"
                  iconSize={8}
                  wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }}
                />
                <Bar
                  dataKey="impl"
                  name="Implementation"
                  stackId="a"
                  fill="#8b5cf6"
                  radius={[0, 0, 0, 0]}
                />
                <Bar
                  dataKey="test"
                  name="Tests"
                  stackId="a"
                  fill="#10b981"
                  radius={[0, 0, 0, 0]}
                />
                <Bar
                  dataKey="bug"
                  name="Bugs"
                  stackId="a"
                  fill="#ef4444"
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-48 flex items-center justify-center text-sm text-gray-400 dark:text-gray-500">
              No velocity data for this period
            </div>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Boards Grid                                                        */}
      {/* ------------------------------------------------------------------ */}
      {data.boards.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-3">
            Boards
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {data.boards.map((b) => {
              const bDonePct = b.cards > 0 ? Math.round((b.cards_done / b.cards) * 100) : 0;
              return (
                <button
                  key={b.board_id}
                  onClick={() => onSelectBoard(b.board_id, b.board_name)}
                  className="text-left bg-white dark:bg-gray-800 rounded-lg border border-gray-200
                    dark:border-gray-700 p-4 transition-all hover:border-blue-300
                    dark:hover:border-blue-500 hover:shadow-md focus:outline-none
                    focus:ring-2 focus:ring-blue-400/50"
                >
                  <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-100 truncate mb-1">
                    {b.board_name}
                  </h4>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                    {b.ideations} ideations &rsaquo; {b.refinements ?? 0} refinements &rsaquo; {b.specs} specs &rsaquo; {b.cards} tasks
                  </p>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium ${completenessTag(bDonePct)}`}
                    >
                      {bDonePct}% done
                    </span>
                    {(b.bugs ?? 0) > 0 && (
                      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300">
                        {b.bugs} bugs ({b.cards > 0 ? Math.round((b.bugs / b.cards) * 100) : 0}%)
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function KpiCard({
  icon,
  title,
  value,
  badge,
  badgeColor,
  extra,
}: {
  icon: React.ReactNode;
  title: string;
  value: number;
  badge?: string;
  badgeColor?: string;
  extra?: React.ReactNode;
}) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <div className="flex items-center gap-1.5 mb-1">
        {icon}
        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">{title}</span>
      </div>
      <div className="flex items-end gap-2">
        <span className="text-2xl font-bold text-gray-800 dark:text-gray-100">{value}</span>
        {badge && (
          <span
            className={`inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium ${badgeColor || ''}`}
          >
            {badge}
          </span>
        )}
      </div>
      {extra}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatWeekLabel(isoDate: string): string {
  try {
    const d = new Date(isoDate + 'T00:00:00');
    const month = d.toLocaleString('en', { month: 'short' });
    return `${month} ${d.getDate()}`;
  } catch {
    return isoDate;
  }
}
