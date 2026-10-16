export type DeliveryGateMode = 'advisory' | 'blocking';

/** Current creation default applies only to an omitted field. */
export function resolveDeliveryGateMode(value: unknown): DeliveryGateMode | null {
  if (value === undefined) return 'blocking';
  return value === 'advisory' || value === 'blocking' ? value : null;
}
