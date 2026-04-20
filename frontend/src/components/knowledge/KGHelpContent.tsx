/**
 * KG help content — canonical, English-only, bundled with the frontend.
 * Rendered by KGHelpModal via MarkdownContent. Keep sections short and
 * concrete; anything longer than ~500 words per section should probably
 * be split.
 *
 * Adding a new NodeType or RelationType? Update three places in lockstep:
 *   1. okto_labs_pulse_core/src/okto_pulse/core/kg/schema.py
 *   2. frontend/src/types/knowledge-graph.ts (NODE_TYPE_CONFIG or EDGE_TYPE_CONFIG)
 *   3. the Node Types / Connection Types section below
 * Bump SCHEMA_VERSION in frontend/src/constants/kg.ts to match backend.
 */

import type { ReactNode } from 'react';
import {
  BookOpen,
  Boxes,
  GitBranch,
  Workflow,
  Compass,
} from 'lucide-react';
import {
  ALL_EDGE_TYPES,
  EDGE_TYPE_CONFIG,
  NODE_TYPE_CONFIG,
  type KGEdgeType,
  type KGNodeType,
} from '@/types/knowledge-graph';

export interface KGHelpSection {
  id: string;
  title: string;
  icon: ReactNode;
  content: string;
}

const ALL_NODE_TYPES = Object.keys(NODE_TYPE_CONFIG) as KGNodeType[];

/** Build the Node Types section from NODE_TYPE_CONFIG so the help stays
 *  in sync with the canonical visual config (single source of truth). */
function buildNodeTypesMarkdown(): string {
  const rows = ALL_NODE_TYPES.map((type) => {
    const cfg = NODE_TYPE_CONFIG[type];
    return `### <span style="display:inline-block;width:10px;height:10px;border-radius:9999px;background:${cfg.color};vertical-align:middle;margin-right:6px"></span>${cfg.icon} ${type}\n\n${cfg.description}`;
  }).join('\n\n');
  return `The graph carries **${ALL_NODE_TYPES.length} node types**. Each one represents a different kind of artifact or claim on the board. The color + icon you see here match exactly what is drawn on the canvas and in the mini-map.\n\n${rows}`;
}

/** Build the Connection Types section from EDGE_TYPE_CONFIG for the same
 *  reason as above — the list below always matches what the canvas draws. */
function buildEdgeTypesMarkdown(): string {
  const rows = (ALL_EDGE_TYPES as KGEdgeType[])
    .map((type) => {
      const cfg = EDGE_TYPE_CONFIG[type];
      return `### <span style="display:inline-block;width:20px;height:3px;background:${cfg.color};vertical-align:middle;margin-right:6px;border-radius:2px"></span>${type}\n\n${cfg.description}`;
    })
    .join('\n\n');
  return `The graph carries **${ALL_EDGE_TYPES.length} connection types**. Each one expresses a different semantic link between nodes — from strong ones like \`supersedes\` (hierarchical replacement) to soft ones like \`relates_to\` (shared topic).\n\n${rows}`;
}

const OVERVIEW = `## What is the Knowledge Graph?

The Knowledge Graph (KG) is the structural memory of your board. Every time
you create or update an ideation, a refinement, a spec, a sprint, a card, or
a decision, a background pipeline consolidates that artifact into a graph of
typed nodes connected by typed edges. You read the graph as a second view
on top of your work — one that highlights dependencies, contradictions,
supersedence chains, and coverage gaps that would be invisible in a flat
list.

## Why bother?

A flat kanban answers "what's on my plate?". The KG answers the harder
questions: _What covers this requirement? Which decisions are still active?
Who depends on this card? What did we supersede last sprint?_ The agent uses
these queries via MCP tools. This screen exposes the same power to humans.

## Who is this for?

The primary persona is a **developer onboarding onto the project**. The KG
compresses weeks of "please point me at the right file" into a visual map
of the work. PMs and stakeholders can also use it, but the information
density is tuned for engineers.

Start by exploring the Node Types and Connection Types sections below —
they are the vocabulary you will see everywhere on the canvas.`;

