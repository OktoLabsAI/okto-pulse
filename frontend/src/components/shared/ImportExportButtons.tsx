/**
 * ImportExportButtons — shared Export/Import pair for the admin catalog
 * panels (Guidelines, Design Systems, Presets, Default Board Config).
 *
 * Export: calls the family's GET .../export, downloads the envelope as
 * `<kind>-YYYYMMDD.json`. Import: file picker → JSON.parse → POST
 * .../import → react-hot-toast summary (created / skipped / errors). A 400
 * from the backend (invalid envelope or invalid item — nothing mutated)
 * surfaces as a toast.error with the backend detail message.
 */

import { useRef, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  downloadJsonFile,
  importExportFilename,
  type ImportExportEnvelope,
  type ImportSummary,
} from '@/services/import-export-api';

interface ImportExportButtonsProps {
  /** Envelope kind — also the exported file prefix (`<kind>-YYYYMMDD.json`). */
  kind: string;
  onExport: () => Promise<ImportExportEnvelope>;
  onImport: (envelope: ImportExportEnvelope) => Promise<ImportSummary>;
  /** Refresh hook invoked after a successful (non-error) import. */
  onImported?: () => void | Promise<void>;
}

export function ImportExportButtons({ kind, onExport, onImport, onImported }: ImportExportButtonsProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  const buttonClass =
    'inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800';

  const handleExport = async () => {
    setBusy(true);
    try {
      const envelope = await onExport();
      downloadJsonFile(importExportFilename(kind), envelope);
      toast.success(`Exported ${envelope.items.length} item${envelope.items.length === 1 ? '' : 's'}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setBusy(false);
    }
  };

  const handleImportFile = async (file: File) => {
    setBusy(true);
    try {
      let envelope: ImportExportEnvelope;
      try {
        envelope = JSON.parse(await file.text()) as ImportExportEnvelope;
      } catch {
        toast.error('Invalid file: not valid JSON');
        return;
      }
      const result = await onImport(envelope);
      const parts = [`${result.created} created`, `${result.skipped.length} skipped`];
      if (result.errors.length > 0) parts.push(`${result.errors.length} error${result.errors.length === 1 ? '' : 's'}`);
      toast.success(`Import: ${parts.join(', ')}`);
      await onImported?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Import failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        type="button"
        disabled={busy}
        onClick={handleExport}
        data-testid={`${kind}-export`}
        title="Download this catalog as a JSON file"
        className={buttonClass}
      >
        <Download size={14} />
        Export
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => fileInputRef.current?.click()}
        data-testid={`${kind}-import`}
        title="Import a previously exported JSON file"
        className={buttonClass}
      >
        <Upload size={14} />
        Import
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        data-testid={`${kind}-import-input`}
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = ''; // allow re-importing the same file
          if (file) void handleImportFile(file);
        }}
      />
    </>
  );
}
