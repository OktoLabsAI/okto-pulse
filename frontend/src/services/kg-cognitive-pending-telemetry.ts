/**
 * Frontend telemetry emitter for the cognitive consolidation panel
 * (KG-03.5 / OR or_229dfe09).
 *
 * Bounded metric: ``kg_health_cognitive_pending_panel_state_total``.
 *
 * Allowed labels (Codex audit val_ed0f9548):
 *   - ``state``: one of ``loading | empty | ready | error``
 *   - ``has_generation``: ``"true"`` | ``"false"``
 *
 * FORBIDDEN as labels:
 *   - ``item_id``, ``source_ref``, ``agent``, ``agent_id``,
 *     ``reason``, ``reason_code``, ``kg_generation_id``,
 *     ``board_id``, any raw API message.
 *
 * The emitter records every sample in an in-memory queue so vitest
 * tests can assert the bounded shape. Production wiring can later
 * forward the queue to the metrics beacon — for now the contract is
 * satisfied by the in-process observability of the bounded sample
 * stream.
 */

export type KGCognitivePendingPanelState =
  | 'loading'
  | 'empty'
  | 'ready'
  | 'error';

export const KG_COGNITIVE_PENDING_PANEL_STATE_VALUES: readonly KGCognitivePendingPanelState[] = [
  'loading',
  'empty',
  'ready',
  'error',
];

export const KG_COGNITIVE_PENDING_PANEL_METRIC_LABELS = [
  'state',
  'has_generation',
] as const;

export interface KGCognitivePendingPanelSample {
  state: KGCognitivePendingPanelState;
  has_generation: 'true' | 'false';
}

const _samples: KGCognitivePendingPanelSample[] = [];

export function recordKGCognitivePendingPanelState(
  state: KGCognitivePendingPanelState,
  hasGeneration: boolean,
): void {
  // Bounded enums by construction — the TypeScript type signature
  // forbids any value outside the enum, and ``has_generation`` is
  // stringified to "true"|"false" so the label space is finite.
  if (!KG_COGNITIVE_PENDING_PANEL_STATE_VALUES.includes(state)) {
    return; // defensive: never emit an unbounded state value.
  }
  _samples.push({
    state,
    has_generation: hasGeneration ? 'true' : 'false',
  });
}

export function getKGCognitivePendingPanelSamples(): KGCognitivePendingPanelSample[] {
  return _samples.map((sample) => ({ ...sample }));
}

export function getKGCognitivePendingPanelEventCount(filter: {
  state?: KGCognitivePendingPanelState;
  has_generation?: 'true' | 'false';
} = {}): number {
  return _samples.filter(
    (s) =>
      (filter.state === undefined || s.state === filter.state) &&
      (filter.has_generation === undefined ||
        s.has_generation === filter.has_generation),
  ).length;
}

export function resetKGCognitivePendingPanelTelemetry(): void {
  _samples.length = 0;
}
