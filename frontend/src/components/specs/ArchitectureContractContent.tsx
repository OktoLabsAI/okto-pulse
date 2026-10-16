/** Present the original structured contract without reinterpreting its protocol. */
export function ArchitectureContractContent({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null) return <span className="text-gray-400">Not specified</span>;
  if (typeof value === 'string') return <p className="whitespace-pre-wrap break-words leading-relaxed">{value}</p>;
  if (typeof value !== 'object') return <span>{String(value)}</span>;
  if (depth >= 5) return <pre className="overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(value, null, 2)}</pre>;
  if (Array.isArray(value)) return <ul className="list-disc space-y-1 pl-4">{value.map((item, index) =>
    <li key={index}><ArchitectureContractContent value={item} depth={depth + 1} /></li>)}</ul>;
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length) return <span className="text-amber-600">Empty object (no declared constraints)</span>;
  return <dl className="space-y-3">{entries.map(([key, item]) => <div key={key}
    className={depth === 0 ? 'rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800' : 'border-l border-gray-200 pl-3 dark:border-gray-700'}>
    <dt className="mb-1 font-semibold text-gray-700 dark:text-gray-200">{key.replace(/_/g, ' ')}</dt>
    <dd className="text-gray-600 dark:text-gray-300"><ArchitectureContractContent value={item} depth={depth + 1} /></dd>
  </div>)}</dl>;
}
