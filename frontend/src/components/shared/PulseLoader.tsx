import { useId } from 'react';

/**
 * PulseLoader — loading animation derived from the landing-page hero pulse.
 *
 * Faithful port of the hero's ECG trace: same path geometry, cyan→blue→navy
 * gradient, gaussian glow and the non-linear dash sweep (the trace
 * accelerates through the heartbeat spike). The viewBox is cropped so the
 * spike sits at the visual center of the loader.
 *
 * SVG defs use `useId()` so multiple loaders can coexist on one screen
 * without gradient/filter id collisions.
 */

type PulseLoaderSize = 'sm' | 'md' | 'lg';

interface PulseLoaderProps {
  /** Optional caption rendered under the trace (e.g. "Loading specs..."). */
  label?: string;
  size?: PulseLoaderSize;
  /** Fills the viewport and adds the hero's grid + radial glow backdrop. */
  fullScreen?: boolean;
  className?: string;
}

const SIZE_TO_HEIGHT: Record<PulseLoaderSize, string> = {
  sm: 'h-16',
  md: 'h-24',
  lg: 'h-36',
};

export function PulseLoader({
  label,
  size = 'md',
  fullScreen = false,
  className = '',
}: PulseLoaderProps) {
  const uid = useId();
  const gradientId = `pulse-loader-gradient-${uid}`;
  const glowId = `pulse-loader-glow-${uid}`;

  const trace = (
    <svg
      // Hero path peaks around x≈340 — a 680-wide window centers the spike.
      viewBox="0 0 680 220"
      role="presentation"
      focusable="false"
      className={`w-full max-w-md ${SIZE_TO_HEIGHT[size]} overflow-visible`}
    >
      <defs>
        <linearGradient
          id={gradientId}
          x1="268"
          y1="110"
          x2="404"
          y2="110"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0%" stopColor="#22d3ee" />
          <stop offset="55%" stopColor="#3b82f6" />
          <stop offset="100%" stopColor="#1e40af" />
        </linearGradient>
        <filter id={glowId} x="-20%" y="-120%" width="140%" height="340%">
          <feGaussianBlur stdDeviation="7" result="blur" />
          <feColorMatrix
            in="blur"
            type="matrix"
            values="0 0 0 0 0.13 0 0 0 0 0.83 0 0 0 0 0.93 0 0 0 0.85 0"
          />
          <feMerge>
            <feMergeNode />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <path
        className="stroke-gray-400/15 dark:stroke-slate-400/10"
        fill="none"
        strokeWidth="3"
        strokeLinecap="round"
        d="M -180 110 L 900 110"
        pathLength={1260}
      />
      <path
        className="pulse-loader__trace"
        fill="none"
        stroke={`url(#${gradientId})`}
        strokeWidth="6"
        strokeLinecap="round"
        strokeLinejoin="round"
        filter={`url(#${glowId})`}
        d="M -180 110 L 268 110 L 300 110 L 312 110 L 324 62 L 340 158 L 356 86 L 372 110 L 404 110 L 900 110"
        pathLength={1260}
      />
    </svg>
  );

  const caption = label ? (
    <div className="text-sm text-gray-500 dark:text-gray-400">{label}</div>
  ) : null;

  if (fullScreen) {
    return (
      <div
        role="status"
        aria-label={label ?? 'Loading'}
        className={`min-h-screen relative flex flex-col items-center justify-center gap-2 overflow-hidden bg-surface-50 dark:bg-surface-950 ${className}`}
      >
        {/* Hero backdrop: faint grid masked radially + cyan/blue radial glows. */}
        <div
          aria-hidden="true"
          className="pulse-loader__grid absolute inset-0 opacity-40"
        />
        <div
          aria-hidden="true"
          className="absolute inset-0"
          style={{
            background:
              'radial-gradient(circle at 50% 48%, rgba(34, 211, 238, 0.10), transparent 34%), radial-gradient(circle at 62% 52%, rgba(59, 130, 246, 0.08), transparent 42%)',
          }}
        />
        <div className="relative z-10 flex flex-col items-center gap-2">
          {trace}
          {caption}
        </div>
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-label={label ?? 'Loading'}
      className={`flex flex-col items-center justify-center gap-1 py-6 ${className}`}
    >
      {trace}
      {caption}
    </div>
  );
}

export default PulseLoader;
