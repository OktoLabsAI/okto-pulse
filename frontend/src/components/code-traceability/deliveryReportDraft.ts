import type { CardDeliveryBatchDraft, DeliverySelectionInput } from '@/types/delivery-evidence';

export function sameDeliveryBasis(batch: CardDeliveryBatchDraft, selection: DeliverySelectionInput) {
  return batch.expected_card_version === selection.expected_card_version
    && batch.expected_spec_edition === selection.expected_spec_edition
    && batch.expected_delivery_revision === selection.expected_delivery_revision;
}
