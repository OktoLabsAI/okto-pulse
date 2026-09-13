import { useCallback, useEffect, useRef, useState } from "react";
import { Brain, CheckCircle2, ExternalLink, RefreshCw, X } from "lucide-react";
import {
  clearCognitiveSkip,
  getReadinessItems,
  getReadinessMetrics,
  recordCognitiveSkip,
} from "@/services/cognitive-readiness-api";
import {
  isRevisitRequiredReason,
  isTechnicalBlocker,
  SELECTABLE_REASON_CODES,
  type CognitiveReadinessItem,
  type CognitiveReadinessListResponse,
  type CognitiveReadinessMetrics,
  type ReadinessSignalFilter,
} from "@/types/cognitive-readiness";
import { usePermissions } from "@/hooks/usePermissions";
import { useDashboardApi } from "@/services/api";
import { useDashboardStore } from "@/store/dashboard";
import {
  useModalStack,
  type ModalStackEntry,
} from "@/contexts/ModalStackContext";
import { DeadLetterInspectorModal } from "./DeadLetterInspectorModal";
import { ReadinessHelp } from "./ReadinessHelp";

interface Props {
  boardId: string;
  boardName?: string;
  onClose: () => void;
  onOpenHealth?: () => void;
}
const PAGE_SIZE = 25;
const REASONS: Record<string, string> = {
  no_reusable_learning: "No reusable knowledge in this work",
  duplicate_bug: "Duplicate bug",
  trivial_fix: "Trivial fix with no reusable learning",
  root_cause_unconfirmed: "Root cause needs investigation",
  evidence_insufficient: "More evidence is needed",
  path_b_pending: "Alternative consolidation path is still pending",
  external_context_missing: "Waiting for external context",
};
const SECTIONS: { id: ReadinessSignalFilter; label: string; help: string }[] = [
  {
    id: "attention",
    label: "Needs attention",
    help: "Pending consolidation, failed processing, graph updates awaiting completion and overdue reviews.",
  },
  {
    id: "deferred",
    label: "Waived or scheduled",
    help: "Explicit waivers and future reviews. A waiver does not add knowledge to the graph.",
  },
  {
    id: "terminal_history",
    label: "History",
    help: "Completed or terminal records. These records do not need another waiver.",
  },
  {
    id: "all",
    label: "All records",
    help: "All source records. One artifact can have several records; counts are not unique artifacts.",
  },
];
const FILTERS: { id: ReadinessSignalFilter; label: string }[] = [
  { id: "cognitive_pending", label: "Awaiting consolidation" },
  { id: "dlq", label: "Failed processing" },
  { id: "open_canonical_debt", label: "Graph update pending" },
  { id: "revisit_required", label: "Scheduled reviews" },
  { id: "skipped", label: "Waivers" },
];
const button =
  "inline-flex items-center justify-center gap-2 rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm font-medium hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed";
const primary = `${button} bg-violet-600 text-white border-violet-600 hover:bg-violet-700 dark:hover:bg-violet-700`;
const input =
  "rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 px-3 py-2 text-sm w-full";

