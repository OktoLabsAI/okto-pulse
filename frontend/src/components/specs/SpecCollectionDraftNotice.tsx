import type { useSpecCollectionEditor } from './useSpecCollectionEditor';

export function SpecCollectionDraftNotice({ editor, active, removed = false, missing = [], onRemoveMissing, currentText }: {
  editor: ReturnType<typeof useSpecCollectionEditor>;
  active: boolean;
  removed?: boolean;
  missing?: string[];
  onRemoveMissing?: () => void;
  currentText?: string;
}) {
  return <div className="space-y-2 text-sm" aria-live="polite">
    {editor.error && <p role="alert" className="text-red-600 dark:text-red-400">{editor.error}</p>}
    {active && editor.unavailable && <p role="status">Current Spec could not be verified. Your draft is preserved; refresh before saving.</p>}
    {active && removed && <p role="alert">This item is no longer available. Your draft is preserved, but it cannot be saved.</p>}
    {active && missing.length > 0 && <div role="alert" className="rounded-lg border border-amber-300 p-3">
      <p>Selected references are no longer available: {missing.join(', ')}.</p>
      {onRemoveMissing && <button className="btn btn-secondary text-xs mt-2" disabled={editor.saving} onClick={onRemoveMissing}>Remove unavailable references</button>}
    </div>}
    {active && (editor.changed || editor.conflict) && !editor.partial && <div className="rounded-lg border border-amber-300 p-3">
      <p>The Spec changed. Review its current content and references before saving your preserved draft.</p>
      {currentText && <details className="mt-2"><summary className="cursor-pointer font-medium">Current saved item</summary><p className="mt-2 whitespace-pre-wrap">{currentText}</p></details>}
      <button className="btn btn-secondary text-xs mt-2" onClick={editor.review}
        disabled={editor.saving || editor.unavailable || removed || (editor.conflict && !editor.changed)}>Review current base</button>
    </div>}
  </div>;
}
