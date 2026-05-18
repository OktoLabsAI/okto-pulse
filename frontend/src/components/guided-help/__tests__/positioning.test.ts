import { describe, expect, it } from 'vitest';

import { calculateGuidedHelpPosition } from '../positioning';

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

describe('guided help positioning', () => {
  it('uses the preferred placement when it fits the viewport', () => {
    const position = calculateGuidedHelpPosition(rect(120, 120, 80, 40), 'right', {
      width: 900,
      height: 700,
    });

    expect(position.placement).toBe('right');
    expect(position.fallback).toBe(false);
    expect(position.left).toBeGreaterThan(200);
    expect(position.arrowSide).toBe('left');
  });

  it('chooses a lower-overflow placement and clamps within the viewport', () => {
    const position = calculateGuidedHelpPosition(rect(760, 220, 80, 40), 'right', {
      width: 900,
      height: 700,
    });

    expect(position.placement).not.toBe('right');
    expect(position.left).toBeGreaterThanOrEqual(16);
    expect(position.left + 320).toBeLessThanOrEqual(884);
  });

  it('uses compact fallback when the anchor rect is unavailable', () => {
    const position = calculateGuidedHelpPosition(null, 'bottom', {
      width: 390,
      height: 640,
    });

    expect(position).toMatchObject({
      placement: 'fallback',
      fallback: true,
      arrowSide: 'none',
    });
    expect(position.left).toBeGreaterThanOrEqual(16);
    expect(position.top).toBeGreaterThanOrEqual(16);
  });
});
