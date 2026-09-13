import { useCallback, useMemo, useState } from 'react';
import type { Node, NodeChange } from '@xyflow/react';

/** Keep DOM measurements in controlled nodes without allowing layout mutations. */
export function useMeasuredLineageNodes<T extends Node>(layout: T[]) {
  const [sizes, setSizes] = useState<Record<string, { width: number; height: number }>>({});
  const nodes = useMemo(() => layout.map(node => sizes[node.id]
    ? { ...node, measured: sizes[node.id] } : node), [layout, sizes]);
  const onNodesChange = useCallback((changes: NodeChange<T>[]) => {
    const dimensions = changes.filter(change => change.type === 'dimensions' && change.dimensions);
    if (!dimensions.length) return;
    setSizes(previous => {
      const ids = new Set(layout.map(node => node.id));
      const next = Object.fromEntries(Object.entries(previous).filter(([id]) => ids.has(id)));
      let changed = Object.keys(next).length !== Object.keys(previous).length;
      for (const change of dimensions) {
        if (change.type !== 'dimensions' || !change.dimensions || !ids.has(change.id)) continue;
        if (next[change.id]?.width === change.dimensions.width && next[change.id]?.height === change.dimensions.height) continue;
        next[change.id] = change.dimensions;
        changed = true;
      }
      return changed ? next : previous;
    });
  }, [layout]);
  return { nodes, onNodesChange };
}
