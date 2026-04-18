/**
 * Shared SVG shape primitives used by the 11 node components.
 *
 * Each shape is rendered into a 140×80 viewport so a node swapping its
 * `node_type` keeps the same bounding box. Strokes use the type colour;
 * fills are intentionally translucent so the shell's label contrasts
 * against the board background in both light and dark themes.
 */

import type { CSSProperties } from 'react';
import { NODE_HEIGHT, NODE_WIDTH } from './NodeShell';

export interface ShapeProps {
  color: string;
  darkColor: string;
  /** Optional style override — used by Alternative's dashed stroke. */
  strokeDasharray?: string;
}

const baseSvgStyle: CSSProperties = {
  width: '100%',
  height: '100%',
  display: 'block',
};

function fillForLight(color: string) {
  return `${color}1A`; // ~10% alpha in light mode
}

function fillForDark(color: string) {
  return `${color}26`; // ~15% alpha in dark mode
}

function activeFill(color: string, darkColor: string) {
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('dark')) {
    return fillForDark(darkColor);
  }
  return fillForLight(color);
}

function activeStroke(color: string, darkColor: string) {
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('dark')) {
    return darkColor;
  }
  return color;
}

export function RoundedRectShape({ color, darkColor }: ShapeProps) {
  return (
    <svg viewBox={`0 0 ${NODE_WIDTH} ${NODE_HEIGHT}`} style={baseSvgStyle}>
      <rect
        x={1}
        y={1}
        width={NODE_WIDTH - 2}
        height={NODE_HEIGHT - 2}
        rx={12}
        ry={12}
        fill={activeFill(color, darkColor)}
        stroke={activeStroke(color, darkColor)}
        strokeWidth={2}
      />
    </svg>
  );
}

export function SquareShape({ color, darkColor, strokeDasharray }: ShapeProps) {
  return (
    <svg viewBox={`0 0 ${NODE_WIDTH} ${NODE_HEIGHT}`} style={baseSvgStyle}>
      <rect
        x={1}
        y={1}
        width={NODE_WIDTH - 2}
        height={NODE_HEIGHT - 2}
        rx={4}
        ry={4}
        fill={activeFill(color, darkColor)}
        stroke={activeStroke(color, darkColor)}
        strokeWidth={2}
        strokeDasharray={strokeDasharray}
      />
    </svg>
  );
}

export function CircleShape({ color, darkColor }: ShapeProps) {
  const cx = NODE_WIDTH / 2;
  const cy = NODE_HEIGHT / 2;
  const rx = NODE_WIDTH / 2 - 2;
  const ry = NODE_HEIGHT / 2 - 2;
  return (
    <svg viewBox={`0 0 ${NODE_WIDTH} ${NODE_HEIGHT}`} style={baseSvgStyle}>
      <ellipse
        cx={cx}
        cy={cy}
        rx={rx}
        ry={ry}
        fill={activeFill(color, darkColor)}
        stroke={activeStroke(color, darkColor)}
        strokeWidth={2}
      />
    </svg>
  );
}

/** Regular hexagon with flat top/bottom — Criterion. */
export function HexagonShape({ color, darkColor }: ShapeProps) {
  const w = NODE_WIDTH;
  const h = NODE_HEIGHT;
  const inset = w * 0.15;
  const points = [
    `${inset},1`,
    `${w - inset},1`,
    `${w - 2},${h / 2}`,
    `${w - inset},${h - 1}`,
    `${inset},${h - 1}`,
    `2,${h / 2}`,
  ].join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={baseSvgStyle}>
      <polygon
        points={points}
        fill={activeFill(color, darkColor)}
        stroke={activeStroke(color, darkColor)}
        strokeWidth={2}
      />
    </svg>
  );
}

/** Regular octagon — Constraint. */
export function OctagonShape({ color, darkColor }: ShapeProps) {
  const w = NODE_WIDTH;
  const h = NODE_HEIGHT;
  const cx = w * 0.2;
  const cy = h * 0.25;
  const points = [
    `${cx},1`,
    `${w - cx},1`,
    `${w - 2},${cy}`,
    `${w - 2},${h - cy}`,
    `${w - cx},${h - 1}`,
    `${cx},${h - 1}`,
    `2,${h - cy}`,
    `2,${cy}`,
  ].join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={baseSvgStyle}>
      <polygon
        points={points}
        fill={activeFill(color, darkColor)}
        stroke={activeStroke(color, darkColor)}
        strokeWidth={2}
      />
    </svg>
  );
}

/** Diamond / rhombus — Assumption and Bug. */
export function DiamondShape({ color, darkColor }: ShapeProps) {
  const w = NODE_WIDTH;
  const h = NODE_HEIGHT;
  const points = [
    `${w / 2},2`,
    `${w - 2},${h / 2}`,
    `${w / 2},${h - 2}`,
    `2,${h / 2}`,
  ].join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={baseSvgStyle}>
      <polygon
        points={points}
        fill={activeFill(color, darkColor)}
        stroke={activeStroke(color, darkColor)}
        strokeWidth={2}
      />
    </svg>
  );
}
