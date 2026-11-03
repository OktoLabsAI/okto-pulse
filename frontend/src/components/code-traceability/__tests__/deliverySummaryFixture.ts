import type { DeliverySummary, DeliveryTask, DeliveryGroup } from '@/types/delivery-summary';

export function deliveryTask(id: string, group: DeliveryGroup): DeliveryTask {
  return { card_id: id, title: `Task ${id}`, card_type: 'normal',
    status: group === 'delivered' || group === 'optional_or_waived' ? 'done' : group === 'pending' ? 'validation' : 'in_progress',
    assignee_id: null, updated_at: '2026-11-03T15:00:00Z', group,
    reason: group === 'optional_or_waived' ? 'waived' : group === 'pending' ? 'awaiting_validation' : group,
    requirement: group === 'optional_or_waived' ? 'waived' : 'required',
    historical_done: group === 'delivered' || group === 'optional_or_waived', exception_applied: false };
}

export function deliverySummary(): DeliverySummary {
  return { contract_version: 'delivery-summary/v1', scope: { board_id:'b',spec_id:'s',spec_edition:2,spec_version:7 },
    projection:{ generation_id:'generation',snapshot_id:'a'.repeat(64),freshness:'current' },
    population_id:'b'.repeat(64),complete:true,task_count:4,required_task_count:3,unique_obligation_count:1,
    groups: ['delivered','in_progress','pending','optional_or_waived'].map((id,index) => ({
      id:id as DeliveryGroup,count:1,loaded:index !== 3,items:index === 3 ? [] : [deliveryTask(String(index),id as DeliveryGroup)],
      has_more:false,next_cursor:null })), mutation_reauthorization_required:true };
}
