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
  type ImportOptions,
} from '@/services/import-export-api';

interface ImportExportButtonsProps {
  /** Envelope kind — also the exported file prefix (`<kind>-YYYYMMDD.json`). */
  kind: string;
  onExport: () => Promise<ImportExportEnvelope>;
  onImport: (
    envelope: ImportExportEnvelope,
    options?: ImportOptions,
  ) => Promise<ImportSummary>;
  /** Ask before replacing same-id objects in a non-versioned catalog. */
  confirmReplacements?: boolean;
  /** Refresh hook invoked after a successful (non-error) import. */
  onImported?: () => void | Promise<void>;
}

export function ImportExportButtons({
  kind,
  onExport,
  onImport,
  onImported,
  confirmReplacements = false,
}: ImportExportButtonsProps) {
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

  const handleImportFiles = async (files: File[]) => {
    setBusy(true);
    try {
      const envelopes: ImportExportEnvelope[] = [];
      for (const file of files) {
        try {
          envelopes.push(JSON.parse(await file.text()) as ImportExportEnvelope);
        } catch {
          toast.error('Invalid file: not valid JSON');
          return;
        }
      }
      const invalidKind = envelopes.find((item) => item?.kind !== kind);
      if (invalidKind) {
        toast.error(`Invalid import: expected ${kind}, received ${invalidKind.kind ?? 'unknown'}`);
        return;
      }
      if (envelopes.some(
        (item) => item?.schema_version !== '1' || !Array.isArray(item?.items),
      )) {
        toast.error('Invalid import envelope');
        return;
      }
      const envelope: ImportExportEnvelope = {
        schema_version: '1',
        kind,
        ...(envelopes.length === 1 && envelopes[0].exported_at
          ? { exported_at: envelopes[0].exported_at }
          : {}),
        items: envelopes.flatMap((item) => item.items),
      };

      let options: ImportOptions | undefined;
      if (confirmReplacements) {
        const preview = await onImport(envelope, { dryRun: true });
        const replacements = preview.skipped.filter(
          (item) => item.reason === 'replacement_requires_confirmation',
        );
        if (replacements.length > 0) {
          const accepted = window.confirm(
            `${replacements.length} existing item${replacements.length === 1 ? '' : 's'} `
            + 'will be replaced because the imported ID already exists. Continue?',
          );
          if (!accepted) {
            toast.error('Import cancelled');
            return;
          }
          options = { replaceExisting: true };
        }
      }

      const result = await onImport(envelope, options);
      const parts = [`${result.created} created`, `${result.skipped.length} skipped`];
      if (result.updated) parts.push(`${result.updated} versioned`);
      if (result.replaced) parts.push(`${result.replaced} replaced`);
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
        title="Download the complete catalog as a JSON file"
        className={buttonClass}
      >
        <Download size={14} />
        Export all
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => fileInputRef.current?.click()}
        data-testid={`${kind}-import`}
        title="Import one or more previously exported JSON files"
        className={buttonClass}
      >
        <Upload size={14} />
        Import
      </button>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept="application/json,.json"
        data-testid={`${kind}-import-input`}
        className="hidden"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = ''; // allow re-importing the same file
          if (files.length > 0) void handleImportFiles(files);
        }}
      />
    </>
  );
}

interface ExportItemButtonProps {
  kind: string;
  itemLabel: string;
  itemId: string;
  onExport: () => Promise<ImportExportEnvelope>;
}

/** Compact per-row export action that downloads a one-item envelope. */
export function ExportItemButton({
  kind,
  itemLabel,
  itemId,
  onExport,
}: ExportItemButtonProps) {
  const [busy, setBusy] = useState(false);

  const handleExport = async () => {
    setBusy(true);
    try {
      const envelope = await onExport();
      const safeLabel = itemLabel
        .trim()
        .replace(/[^a-z0-9_-]+/gi, '-')
        .replace(/^-+|-+$/g, '');
      const dated = importExportFilename(kind).replace(`${kind}-`, '');
      downloadJsonFile(`${kind}-${safeLabel || itemId}-${dated}`, envelope);
      toast.success(`Exported ${itemLabel}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Export failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => void handleExport()}
      data-testid={`${kind}-export-${itemId}`}
      title={`Export ${itemLabel} as JSON`}
      aria-label={`Export ${itemLabel}`}
      className="rounded p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50 dark:hover:bg-gray-700 dark:hover:text-gray-200"
    >
      <Download size={14} />
    </button>
  );
}
