import { useRef, useState } from 'react';
import { FileUp, X } from 'lucide-react';
import type { ArchitectureDiagramType } from '@/types';

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
  replaceOptions?: { id: string; title: string }[];
}

export function ExcalidrawImportDialog({ open, onClose, onImport, replaceOptions = [] }: ExcalidrawImportDialogProps) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [title, setTitle] = useState('Imported architecture');
  const [description, setDescription] = useState('');
  const [replaceDiagramId, setReplaceDiagramId] = useState<string>('');
  const [payloadText, setPayloadText] = useState('{\n  "type": "excalidraw",\n  "version": 2,\n  "elements": [],\n  "appState": {},\n  "files": {}\n}');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  if (!open) return null;

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    const text = await file.text();
    setPayloadText(text);
    if (title === 'Imported architecture') {
      setTitle(file.name.replace(/\.(excalidraw|json)$/i, ''));
    }
  };

  const handleImport = async () => {
    setError('');
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
    setSaving(true);
    try {
      await onImport({
        title: title.trim() || 'Imported architecture',
        description: description.trim() || undefined,
        diagramType: 'container',
        payload: parsed as Record<string, unknown>,
        replaceDiagramId: replaceDiagramId || null,
      });
      onClose();
    } finally {
      setSaving(false);
    }
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
              onChange={(event) => setPayloadText(event.target.value)}
              className="w-full h-[430px] px-3 py-2 text-xs font-mono border border-gray-300 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100 resize-none"
            />
            {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          </main>
        </div>

        <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-700 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="btn btn-secondary text-sm">Cancel</button>
          <button type="button" onClick={handleImport} disabled={saving} className="btn btn-primary text-sm disabled:opacity-50">
            {saving ? 'Importing...' : 'Import'}
          </button>
        </div>
      </div>
    </div>
  );
}
