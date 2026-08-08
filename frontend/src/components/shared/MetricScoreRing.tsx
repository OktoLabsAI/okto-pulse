export type MetricScoreDirection =
  | 'higher-is-better'
  | 'lower-is-better';

export type MetricScoreTone =
  | 'neutral'
  | 'info'
  | 'success'
  | 'warning'
  | 'danger';

export type MetricScoreStatus = 'neutral' | 'met' | 'not-met';

export interface MetricScoreRingProps {
  label: string;
  value: number;
  max?: number;
  direction: MetricScoreDirection;
  threshold?: number | null;
  /**
   * Optional presentational tone for metrics without a threshold or semantic
   * status. A configured threshold always determines the ring tone.
   */
  tone?: MetricScoreTone;
  /**
   * Optional semantic result for metrics whose result is resolved outside this
   * component. A configured threshold always takes precedence.
   */
  status?: MetricScoreStatus;
  testId?: string;
  className?: string;
}

const TONE_CLASSES: Record<MetricScoreTone, string> = {
  neutral:
    'border-surface-300 text-surface-700 dark:border-surface-600 dark:text-surface-200',
  info:
    'border-blue-400 text-blue-700 dark:border-blue-500 dark:text-blue-300',
  success:
    'border-emerald-400 text-emerald-700 dark:border-emerald-500 dark:text-emerald-300',
  warning:
    'border-amber-400 text-amber-700 dark:border-amber-500 dark:text-amber-300',
  danger:
    'border-red-400 text-red-700 dark:border-red-500 dark:text-red-300',
};

function formatScore(value: number): string {
  return Number.isInteger(value)
    ? String(value)
    : String(Number(value.toFixed(2)));
}

function statusTone(status: MetricScoreStatus): MetricScoreTone {
  if (status === 'met') return 'success';
  if (status === 'not-met') return 'danger';
  return 'neutral';
}

/**
 * Accessible circular presentation for a bounded numeric quality metric.
 *
 * Threshold semantics follow the metric direction: higher-is-better means a
 * minimum threshold, while lower-is-better means a maximum threshold.
 */
export function MetricScoreRing({
  label,
  value,
  max = 100,
  direction,
  threshold,
  tone,
  status,
  testId = 'metric-score-ring',
  className = '',
}: MetricScoreRingProps) {
  const hasThreshold = threshold != null;
  const thresholdStatus: MetricScoreStatus | null = hasThreshold
    ? (
        direction === 'higher-is-better'
          ? value >= threshold
          : value <= threshold
      )
      ? 'met'
      : 'not-met'
    : null;
  const resolvedStatus = thresholdStatus ?? status ?? 'neutral';
  const resolvedTone = thresholdStatus != null
    ? statusTone(thresholdStatus)
    : status != null
      ? statusTone(status)
      : tone ?? 'neutral';

  const formattedValue = formatScore(value);
  const formattedMax = formatScore(max);
  const directionLabel = direction === 'higher-is-better'
    ? 'Higher is better'
    : 'Lower is better';
  const thresholdLabel = hasThreshold
    ? `${direction === 'higher-is-better' ? 'Minimum' : 'Maximum'} ${formatScore(threshold)}`
    : 'No threshold configured';
  const resultContext = hasThreshold ? 'threshold' : 'status';
  const resultLabel = resolvedStatus === 'met'
    ? `, ${resultContext} met`
    : resolvedStatus === 'not-met'
      ? `, ${resultContext} not met`
      : '';

  return (
    <div
      className={`flex min-w-0 flex-col items-center text-center ${className}`.trim()}
    >
      <div
        role="img"
        aria-label={`${label} score ${formattedValue} out of ${formattedMax}, ${directionLabel.toLowerCase()}, ${thresholdLabel}${resultLabel}`}
        data-testid={testId}
        data-direction={direction}
        data-status={resolvedStatus}
        className={`flex h-20 w-20 shrink-0 items-center justify-center rounded-full border-4 ${TONE_CLASSES[resolvedTone]}`}
      >
        <span aria-hidden="true" className="text-2xl font-bold leading-none">
          {formattedValue}
          <span className="ml-0.5 text-sm font-semibold text-surface-500 dark:text-surface-400">
            /{formattedMax}
          </span>
        </span>
      </div>
      <p className="mt-2 text-xs font-semibold text-surface-700 dark:text-surface-200">
        {label}
      </p>
      <p className="mt-1 text-[10px] text-surface-500 dark:text-surface-400">
        {directionLabel}
      </p>
      <p className="mt-0.5 text-[10px] text-surface-500 dark:text-surface-400">
        {thresholdLabel}
        {resolvedStatus === 'met' && (
          <span className="ml-1 font-semibold text-emerald-600 dark:text-emerald-400">
            · Met
          </span>
        )}
        {resolvedStatus === 'not-met' && (
          <span className="ml-1 font-semibold text-red-600 dark:text-red-400">
            · Not met
          </span>
        )}
      </p>
    </div>
  );
}
