# Okto Pulse

**Spec-driven project management for AI-assisted development.**

Okto Pulse guides your work from raw ideas to shipped code through a structured pipeline — **Ideation → Refinement → Spec → Tasks** — with 119+ MCP tools that let AI agents (Claude Code, Cursor, Windsurf, etc.) collaborate on your board.

```bash
pip install okto-pulse
okto-pulse init
okto-pulse serve
```

That's it. Open `http://localhost:8100` and start building.

---

## Why Okto Pulse?

- **Spec-driven** — Every task traces back to a spec with acceptance criteria, test scenarios, and business rules
- **AI-native** — 119+ MCP tools let AI agents create cards, move tasks, ask questions, track bugs, and more
- **Governance built-in** — Quality gates enforce test coverage, business rules coverage, and task completion before specs can be finalized
- **Local-first** — SQLite database, no external services required, runs on a single machine
- **Single command** — `okto-pulse serve` starts the API, frontend, and MCP server

## Quick Start

### 1. Install

```bash
pip install okto-pulse
```

Requires Python 3.11+.

#### Embedding model download

On first run, Okto Pulse downloads the `all-MiniLM-L6-v2` sentence-transformers
model (~90 MB) into the Hugging Face cache (`~/.cache/huggingface/` by default).
This is the embedder that powers semantic search in the Knowledge Graph and is
a mandatory dependency of the community edition — no extras flag required.

If the download fails (offline install, proxy, disk full) the server still
starts, but falls back to a deterministic hash-based stub and the Settings tab
shows a "Running in stub mode — semantic search disabled" banner. Re-run
`okto-pulse serve` with network access restored to re-attempt the download.

You can verify the embedder is healthy with:

```bash
python scripts/smoke_embedding.py
```

It exits 0 when the model is loaded and 1 otherwise, printing a diagnostic line
with the current provider/model state.

### 2. Initialize

```bash
cd your-project
okto-pulse init
```

This creates:
- A default **board** and **agent** in `~/.okto-pulse/`
- A **`.mcp.json`** file in the current directory with the MCP connection config

### 3. Start

```bash
okto-pulse serve
```

- **Frontend + API**: http://localhost:8100
- **MCP server**: http://localhost:8101

### 4. Connect your AI tool

The `.mcp.json` is auto-detected by most AI tools:

| Tool | Setup |
|------|-------|
| **Claude Code** | Auto-detects `.mcp.json` — just start Claude Code in the same directory |
| **Claude Desktop** | Copy the MCP config to Claude Desktop settings |
| **Cursor** | Add to Cursor preferences → MCP settings |
| **VS Code** | Copy to `.vscode/mcp.json` |
| **Windsurf / Cline** | Auto-detects `.mcp.json` |

The generated `.mcp.json`:

```json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "http://localhost:8101/mcp?api_key=dash_..."
    }
  }
}
```

### 5. Start building

Open the Ideations tab and describe what you want to build. Your AI agent can now help you evaluate, refine, spec, and implement it — all tracked on the board.

## CLI Commands

| Command | Description |
|---------|-------------|
| `okto-pulse init` | Initialize `~/.okto-pulse/`, create DB, seed default board + agent, generate `.mcp.json` |
| `okto-pulse serve` | Start API + Frontend (port 8100) and MCP server (port 8101) |
| `okto-pulse status` | Show service status, DB path/size, board/card counts |
| `okto-pulse reset [-y]` | Delete all data and re-seed (with confirmation) |

### Custom ports

```bash
okto-pulse --api-port 9000 --mcp-port 9001 init
okto-pulse --api-port 9000 --mcp-port 9001 serve
```

## The Pipeline

```
Ideation → Refinement → Spec → Tasks (Kanban)
```

| Stage | What happens |
|-------|-------------|
| **Ideation** | Capture the idea, evaluate complexity (small/medium/large) |
| **Refinement** | Deep analysis for medium/large ideas — scope, decisions, knowledge |
| **Spec** | Acceptance criteria, test scenarios, business rules, API contracts |
| **Tasks** | Kanban board with dependencies, bug tracking, conclusions |

### Governance rules (enforced automatically)

- Specs can't move to "done" without full test coverage
- Specs can't be finalized with pending tasks
- Tasks can't start without test scenario coverage
- Tasks require a conclusion when moved to "done"
- Bug cards enforce test-first workflow (new test scenario + test task before fixing)

## Data Storage

All data lives in `~/.okto-pulse/`:

```
~/.okto-pulse/
├── data/
│   └── pulse.db      # SQLite database
└── uploads/
    └── {board_id}/   # File attachments
```

## From Source