export function artifactTarget(
  item: CognitiveReadinessItem,
): ModalStackEntry | null {
  const [kind, ...rest] = item.artifact_id.split(":");
  const id = rest.join(":");
  if (!id) return null;
  if (["card", "task", "test", "bug"].includes(kind))
    return { type: "card", id };
  if (["spec", "ideation", "refinement", "sprint", "story"].includes(kind))
    return {
      type: kind as "spec" | "ideation" | "refinement" | "sprint" | "story",
      id,
    };
  return null;
}
function advice(item: CognitiveReadinessItem) {
  if (item.signal === "terminal_history")
    return {
      label: "History — no action needed",
      text: "This record is completed or terminal. Open the source to review its context; no waiver is needed.",
      tone: "border-l-emerald-500 dark:border-l-emerald-400",
    };
  if (item.signal === "dlq")
    return {
      label: "Processing failed",
      text: "An attempt failed and was moved to the failed-processing queue. Inspect its error before deciding whether to retry. A waiver cannot fix this.",
      tone: "border-l-rose-500 dark:border-l-rose-400",
    };
  if (item.signal === "open_canonical_debt")
    return {
      label: "Graph update still pending",
      text: "The authoritative graph update has not finished. Inspect Knowledge Graph Health and its recovery diagnostics; do not waive a technical failure.",
      tone: "border-l-rose-500 dark:border-l-rose-400",
    };
  if (item.readiness_effect === "blocking_technical")
    return {
      label: "Related technical failure",
      text: "Another record for this artifact has a technical failure. Inspect that failure first; this cognitive record cannot hide or resolve it.",
      tone: "border-l-rose-500 dark:border-l-rose-400",
    };
  if (item.status === "skipped")
    return {
      label:
        item.readiness_effect === "blocking_revisit_lapsed"
          ? "Review is overdue"
          : item.revisit_at
            ? "Review scheduled"
            : "Consolidation waived",
      text: "No knowledge is produced by this waiver. Reconsider it to return this item to pending consolidation.",
      tone: "border-l-sky-500 dark:border-l-sky-400",
    };
  return {
    label:
      item.status === "in_progress"
        ? "Consolidation in progress"
        : "Awaiting consolidation",
    text: "Open the source and review its reusable decisions or learning. Follow the Pulse consolidation workflow with an agent. This page does not execute consolidation.",
    tone: "border-l-amber-500 dark:border-l-amber-400",
  };
}

