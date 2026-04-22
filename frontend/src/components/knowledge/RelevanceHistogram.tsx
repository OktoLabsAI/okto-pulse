/**
 * RelevanceHistogram — compact 10-bucket distribution chart of KGNode.relevance_score.
 *
 * Lives below the relevance slider in the controls panel. Diagnostic value:
 * if all buckets are concentrated in one column, the user immediately
 * understands "the data is constant" rather than thinking the filter is
 * broken when sliding it has no visible effect.
 *
 * Buckets are fixed at 0..1 with width 0.1 each. Bucket 9 includes 1.0.
 * The bar at bucket index B is highlighted when (B + 1) * 0.1 > minRelevance,
 * i.e. nodes whose relevance falls in that bucket would survive the current
 * filter; bars below the threshold are dimmed.
 */

import { useMemo } from 'react';

interface Props {
  scores: number[];
  /** Current slider value (0..1) — used to dim buckets that are filtered out. */
  threshold: number;
}

const BUCKET_COUNT = 10;
const BUCKET_WIDTH = 1 / BUCKET_COUNT;

export function RelevanceHistogram({ scores, threshold }: Props) {
  const buckets = useMemo(() => {
    const counts = new Array(BUCKET_COUNT).fill(0) as number[];
    for (const s of scores) {
      if (typeof s !== 'number' || Number.isNaN(s)) continue;
      const clamped = Math.max(0, Math.min(0.9999999, s));
      const idx = Math.min(BUCKET_COUNT - 1, Math.floor(clamped / BUCKET_WIDTH));
      counts[idx] += 1;
    }
    return counts;
  }, [scores]);

  const max = useMemo(() => Math.max(1, ...buckets), [buckets]);

  if (scores.length === 0) {
    return (
      <p className="text-[10px] text-gray-500 dark:text-gray-500 mt-2">
        No relevance data to display.
      </p>
    );
  }

  return (
    <div className="mt-2" data-testid="kg-relevance-histogram">
      <div
        className="flex items-end gap-px h-8"
        role="img"
        aria-label="Node relevance distribution across 10 buckets"
      >
        {buckets.map((count, i) => {
          const heightPct = max === 0 ? 0 : Math.max(2, Math.round((count / max) * 100));
          const bucketCeiling = (i + 1) * BUCKET_WIDTH;
          const aboveThreshold = bucketCeiling > threshold;
          return (
            <div
              key={i}
              className={`flex-1 rounded-sm transition-all ${
                aboveThreshold
                  ? 'bg-blue-500/80 dark:bg-blue-400/70'
                  : 'bg-gray-300/60 dark:bg-gray-700/60'
              }`}
              style={{ height: `${heightPct}%` }}
              title={`Bucket ${(i * BUCKET_WIDTH).toFixed(1)}–${bucketCeiling.toFixed(1)}: ${count} node${count === 1 ? '' : 's'}`}
            />
          );
        })}
      </div>
      <div className="flex justify-between text-[9px] text-gray-500 dark:text-gray-500 mt-0.5">
        <span>0%</span>
        <span>distribution (n={scores.length})</span>
        <span>100%</span>
      </div>
    </div>
  );
}