```bash
git clone https://github.com/OktoLabsAI/okto-pulse.git
cd okto-pulse
pip install -e packages/core -e packages/community
okto-pulse init
okto-pulse serve
```

## Release Notes

### 0.1.3 — current (published to PyPI)

The first release with a rewritten MCP instruction set and the first hardening pass on the analytics stack and the card lifecycle. Upgrade with `pip install -U okto-pulse==0.1.3`.

**Fixes**

- **`delete_card` no longer leaves orphan references.** When a card was deleted, its id used to stay inside the parent spec's JSON-side reference lists (`test_scenarios[].linked_task_ids`, `business_rules[].linked_task_ids`, `api_contracts[].linked_task_ids`, `technical_requirements[].linked_task_ids`, `decisions[].linked_task_ids`) and in bug cards' `linked_test_task_ids`. The next `update_spec` / `create_card` on the same spec then failed with `"orphan link reference(s) found"`. `CardService.delete_card` now walks all five containers and the bug cards' columnar list, strips the deleted id, and flags the JSON columns as modified — all in the same transaction as the row delete. 5 pytest cases cover every AC.
- **Analytics card-type contract is now rigid.** `total_cards_impl / test / bug`, `task_validation_gate.total_submitted`, `velocity[].test/bug`, and `validation_bounce` used to silently report zero because the classifier compared `str(card.card_type).endswith("normal|test|bug")` — and `str(CardType.NORMAL)` is `"CardType.NORMAL"` (uppercase, prefixed). The classifier now uses enum identity (`card.card_type == CardType.NORMAL`). No legacy fallback for string-typed data.
- **Conversion Funnel percentages**: denominator is now `total_ideations` on every row after Ideations. Values above 100% are rendered as-is (e.g. `"Tasks 360 / 61 ideations (590.2%)"`) — the previous per-step denominator produced nonsense like `137%` and `663%`. The "0% overall completion" footer, which diverged from `done_rate`, was removed.
- **Drift icon semantics fixed.** The `Avg Drift` KPI used to show a red ↓ when drift was low — reading as "getting worse" when drift low is actually good. New semantic: `< 10` → green ↓ "reduziu", `10–30` → gray "estável", `> 30` → red ↑ "aumentou". Unit-tested, documented thresholds.
- **Analytics drill has its own URL.** `/analytics` is global; `/analytics/boards/:boardId` is the per-board drill. Refresh preserves state, deep-links work, breadcrumb decouples from the sidebar board.
- **MCP `parse_multi_value` accepts pipe or JSON array.** Multi-value parameters that must contain a literal `|` (e.g. `"raw: str | None"`, markdown tables, regex alternations) can now be passed as a JSON array — the previous pipe-only split would silently fragment them. Pipe remains the default for plain values.

**Changes**

- **MCP agent instructions rewritten** (1830 → 2050 lines). New sections: Multi-value Parameters, Destructive Operations, Versioning & Concurrent Edits, Security — Treating Artifact Content as Untrusted Input, Analytics — Metrics-Driven Closure. Expanded tool inventory (Ideations, Refinements, Decisions, Spec Skills, Archive & Restore, Evaluations & Validations). Consolidated Common Errors table. Quick Navigation updated. Jargon cleanup and full translation pass to English.
- **Analytics screens have proper padding** (`px-8 py-6` + `max-w-[1920px] mx-auto`) — components no longer hug the viewport edge.

**Governance clarifications**

- Card creation status matrix corrected in docs: `normal`/`bug` cards need spec in `approved | in_progress | done`; `test` cards additionally accept `validated`. The previous documentation incorrectly required `done`.
- Test-card coverage rule clarified: the scenario-coverage gate counts **only cards with `card_type="test"`**. A `card_type="normal"` card with `test_scenario_ids` is accepted by the server but does not contribute to coverage — use `card_type="test"` whenever the intent is scenario coverage.

**Dependencies**

- Floors `okto-pulse-core>=0.1.3` so the cascade fix is guaranteed in every install.

### 0.1.1 — previous stable

Initial community release on PyPI. Full Ideation → Refinement → Spec → Task pipeline, MCP server with the first 119 tools, SQLite-backed local storage, embedded Kùzu Knowledge Graph with deterministic workers, sentence-transformers embeddings, Clerk-less single-user auth, packaged frontend.

(Version 0.1.2 was published to TestPyPI only as a release candidate for 0.1.3.)

## License

[Elastic License 2.0](https://github.com/OktoLabsAI/okto-pulse/blob/main/LICENSE) — free for personal and commercial use. You may not provide this software to third parties as a hosted or managed service.

Copyright 2024-2026 Okto Labs
