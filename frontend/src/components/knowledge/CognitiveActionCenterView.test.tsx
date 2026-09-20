/**
 * Tests for CognitiveActionCenterView (S3.3 / card 974f5146, spec 2731a346).
 *
 * Cobre as invariantes da UI: technical blockers (DLQ/open debt) NÃO oferecem
 * skip; would_block_done só aparece quando o backend marca; skip/clear via
 * write-path central; 409 mostrado sem mascarar o blocker técnico; métricas
 * bounded; aliases card/bug. A precedência/enforcement vêm do backend — a UI
 * só renderiza.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

const permissionHas = vi.hoisted(() => vi.fn((_flag: string) => true));
const navigation = vi.hoisted(() => vi.fn());
const entityApi = vi.hoisted(() => {
  const read = vi.fn(async () => {
    throw new Error("Source unavailable");
  });
  return {
    getCard: read,
    getSpec: read,
    getIdeation: read,
    getRefinement: read,
    getStory: read,
    getSprint: read,
  };
});
vi.mock("@/services/api", () => ({ useDashboardApi: () => entityApi }));
vi.mock("@/contexts/ModalStackContext", () => ({
  useModalStack: () => ({ push: navigation }),
}));
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({
    preset: "Full Control",
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: permissionHas,
  }),
}));

import { CognitiveActionCenterView } from "./CognitiveActionCenterView";
import * as api from "@/services/cognitive-readiness-api";
import type {
  CognitiveReadinessItem,
  CognitiveReadinessListResponse,
  CognitiveReadinessMetrics,
} from "@/types/cognitive-readiness";

function item(over: Partial<CognitiveReadinessItem>): CognitiveReadinessItem {
  return {
    artifact_id: "card:aaaa",
    source_ref_original: "card:aaaa",
    aliases: ["card:aaaa"],
    artifact_type: "card",
    signal: "cognitive_pending",
    signal_source: "cognitive_item",
    status: "pending",
    outcome_type: null,
    reason_code: null,
    error_cause: null,
    revisit_at: null,
    readiness_effect: "blocking_cognitive",
    blocking: true,
    precedence_explanation: { tier: "cognitive_active" },
    would_block_done: false,
    ...over,
  };
}

const COGNITIVE = item({
  artifact_id: "card:cog1",
  source_ref_original: "card:cog1",
});
const DLQ = item({
  artifact_id: "card:dlq1",
  source_ref_original: "card:dlq1",
  signal: "dlq",
  signal_source: "dlq",
  status: "dead_lettered",
  error_cause: "technical_dlq",
  readiness_effect: "blocking_technical",
  blocking: true,
  would_block_done: true,
});
const SKIPPED = item({
  artifact_id: "card:skip1",
  source_ref_original: "bug:skip1",
  aliases: ["bug:skip1", "card:skip1"],
  signal: "skipped",
  status: "skipped",
  outcome_type: "no_action_required",
  reason_code: "trivial_fix",
  readiness_effect: "ready_skip",
  blocking: false,
});

const METRICS: CognitiveReadinessMetrics = {
  board_id: "b",
  total: 3,
  by_status: { pending: 1, dead_lettered: 1, skipped: 1 },
  by_outcome_type: { none: 2, no_action_required: 1 },
  by_reason_code: { none: 2, trivial_fix: 1 },
  by_artifact_type: { card: 3 },
  by_readiness_effect: {
    blocking_cognitive: 1,
    blocking_technical: 1,
    ready_skip: 1,
  },
  by_signal: { cognitive_pending: 1, dlq: 1, skipped: 1 },
  by_signal_source: { cognitive_item: 2, dlq: 1 },
  by_age_bucket: { lt_1d: 3 },
  technical_blocking_signals: 1,
  cognitive_pending_signals: 1,
  expired_revisit_skips: 0,
  open_canonical_debt: 0,
  technical_dlq: 1,
  terminal_history: 0,
};

function listResponse(
  items: CognitiveReadinessItem[],
  enforcement = false,
): CognitiveReadinessListResponse {
  return {
    board_id: "b",
    items,
    summary: {
      by_signal: {},
      technical_blocking_signals: 1,
      cognitive_pending_signals: 1,
      enforcement_active: enforcement,
      total: items.length,
      limit: 200,
      offset: 0,
    },
    precedence: ["technical_dlq", "canonical_debt_open", "cognitive_active"],
  };
}

function mockList(items: CognitiveReadinessItem[], enforcement = false) {
  vi.spyOn(api, "getReadinessItems").mockResolvedValue(
    listResponse(items, enforcement),
  );
  vi.spyOn(api, "getReadinessMetrics").mockResolvedValue(METRICS);
}

afterEach(() => {
  vi.restoreAllMocks();
});
beforeEach(() => {
  permissionHas.mockImplementation(() => true);
  navigation.mockClear();
  entityApi.getCard.mockReset();
  entityApi.getCard.mockRejectedValue(new Error("Source unavailable"));
});

function completeWaiver(reason = "no_reusable_learning") {
  fireEvent.change(screen.getByTestId("cac-skip-reason"), {
    target: { value: reason },
  });
  fireEvent.change(screen.getByTestId("cac-skip-justification"), {
    target: { value: "Reviewed the source; no reusable learning remains." },
  });
}

describe("CognitiveActionCenterView", () => {
  test("starts with attention, explains purpose and opens the source through the modal stack", async () => {
    mockList([COGNITIVE]);
    entityApi.getCard.mockResolvedValue({
      title: "Reusable deployment lessons",
      board_id: "b",
    } as never);
    render(
      <CognitiveActionCenterView
        boardId="b"
        boardName="Example"
        onClose={vi.fn()}
      />,
    );
    await screen.findByText("Reusable deployment lessons");
    expect(api.getReadinessItems).toHaveBeenCalledWith(
      "b",
      expect.objectContaining({ signal: "attention", limit: 25, offset: 0 }),
      expect.any(AbortSignal),
    );
    expect(
      screen.getByText(/Nothing runs simply by opening/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open source" }));
    expect(navigation).toHaveBeenCalledWith({ type: "card", id: "cog1" });
  });

  test("never offers a waiver on historical, running, or technically blocked records", async () => {
    mockList([
      item({ signal: "terminal_history", status: "consolidated" }),
      item({ artifact_id: "card:running", status: "in_progress" }),
      item({
        artifact_id: "card:blocked",
        readiness_effect: "blocking_technical",
      }),
    ]);
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    await screen.findByTestId("cac-table");
    expect(screen.queryByTestId("cac-skip-toggle")).toBeNull();
    expect(screen.queryByTestId("cac-clear")).toBeNull();
  });

  test("reports failed processing without a repair entry point or write", async () => {
    mockList([DLQ]);
    const write = vi.spyOn(api, "recordCognitiveSkip");
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    await screen.findByTestId("cac-table");
    expect(screen.queryByRole("button", { name: "Inspect failed processing" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Failed processing" })).toBeNull();
    expect(screen.getByText(/Processing is unavailable for this artifact/)).toBeInTheDocument();
    expect(write).not.toHaveBeenCalled();
  });

  test("KG Health navigation uses its supplied callback", async () => {
    mockList([
      item({
        signal: "open_canonical_debt",
        signal_source: "canonical_debt",
        readiness_effect: "blocking_technical",
      }),
    ]);
    const health = vi.fn();
    render(
      <CognitiveActionCenterView
        boardId="b"
        onClose={vi.fn()}
        onOpenHealth={health}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Open KG Health" }),
    );
    expect(health).toHaveBeenCalledOnce();
  });

  test("does not fetch records or expose actions without read authority", async () => {
    mockList([COGNITIVE]);
    permissionHas.mockReturnValue(false);
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /do not have permission/,
    );
    expect(api.getReadinessItems).not.toHaveBeenCalled();
    expect(screen.queryByTestId("cac-table")).toBeNull();
  });

  test("metrics failure does not hide available records", async () => {
    mockList([COGNITIVE]);
    vi.mocked(api.getReadinessMetrics).mockRejectedValue(
      new Error("Metrics down"),
    );
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    await screen.findByText("card:cog1");
    expect(
      screen.getByText(/Board counters are unavailable/),
    ).toBeInTheDocument();
    expect(screen.getByTestId("cac-counter-dlq")).toHaveTextContent("—");
  });

  test("server pagination is reachable and changing sections resets the offset", async () => {
    mockList([COGNITIVE]);
    vi.mocked(api.getReadinessItems).mockImplementation(
      async (_board, options) => ({
        ...listResponse([COGNITIVE]),
        summary: {
          ...listResponse([]).summary,
          total: 240,
          limit: 25,
          offset: options?.offset || 0,
        },
      }),
    );
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(api.getReadinessItems).toHaveBeenLastCalledWith(
        "b",
        expect.objectContaining({ offset: 25 }),
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "History" }),
    );
    await waitFor(() =>
      expect(api.getReadinessItems).toHaveBeenLastCalledWith(
        "b",
        expect.objectContaining({ signal: "terminal_history", offset: 0 }),
        expect.any(AbortSignal),
      ),
    );
  });

  test("waiver has no default reason, requires justification and validates future reviews", async () => {
    mockList([COGNITIVE]);
    const write = vi
      .spyOn(api, "recordCognitiveSkip")
      .mockResolvedValue({} as never);
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("cac-skip-toggle"));
    expect(screen.getByTestId("cac-skip-reason")).toHaveValue("");
    fireEvent.submit(screen.getByTestId("cac-skip-form"));
    expect(write).not.toHaveBeenCalled();
    completeWaiver("evidence_insufficient");
    fireEvent.change(screen.getByTestId("cac-skip-revisit"), {
      target: { value: "2000-01-01T12:00" },
    });
    fireEvent.submit(screen.getByTestId("cac-skip-form"));
    expect(write).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("future review date");
    fireEvent.change(screen.getByTestId("cac-skip-revisit"), {
      target: { value: "2099-01-01T12:00" },
    });
    fireEvent.submit(screen.getByTestId("cac-skip-form"));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith(
        "b",
        expect.objectContaining({
          reasonCode: "evidence_insufficient",
          revisitAt: new Date("2099-01-01T12:00").toISOString(),
        }),
      ),
    );
  });

  test("portal help is accessible with focus and Escape without entering the center layout", async () => {
    mockList([]);
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    await screen.findByTestId("cac-empty-state");
    const help = screen.getByRole("button", {
      name: "Help: Completion policy",
    });
    fireEvent.focus(help);
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip.parentElement).toBe(document.body);
    expect(help).toHaveAttribute("aria-describedby", tooltip.id);
    fireEvent.keyDown(help, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  test("a late response from the old filter cannot overwrite the new result", async () => {
    mockList([]);
    let finish!: (value: CognitiveReadinessListResponse) => void;
    vi.mocked(api.getReadinessItems)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValue(listResponse([DLQ]));
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Record type"), {
      target: { value: "dlq" },
    });
    await screen.findByText("card:dlq1");
    finish(listResponse([COGNITIVE]));
    await waitFor(() => expect(screen.queryByText("card:cog1")).toBeNull());
  });

  test("switching boards removes the previous board form and ignores late source titles", async () => {
    mockList([COGNITIVE]);
    let finish!: (value: never) => void;
    entityApi.getCard.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const view = render(
      <CognitiveActionCenterView boardId="b" onClose={vi.fn()} />,
    );
    fireEvent.click(await screen.findByTestId("cac-skip-toggle"));
    vi.mocked(api.getReadinessItems).mockResolvedValue(listResponse([]));
    view.rerender(
      <CognitiveActionCenterView boardId="other" onClose={vi.fn()} />,
    );
    await screen.findByTestId("cac-empty-state");
    finish({ title: "Private old title", board_id: "b" } as never);
    expect(screen.queryByTestId("cac-skip-form")).toBeNull();
    expect(screen.queryByText("Private old title")).toBeNull();
  });

  test("cross-board titles are never displayed", async () => {
    mockList([COGNITIVE]);
    entityApi.getCard.mockResolvedValue({
      title: "Wrong board title",
      board_id: "other",
    } as never);
    render(<CognitiveActionCenterView boardId="b" onClose={vi.fn()} />);
    await screen.findByText("card:cog1");
    expect(screen.queryByText("Wrong board title")).toBeNull();
  });
  test("renderiza linhas, counters e painel de métricas bounded", async () => {
    mockList([COGNITIVE, DLQ, SKIPPED]);
    vi.mocked(api.getReadinessMetrics).mockResolvedValue({
      ...METRICS,
      cognitive_pending_signals: 3,
      by_signal: { ...METRICS.by_signal, revisit_required: 2 },
    });
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("cac-table")).toBeInTheDocument(),
    );
    expect(screen.getByText("card:cog1")).toBeInTheDocument();
    expect(screen.getByText("card:dlq1")).toBeInTheDocument();
    expect(screen.getByTestId("cac-counter-dlq")).toHaveTextContent("1");
    expect(screen.getByTestId("cac-counter-cognitive_pending")).toHaveTextContent(/^1$/);
    expect(screen.getByTestId("cac-counter-revisit_required")).toHaveTextContent(/^2$/);
    expect(screen.getByTestId("cac-metrics-panel")).toBeInTheDocument();
    // métrica bounded by_reason_code mostra label clamp, não free-text
    expect(screen.getByTestId("cac-metric-reason_code")).toHaveTextContent(
      "trivial_fix",
    );
  });

  test("technical blocker (DLQ) NÃO oferece skip e marca would_block_done", async () => {
    mockList([DLQ], true);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText("card:dlq1"));
    const row = screen.getByText("card:dlq1").closest("article")!;
    expect(
      within(row).getByTestId("cac-technical-no-skip"),
    ).toBeInTheDocument();
    expect(within(row).queryByTestId("cac-skip-toggle")).toBeNull();
    expect(within(row).getByTestId("cac-would-block-done")).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: /inspect failed processing/i })).toBeNull();
    expect(row).toHaveTextContent('Processing is unavailable for this artifact');
    expect(row).not.toHaveTextContent('queue access');

    // error_cause técnico exibido, reason_code cognitivo ausente
    expect(within(row).getByTestId("cac-error-cause")).toHaveTextContent(
      "technical_dlq",
    );
  });

  test("linha cognitiva pending NÃO mostra would_block_done quando advisory", async () => {
    mockList([COGNITIVE], false);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText("card:cog1"));
    const row = screen.getByText("card:cog1").closest("article")!;
    expect(within(row).queryByTestId("cac-would-block-done")).toBeNull();
    expect(within(row).getByTestId("cac-skip-toggle")).toBeInTheDocument();
    expect(screen.getByTestId("cac-enforcement")).toHaveTextContent(
      /advisory/i,
    );
  });

  test("skip via write-path central refaz fetch", async () => {
    mockList([COGNITIVE]);
    const skipSpy = vi
      .spyOn(api, "recordCognitiveSkip")
      .mockResolvedValue({} as never);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText("card:cog1"));
    fireEvent.click(screen.getByTestId("cac-skip-toggle"));
    await waitFor(() => screen.getByTestId("cac-skip-form"));
    completeWaiver();
    fireEvent.click(screen.getByTestId("cac-skip-confirm"));

    await waitFor(() => expect(skipSpy).toHaveBeenCalledTimes(1));
    expect(skipSpy).toHaveBeenCalledWith(
      "b",
      expect.objectContaining({
        sourceRef: "card:cog1",
        reasonCode: "no_reusable_learning",
      }),
    );
  });

  test("skip 409 mostra erro sem mascarar blocker técnico", async () => {
    mockList([COGNITIVE]);
    vi.spyOn(api, "recordCognitiveSkip").mockRejectedValue(
      new api.ReadinessActionError(
        "technical_debt_cannot_be_skipped",
        "Canonical debt is OPEN for this artifact.",
        409,
      ),
    );
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText("card:cog1"));
    fireEvent.click(screen.getByTestId("cac-skip-toggle"));
    await waitFor(() => screen.getByTestId("cac-skip-form"));
    completeWaiver();
    fireEvent.click(screen.getByTestId("cac-skip-confirm"));

    await waitFor(() =>
      expect(screen.getByTestId("cac-action-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("cac-action-error")).toHaveTextContent(
      /canonical debt is open/i,
    );
    expect(screen.getByTestId("cac-action-error")).toHaveTextContent(
      /canonical debt/i,
    );
  });

  test("clear/reopen de skip válido chama o caminho central", async () => {
    mockList([SKIPPED]);
    const clearSpy = vi
      .spyOn(api, "clearCognitiveSkip")
      .mockResolvedValue({} as never);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);

    await waitFor(() => screen.getByText("card:skip1"));
    const row = screen.getByText("card:skip1").closest("article")!;
    // aliases card/bug visíveis
    expect(within(row).getByTestId("cac-aliases")).toBeInTheDocument();
    fireEvent.click(within(row).getByTestId("cac-clear"));
    expect(clearSpy).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm reconsideration" }),
    );
    await waitFor(() =>
      expect(clearSpy).toHaveBeenCalledWith("b", "bug:skip1"),
    );
  });

  test("empty state", async () => {
    mockList([]);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByTestId("cac-empty-state")).toBeInTheDocument(),
    );
  });

  test("error state quando fetch falha", async () => {
    vi.spyOn(api, "getReadinessItems").mockRejectedValue(
      new Error("Network down"),
    );
    vi.spyOn(api, "getReadinessMetrics").mockRejectedValue(
      new Error("Network down"),
    );
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByTestId("cac-error")).toHaveTextContent(
        /network down/i,
      ),
    );
  });

  test("filtro por signal refaz fetch com o signal selecionado", async () => {
    mockList([COGNITIVE]);
    render(<CognitiveActionCenterView boardId="b" onClose={() => {}} />);
    await waitFor(() => screen.getByText("card:cog1"));
    fireEvent.change(screen.getByLabelText("Record type"), {
      target: { value: "dlq" },
    });
    await waitFor(() =>
      expect(api.getReadinessItems).toHaveBeenCalledWith(
        "b",
        expect.objectContaining({ signal: "dlq" }),
        expect.any(AbortSignal),
      ),
    );
  });
});
