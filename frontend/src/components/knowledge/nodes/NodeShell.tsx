/**
 * NodeShell — shared wrapper for all 11 KG node components (Spec 8 / S2.1).
 *
 * Responsibilities:
 *  - Layout: a fixed 140×80 container so layout engines can reason about
 *    consistent dimensions across node types.
 *  - Shape slot: each specialised wrapper renders its SVG/div shape as a
 *    child positioned absolutely behind the label overlay.
 *  - Selection visuals (AC-3): a glow when the node itself is selected; a
 *    fade when some *other* node is selected and this one is not a neighbour.
 *  - Handles: four source + four target handles (N/E/S/W) so arbitrary
 *    direction edges can attach on the shortest side.
 *
 * Explicitly NOT handled here:
 *  - Hover tooltip / info card — deferred to Sprint 5.
 *  - Concrete shape rendering — each node type owns its shape.
 */

import { memo, type ReactNode } from 'react';
import { Handle, Position } from '@xyflow/react';
import type { KGNodeData } from './types';

export interface NodeShellProps {
  data: KGNodeData;
  /** The shape SVG/div — rendered absolutely behind the label overlay. */
  children: ReactNode;
  /** Icon shown to the left of the title. */
  icon?: ReactNode;
  /** Foreground colour for the title and icon in light mode. */
  color: string;
  /** Foreground colour in dark mode (resolved via the root `dark` class). */
  darkColor: string;
  /** Optional data-testid so snapshot tests can pin a stable selector. */
  testId?: string;
}

export const NODE_WIDTH = 140;
export const NODE_HEIGHT = 80;

function isDarkMode(): boolean {
  if (typeof document === 'undefined') return false;
  return document.documentElement.classList.contains('dark');
}

function NodeShellImpl({ data, children, icon, color, darkColor, testId }: NodeShellProps) {
  const { kgNode, isSelected, isConnectedToSelected, hasSelection } = data;
  const dark = isDarkMode();
  const activeColor = dark ? darkColor : color;
  const isFaded = !!hasSelection && !isSelected && !isConnectedToSelected;

  return (
    <div
      data-testid={testId ?? `kg-node-${kgNode.node_type.toLowerCase()}`}
      data-node-id={kgNode.id}
      data-selected={isSelected ? 'true' : 'false'}
      data-faded={isFaded ? 'true' : 'false'}
      data-connected={isConnectedToSelected ? 'true' : 'false'}
      style={{
        position: 'relative',
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        // Tiered opacity (Spec 8 / S4.2):
        //   selected or connected → 1 (fully opaque + glow on the selected one)
        //   other nodes when something IS selected → 0.8 (gentle fade, not hidden)
        //   no selection → 1 (all nodes at full opacity)
        opacity: isFaded ? 0.8 : 1,
        transition: 'opacity 120ms ease-out, filter 120ms ease-out',
        filter: isSelected
          ? `drop-shadow(0 0 6px ${activeColor})`
          : undefined,
      }}
    >
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'none',
        }}
      >
        {children}
      </div>

      <div
        style={{
          position: 'relative',
          zIndex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
          padding: '6px 10px',
          textAlign: 'center',
          color: dark ? '#F9FAFB' : '#111827',
          fontFamily: 'system-ui, sans-serif',
          fontSize: 12,
          fontWeight: 500,
          gap: 4,
        }}
      >
        {icon ? (
          <span
            aria-hidden
            style={{
              color: activeColor,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 14,
            }}
          >
            {icon}
          </span>
        ) : null}
        <span
          title={kgNode.title}
          style={{
            maxWidth: '100%',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {kgNode.title}
        </span>
      </div>

      {SIDES.map(({ id, position }) => (
        <Handle
          key={`source-${id}`}
          id={`s-${id}`}
          type="source"
          position={position}
          style={HANDLE_STYLE}
        />
      ))}
      {SIDES.map(({ id, position }) => (
        <Handle
          key={`target-${id}`}
          id={`t-${id}`}
          type="target"
          position={position}
          style={HANDLE_STYLE}
        />
      ))}
    </div>
  );
}

const SIDES: Array<{ id: string; position: Position }> = [
  { id: 'top', position: Position.Top },
  { id: 'right', position: Position.Right },
  { id: 'bottom', position: Position.Bottom },
  { id: 'left', position: Position.Left },
];

const HANDLE_STYLE = {
  width: 6,
  height: 6,
  background: 'transparent',
  border: 'none',
} as const;

export const NodeShell = memo(NodeShellImpl);
