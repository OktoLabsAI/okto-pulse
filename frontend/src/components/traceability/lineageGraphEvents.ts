import type { LineageEntityType } from '@/types';

export const LINEAGE_GRAPH_EVENT = 'okto:open-lineage-graph';

export interface OpenLineageGraphDetail {
  entityType: Exclude<LineageEntityType, 'artifact'>;
  entityId: string;
}

export function openLineageGraph(
  entityType: OpenLineageGraphDetail['entityType'],
  entityId: string,
) {
  window.dispatchEvent(
    new CustomEvent<OpenLineageGraphDetail>(LINEAGE_GRAPH_EVENT, {
      detail: { entityType, entityId },
    }),
  );
}