/** Board identity resets forms and outstanding requests. No read initiates work. */
export function CognitiveActionCenterView(props: Props) {
  return <ActionCenter key={props.boardId} {...props} />;
}
function ActionCenter({ boardId, boardName, onClose, onOpenHealth }: Props) {
  const permissions = usePermissions(boardId);
  const ready =
    !permissions.isLoading &&
    !permissions.error &&
    !permissions.ownerReviewRequired;
  const canRead = ready && permissions.has("kg.operations.cognitive.read");
  const canSkip = ready && permissions.has("kg.operations.cognitive.skip");
  const canClear = ready && permissions.has("kg.operations.cognitive.clear");
  const canQueue = ready && permissions.has("kg.operations.queue.read");
  const canHealth = ready && permissions.has("kg.operations.health.read");
  const [data, setData] = useState<CognitiveReadinessListResponse | null>(null);
  const [metrics, setMetrics] = useState<CognitiveReadinessMetrics | null>(
    null,
  );
  const [signal, setSignal] = useState<ReadinessSignalFilter>("attention");
  const [search, setSearch] = useState("");
  const [activeSearch, setActiveSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [metricsError, setMetricsError] = useState(false);
  const [updated, setUpdated] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [dlq, setDlq] = useState(false);
  const request = useRef<AbortController | null>(null);
  const api = useDashboardApi();
  const { push } = useModalStack();
  const openCard = useDashboardStore((s) => s.openCardModal);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const fetchAll = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setData(null);
    setTitles({});
    setMetrics(null);
    setError(null);
    setMetricsError(false);
    setLoading(true);
    if (!canRead) {
      setLoading(false);
      return;
    }
    const [items, stats] = await Promise.allSettled([
      getReadinessItems(
        boardId,
        { signal, search: activeSearch || undefined, limit: PAGE_SIZE, offset },
        controller.signal,
      ),
      getReadinessMetrics(boardId, controller.signal),
    ]);
    if (controller.signal.aborted) return;
    if (items.status === "fulfilled") {
      if (offset > 0 && items.value.summary.total <= offset) {
        setOffset(0);
        return;
      }
      setData(items.value);
      setUpdated(new Date().toLocaleTimeString());
    } else
      setError(
        items.reason instanceof Error
          ? items.reason.message
          : "Could not load readiness.",
      );
    if (stats.status === "fulfilled") setMetrics(stats.value);
    else setMetricsError(true);
    setLoading(false);
  }, [boardId, signal, activeSearch, offset, canRead]);
  useEffect(() => {
    if (!permissions.isLoading) void fetchAll();
    return () => request.current?.abort();
  }, [fetchAll, permissions.isLoading]);
  // Bounded visible-page title enrichment via existing authorized entity GETs.
  useEffect(() => {
    if (!canRead || !data) return;
    let cancelled = false;
    let cursor = 0;
    const targets = [
      ...new Map(
        data.items.map((i) => [i.artifact_id, artifactTarget(i)]),
      ).entries(),
    ];
    const getters = {
      card: api.getCard,
      spec: api.getSpec,
      ideation: api.getIdeation,
      refinement: api.getRefinement,
      sprint: api.getSprint,
      story: api.getStory,
    };
    const worker = async () => {
      while (!cancelled && cursor < targets.length) {
        const [key, target] = targets[cursor++];
        if (!target || target.type === "kg_node") continue;
        try {
          const entity = await getters[target.type](encodeURIComponent(target.id));
          if (!cancelled && entity.board_id === boardId && entity.title)
            setTitles((old) => ({ ...old, [key]: entity.title }));
        } catch {
          /* Explicit reference fallback for unavailable/deleted/forbidden sources. */
        }
      }
    };
    for (let i = 0; i < 4; i++) void worker();
    return () => {
      cancelled = true;
    };
  }, [api, boardId, canRead, data]);
  const choose = (value: ReadinessSignalFilter) => {
    setSignal(value);
    setOffset(0);
    setNotice("");
  };
  const openArtifact = (item: CognitiveReadinessItem) => {
    const target = artifactTarget(item);
    if (!target) return;
    if (target.type === "card") openCard(target.id);
    push(target);
  };
  const changed = (message: string) => {
    setNotice(message);
    void fetchAll();
  };
  const section = SECTIONS.find((s) => s.id === signal);
  return (
    <div
      data-testid="cognitive-action-center"
      className="h-full flex flex-col bg-slate-50 dark:bg-slate-950 text-slate-800 dark:text-slate-100"
    >
      <header className="shrink-0 border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-6 py-4 flex gap-4 items-start justify-between">
        <div>
          <div className="flex items-center gap-2 text-violet-600 dark:text-violet-400 text-sm font-medium">
            <Brain size={18} />
            Cognitive Action Center{" "}
            <span className="text-slate-600 dark:text-slate-400">
              / {boardName || "Current board"}
            </span>
          </div>
          <h1 className="text-xl sm:text-2xl font-semibold mt-1">
            Resolve knowledge gaps
          </h1>
          <p className="text-sm text-slate-600 dark:text-slate-400 dark:text-slate-400 mt-1 max-w-3xl">
            Review what is missing, inspect failed processing, or explain why
            consolidation is not needed. Nothing runs simply by opening this
            page.
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            className={button}
            onClick={() => void fetchAll()}
            disabled={loading || !canRead}
            aria-label="Refresh readiness"
          >
            <RefreshCw size={16} />
            <span className="hidden sm:inline">Refresh</span>
          </button>
          <button
            className={button}
            onClick={onClose}
            aria-label="Close action center"
          >
            <X size={18} />
          </button>
        </div>
      </header>
      <div className="flex-1 min-h-0 overflow-auto">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-5 space-y-5">
          <details className="rounded-xl border border-violet-200 dark:border-violet-800 bg-violet-50 dark:bg-violet-950/30 p-4">
            <summary className="cursor-pointer font-medium">
              How to use this center
            </summary>
            <ol className="grid md:grid-cols-3 gap-5 mt-4 text-sm list-decimal list-inside">
              <li>
                <strong>Understand the work.</strong>
                <p className="mt-1">
                  Open the source to review its decisions, evidence and reusable
                  learning. Consolidation records that knowledge in the graph.
                </p>
              </li>
              <li>
                <strong>Choose the next step.</strong>
                <p className="mt-1">
                  Use an agent following the Pulse workflow for consolidation.
                  For failures, inspect the failed-processing queue or KG Health
                  before retrying.
                </p>
              </li>
              <li>
                <strong>Make an explicit decision.</strong>
                <p className="mt-1">
                  Only waive work when no consolidation is needed, or schedule a
                  review if information is missing. Technical failures cannot be
                  waived.
                </p>
              </li>
            </ol>
          </details>
          {canRead && (
            <div
              className="grid grid-cols-3 gap-2 sm:gap-3"
              aria-label="Board-wide summary"
            >
              {(
                [
                  [
                    "cognitive_pending",
                    "Awaiting consolidation",
                    metrics?.by_signal.cognitive_pending ??
                      (metrics ? 0 : undefined),
                    "Work with knowledge still to process. Counts are source records, not unique artifacts.",
                  ],
                  [
                    "dlq",
                    "Failed processing",
                    metrics?.technical_dlq,
                    "Failed processing attempts. Inspect the actual error in the queue before requeuing.",
                  ],
                  [
                    "revisit_required",
                    "Scheduled reviews",
                    metrics?.by_signal.revisit_required ??
                      (metrics ? 0 : undefined),
                    `All time-limited waivers, including future reviews. ${metrics?.expired_revisit_skips ?? "Unknown number of"} overdue reviews are also in Needs attention.`,
                  ],
                ] as const
              ).map(([id, label, count, help]) => (
                <div
                  key={id}
                  className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4"
                >
                  <div className="text-sm text-slate-600 dark:text-slate-400">
                    {label}
                    <ReadinessHelp label={label}>{help}</ReadinessHelp>
                  </div>
                  <button
                    className="text-3xl font-semibold mt-1 text-violet-600 dark:text-violet-400"
                    data-testid={`cac-counter-${id}`}
                    onClick={() => choose(id)}
                    aria-label={`Filter: ${label}`}
                  >
                    {count ?? "—"}
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="text-sm flex flex-wrap justify-between gap-2 text-slate-600 dark:text-slate-400">
            <span data-testid="cac-enforcement">
              {!data
                ? "Checking board completion policy…"
                : data.summary.enforcement_active
                  ? "Completion checks are active: marked items may prevent Done."
                  : "Advisory mode: these records do not block Done on this board."}
              <ReadinessHelp label="Completion policy">
                Only the backend determines whether an item would block Done.
                Advisory mode does not mean a technical failure has been
                repaired.
              </ReadinessHelp>
            </span>
            {updated && (
              <span>Last loaded {updated} · Refresh for updates</span>
            )}
          </div>
          <nav className="flex flex-wrap gap-2" aria-label="Readiness sections">
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                className={s.id === signal ? primary : button}
                aria-pressed={signal === s.id}
                onClick={() => choose(s.id)}
              >
                {s.label}
              </button>
            ))}
          </nav>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            {section?.help ||
              "Filtered board records. The summary above always covers the entire board."}
          </p>
          <div className="flex flex-wrap gap-3 items-end">
            <label className="text-sm">
              Focus on
              <select
                className={`${input} mt-1`}
                aria-label="Record type"
                value={FILTERS.some((f) => f.id === signal) ? signal : ""}
                onChange={(e) =>
                  choose(
                    (e.target.value || "attention") as ReadinessSignalFilter,
                  )
                }
              >
                <option value="">Choose a specific record type</option>
                {FILTERS.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <form
              className="flex items-end gap-2 flex-1 min-w-60"
              onSubmit={(e) => {
                e.preventDefault();
                setActiveSearch(search.trim());
                setOffset(0);
              }}
            >
              <label className="text-sm flex-1">
                Find a reference
                <ReadinessHelp label="Reference search">
                  Search an artifact ID, source reference or reason code across
                  the selected section. This search does not match source
                  titles.
                </ReadinessHelp>
                <input
                  className={`${input} mt-1`}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Paste an artifact ID or reference"
                />
              </label>
              <button className={button}>Search</button>
            </form>
            {(activeSearch || !section) && (
              <button
                className={button}
                onClick={() => {
                  setSearch("");
                  setActiveSearch("");
                  choose("attention");
                }}
              >
                Clear filters
              </button>
            )}
          </div>
          {notice && (
            <p
              role="status"
              className="rounded-lg bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200 p-3"
            >
              {notice}
            </p>
          )}
          {!permissions.isLoading && !canRead && (
            <p role="alert">
              You do not have permission to view cognitive readiness. Ask a
              board administrator for access.
            </p>
          )}
          {canRead && metricsError && (
            <p role="status" className="text-amber-600">
              Board counters are unavailable. Records below can still be
              reviewed.
            </p>
          )}
          {loading && <p role="status">Loading readiness records…</p>}
          {error && (
            <div
              role="alert"
              data-testid="cac-error"
              className="rounded-xl border border-rose-400 p-4"
            >
              <p>{error}</p>
              <button
                className={`${button} mt-2`}
                onClick={() => void fetchAll()}
              >
                Retry
              </button>
            </div>
          )}
          {canRead && data && !loading && (
            <>
              <p className="text-sm text-slate-600 dark:text-slate-400">
                {data.summary.total === 0
                  ? "0 records"
                  : `${offset + 1}–${offset + data.items.length} of ${data.summary.total} records`}{" "}
                · One artifact may have multiple processing records.
              </p>
              {data.items.length === 0 ? (
                <div
                  data-testid="cac-empty-state"
                  className="rounded-xl border border-slate-200 dark:border-slate-800 p-10 text-center"
                >
                  <CheckCircle2 className="mx-auto text-emerald-500 mb-3" />
                  <h2 className="font-semibold">
                    {signal === "attention" && !activeSearch
                      ? "Nothing needs attention here"
                      : "No matching records"}
                  </h2>
                  <p className="text-sm text-slate-600 dark:text-slate-400 mt-2">
                    {activeSearch
                      ? "Try another reference or clear the filters."
                      : "Check waived work, scheduled reviews or history in the other sections."}
                  </p>
                </div>
              ) : (
                <div data-testid="cac-table" className="space-y-3">
                  {data.items.map((item, index) => (
                    <ReadinessCard
                      key={`${item.artifact_id}:${item.signal_source}:${index}`}
                      item={item}
                      boardId={boardId}
                      title={titles[item.artifact_id]}
                      canSkip={canSkip}
                      canClear={canClear}
                      onChanged={changed}
                      onOpen={
                        artifactTarget(item)
                          ? () => openArtifact(item)
                          : undefined
                      }
                      onQueue={canQueue ? () => setDlq(true) : undefined}
                      onHealth={canHealth ? onOpenHealth : undefined}
                    />
                  ))}
                </div>
              )}
              <nav
                className="flex justify-end gap-2"
                aria-label="Readiness pagination"
              >
                <button
                  className={button}
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </button>
                <button
                  className={button}
                  disabled={offset + data.items.length >= data.summary.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </button>
              </nav>
            </>
          )}
          {canRead && metrics && (
            <details
              data-testid="cac-metrics-panel"
              className="border-t border-slate-200 dark:border-slate-800 pt-4 text-sm text-slate-600 dark:text-slate-400"
            >
              <summary className="cursor-pointer">
                Technical metrics · {metrics.total} board-wide records
              </summary>
              <p className="my-2">
                Diagnostic counts use internal categories; they are not a
                measure of knowledge quality.
              </p>
              <pre
                className="overflow-auto text-xs"
                data-testid="cac-metric-reason_code"
              >
                {JSON.stringify(
                  {
                    status: metrics.by_status,
                    readiness: metrics.by_readiness_effect,
                    reasons: metrics.by_reason_code,
                    age: metrics.by_age_bucket,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
          )}
        </div>
      </div>
      {dlq && canQueue && (
        <DeadLetterInspectorModal
          boardId={boardId}
          onClose={() => {
            setDlq(false);
            void fetchAll();
          }}
        />
      )}
    </div>
  );
}

function ReadinessCard({
  item,
  boardId,
  title,
  canSkip,
  canClear,
  onChanged,
  onOpen,
  onQueue,
  onHealth,
}: {
  item: CognitiveReadinessItem;
  boardId: string;
  title?: string;
  canSkip: boolean;
  canClear: boolean;
  onChanged: (message: string) => void;
  onOpen?: () => void;
  onQueue?: () => void;
  onHealth?: () => void;
}) {
  const [action, setAction] = useState<"waive" | "reopen" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const technical =
    isTechnicalBlocker(item) || item.readiness_effect === "blocking_technical";
  const pending =
    item.signal_source === "cognitive_item" &&
    item.signal === "cognitive_pending" &&
    ["pending", "failed"].includes(item.status || "");
  const canWaive = canSkip && pending && !technical;
  const canReopen =
    canClear &&
    item.signal_source === "cognitive_item" &&
    item.status === "skipped" &&
    item.signal !== "terminal_history";
  const info = advice(item);
  const submit = async (
    reason?: string,
    justification?: string,
    revisitAt?: string,
  ) => {
    if (busy || (action === "waive" ? !canWaive : !canReopen)) return;
    setBusy(true);
    setError("");
    try {
      if (action === "waive")
        await recordCognitiveSkip(boardId, {
          sourceRef: item.source_ref_original,
          reasonCode: reason!,
          justification,
          revisitAt,
        });
      else await clearCognitiveSkip(boardId, item.source_ref_original);
      onChanged(
        action === "waive"
          ? "Waiver recorded. No knowledge was added to the graph by this action."
          : "Waiver removed. The item is pending again; this action did not run consolidation.",
      );
      setAction(null);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Action could not be completed. Refresh and review the current state.",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <article
      className={`rounded-xl border border-slate-200 dark:border-slate-700 border-l-4 ${info.tone} bg-white dark:bg-slate-900 p-5`}
    >
      <div className="flex flex-col lg:flex-row gap-5 justify-between">
        <div className="min-w-0 flex-1">
          <div className="text-xs font-medium uppercase tracking-wide text-slate-600 dark:text-slate-400 mb-1">
            {item.artifact_type} · {info.label}
          </div>
          {onOpen ? (
            <button
              onClick={onOpen}
              className="text-left text-base font-semibold text-violet-700 dark:text-violet-300 hover:underline inline-flex items-start gap-2"
            >
              {title || "Open source artifact"}
              <ExternalLink size={16} className="shrink-0 mt-1" />
            </button>
          ) : (
            <h2 className="font-semibold">{title || "Source record"}</h2>
          )}
          <p className="text-xs text-slate-600 dark:text-slate-400 break-all mt-1">
            {item.artifact_id}
          </p>
          {!title && (
            <p className="text-xs text-slate-600 dark:text-slate-400 mt-1">
              Source title unavailable or still loading. Use the reference to
              identify this work.
            </p>
          )}
          <p className="text-sm mt-3 max-w-3xl">{info.text}</p>
          {item.reason_code && (
            <p className="text-sm mt-2" data-testid="cac-reason-code">
              <strong>Reason: </strong>
              {REASONS[item.reason_code] || item.reason_code}
            </p>
          )}
          {item.justification && (
            <p className="text-sm mt-2 whitespace-pre-wrap break-words">
              <strong>Recorded justification: </strong>
              {item.justification}
            </p>
          )}
          {item.actor && (
            <p className="text-xs text-slate-600 dark:text-slate-400 mt-1">
              Decision recorded by {item.actor}
            </p>
          )}
          {item.revisit_at && (
            <p className="text-sm mt-2">
              <strong>Review date: </strong>
              {new Date(item.revisit_at).toLocaleString()} (your timezone)
            </p>
          )}
          <p
            className="text-sm mt-2"
            data-testid={
              item.would_block_done ? "cac-would-block-done" : undefined
            }
          >
            <strong>Completion impact: </strong>
            {item.would_block_done
              ? "The backend reports this artifact would block Done."
              : "This record does not currently block Done."}
          </p>
        </div>
        <div className="flex flex-wrap lg:flex-col gap-2 lg:w-56 shrink-0">
          {(item.signal === "dlq" ||
            item.precedence_explanation.tier === "technical_dlq") &&
            (onQueue ? (
              <button className={primary} onClick={onQueue}>
                Inspect failed processing
              </button>
            ) : (
              <p className="text-sm text-slate-600 dark:text-slate-400">
                Ask a board administrator with queue access to inspect this
                failure.
              </p>
            ))}
          {(item.signal === "open_canonical_debt" ||
            item.precedence_explanation.tier === "canonical_debt_open") &&
            (onHealth ? (
              <button className={primary} onClick={onHealth}>
                Open KG Health
              </button>
            ) : (
              <p className="text-sm text-slate-600 dark:text-slate-400">
                Open KG Health from the board menu, or ask an administrator to
                review this graph update.
              </p>
            ))}
          {onOpen && (
            <button className={button} onClick={onOpen}>
              Open source
            </button>
          )}
          {canWaive && (
            <button
              data-testid="cac-skip-toggle"
              className={button}
              disabled={busy}
              onClick={() => {
                setAction("waive");
                setError("");
              }}
            >
              Waive or schedule review…
            </button>
          )}
          {canReopen && (
            <button
              data-testid="cac-clear"
              className={button}
              disabled={busy}
              onClick={() => {
                setAction("reopen");
                setError("");
              }}
            >
              Reconsider waiver…
            </button>
          )}
          {technical && (
            <p
              data-testid="cac-technical-no-skip"
              className="text-xs text-rose-600 dark:text-rose-300"
            >
              Technical failures cannot be waived.
            </p>
          )}
          {pending && !canSkip && !technical && (
            <p className="text-xs text-slate-600 dark:text-slate-400">
              You can review this item, but do not have permission to waive it.
            </p>
          )}
        </div>
      </div>
      <details className="mt-4 text-xs text-slate-600 dark:text-slate-400">
        <summary className="cursor-pointer">
          Technical details and references
        </summary>
        <div className="mt-2 space-y-1 break-all">
          <div>Source: {item.source_ref_original}</div>
          <div data-testid="cac-aliases">
            Aliases: {item.aliases.join(", ")}
          </div>
          <div>
            Record: {item.signal_source} / {item.signal} / {item.status}
          </div>
          <div data-testid="cac-error-cause">
            Technical category: {item.error_cause || "None"}
          </div>
        </div>
        <pre className="whitespace-pre-wrap mt-2">
          {JSON.stringify(item.precedence_explanation, null, 2)}
        </pre>
        <p className="mt-2">
          The category is not the full error report. Use the failed-processing
          inspector or KG Health for diagnostics.
        </p>
      </details>
      {action && (
        <div className="mt-4 border-t border-slate-200 dark:border-slate-700 pt-4">
          {error && (
            <p
              role="alert"
              data-testid="cac-action-error"
              className="text-rose-600 mb-3"
            >
              {error}
            </p>
          )}
          {action === "waive" ? (
            <WaiverForm
              busy={busy}
              onSubmit={submit}
              onCancel={() => setAction(null)}
            />
          ) : (
            <div>
              <h3 className="font-semibold">
                Return this item to pending consolidation?
              </h3>
              <p className="text-sm mt-2">
                This removes the current waiver reason and review date. It does
                not reopen the Spec or task, erase graph data, or execute
                consolidation here. Pending work may prevent Done when the board
                policy requires it.
              </p>
              <div className="flex gap-2 mt-3">
                <button
                  className={primary}
                  disabled={busy}
                  onClick={() => void submit()}
                >
                  Confirm reconsideration
                </button>
                <button
                  className={button}
                  disabled={busy}
                  onClick={() => setAction(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </article>
  );
}

function WaiverForm({
  busy,
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  onSubmit: (reason: string, justification: string, revisitAt?: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [justification, setJustification] = useState("");
  const [date, setDate] = useState("");
  const [error, setError] = useState("");
  const revisit = isRevisitRequiredReason(reason);
  return (
    <form
      data-testid="cac-skip-form"
      onSubmit={(e) => {
        e.preventDefault();
        const when = new Date(date);
        if (!reason || !justification.trim()) {
          setError("Choose a reason and explain your decision.");
          return;
        }
        if (
          revisit &&
          (!date ||
            !Number.isFinite(when.getTime()) ||
            when.getTime() <= Date.now())
        ) {
          setError("Choose a future review date.");
          return;
        }
        setError("");
        onSubmit(
          reason,
          justification.trim(),
          revisit ? when.toISOString() : undefined,
        );
      }}
    >
      <h3 className="font-semibold">
        {revisit
          ? "Schedule a review"
          : "Record why consolidation is not needed"}
      </h3>
      <p className="text-sm my-2">
        This records a waiver only. It does not consolidate knowledge, delete
        the source, or repair a technical failure.{" "}
        {revisit
          ? "When the review date passes, the item needs attention again."
          : "Without a review date, this waiver remains until explicitly reconsidered."}
      </p>
      <fieldset disabled={busy} className="grid md:grid-cols-2 gap-3">
        <label className="text-sm">
          Reason
          <select
            required
            aria-label="Waiver reason"
            data-testid="cac-skip-reason"
            className={`${input} mt-1`}
            value={reason}
            onChange={(e) => {
              setReason(e.target.value);
              setError("");
            }}
          >
            <option value="">Choose a reason…</option>
            <optgroup label="No review date">
              {SELECTABLE_REASON_CODES.filter(
                (r) => !isRevisitRequiredReason(r),
              ).map((r) => (
                <option key={r} value={r}>
                  {REASONS[r]}
                </option>
              ))}
            </optgroup>
            <optgroup label="Future review required">
              {SELECTABLE_REASON_CODES.filter(isRevisitRequiredReason).map(
                (r) => (
                  <option key={r} value={r}>
                    {REASONS[r]}
                  </option>
                ),
              )}
            </optgroup>
          </select>
        </label>
        {revisit && (
          <label className="text-sm">
            Review date (your local timezone)
            <input
              required
              type="datetime-local"
              data-testid="cac-skip-revisit"
              className={`${input} mt-1`}
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </label>
        )}
        <label className="text-sm md:col-span-2">
          Why is this appropriate?
          <ReadinessHelp label="Waiver justification">
            Record enough context for another person to understand the decision.
            Missing information needs a future review, not a permanent waiver.
            Do not include secrets.
          </ReadinessHelp>
          <textarea
            required
            maxLength={4000}
            rows={2}
            data-testid="cac-skip-justification"
            className={`${input} mt-1`}
            value={justification}
            onChange={(e) => setJustification(e.target.value)}
            placeholder="Explain what you reviewed and why this decision is appropriate."
          />
        </label>
      </fieldset>
      {error && (
        <p role="alert" className="text-rose-600 text-sm mt-2">
          {error}
        </p>
      )}
      <div className="flex gap-2 mt-3">
        <button
          className={primary}
          data-testid="cac-skip-confirm"
          disabled={busy}
        >
          {busy
            ? "Saving…"
            : revisit
              ? "Confirm scheduled review"
              : "Confirm waiver"}
        </button>
        <button
          type="button"
          className={button}
          disabled={busy}
          onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