const CONSOLIDATION_PROCESS = `Every artifact on the board becomes a KG node through a 5-step pipeline.
Nothing is written synchronously in the request thread — the heavy work
happens in the background, so creating a card or updating a spec stays fast.

### 1. Event

A write to any SDLC artifact (ideation, refinement, spec, sprint, card,
decision) publishes a domain event on the same transaction. If the write
commits, the event is durably persisted; if it rolls back, no event fires.
This is the **outbox pattern** — no chance of a silent split between what
the board says and what the graph eventually shows.

### 2. Queue

Each event is translated into a consolidation-queue row. The queue is a
plain Postgres/SQLite table so you can inspect it in the Pending Queue view
at any time. Rows move through \`pending → in_progress → done\` (or
\`failed\` on error, with an explicit \`last_error\` column for debugging).

### 3. Worker

A background worker polls the queue every few seconds (configurable in
Settings) and claims a batch. It resolves the artifact's current content,
computes a content hash, and skips the consolidation entirely if the hash
matches a previously-committed version — this is what makes retry cheap
and safe.

### 4. Embeddings

Textual fields (title, content, summary) are embedded via a local
sentence-transformer model (default: all-MiniLM-L6-v2, 384 dimensions).
The embeddings power the similarity-based queries — "find cards like this
one", "what supersedes decision X", semantic search in Global Discovery.
The vector index is an HNSW inside Kùzu, so nothing leaves the machine.

### 5. Graph (Kùzu)

Finally the worker writes typed nodes and edges to a Kùzu graph database.
Existing nodes are updated (not duplicated) when the content hash changes,
and supersedence chains keep the full history queryable. Once this step
commits, the new state is visible on the canvas the next time the page
refreshes or the live-events indicator fires.

### When something goes wrong

If a consolidation fails the row stays in \`failed\` state with the error
text. Use the **Pending Queue** view's retry button to reprocess a single
row, or **Historical Consolidation** (Settings) to backfill a whole board.
For artifacts that never entered the queue at all (created before the
pipeline existed, or via an import), the badge reads **\`not_queued\`** and
only a historical backfill can pick them up.`;

const HOW_TO_EXPLORE = `The KG screen is a read-only visual query tool — you cannot mutate the
graph here, only see it and ask questions of it. Five controls matter.

### Node type filter

The **Node Types** list on the left sidebar is a checkbox per type. Hide a
type to declutter (e.g. hide Assumption nodes to focus on Decisions). The
heading shows the current page size (not the total in the board).

### Edge type chips

The **Edge Types** row below is ten colored pills, one per connection type.
Click to toggle. Active pills carry a ✓ prefix and a ring so the "all on"
default is visually distinct from "I disabled some".

### Relevance slider

Each node has a \`relevance_score\` in [0, 1]. The slider filters out
anything below the threshold. Below the slider sits a mini-histogram of
the distribution — it is a diagnostic, not just decoration. If all bars are
stacked in one bucket, the consolidation pipeline is not yet producing
variable scores and the filter will not meaningfully change what you see.

### Search

A substring match across node titles and content. Pairs well with the
relevance slider for "what high-importance nodes mention 'SSO'?".

### Canvas + minimap + drill-down

The canvas drags, zooms, and auto-fits when you load more pages. Hover a
node briefly (500 ms) to see a tooltip; single-click to open the side
preview; double-click to open the full detail modal. The mini-map in the
corner is faithful to the canvas' current viewport and is pannable.

### Sidebar collapse

The sidebar itself can be collapsed (right-arrow icon in its header). Use
this when you want the canvas to take the whole screen. The state is
persisted to localStorage so your preference survives reloads.

### Beyond the canvas

Other sub-views reachable from the left nav:

- **Global Discovery** — semantic search across boards you can access.
- **Audit Log** — every consolidation session the worker committed, with
  counts of nodes/edges added or superseded.
- **Pending Queue** — live state of the consolidation worker: what is
  waiting, what is running, what failed.
- **Pending Tree** — the same info organized by SDLC hierarchy
  (ideation → refinement → spec → sprint → card) with a status badge per
  artifact.
- **Settings** — toggle consolidation, pick an embedding provider, or
  trigger historical backfill.`;

export const KG_HELP_SECTIONS: KGHelpSection[] = [
  {
    id: 'overview',
    title: 'Overview',
    icon: <BookOpen size={16} />,
    content: OVERVIEW,
  },
  {
    id: 'node-types',
    title: 'Node Types',
    icon: <Boxes size={16} />,
    content: buildNodeTypesMarkdown(),
  },
  {
    id: 'connection-types',
    title: 'Connection Types',
    icon: <GitBranch size={16} />,
    content: buildEdgeTypesMarkdown(),
  },
  {
    id: 'consolidation-process',
    title: 'Consolidation Process',
    icon: <Workflow size={16} />,
    content: CONSOLIDATION_PROCESS,
  },
  {
    id: 'how-to-explore',
    title: 'How to Explore',
    icon: <Compass size={16} />,
    content: HOW_TO_EXPLORE,
  },
];
