export const deliveryGroups = ['delivered', 'in_progress', 'pending', 'optional_or_waived'] as const;
export type DeliveryGroup = typeof deliveryGroups[number];
export type DeliveryReason = 'delivered' | 'in_progress' | 'not_started' | 'awaiting_validation'
  | 'paused' | 'needs_rework' | 'cancelled' | 'awaiting_dependency' | 'awaiting_confirmation'
  | 'updating' | 'unavailable' | 'optional' | 'waived';
export interface DeliveryTask {
  card_id: string; title: string; card_type: 'normal' | 'bug' | 'test'; status: string;
  assignee_id: string | null; updated_at: string | null; group: DeliveryGroup; reason: DeliveryReason;
  requirement: 'required' | 'optional' | 'waived' | 'unknown'; historical_done: boolean; exception_applied: boolean;
}
export interface DeliveryTaskGroup {
  id: DeliveryGroup; count: number | null; loaded: boolean; items: DeliveryTask[];
  has_more: boolean; next_cursor: string | null;
}
export interface DeliverySummary {
  contract_version: 'delivery-summary/v1';
  scope: { board_id: string; spec_id: string; spec_edition: number; spec_version: number };
  projection: { generation_id: string | null; snapshot_id: string | null;
    freshness: 'current' | 'updating' | 'pending_initial' | 'unavailable' };
  population_id: string; complete: boolean; task_count: number | null;
  required_task_count: number | null; unique_obligation_count: number | null;
  groups: DeliveryTaskGroup[]; mutation_reauthorization_required: true;
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const identity = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const digest = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const reasons = ['delivered','in_progress','not_started','awaiting_validation','paused','needs_rework','cancelled',
  'awaiting_dependency','awaiting_confirmation','updating','unavailable','optional','waived'];

/** Reject ambiguous populations; a failed read is never an empty task list. */
export function isDeliverySummary(value: unknown): value is DeliverySummary {
  if (!object(value) || value.contract_version !== 'delivery-summary/v1' || !object(value.scope)
    || !identity(value.scope.board_id) || !identity(value.scope.spec_id)
    || !count(value.scope.spec_edition) || value.scope.spec_edition < 1
    || !count(value.scope.spec_version) || value.scope.spec_version < 1
    || !digest(value.population_id) || typeof value.complete !== 'boolean'
    || value.mutation_reauthorization_required !== true || !object(value.projection)
    || !['current','updating','pending_initial','unavailable'].includes(String(value.projection.freshness))
    || !(value.projection.generation_id === null || identity(value.projection.generation_id))
    || !(value.projection.snapshot_id === null || digest(value.projection.snapshot_id))
    || value.projection.freshness === 'current' && (!value.projection.generation_id || !value.projection.snapshot_id)
    || value.complete && value.projection.freshness !== 'current'
    || !Array.isArray(value.groups) || value.groups.length !== 4) return false;
  const ids = new Set<string>();
  for (const [index, group] of value.groups.entries()) {
    if (!object(group) || group.id !== deliveryGroups[index] || typeof group.loaded !== 'boolean'
      || typeof group.has_more !== 'boolean' || !Array.isArray(group.items) || group.items.length > 20
      || !(group.next_cursor === null || typeof group.next_cursor === 'string' && group.next_cursor.length > 0 && group.next_cursor.length <= 8192)
      || group.has_more !== Boolean(group.next_cursor)
      || group.has_more && group.items.length === 0
      || !group.loaded && (group.items.length > 0 || group.has_more)
      || !(value.complete ? count(group.count) && group.count >= group.items.length : group.count === null)) return false;
    for (const task of group.items) {
      if (!object(task) || !identity(task.card_id) || ids.has(task.card_id)
        || typeof task.title !== 'string' || task.title.length < 1 || task.title.length > 500
        || !['normal','bug','test'].includes(String(task.card_type)) || !identity(task.status)
        || !(task.assignee_id === null || identity(task.assignee_id))
        || !(task.updated_at === null || typeof task.updated_at === 'string' && Number.isFinite(Date.parse(task.updated_at)))
        || task.group !== group.id || !reasons.includes(String(task.reason))
        || !['required','optional','waived','unknown'].includes(String(task.requirement))
        || typeof task.historical_done !== 'boolean' || typeof task.exception_applied !== 'boolean'
        || task.historical_done !== (task.status === 'done')
        || group.id === 'delivered' && (task.status !== 'done' || task.reason !== 'delivered' || task.requirement !== 'required')
        || group.id === 'in_progress' && (task.status !== 'in_progress' || task.reason !== 'in_progress')
        || (group.id === 'optional_or_waived') !== ['optional','waived'].includes(String(task.requirement))
        || group.id === 'optional_or_waived' && task.reason !== task.requirement) return false;
      ids.add(task.card_id);
    }
  }
  const totals = [value.task_count, value.required_task_count, value.unique_obligation_count];
  if (!value.complete) return totals.every(total => total === null);
  if (!totals.every(count)) return false;
  const groups = value.groups as unknown as DeliveryTaskGroup[];
  return groups.reduce((total, group) => total + group.count!, 0) === value.task_count
    && groups.slice(0, 3).reduce((total, group) => total + group.count!, 0) === value.required_task_count;
}
