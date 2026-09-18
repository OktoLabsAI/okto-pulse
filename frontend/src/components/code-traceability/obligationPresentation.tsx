// Shared presentation for delivery obligations: the stable ref is rendered
// as "Tipo (id)" with a per-type color on the type label (mockup feedback
// 2026-09-17: no raw ids as the primary metadata; type name colored per
// collection, slightly larger than the old 10px code font).

const TYPE_STYLES: Record<string, { label: string; className: string }> = {
  fr: { label: 'FR', className: 'text-blue-600 dark:text-blue-400' },
  tr: { label: 'TR', className: 'text-sky-600 dark:text-sky-400' },
  br: { label: 'BR', className: 'text-amber-600 dark:text-amber-400' },
  ac: { label: 'AC', className: 'text-emerald-600 dark:text-emerald-400' },
  api: { label: 'API', className: 'text-violet-600 dark:text-violet-400' },
  ir: { label: 'IR', className: 'text-pink-600 dark:text-pink-400' },
  or: { label: 'OR', className: 'text-teal-600 dark:text-teal-400' },
  decision: { label: 'Decision', className: 'text-orange-600 dark:text-orange-400' },
  card: { label: 'Card', className: 'text-gray-500 dark:text-gray-400' },
  spec: { label: 'Spec', className: 'text-gray-500 dark:text-gray-400' },
};

export function obligationRefPrefix(ref: string): string {
  const idx = ref.indexOf(':');
  return idx === -1 ? '' : ref.slice(0, idx);
}

/** "fr:fr_abc" → { label: 'FR', colored } */
export function obligationType(ref: string): { label: string; className: string } {
  return TYPE_STYLES[obligationRefPrefix(ref)] ?? { label: '?', className: 'text-gray-500 dark:text-gray-400' };
}

/** Inline "Tipo (id)" presentation for the secondary line under a title. */
export function ObligationRefText({ value }: { value: string }) {
  const type = obligationType(value);
  const idx = value.indexOf(':');
  const id = idx === -1 ? value : value.slice(idx + 1);
  return (
    <span className="text-xs">
      <span className={`font-medium ${type.className}`}>{type.label}</span>
      {' '}
      <span className="text-gray-400 dark:text-gray-500">({id})</span>
    </span>
  );
}
