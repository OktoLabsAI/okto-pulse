import {
  AlertCircle,
  Check,
  Download,
  FileCode2,
  FileText,
  Loader2,
  RefreshCw,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  useEffect,
  useId,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import toast from 'react-hot-toast';

import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import {
  buildPreflightReportDocument,
} from '@/lib/entity-export/reportDocument';
import { renderReportHtml } from '@/lib/entity-export/renderHtml';
import { renderReportMarkdown } from '@/lib/entity-export/renderMarkdown';
import { downloadBlob } from '@/lib/entity-export/security';
import { useEntityExportApi } from '@/services/entity-export-api';
import type {
  EntityExportFormat,
  EntityExportManifestSection,
  EntityExportPreflight,
  EntityExportScope,
  EntityExportType,
} from '@/types/entity-export';

type DialogTab = 'settings' | 'preview';

const STATE_LABELS: Record<EntityExportManifestSection['state'], string> = {
  included: 'Included',
  empty: 'Empty',
  omitted: 'Permission omitted',
  unavailable: 'Unavailable',
  not_applicable: 'Not applicable',
  error: 'Error',
};

const STATE_TONES: Record<EntityExportManifestSection['state'], string> = {
  included: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300',
  empty: 'bg-surface-200 text-surface-600 dark:bg-surface-700 dark:text-surface-300',
  omitted: 'bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-300',
  unavailable: 'bg-orange-100 text-orange-700 dark:bg-orange-950/60 dark:text-orange-300',
  not_applicable: 'bg-surface-200 text-surface-500 dark:bg-surface-800 dark:text-surface-400',
  error: 'bg-red-100 text-red-700 dark:bg-red-950/60 dark:text-red-300',
};

const ENTITY_TYPE_LABELS: Record<EntityExportType, string> = {
  story: 'Story',
  ideation: 'Ideation',
  refinement: 'Refinement',
  spec: 'Spec',
  sprint: 'Sprint',
  card: 'Card',
};

function humanizeStatus(status: string | null | undefined): string {
  if (!status) return 'Status not reported';
  return status
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function statusTone(status: string | null | undefined): string {
  if (status && ['approved', 'done', 'validated'].includes(status.toLowerCase())) {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/35 dark:text-emerald-300';
  }
  return 'border-surface-200 bg-surface-100 text-surface-600 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-300';
}

function lifecycleLabel(preflight: EntityExportPreflight): string {
  const parts = [
    preflight.identity.edition == null ? null : `Edition ${preflight.identity.edition}`,
    preflight.identity.version == null ? null : `Revision ${preflight.identity.version}`,
  ].filter((part): part is string => part !== null);
  return parts.join(' / ') || 'Lifecycle version not reported';
}

function stateCanBeDeselected(section: EntityExportManifestSection): boolean {
  return section.section_key !== 'base'
    && section.section_key !== 'identity'
    && (section.state === 'included' || section.state === 'empty');
}

function defaultSelectedSections(preflight: EntityExportPreflight): Set<string> {
  return new Set(preflight.sections
    .filter((section) => section.state !== 'not_applicable')
    .map((section) => section.section_key));
}

function humanError(error: unknown): string {
  if (error instanceof DOMException && error.name === 'AbortError') return '';
  return error instanceof Error ? error.message : 'The export could not be prepared.';
}

interface EntityExportDialogProps {
  boardId: string;
  entityType: EntityExportType;
  entityId: string;
  entityTitle: string;
  onClose: () => void;
  opener: HTMLElement | null;
}

export function EntityExportDialog({
  boardId,
  entityType,
  entityId,
  entityTitle,
  onClose,
  opener,
}: EntityExportDialogProps) {
  const api = useEntityExportApi();
  const titleId = useId();
  const tabsId = useId();
  const { dialogRef, onKeyDown } = useDialogFocusTrap(true, '[data-export-initial-focus]');
  const [tab, setTab] = useState<DialogTab>('settings');
  const [scope, setScope] = useState<EntityExportScope>('complete');
  const [format, setFormat] = useState<EntityExportFormat>('markdown');
  const [preflight, setPreflight] = useState<EntityExportPreflight | null>(null);
  const [selectedSections, setSelectedSections] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEscapeToClose(onClose, {
    enabled: true,
    canClose: !downloading,
    priority: 100,
  });

  useEffect(() => {
    const parentDialog = opener?.closest<HTMLElement>('[role="dialog"]') ?? null;
    const previousAriaModal = parentDialog?.getAttribute('aria-modal') ?? null;
    parentDialog?.removeAttribute('aria-modal');
    return () => {
      if (!parentDialog) return;
      if (previousAriaModal === null) parentDialog.removeAttribute('aria-modal');
      else parentDialog.setAttribute('aria-modal', previousAriaModal);
    };
  }, [opener]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setPreflight(null);
    api.preflight(boardId, entityType, entityId, { scope }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setPreflight(result);
        setSelectedSections(defaultSelectedSections(result));
        setFormat((current) => result.formats.includes(current)
          ? current
          : result.formats[0] ?? 'markdown');
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setPreflight(null);
        const message = humanError(caught);
        if (message) setError(message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [api, boardId, entityId, entityType, reloadKey, scope]);

  const selectedManifest = useMemo(
    () => preflight?.sections.filter((section) => selectedSections.has(section.section_key)) ?? [],
    [preflight, selectedSections],
  );
  const blockingSections = selectedManifest.filter(
    (section) => section.state === 'unavailable' || section.state === 'error',
  );
  const permissionOmissions = selectedManifest.filter((section) => section.state === 'omitted');
  const includedSections = selectedManifest.filter((section) => section.state === 'included');
  const canDownload = Boolean(
    preflight
      && preflight.complete_for_actor
      && preflight.formats.includes(format)
      && selectedManifest.length > 0
      && blockingSections.length === 0,
  );
  const previewDocument = useMemo(
    () => preflight
      ? buildPreflightReportDocument(preflight, selectedSections)
      : null,
    [preflight, selectedSections],
  );
  const previewContent = useMemo(() => {
    if (!previewDocument) return '';
    return format === 'html'
      ? renderReportHtml(previewDocument)
      : renderReportMarkdown(previewDocument);
  }, [format, previewDocument]);

  const toggleSection = (section: EntityExportManifestSection) => {
    if (!stateCanBeDeselected(section)) return;
    setSelectedSections((current) => {
      const next = new Set(current);
      if (next.has(section.section_key)) next.delete(section.section_key);
      else next.add(section.section_key);
      return next;
    });
  };

  const handleDownload = async () => {
    if (!preflight || !canDownload) return;
    const controller = new AbortController();
    setDownloading(true);
    setError(null);
    try {
      const requestedSections = Array.from(selectedSections).sort();
      // The fingerprint covers both the entity snapshot and the requested
      // section set. Seal the exact selection immediately before download so
      // a subset never reuses the fingerprint issued for the full manifest.
      const downloadPreflight = await api.preflight(
        boardId,
        entityType,
        entityId,
        { scope, sections: requestedSections },
        controller.signal,
      );
      const requestedManifest = downloadPreflight.sections.filter(
        (section) => section.section_key === 'base'
          || section.section_key === 'identity'
          || selectedSections.has(section.section_key),
      );
      const returnedKeys = new Set(requestedManifest.map((section) => section.section_key));
      const missingSections = requestedSections.filter((key) => !returnedKeys.has(key));
      const blockingRequestedSections = requestedManifest.filter(
        (section) => section.state === 'unavailable' || section.state === 'error',
      );
      if (
        !downloadPreflight.complete_for_actor
        || missingSections.length > 0
        || blockingRequestedSections.length > 0
        || !downloadPreflight.formats.includes(format)
      ) {
        throw new Error('The selected report is no longer complete. Review the refreshed sources and try again.');
      }
      const attachment = await api.download(
        boardId,
        entityType,
        entityId,
        {
          format,
          scope,
          sections: requestedSections,
          expected_snapshot_fingerprint: downloadPreflight.snapshot_fingerprint,
        },
        entityTitle,
        controller.signal,
      );
      downloadBlob(attachment.blob, attachment.filename);
      toast.success(`${format === 'html' ? 'HTML' : 'Markdown'} report downloaded`);
      onClose();
    } catch (caught) {
      const message = humanError(caught);
      if (message) setError(message);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 p-3 sm:p-6">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={loading || downloading}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-surface-200 bg-white shadow-2xl dark:border-surface-700 dark:bg-surface-900"
      >
        <header className="flex items-start justify-between gap-4 border-b border-surface-200 px-5 py-4 dark:border-surface-700 sm:px-6">
          <div className="flex min-w-0 items-start gap-3">
            <span className="mt-0.5 rounded-xl bg-violet-100 p-2 text-violet-700 dark:bg-violet-950/60 dark:text-violet-300">
              <Download size={18} aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 id={titleId} className="text-base font-semibold text-surface-900 dark:text-white">
                  Export Report
                </h2>
                <span className="rounded-md border border-violet-200 bg-violet-50 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-violet-700 dark:border-violet-800 dark:bg-violet-950/35 dark:text-violet-300">
                  {ENTITY_TYPE_LABELS[preflight?.identity.entity_type ?? entityType]}
                </span>
              </div>
              <p className="mt-1 break-words text-sm font-medium text-surface-700 dark:text-surface-200">
                {preflight?.identity.title || entityTitle}
              </p>
              {preflight && (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-surface-500 dark:text-surface-400">
                  <span className={`rounded-full border px-2 py-0.5 font-semibold ${statusTone(preflight.identity.status)}`}>
                    {humanizeStatus(preflight.identity.status)}
                  </span>
                  <span>{lifecycleLabel(preflight)}</span>
                </div>
              )}
            </div>
          </div>
          <button
            type="button"
            aria-label="Close export report"
            disabled={downloading}
            onClick={onClose}
            className="rounded-lg p-1.5 text-surface-400 hover:bg-surface-100 hover:text-surface-700 disabled:opacity-40 dark:hover:bg-surface-800 dark:hover:text-surface-200"
          >
            <X size={18} />
          </button>
        </header>

        <div className="border-b border-surface-200 px-5 pt-3 dark:border-surface-700 sm:px-6">
          <AccessibleTabList
            idBase={tabsId}
            ariaLabel="Export report workflow"
            items={[
              { id: 'settings', label: 'Export settings' },
              { id: 'preview', label: 'Preview & manifest' },
            ] as const}
            value={tab}
            onValueChange={setTab}
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-6">
          {error && (
            <div role="alert" className="mb-4 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
              <AlertCircle size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
              <span>{error}</span>
            </div>
          )}

          {loading && !preflight ? (
            <div role="status" className="flex min-h-64 flex-col items-center justify-center text-sm text-surface-500 dark:text-surface-400">
              <Loader2 size={28} className="mb-3 animate-spin text-violet-500" aria-hidden="true" />
              Checking report completeness and permissions...
            </div>
          ) : !preflight ? (
            <div className="flex min-h-64 flex-col items-center justify-center text-center">
              <AlertCircle size={30} className="mb-3 text-red-500" aria-hidden="true" />
              <p className="text-sm font-semibold text-surface-800 dark:text-surface-100">Preflight is required before export</p>
              <button
                type="button"
                data-export-initial-focus
                onClick={() => setReloadKey((value) => value + 1)}
                className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-surface-300 px-3 py-2 text-xs font-semibold text-surface-700 dark:border-surface-600 dark:text-surface-200"
              >
                <RefreshCw size={14} /> Retry preflight
              </button>
            </div>
          ) : (
            <>
              <AccessibleTabPanel idBase={tabsId} tabId="settings" value={tab} className="space-y-5">
                <div className="grid gap-4 lg:grid-cols-2">
                  <fieldset>
                    <legend className="mb-2 text-xs font-semibold text-surface-800 dark:text-surface-100">Format</legend>
                    <div className="grid grid-cols-2 gap-2">
                      {([
                        ['markdown', 'Markdown', FileText],
                        ['html', 'Rich HTML', FileCode2],
                      ] as const).map(([value, label, Icon]) => {
                        const supported = preflight.formats.includes(value);
                        return (
                          <button
                            key={value}
                            type="button"
                            data-export-initial-focus={value === 'markdown' ? '' : undefined}
                            disabled={!supported}
                            aria-pressed={format === value}
                            onClick={() => setFormat(value)}
                            className={`flex min-h-16 items-center gap-3 rounded-xl border p-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${format === value ? 'border-violet-500 bg-violet-50 text-violet-800 ring-1 ring-violet-500 dark:bg-violet-950/35 dark:text-violet-200' : 'border-surface-200 text-surface-700 hover:border-surface-300 dark:border-surface-700 dark:text-surface-200'}`}
                          >
                            <Icon size={19} aria-hidden="true" />
                            <span><strong className="block text-xs">{label}</strong><span className="mt-0.5 block text-[10px] opacity-70">{value === 'html' ? 'Visual, standalone and print-ready' : 'Portable, diff-friendly document'}</span></span>
                          </button>
                        );
                      })}
                    </div>
                  </fieldset>

                  <fieldset>
                    <legend className="mb-2 text-xs font-semibold text-surface-800 dark:text-surface-100">Report scope</legend>
                    <div className="grid grid-cols-2 gap-2">
                      {([
                        ['current', 'Current', 'Current lifecycle edition only'],
                        ['complete', 'Complete', 'Current result and available history'],
                      ] as const).map(([value, label, description]) => (
                        <button
                          key={value}
                          type="button"
                          aria-pressed={scope === value}
                          onClick={() => setScope(value)}
                          className={`min-h-16 rounded-xl border p-3 text-left transition-colors ${scope === value ? 'border-violet-500 bg-violet-50 text-violet-800 ring-1 ring-violet-500 dark:bg-violet-950/35 dark:text-violet-200' : 'border-surface-200 text-surface-700 hover:border-surface-300 dark:border-surface-700 dark:text-surface-200'}`}
                        >
                          <strong className="block text-xs">{label}</strong>
                          <span className="mt-1 block text-[10px] opacity-70">{description}</span>
                        </button>
                      ))}
                    </div>
                  </fieldset>
                </div>

                <section aria-labelledby="export-content-title">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 id="export-content-title" className="text-sm font-semibold text-surface-900 dark:text-white">Content manifest</h3>
                      <p className="mt-0.5 text-[11px] text-surface-500 dark:text-surface-400">Every requested section receives an explicit state. Permission and source gaps are never hidden.</p>
                    </div>
                    <button
                      type="button"
                      disabled={loading}
                      onClick={() => setReloadKey((value) => value + 1)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-surface-300 px-2.5 py-1.5 text-[11px] font-semibold text-surface-600 hover:bg-surface-50 disabled:opacity-40 dark:border-surface-600 dark:text-surface-300 dark:hover:bg-surface-800"
                    >
                      <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> Refresh
                    </button>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {preflight.sections.map((section) => {
                      const selectable = stateCanBeDeselected(section);
                      const selected = selectedSections.has(section.section_key);
                      return (
                        <button
                          key={section.section_key}
                          type="button"
                          disabled={!selectable}
                          aria-pressed={selected}
                          onClick={() => toggleSection(section)}
                          className="flex min-h-16 items-start gap-3 rounded-xl border border-surface-200 bg-white p-3 text-left disabled:cursor-default dark:border-surface-700 dark:bg-surface-900"
                        >
                          <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${selected ? 'border-violet-600 bg-violet-600 text-white' : 'border-surface-300 dark:border-surface-600'}`}>
                            {selected && <Check size={11} aria-hidden="true" />}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="flex flex-wrap items-center justify-between gap-2">
                              <strong className="truncate text-xs text-surface-800 dark:text-surface-100">{section.label}</strong>
                              <span className={`rounded-full px-2 py-0.5 text-[9px] font-bold uppercase ${STATE_TONES[section.state]}`}>{STATE_LABELS[section.state]}</span>
                            </span>
                            <span className="mt-1 block text-[10px] text-surface-500 dark:text-surface-400">{section.message || section.reason_code || (section.total_count == null ? 'State verified by the server' : `${section.total_count} record(s)`)}</span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </section>
              </AccessibleTabPanel>

              <AccessibleTabPanel idBase={tabsId} tabId="preview" value={tab} className="space-y-4">
                <div className="grid gap-3 sm:grid-cols-3">
                  <SummaryCard label="Included" value={`${includedSections.length} section${includedSections.length === 1 ? '' : 's'}`} icon={<Check size={15} />} />
                  <SummaryCard label="Permission omitted" value={`${permissionOmissions.length} section${permissionOmissions.length === 1 ? '' : 's'}`} icon={<ShieldCheck size={15} />} />
                  <SummaryCard label="Issues" value={`${blockingSections.length} section${blockingSections.length === 1 ? '' : 's'}`} icon={<AlertCircle size={15} />} />
                </div>
                <p className="text-[11px] text-surface-500 dark:text-surface-400">This is a structural preflight preview, not a copy of permission-sensitive report content. The downloaded attachment is rendered authoritatively by the server from a newly sealed snapshot of the exact section selection.</p>
                {format === 'html' ? (
                  <iframe
                    title="HTML report structural preview"
                    sandbox=""
                    referrerPolicy="no-referrer"
                    srcDoc={previewContent}
                    className="h-[430px] w-full rounded-xl border border-surface-300 bg-white dark:border-surface-700"
                  />
                ) : (
                  <pre className="max-h-[430px] overflow-auto whitespace-pre-wrap rounded-xl border border-surface-300 bg-surface-950 p-4 text-[11px] leading-relaxed text-surface-100 dark:border-surface-700"><code>{previewContent}</code></pre>
                )}
                <details className="rounded-xl border border-surface-200 bg-surface-50 p-3 text-[11px] dark:border-surface-700 dark:bg-surface-800/30">
                  <summary className="cursor-pointer font-semibold text-surface-700 dark:text-surface-200">
                    Technical export metadata
                  </summary>
                  <dl className="mt-3 grid gap-3 sm:grid-cols-2">
                    <TechnicalMetadata label="Entity ID" value={preflight.identity.entity_id} />
                    <TechnicalMetadata label="Schema" value={preflight.schema_version} />
                    <TechnicalMetadata label="Snapshot fingerprint" value={preflight.snapshot_fingerprint} wide />
                  </dl>
                </details>
              </AccessibleTabPanel>
            </>
          )}
        </div>

        <footer className="border-t border-surface-200 bg-surface-50 px-5 py-4 dark:border-surface-700 dark:bg-surface-900 sm:px-6">
          {preflight && !preflight.source_complete && preflight.complete_for_actor && (
            <p className="mb-3 text-[11px] text-amber-700 dark:text-amber-300">This is complete for your access, not globally complete. {permissionOmissions.length} permission-omitted section(s) remain visible in the manifest.</p>
          )}
          {preflight && (!preflight.complete_for_actor || blockingSections.length > 0) && (
            <p role="alert" className="mb-3 text-[11px] text-red-700 dark:text-red-300">Download is blocked because the selected snapshot is incomplete for you or contains unavailable/error sections. Refresh after resolving the reported sources.</p>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-[10px] text-surface-500 dark:text-surface-400">Snapshot mismatch at download time fails closed; a changed entity is never exported under an older preflight.</p>
            <div className="flex items-center gap-2">
              <button type="button" onClick={onClose} disabled={downloading} className="rounded-lg border border-surface-300 px-3 py-2 text-xs font-semibold text-surface-600 disabled:opacity-40 dark:border-surface-600 dark:text-surface-300">Cancel</button>
              <button
                type="button"
                onClick={() => void handleDownload()}
                disabled={!canDownload || downloading || loading}
                className="inline-flex min-w-36 items-center justify-center gap-1.5 rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {downloading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                {downloading ? 'Generating...' : `Download ${format === 'html' ? 'HTML' : 'Markdown'}`}
              </button>
            </div>
          </div>
        </footer>
      </div>
    </div>
  );
}

function TechnicalMetadata({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? 'sm:col-span-2' : undefined}>
      <dt className="font-semibold uppercase tracking-wide text-surface-500 dark:text-surface-400">{label}</dt>
      <dd className="mt-1 break-all font-mono text-[10px] text-surface-700 dark:text-surface-200">{value}</dd>
    </div>
  );
}

function SummaryCard({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-surface-200 bg-surface-50 p-3 dark:border-surface-700 dark:bg-surface-800/40">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-violet-600 dark:text-violet-300">{icon}{label}</div>
      <p className="mt-1 truncate font-mono text-xs font-semibold text-surface-800 dark:text-surface-100">{value}</p>
    </div>
  );
}
