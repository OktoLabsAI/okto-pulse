import { useRef, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import toast from 'react-hot-toast';

import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import type { GuidelineExportEnvelopeV2 } from '@/types/policy-governance';

interface GuidelinePolicyTransferProps {
  boardId: string;
  onImported: () => void | Promise<void>;
}

function policyTransferError(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) return error.message;
  return error instanceof Error ? error.message : 'Unexpected policy transfer error.';
}

function parseEnvelope(raw: string): GuidelineExportEnvelopeV2 {
  const value = JSON.parse(raw) as Partial<GuidelineExportEnvelopeV2>;
  if (
    value.contract_version !== 'guideline-export/v2'
    || value.schema_version !== '2'
    || value.kind !== 'guidelines'
    || !Array.isArray(value.guidelines)
  ) {
    throw new Error('Select a guideline-export/v2 JSON file.');
  }
  return value as GuidelineExportEnvelopeV2;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function envelopeContainsBlockingRules(
  envelope: GuidelineExportEnvelopeV2,
): boolean {
  let containsBlockingRules = false;
  for (const aggregate of envelope.guidelines as unknown[]) {
    if (!isRecord(aggregate) || !Array.isArray(aggregate.revisions)) {
      throw new Error('Unable to classify policy rules in this import.');
    }
    for (const revision of aggregate.revisions) {
      if (!isRecord(revision) || !Array.isArray(revision.rules)) {
        throw new Error('Unable to classify policy rules in this import.');
      }
      for (const rule of revision.rules) {
        if (
          !isRecord(rule)
          || (rule.enforcement !== 'advisory'
            && rule.enforcement !== 'blocking')
        ) {
          throw new Error('Unable to classify policy rules in this import.');
        }
        if (rule.enforcement === 'blocking') {
          containsBlockingRules = true;
        }
      }
    }
  }
  return containsBlockingRules;
}

export function GuidelinePolicyTransfer({
  boardId,
  onImported,
}: GuidelinePolicyTransferProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<'export' | 'import' | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const authorityReady =
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired;
  const canExport =
    authorityReady && permissions.has('guidelines.revisions.read');
  const canImport =
    authorityReady
    && permissions.has('guidelines.revisions.create');
  const canAuthorBlockingRules =
    authorityReady && permissions.has('guidelines.rules.author_blocking');

  const exportPolicy = async () => {
    if (!canExport || busy) return;
    setBusy('export');
    setStatus(null);
    try {
      const envelope = await api.exportGuidelinePolicy(boardId);
      const blob = new Blob(
        [JSON.stringify(envelope, null, 2)],
        { type: 'application/json' },
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `guideline-policy-${boardId}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setStatus(`Exported ${envelope.guidelines.length} guideline aggregate(s).`);
    } catch (error) {
      toast.error(policyTransferError(error));
    } finally {
      setBusy(null);
    }
  };

  const importPolicy = async (file: File) => {
    if (!canImport || busy) return;
    setBusy('import');
    setStatus(null);
    try {
      const envelope = parseEnvelope(await file.text());
      if (
        envelopeContainsBlockingRules(envelope)
        && !canAuthorBlockingRules
      ) {
        throw new Error(
          'Importing blocking rules requires guidelines.rules.author_blocking.',
        );
      }
      const preview = await api.importGuidelinePolicy(
        boardId,
        envelope,
        { dryRun: true },
      );
      if (
        preview.transaction_status === 'rolled_back'
        || preview.conflict_count > 0
        || preview.overwritten_row_count > 0
      ) {
        throw new Error(
          preview.error_code
          ?? `Import preview found ${preview.conflict_count} conflict(s).`,
        );
      }
      const result = await api.importGuidelinePolicy(boardId, envelope);
      if (
        result.transaction_status !== 'committed'
        || result.overwritten_row_count !== 0
      ) {
        throw new Error(result.error_code ?? 'Policy import did not commit.');
      }
      setStatus(
        `Imported ${result.created_count}; skipped ${result.skip_identical_count} identical aggregate(s).`,
      );
      await onImported();
    } catch (error) {
      toast.error(policyTransferError(error));
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        disabled={!canExport || busy !== null}
        data-testid="guidelines-export"
        onClick={() => void exportPolicy()}
        title={
          canExport
            ? 'Export immutable guideline policy'
            : 'Requires guidelines.revisions.read'
        }
        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        <Download size={13} />
        {busy === 'export' ? 'Exporting…' : 'Export v2'}
      </button>
      <button
        type="button"
        disabled={!canImport || busy !== null}
        data-testid="guidelines-import"
        onClick={() => fileInputRef.current?.click()}
        title={
          canImport
            ? 'Dry-run and import immutable guideline policy'
            : 'Requires guidelines.revisions.create'
        }
        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        <Upload size={13} />
        {busy === 'import' ? 'Importing…' : 'Import v2'}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        data-testid="guidelines-import-input"
        className="hidden"
        aria-label="Import guideline policy v2"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void importPolicy(file);
        }}
      />
      {status && (
        <span className="sr-only" role="status">
          {status}
        </span>
      )}
    </div>
  );
}
