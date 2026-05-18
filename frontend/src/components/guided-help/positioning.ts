import type { GuidedHelpPlacement } from './types';

export interface GuidedHelpViewport {
  width: number;
  height: number;
}

export interface GuidedHelpPopoverSize {
  width: number;
  height: number;
}

export interface GuidedHelpPosition {
  top: number;
  left: number;
  placement: GuidedHelpPlacement;
  fallback: boolean;
  arrowSide: 'top' | 'right' | 'bottom' | 'left' | 'none';
}

const GAP = 12;
const MARGIN = 16;
export const GUIDED_HELP_POPOVER_SIZE: GuidedHelpPopoverSize = { width: 320, height: 210 };

function viewportFromWindow(): GuidedHelpViewport {
  if (typeof window === 'undefined') {
    return { width: 1024, height: 768 };
  }
  return {
    width: window.innerWidth || 1024,
    height: window.innerHeight || 768,
  };
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

function rectUsable(rect: DOMRect | null): rect is DOMRect {
  if (!rect) return false;
  return Number.isFinite(rect.top) && Number.isFinite(rect.left) && rect.width > 0 && rect.height > 0;
}

function scoreOverflow(
  top: number,
  left: number,
  size: GuidedHelpPopoverSize,
  viewport: GuidedHelpViewport,
): number {
  const rightOverflow = Math.max(0, left + size.width + MARGIN - viewport.width);
  const leftOverflow = Math.max(0, MARGIN - left);
  const bottomOverflow = Math.max(0, top + size.height + MARGIN - viewport.height);
  const topOverflow = Math.max(0, MARGIN - top);
  return rightOverflow + leftOverflow + bottomOverflow + topOverflow;
}

function positionForPlacement(
  rect: DOMRect,
  placement: GuidedHelpPlacement,
  size: GuidedHelpPopoverSize,
): Omit<GuidedHelpPosition, 'fallback'> {
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;

  if (placement === 'top') {
    return {
      top: rect.top - size.height - GAP,
      left: centerX - size.width / 2,
      placement,
      arrowSide: 'bottom',
    };
  }
  if (placement === 'left') {
    return {
      top: centerY - size.height / 2,
      left: rect.left - size.width - GAP,
      placement,
      arrowSide: 'right',
    };
  }
  if (placement === 'right') {
    return {
      top: centerY - size.height / 2,
      left: rect.right + GAP,
      placement,
      arrowSide: 'left',
    };
  }
  return {
    top: rect.bottom + GAP,
    left: centerX - size.width / 2,
    placement: 'bottom',
    arrowSide: 'top',
  };
}

function fallbackPosition(
  viewport: GuidedHelpViewport,
  size: GuidedHelpPopoverSize,
): GuidedHelpPosition {
  return {
    top: clamp((viewport.height - size.height) / 2, MARGIN, viewport.height - size.height - MARGIN),
    left: clamp((viewport.width - size.width) / 2, MARGIN, viewport.width - size.width - MARGIN),
    placement: 'fallback',
    fallback: true,
    arrowSide: 'none',
  };
}

export function calculateGuidedHelpPosition(
  anchorRect: DOMRect | null,
  preferredPlacement: GuidedHelpPlacement = 'bottom',
  viewport: GuidedHelpViewport = viewportFromWindow(),
  size: GuidedHelpPopoverSize = GUIDED_HELP_POPOVER_SIZE,
): GuidedHelpPosition {
  if (!rectUsable(anchorRect) || preferredPlacement === 'fallback') {
    return fallbackPosition(viewport, size);
  }

  const placements: GuidedHelpPlacement[] = [];
  for (const placement of [preferredPlacement, 'bottom', 'right', 'left', 'top'] as GuidedHelpPlacement[]) {
    if (placement !== 'fallback' && !placements.includes(placement)) {
      placements.push(placement);
    }
  }

  const candidates = placements.map((placement) => {
    const raw = positionForPlacement(anchorRect, placement, size);
    return {
      ...raw,
      top: clamp(raw.top, MARGIN, viewport.height - size.height - MARGIN),
      left: clamp(raw.left, MARGIN, viewport.width - size.width - MARGIN),
      fallback: false,
      overflow: scoreOverflow(raw.top, raw.left, size, viewport),
    };
  });

  const best = candidates.sort((a, b) => a.overflow - b.overflow)[0];
  return {
    top: best.top,
    left: best.left,
    placement: best.placement,
    fallback: false,
    arrowSide: best.arrowSide,
  };
}

export function isUsableAnchorRect(rect: DOMRect | null): boolean {
  return rectUsable(rect);
}
