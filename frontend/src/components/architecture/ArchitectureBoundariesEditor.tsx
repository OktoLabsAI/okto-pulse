interface Props {
  value: string[];
  onChange: (value: string[]) => void;
  readOnly?: boolean;
}

/** Each row is one boundary; punctuation never splits authored content. */
export function ArchitectureBoundariesEditor({ value, onChange, readOnly = false }: Props) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-xs text-gray-500 dark:text-gray-400">Boundaries</legend>
      {readOnly ? (
        value.length ? <ul className="list-disc pl-4 text-xs">{value.map((item, index) => <li key={index}>{item}</li>)}</ul>
          : <p className="text-xs text-gray-500">No boundaries provided.</p>
      ) : (
        <>
          {value.map((item, index) => (
            <div key={index} className="flex items-start gap-2">
              <label className="min-w-0 flex-1 text-xs text-gray-500 dark:text-gray-400">
                Boundary {index + 1}
                <textarea
                  value={item}
                  onChange={(event) => onChange(value.map((entry, i) => i === index ? event.target.value : entry))}
                  aria-invalid={!item.trim()}
                  required
                  rows={2}
                  className="mt-1 w-full rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100"
                />
                {!item.trim() && <span>Enter a boundary or remove this item.</span>}
              </label>
              <button type="button" aria-label={`Remove boundary ${index + 1}`} onClick={() => onChange(value.filter((_, i) => i !== index))} className="btn btn-secondary text-xs">Remove</button>
            </div>
          ))}
          <button type="button" onClick={() => onChange([...value, ''])} className="btn btn-secondary text-xs">Add boundary</button>
        </>
      )}
    </fieldset>
  );
}
