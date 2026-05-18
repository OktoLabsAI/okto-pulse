import { useRef, useState } from 'react';
import { FileUp, X, AlertTriangle, Info } from 'lucide-react';
import type { ArchitectureDiagramType } from '@/types';

export interface ExcalidrawImportPreflight {
  valid: boolean;
  warnings: string[];
  issues: string[];
  suggested_fixes: string[];
}

interface ExcalidrawImportDialogProps {
  open: boolean;
  onClose: () => void;
  onImport: (data: {
    title: string;
    description?: string;
    diagramType: ArchitectureDiagramType;
    payload: Record<string, unknown>;
    replaceDiagramId?: string | null;
  }) => Promise<void>;
  /**
   * Spec cc497a0d — optional pre-validation hook. When provided, the dialog calls
   * this with the parsed Excalidraw payload before invoking onImport and surfaces
   * warnings (semantic_metadata_normalized) and suggested_fixes inline. When the
   * preflight reports valid=false, the Import button blocks until issues are addressed.
   */
  onValidate?: (payload: Record<string, unknown>) => Promise<ExcalidrawImportPreflight>;
  replaceOptions?: { id: string; title: string }[];
}

export function ExcalidrawImportDialog({
  open,
  onClose,
  onImport,
  onValidate,
  replaceOptions = [],
}: ExcalidrawImportDialogProps) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [title, setTitle] = useState('Imported architecture');
  const [description, setDescription] = useState('');
  const [replaceDiagramId, setReplaceDiagramId] = useState<string>('');
  const [payloadText, setPayloadText] = useState('{\n  "type": "excalidraw",\n  "version": 2,\n  "elements": [],\n  "appState": {},\n  "files": {}\n}');
  const [error, setError] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [issues, setIssues] = useState<string[]>([]);
  const [suggestedFixes, setSuggestedFixes] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  if (!open) return null;

  const resetFeedback = () => {
    setError('');
    setWarnings([]);
    setIssues([]);
    setSuggestedFixes([]);
  };

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    const text = await file.text();
    setPayloadText(text);
    if (title === 'Imported architecture') {
      setTitle(file.name.replace(/\.(excalidraw|json)$/i, ''));
    }
  };

  const handleImport = async () => {
    resetFeedback();
    let parsed: unknown;
    try {
      parsed = JSON.parse(payloadText);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invalid JSON');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError('Payload must be a JSON object');
      return;
    }
    const objectPayload = parsed as Record<string, unknown>;
    setSaving(true);
    try {
      if (onValidate) {
        try {
          const preflight = await onValidate(objectPayload);
          setWarnings(preflight.warnings || []);
          setIssues(preflight.issues || []);
          setSuggestedFixes(preflight.suggested_fixes || []);
          if (!preflight.valid) {
            setError(
              preflight.issues.length > 0
                ? `Payload rejected: ${preflight.issues.length} issue(s) — review the suggested fixes below.`
                : 'Payload rejected by validation.',
            );
            return;
          }
        } catch (validationErr) {
          setError(
            validationErr instanceof Error
              ? `Validation failed: ${validationErr.message}`
              : 'Validation failed.',
          );
          return;
        }
      }
      await onImport({
        title: title.trim() || 'Imported architecture',
        description: description.trim() || undefined,
        diagramType: 'container',
        payload: objectPayload,
        replaceDiagramId: replaceDiagramId || null,
      });
      onClose();
    } catch (importErr) {
      const message = importErr instanceof Error ? importErr.message : 'Import failed.';
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  const renderFeedback = () => {
    if (!error && warnings.length === 0 && suggestedFixes.length === 0) return null;
    return (
      <div className="space-y-2" data-testid="excalidraw-import-feedback">
        {error && (
          <p className="text-sm text-red-600 dark:text-red-400" role="alert">{error}</p>
        )}
        {warnings.length > 0 && (
          <div
            className="rounded border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/30 p-2 text-amber-900 dark:text-amber-200 text-xs"
            data-testid="excalidraw-import-warnings"
          >
            <div className="flex items-center gap-1 font-semibold">
              <Info size={14} />
              Normalized fields ({warnings.length})
            </div>
            <ul className="mt-1 list-disc list-inside space-y-0.5">
              {warnings.map((warning, idx) => (
                <li key={idx}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
        {issues.length > 0 && (
          <div
            className="rounded border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-950/30 p-2 text-red-800 dark:text-red-300 text-xs"
            data-testid="excalidraw-import-issues"
          >
            <div className="flex items-center gap-1 font-semibold">
              <AlertTriangle size={14} />
              Issues ({issues.length})
            </div>
            <ul className="mt-1 list-disc list-inside space-y-0.5">
              {issues.map((issue, idx) => (
                <li key={idx}>{issue}</li>
              ))}
            </ul>
          </div>
        )}
        {suggestedFixes.length > 0 && (
          <div
            className="rounded border border-sky-300 dark:border-sky-700 bg-sky-50 dark:bg-sky-950/30 p-2 text-sky-900 dark:text-sky-200 text-xs"
            data-testid="excalidraw-import-fixes"
          >
            <div className="flex items-center gap-1 font-semibold">
              <Info size={14} />
              Suggested fixes
            </div>
            <ul className="mt-1 list-disc list-inside space-y-0.5">
              {suggestedFixes.map((fix, idx) => (
                <li key={idx}>{fix}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-[80] bg-black/50 flex items-center justify-center p-4">
      <div className="w-full max-w-4xl bg-white dark:bg-gray-900 rounded-lg shadow-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-wide text-gray-500">Architecture Import</p>
            <h3 className="text-base font-semibold text-gray-900 dark:text-white">Import Excalidraw</h3>
          </div>
          <button type="button" onClick={onClose} className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded" title="Close">
            <X size={18} />
          </button>
        </div>

        <div className="grid grid-cols-[280px_1fr] min-h-[520px]">
          <aside className="border-r border-gray-200 dark:border-gray-700 p-4 space-y-4 bg-gray-50 dark:bg-gray-950">
            <input
              ref={fileRef}
              type="file"
              accept=".json,.excalidraw,application/json"
              className="hidden"
              onChange={(event) => handleFile(event.target.files?.[0])}
            />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="w-full h-28 border-2 border-dashed border-cyan-300 dark:border-cyan-800 rounded-lg bg-cyan-50 dark:bg-cyan-950/30 text-cyan-700 dark:text-cyan-200 flex flex-col items-center justify-center gap-2"
            >
              <FileUp size={22} />
              <span className="text-sm font-medium">Choose JSON</span>
            </button>

            <label className="block">
              <span className="text-xs text-gray-500">Title</span>
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100"
              />
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Replace</span>
              <select
                value={replaceDiagramId}
                onChange={(event) => setReplaceDiagramId(event.target.value)}
                className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100"
              >
                <option value="">New diagram</option>
                {replaceOptions.map((item) => (
                  <option key={item.id} value={item.id}>{item.title}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-gray-500">Description</span>
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={4}
                className="mt-1 w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-700 rounded bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 resize-none"
              />
            </label>
          </aside>

          <main className="p-4 space-y-3">
            <textarea
              value={payloadText}
              onChange={(event) => {
                setPayloadText(event.target.value);
                if (warnings.length || issues.length || error) resetFeedback();
              }}
              className="w-full h-[380px] px-3 py-2 text-xs font-mono border border-gray-300 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none"
            />
            {renderFeedback()}
          </main>
        </div>

        <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-700 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="btn btn-secondary text-sm">Cancel</button>
          <button
            type="button"
            onClick={handleImport}
            disabled={saving || issues.length > 0}
            className="btn btn-primary text-sm disabled:opacity-50"
          >
            {saving ? 'Importing...' : issues.length > 0 ? 'Fix issues first' : 'Import'}
          </button>
        </div>
      </div>
    </div>
  );
}
