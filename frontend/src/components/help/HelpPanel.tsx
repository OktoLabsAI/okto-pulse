/**
 * HelpPanel - In-app user guide with quickstart and deep-dive sections.
 * Content adapts based on edition (community vs ecosystem).
 */

import { useState } from 'react';

// MCP configuration - uses runtime config or environment variable or defaults to port 8101
const getMcpBaseUrl = () => {
  if (typeof window !== 'undefined' && (window as any).OKTO_PULSE_CONFIG?.MCP_URL) {
    return (window as any).OKTO_PULSE_CONFIG.MCP_URL;
  }
  const mcpPort = typeof import.meta.env.VITE_MCP_PORT !== 'undefined' ? import.meta.env.VITE_MCP_PORT : '8101';
  return `http://127.0.0.1:${mcpPort}/mcp`;
};
const MCP_BASE_URL = getMcpBaseUrl();
const MCP_URL_EXAMPLE = `${MCP_BASE_URL}?api_key=dash_your_key_here`;
const MCP_URL_EXAMPLE_KEY = `${MCP_BASE_URL}?api_key=dash_a1b2c3d4...`;

// Helper to extract port from MCP URL for display
const getMcpPort = () => {
  const match = MCP_BASE_URL.match(/:(\d+)\/mcp/);
  return match ? match[1] : '8101';
};
const MCP_PORT = getMcpPort();
const WEB_URL = typeof window !== 'undefined' && (window as any).OKTO_PULSE_CONFIG?.API_URL
  ? (window as any).OKTO_PULSE_CONFIG.API_URL.replace('/api/v1', '')
  : (typeof import.meta.env.VITE_API_URL !== 'undefined' && import.meta.env.VITE_API_URL !== '/api/v1'
      ? import.meta.env.VITE_API_URL.replace('/api/v1', '')
      : `http://127.0.0.1:8100`);
import { X, ChevronRight, Rocket, Lightbulb, FileText, LayoutList, Bug, BarChart3, BookOpen, Shield, Users, Bot, GitBranch, Settings, CheckCircle, Network } from 'lucide-react';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import pulseIcon from '@/assets/pulse-icon.svg';

interface HelpPanelProps {
  onClose: () => void;
}

const isEcosystem = typeof __AUTH_MODE__ !== 'undefined' && __AUTH_MODE__ === 'clerk';

interface Section {
  id: string;
  title: string;
  icon: React.ReactNode;
  content: string;
}

function getSections(): Section[] {
  const sections: Section[] = [
    {
      id: 'quickstart',
      title: 'Quickstart',
      icon: <Rocket size={16} />,
      content: isEcosystem ? `
## Welcome to Okto Pulse (Ecosystem)

Okto Pulse is a **spec-driven project management tool** designed for AI-assisted software development. It guides you from raw ideas to shipped code through a structured pipeline:

**Ideation** → **Refinement** → **Spec** → **Tasks**

### Get started

#### Step 1 — Create a board

Click **"+ New Dashboard"** in the sidebar or menu. A board is your project workspace where ideations, specs, and tasks live.

#### Step 2 — Create an agent and connect it via MCP

This is the most important step. AI agents (Claude Code, Cursor, Windsurf, etc.) interact with your board through the **MCP protocol**. Without an agent connected, you won't get the full AI-assisted experience.

1. Open the **Menu** (☰) → **Agents**
2. In the **"My Agents"** tab, fill in the agent name (e.g. "Claude Code") and click **Create**
3. The system generates an **API Key** (starts with \`dash_\`) — **copy it immediately**, it's only shown once
4. Switch to the **"Board Access"** tab
5. Select your agent from the dropdown and click **Grant Access** to link it to the current board
6. Back in "My Agents", click the **MCP config** button and choose your tool format (Claude Code, Cursor, VS Code, Windsurf, etc.)
7. Copy the generated config into your tool's MCP configuration file

**Example \`.mcp.json\` for Claude Code / Claude Desktop:**

\`\`\`json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "${MCP_URL_EXAMPLE}"
    }
  }
}
\`\`\`

> **Important:** The agent must have board access to interact with it. One agent can access multiple boards.

#### Step 3 — Create your first ideation

Go to the **Ideations** tab and describe your idea. The AI agent can now help you evaluate, refine, and spec it out through MCP tools.

#### Step 4 — Let the pipeline guide you

Evaluate the ideation → refine if needed → write a spec with acceptance criteria and test scenarios → break it into task cards on the Kanban board.

### Multi-user collaboration

You can **share boards** with other team members:
- Click **Share** in the menu to invite collaborators
- Each member sees the same board in real-time
- Activity logs track who did what and when
- Each team member can have their own agents connected

### The pipeline at a glance

| Stage | Purpose | Key actions |
|-------|---------|-------------|
| **Ideation** | Capture the idea | Write, ask questions, evaluate complexity |
| **Refinement** | Deepen analysis (medium/large ideas) | Break down scope, add knowledge, make decisions |
| **Spec** | Define what to build | Acceptance criteria, test scenarios, business rules, API contracts |
| **Tasks** | Build it | Kanban cards, dependencies, conclusions, bug tracking |

> **Tip:** Each stage has a Q&A system — use it to clarify doubts and document decisions before moving forward.
` : `
## Welcome to Okto Pulse (Community)

Okto Pulse is a **spec-driven project management tool** designed for AI-assisted software development. It guides you from raw ideas to shipped code through a structured pipeline:

**Ideation** → **Refinement** → **Spec** → **Tasks**

### Get started

#### Step 1 — Initialize (first time only)

If you haven't already, run in your terminal:

\`\`\`bash
okto-pulse init
\`\`\`

This creates:
- A default **board** ("My Board")
- A default **agent** ("Local Agent") with an API key
- A **\`.mcp.json\`** file in the current directory with the MCP connection config

The output will show something like:

\`\`\`
Board created: My Board
Agent created: Local Agent
API Key: dash_a1b2c3d4...
MCP URL: ${MCP_URL_EXAMPLE_KEY}

.mcp.json generated at: /your/project/.mcp.json
\`\`\`

#### Step 2 — Start the server

\`\`\`bash
okto-pulse serve
\`\`\`

This starts the **API + Frontend** (port 8100) and the **MCP server** (port ${MCP_PORT}). Open \`${WEB_URL}\` in your browser.

#### Step 3 — Connect your AI tool via MCP

The \`.mcp.json\` generated by \`okto-pulse init\` is ready to use. Most AI tools auto-detect it:

- **Claude Code / Claude Desktop** — reads \`.mcp.json\` from your project root automatically
- **Cursor** — add to your MCP settings in Cursor preferences
- **VS Code (Copilot)** — add to \`.vscode/mcp.json\`
- **Windsurf / Cline** — reads \`.mcp.json\` from project root

**The \`.mcp.json\` format:**

\`\`\`json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "${MCP_URL_EXAMPLE}"
    }
  }
}
\`\`\`

> **Important:** The MCP server must be running (\`okto-pulse serve\`) for AI tools to connect. The agent must have access to the board — the default setup handles this automatically.

#### Step 4 — Create your first ideation

Go to the **Ideations** tab and describe your idea. The AI agent can now help you evaluate, refine, and spec it out through the 119+ MCP tools available.

#### Step 5 — Let the pipeline guide you

Evaluate the ideation → refine if needed → write a spec with acceptance criteria and test scenarios → break it into task cards on the Kanban board.

### Managing agents

To create additional agents or manage board access, open **Menu** (☰) → **Agents**:
- **My Agents** tab — create, view API keys, regenerate keys, delete agents
- **Board Access** tab — grant or revoke agent access to the current board

### Custom ports

\`\`\`bash
okto-pulse --api-port 9000 --mcp-port 9001 serve
\`\`\`

Remember to update the MCP URL in your \`.mcp.json\` if you change the MCP port.

### The pipeline at a glance

| Stage | Purpose | Key actions |
|-------|---------|-------------|
| **Ideation** | Capture the idea | Write, ask questions, evaluate complexity |
| **Refinement** | Deepen analysis (medium/large ideas) | Break down scope, add knowledge, make decisions |
| **Spec** | Define what to build | Acceptance criteria, test scenarios, business rules, API contracts |
| **Tasks** | Build it | Kanban cards, dependencies, conclusions, bug tracking |

> **Tip:** Each stage has a Q&A system — use it to clarify doubts and document decisions before moving forward.
`,
    },
    {
      id: 'agents',
      title: 'Agents & MCP',
      icon: <Bot size={16} />,
      content: isEcosystem ? `
## Agents & MCP — Connect AI tools to your board

Agents are the bridge between AI tools (Claude Code, Cursor, Windsurf, etc.) and your Okto Pulse boards. Each agent has its own identity, API key, and board access permissions.

### How it works

\`\`\`
Your AI Tool (Claude Code, Cursor, etc.)
    ↓ MCP protocol
Okto Pulse MCP Server (port ${MCP_PORT})
    ↓ API key authentication
Agent identity resolved → board access checked
    ↓
119+ tools available (create cards, move specs, add comments, etc.)
\`\`\`

### Creating an agent

1. Open **Menu** (☰) → **Agents**
2. In the **"My Agents"** tab, enter:
   - **Name** — e.g. "Claude Code", "Cursor Agent", "CI Bot"
   - **Description** — optional, what this agent does
   - **Objective** — optional, the agent's primary goal
3. Click **Create**
4. **Copy the API key immediately** — it starts with \`dash_\` and is only shown once

### Granting board access

An agent needs explicit access to each board it should interact with:

1. Open **Menu** (☰) → **Agents** → **"Board Access"** tab
2. Select an agent from the dropdown (only agents without access are shown)
3. Click **Grant Access**

To revoke access, click **Revoke** next to the agent in the same tab.

> One agent can access multiple boards. One board can have multiple agents.

### Configuring your AI tool

Click the **MCP config** button next to your agent to get a pre-formatted config snippet. Supported formats:

| Tool | Config location |
|------|----------------|
| **Claude Code** | \`.mcp.json\` in project root (auto-detected) |
| **Claude Desktop** | Claude Desktop settings → MCP servers |
| **Cursor** | Cursor preferences → MCP settings |
| **VS Code** | \`.vscode/mcp.json\` |
| **Windsurf** | \`.mcp.json\` in project root |
| **Cline** | Cline settings → MCP servers |

**Example \`.mcp.json\`:**

\`\`\`json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "${MCP_URL_EXAMPLE}"
    }
  }
}
\`\`\`

### API key management

- **Regenerate key** — If a key is compromised, click **Regenerate** in the Agents panel. The old key stops working immediately.
- **Key format** — Always starts with \`dash_\` followed by 48 hex characters
- **Storage** — Keys are hashed (SHA256) in the database. Okto Pulse cannot recover a lost key — regenerate instead.

### Agent activity tracking

Every action taken by an agent is logged in the activity feed:
- Which agent performed the action
- What changed (card moved, comment added, etc.)
- When it happened

View activity in **Analytics** or the board's activity log.

### Multiple agents per board

A common setup is to have different agents for different purposes:
- **Development agent** — Creates cards, moves tasks, adds conclusions
- **Review agent** — Reviews specs, asks questions, adds comments
- **CI/CD agent** — Reports test results, creates bug cards
` : `
## Agents & MCP — Connect AI tools to your board

Agents are the bridge between AI tools (Claude Code, Cursor, Windsurf, etc.) and your Okto Pulse boards. The Community edition comes with a **default agent pre-configured**.

### How it works

\`\`\`
Your AI Tool (Claude Code, Cursor, etc.)
    ↓ MCP protocol
Okto Pulse MCP Server (default port ${MCP_PORT})
    ↓ API key authentication
Agent identity resolved → board access checked
    ↓
119+ tools available (create cards, move specs, add comments, etc.)
\`\`\`

### Default setup (automatic)

When you run \`okto-pulse init\`, the system creates:
- A **"Local Agent"** with an API key
- A **"My Board"** with the agent already granted access
- A **\`.mcp.json\`** file in the current directory

This means **your AI tool is ready to connect immediately** — no manual setup required.

### Connecting your AI tool

Most tools auto-detect the \`.mcp.json\` file:

| Tool | How to connect |
|------|---------------|
| **Claude Code** | Auto-detects \`.mcp.json\` in project root — just start Claude Code |
| **Claude Desktop** | Copy the config to Claude Desktop settings → MCP servers |
| **Cursor** | Add the MCP URL to Cursor preferences → MCP settings |
| **VS Code** | Copy to \`.vscode/mcp.json\` |
| **Windsurf / Cline** | Auto-detects \`.mcp.json\` in project root |

**The \`.mcp.json\` file looks like this:**

\`\`\`json
{
  "mcpServers": {
    "okto-pulse": {
      "url": "${MCP_URL_EXAMPLE}"
    }
  }
}
\`\`\`

### Creating additional agents

You can create more agents beyond the default one:

1. Open **Menu** (☰) → **Agents**
2. In the **"My Agents"** tab, enter a name and click **Create**
3. **Copy the API key immediately** — it's only shown once
4. Switch to **"Board Access"** tab and grant the new agent access to your board

### Managing board access

In **Menu** (☰) → **Agents** → **"Board Access"** tab:
- See which agents have access to the current board
- **Grant Access** — select an agent from the dropdown
- **Revoke** — remove an agent's access

### API key management

- **View key** — In the Agents panel, your key is visible (copy it)
- **Regenerate key** — If compromised, click **Regenerate**. The old key stops working immediately.
- **Lost key** — Keys are hashed in the database. If lost, regenerate a new one.

### Running on custom ports

If you changed the MCP port:

\`\`\`bash
okto-pulse --mcp-port 9001 init
okto-pulse --mcp-port 9001 serve
\`\`\`

The \`.mcp.json\` is regenerated with the new port on \`init\`. Make sure your AI tool points to the correct URL.

### Troubleshooting

- **"Connection refused"** — Make sure \`okto-pulse serve\` is running
- **"Unauthorized"** — Check the API key in \`.mcp.json\` matches the one from \`init\` output
- **"No board access"** — Grant the agent access in Menu → Agents → Board Access
- **Tools not appearing** — Restart your AI tool after adding the MCP config
`,
    },
    {
      id: 'ideations',
      title: 'Ideations',
      icon: <Lightbulb size={16} />,
      content: `
## Ideations — Capture ideas before building

An ideation is the starting point. It answers: *"What do we want to do and why?"*

### Creating an ideation

Click **"+ New Ideation"** in the Ideations tab and fill in:
- **Title** — Short description of the idea
- **Description** — Full explanation of the problem and proposed solution
- **Context** — Background info, user needs, market context

### Ideation lifecycle

\`draft\` → \`review\` → \`approved\` → \`evaluating\` → \`done\`

- **Draft** — Initial state; write and iterate freely
- **Review** — Submitted for review by the team or AI agent
- **Approved** — Accepted as viable; ready for complexity evaluation
- **Evaluating** — Being assessed for complexity and feasibility
- **Done** — Evaluation complete; complexity assigned (small / medium / large)

Additional status: \`cancelled\`

### Complexity and what comes next

| Complexity | Next step |
|-----------|-----------|
| **Small** | Skip refinement → create a Spec directly |
| **Medium / Large** | Create a Refinement first to break down the scope |

### Q&A and Choice Boards

Use the Q&A section to ask and answer questions about the ideation. This creates a documented decision trail that stays with the idea.

**Choice questions** let you present multiple options (single or multi-select) for the team or AI agent to vote on. Responses are tracked with responder info and timestamps. You can also allow free-text responses alongside predefined choices.

### Snapshots and History

When an ideation moves to "done", an immutable **snapshot** is captured. This preserves the exact state for future reference.

The **History** tab shows a full change log with field-level diffs — who changed what and when. This lets you trace every decision back to its origin.

### Deriving specs and refinements

From a "done" ideation, click **"Derive Spec/Refinement"**:
- **Small ideas** → derive a Spec directly
- **Medium/large ideas** → derive a Refinement first, then specs from the refinement
`,
    },
    {
      id: 'refinements',
      title: 'Refinements',
      icon: <FileText size={16} />,
      content: `
## Refinements — Deepen medium/large ideas

A refinement takes a "done" ideation and breaks it into detailed analysis: scope decisions, knowledge bases, and architecture considerations.

### When to use refinements

- The ideation was evaluated as **medium** or **large** complexity
- The scope is too broad for a single spec
- You need to make architectural decisions before specifying

### Creating a refinement

From a "done" ideation, click **"Derive Spec/Refinement"**. Choose Refinement for medium/large ideas.

### Refinement lifecycle

\`draft\` → \`review\` → \`approved\` → \`done\`

- **Draft** — Initial state; analyze and document freely
- **Review** — Submitted for review
- **Approved** — Analysis validated; ready to derive specs
- **Done** — Fully processed; specs derived

Additional status: \`cancelled\`

### Key features

- **Description & Analysis** — Detailed breakdown of the problem space
- **In-scope / Out-of-scope** — Explicitly define boundaries to prevent scope creep
- **Decisions** — Document key technical or product decisions with rationale
- **Knowledge Base** — Attach reference documents, technical notes, or research
- **Q&A** — Same question system as ideations, with choice questions
- **Snapshots** — Immutable versions captured at key milestones
- **History** — Full change log with field-level diffs

### From refinement to spec

When a refinement is "done", you can derive one or more Specs from it. Each spec inherits context from the refinement. You can also derive specs directly from the refinement modal.
`,
    },
    {
      id: 'specs',
      title: 'Specs',
      icon: <FileText size={16} />,
      content: `
## Specs — Define exactly what to build

A spec is the **blueprint for implementation**. It contains everything needed to write code: acceptance criteria, test scenarios, business rules, API contracts, mockups, and functional requirements.

### Creating a spec

Specs are derived from "done" ideations (small) or "done" refinements (medium/large). Click **"Derive Spec"** from the source entity.

### Spec lifecycle

\`draft\` → \`review\` → \`approved\` → \`validated\` → \`in_progress\` → \`done\`

- **Draft** — Write and iterate on the spec freely
- **Review** — Submitted for review by the team or AI agent
- **Approved** — Content accepted; eligible for validation
- **Validated** — Passes the Spec Validation Gate (see Validation Gates section)
- **In Progress** — Tasks are being worked on
- **Done** — All tasks complete + coverage checks pass

Additional status: \`cancelled\`

### Spec tabs

| Tab | Purpose |
|-----|---------|
| **Details** | Title, description, context (supports Markdown + Mermaid diagrams), functional & technical requirements, acceptance criteria |
| **Test Scenarios** | Given/When/Then format with types (unit, integration, e2e, manual), status tracking, task linking, **EvidenceBadge** (✓/?) for scenarios in \`automated/passed/failed\` — see Test Evidence Gate in Governance |
| **Business Rules** | When/Then format for domain logic, linked to functional requirements |
| **API Contracts** | Endpoint definitions — method, path, request body, success/error responses |
| **Technical Reqs** | Technical constraints and implementation details, linked to task cards |
| **Mockups** | HTML/Tailwind screen mockups with types (page, modal, drawer, popover, panel) and design annotations |
| **Skills** | Multi-section reusable knowledge blocks (prompts, guidelines) that agents can load |
| **Knowledge Base** | Attach reference documents, technical notes, research, or file content |
| **Q&A** | Text questions, choice boards (single/multi-select), free-text responses |
| **Cards** | View all derived task cards, link/unlink cards |
| **Sprints** | Create and manage sprints, assign cards, use sprint suggestion algorithm |
| **History** | Full change log with field-level diffs |

### Coverage governance

The system enforces traceability:
- **Test scenarios** must cover all acceptance criteria
- **Business rules** must cover all functional requirements (if enabled)
- **API contracts** must be covered by tasks (if enabled)
- **Technical requirements** must link to task cards (if enabled)

These checks can be bypassed per-spec (\`skip_test_coverage\`, \`skip_rules_coverage\`, \`skip_trs_coverage\`, \`skip_contract_coverage\`) or globally in board Settings.

### Spec Validation Gate

Before a spec can move from **approved** to **validated**, it can be evaluated on 3 dimensions:

| Metric | What it measures |
|--------|-----------------|
| **Completeness** (0–100) | How thoroughly does the spec define what to build? |
| **Assertiveness** (0–100) | How measurable and testable are the requirements? |
| **Ambiguity** (0–100) | How many ways can the requirements be interpreted? (lower is better) |

Thresholds are configurable per board in Settings. Multiple evaluations can be submitted; the spec must pass all three to advance.

### Export

Specs can be exported as **Markdown** for external documentation or sharing.
`,
    },
    {
      id: 'tasks',
      title: 'Tasks (Kanban)',
      icon: <LayoutList size={16} />,
      content: `
## Tasks — The Kanban board

Tasks are the work items that implement a spec. They live on a Kanban board with drag-and-drop columns.

### Creating tasks

Tasks are created within a spec context. Each task links back to its parent spec, ensuring traceability from idea to implementation.

### Task lifecycle (columns)

\`not_started\` → \`started\` → \`in_progress\` → \`validation\` → \`done\`

Additional statuses: \`on_hold\`, \`cancelled\`

### Card types

| Type | Purpose | Validation gate |
|------|---------|-----------------|
| **Normal** | Regular implementation task | Subject to Task Validation Gate when enabled |
| **Bug** | Defect tracking (see Bug Tracking section) | Subject to gate; additional test-first workflow |
| **Test** | Test implementation task | **Bypasses** the validation gate — moves directly through the kanban |

> Only \`card_type=test\` cards count toward **scenario-coverage**. A normal card with \`test_scenario_ids\` is accepted by the server but does not satisfy coverage. Use \`test\` whenever the intent is scenario coverage.

### Task detail tabs

| Tab | Purpose |
|-----|---------|
| **Details** | Title, description, status, priority, assignee, labels |
| **Tests** | Linked test scenarios from the parent spec, coverage display |
| **Mockups** | UI mockups from the parent spec for reference |
| **Knowledge** | Attached knowledge base content |
| **Conclusion** | Completion summary (see below) |
| **Validations** | Validation history with scores |
| **Q&A** | Questions and answers about the task |
| **Comments** | Discussion thread with choice boards |
| **Activity** | Full activity log with timestamps and actor info |

### Task conclusions

Before a task moves to **done**, a **conclusion** is required:
- **What was done** — Summary of the work performed
- **Changes / files modified** — What was changed
- **Decisions made** — Any decisions taken during implementation
- **Test results** — Outcome of testing
- **Follow-ups** — Items that need further attention
- **Completeness %** (0–100) — How complete is the implementation
- **Drift score** (0–100) — How much did the implementation deviate from the plan

### Task Validation Gate

When the board has \`require_task_validation\` enabled, tasks pass through a **validation** column before done. An evaluator (human or AI) scores the task on:

| Metric | Default threshold |
|--------|------------------|
| **Confidence** (0–100) | ≥ 70% |
| **Completeness** (0–100) | ≥ 80% |
| **Drift** (0–100) | ≤ 50% |

If the task **passes** all thresholds → moves to \`done\`. If it **fails** → returns to \`not_started\` for rework. Thresholds are configurable per board in Settings.

### Dependencies

Tasks can have dependencies on other tasks:
- Add a dependency via the task detail panel
- A task **cannot be moved forward** if its dependencies are not yet done
- Circular dependencies are automatically rejected

### Priority levels

\`critical\` > \`very_high\` > \`high\` > \`medium\` > \`low\` > \`none\`

### Labels and attachments

- **Labels** — Free-form tags to categorize tasks (e.g., "frontend", "database", "auth")
- **Attachments** — Upload files directly to a task card

### Export

Tasks can be exported as **Markdown** for documentation or sharing.

### Archiving

Done or cancelled tasks can be archived to keep the board clean. Archived tasks can be restored at any time. Use the **"Show archived"** toggle to view them.
`,
    },
    {
      id: 'bugs',
      title: 'Bug tracking',
      icon: <Bug size={16} />,
      content: `
## Bug cards — Track and fix defects

Bug cards are a special type of task card (\`card_type=bug\`) for defects discovered during or after implementation.

### When to create bugs

- A completed task doesn't work as expected
- A test scenario fails after code changes
- You discover a regression in existing functionality

> **Important:** Always register bugs — never fix them silently. Untracked bugs mean unmeasured quality.

### Creating a bug card

Bug cards require:

| Field | Required | Description |
|-------|----------|-------------|
| \`card_type\` | Yes | Must be \`"bug"\` |
| \`origin_task_id\` | Yes | The task where the bug was found |
| \`severity\` | Yes | \`critical\`, \`major\`, or \`minor\` |
| \`expected_behavior\` | Yes | What should happen |
| \`observed_behavior\` | Yes | What actually happens |
| \`steps_to_reproduce\` | No | How to reproduce |
| \`action_plan\` | No | Proposed fix approach |

The \`spec_id\` is auto-resolved from the origin task.

### Bug workflow (test-first)

The system enforces a **test-first** approach for bugs:

1. **Create** the bug card (status: \`not_started\`)
2. **Create a new test scenario** that covers the bug's failure case
3. **Create a test task** and link it to the bug
4. **Move to \`in_progress\`** — blocked until test task is linked
5. **Fix** the bug
6. **Move to \`done\`** with a conclusion

### Severity levels

| Level | Meaning |
|-------|---------|
| **Critical** | System down, data loss, security vulnerability |
| **Major** | Core feature broken, significant UX impact |
| **Minor** | Cosmetic, edge case, workaround exists |
`,
    },
    {
      id: 'analytics',
      title: 'Analytics',
      icon: <BarChart3 size={16} />,
      content: `
## Analytics — Track progress and quality

The Analytics dashboard (accessible from the menu) provides metrics across your boards with three drill-down levels.

### Dashboard levels

| Level | What it shows |
|-------|--------------|
| **Overview** | System-wide metrics across all boards — pipeline funnel, velocity, quality |
| **Board** | Per-board analytics — drill into a specific board's progress |
| **Entity** | Deep-dive into a specific ideation, spec, refinement, or card |

Click any board or entity in the overview to drill down. Breadcrumb navigation lets you go back.

### Available metrics

- **Pipeline funnel** — Ideations → Refinements → Specs → Tasks → Done
- **Specs done** — Completion rate over time
- **Bug rate** — Bugs per spec and per task
- **Bug severity distribution** — Critical / Major / Minor breakdown
- **Test coverage** — Acceptance criteria with linked test scenarios
- **Rules coverage** — Functional requirements with linked business rules
- **Triage time** — How quickly bugs get their first test task linked
- **Velocity** — Throughput and cycle time
- **Validation gate metrics** — Spec and task validation pass/fail rates
- **Agent performance** — Which agents are most active and effective
- **Sprint analytics** — Sprint progress and delivery metrics

### Filtering

Use the **date range filter** (default: last 30 days) to focus on a specific time period.

### Export

Analytics data can be exported as **CSV** at each level (overview, board, entity) for external reporting and analysis.
`,
    },
    {
      id: 'guidelines',
      title: 'Guidelines',
      icon: <BookOpen size={16} />,
      content: `
## Guidelines — Shared knowledge and standards

Guidelines are reusable documents that define standards, patterns, and conventions for your project.

### Types

- **Board guidelines** — Specific to a single board
- **Global guidelines** — Available across all boards, can be linked to any board

### Creating guidelines

Open **Guidelines** from the menu. You'll see two tabs:
1. **Board Guidelines** — Create inline or link from the global catalog
2. **Global Catalog** — Create, edit, and manage global guidelines

Guidelines support **Markdown** with Mermaid diagrams, tags, and version tracking (global only).

### Use cases

- Coding standards and conventions
- Architecture patterns
- Review checklists
- Onboarding documentation
- Decision records
`,
    },
    {
      id: 'governance',
      title: 'Governance & rules',
      icon: <Shield size={16} />,
      content: `
## Governance — Quality gates enforced by the system

Okto Pulse enforces a set of governance rules to maintain quality and traceability throughout the pipeline.

### Spec Validation Gate (approved → validated)

When \`require_spec_validation\` is enabled in board settings, specs must pass an evaluation before implementation can begin:

| Metric | What it measures | Default |
|--------|-----------------|---------|
| **Completeness** | How thoroughly defined | ≥ 70 |
| **Assertiveness** | How measurable/testable | ≥ 70 |
| **Ambiguity** | How many interpretations possible | ≤ 30 |

Multiple evaluations can be submitted (by humans or AI agents). The spec must pass all thresholds to advance.

### Spec → Done gates

| Rule | What it checks |
|------|---------------|
| **Test coverage** | Every acceptance criterion has at least one test scenario |
| **Rules coverage** | Every functional requirement has at least one business rule |
| **Contract coverage** | Every API contract has a linked task |
| **TR coverage** | Every technical requirement links to a task card |
| **Tasks complete** | All linked tasks (non-bug) are done or cancelled |

### Task Validation Gate (in_progress → done)

When \`require_task_validation\` is enabled in board settings, tasks must pass through a validation column:

| Metric | What it measures | Default |
|--------|-----------------|---------|
| **Confidence** | How confident the evaluator is in correctness | ≥ 70% |
| **Completeness** | How complete is the implementation | ≥ 80% |
| **Drift** | How much did it deviate from the plan | ≤ 50% |

Pass → task moves to \`done\`. Fail → task returns to \`not_started\` for rework.

### Task → Started gates

| Rule | What it checks |
|------|---------------|
| **Test scenario coverage** | Every test scenario has at least one linked task card |
| **Business rules coverage** | Every functional requirement has at least one linked business rule |
| **TR coverage** | Every technical requirement links to a task card |

### Task → Done gates

| Rule | What it checks |
|------|---------------|
| **Conclusion required** | A conclusion describing the work must be provided |

### Bug → In Progress gates

| Rule | What it checks |
|------|---------------|
| **Test task linked** | At least one new test task must be linked to the bug |
| **New scenario required** | The test scenario must have been created *after* the bug card |

### Test Evidence Gate (NC-9)

Marking a test scenario as \`automated\`, \`passed\`, or \`failed\` requires **proof of real execution** — preventing "test theater" where statuses are flipped without running anything.

| Field | Required (one of) |
|-------|-------------------|
| \`test_file_path\` | path to the test file (e.g. \`tests/test_auth.py\`) |
| \`test_function\` | function name (e.g. \`test_login_with_invalid_token\`) |
| \`last_run_at\` | ISO timestamp of the last run |
| \`output_snippet\` | up to 80 chars of test output |
| \`test_run_id\` | CI run ID or external test runner reference |

When evidence is present, an inline **EvidenceBadge** (✓ green) appears next to the status. Without evidence the badge is **? gray** and the gate blocks the transition unless \`skip_test_evidence_global\` is ON. When the skip flag is active, an app-wide amber banner reminds operators that the gate is bypassed.

The same evidence is required by the **sprint close** gate as defense-in-depth — a sprint cannot move to \`closed\` if any of its scoped scenarios lack evidence (and the skip flag is off).

### Overrides

All coverage checks can be bypassed:
- **Per spec** — Toggle \`skip_test_coverage\`, \`skip_rules_coverage\`, \`skip_trs_coverage\`, or \`skip_contract_coverage\` on the spec
- **Per sprint** — Override validation thresholds per sprint
- **Per board** — Toggle global overrides in **Board Settings** (menu)

### Dependencies

- A task cannot move forward if its dependencies are not done
- Dependencies create a directed acyclic graph (circular dependencies are rejected)
`,
    },
    {
      id: 'sprints',
      title: 'Sprints',
      icon: <GitBranch size={16} />,
      content: `
## Sprints — Incremental delivery

Sprints break a spec into incremental deliverables. Each sprint groups a subset of tasks with a clear objective, expected outcome, and timeline.

### Creating sprints

Sprints are created within a spec. Go to the **Sprints** tab in the spec modal:
- Click **"+ New Sprint"** to create manually
- Or use **"Suggest Sprints"** to let the AI suggest an optimal breakdown based on task dependencies and complexity

### Sprint lifecycle

\`draft\` → \`active\` → \`review\` → \`closed\`

- **Draft** — Define objective, assign tasks, set timeline
- **Active** — Work in progress; tasks are being executed
- **Review** — Sprint work complete; evaluating outcomes
- **Closed** — Sprint done; evaluation captured

Additional status: \`cancelled\`

### Sprint properties

| Property | Description |
|----------|-------------|
| **Objective** | What this sprint aims to achieve |
| **Expected Outcome** | Concrete deliverables |
| **Start / End Date** | Timeline boundaries |
| **Scoped Test Scenarios** | Which test scenarios apply to this sprint |
| **Scoped Business Rules** | Which business rules apply |

### Assigning tasks to sprints

Tasks are assigned to sprints from the sprint detail view or from the spec's Sprints tab. A task can only belong to one sprint at a time.

### Sprint evaluations

When a sprint moves to **review** or **closed**, an evaluation can be submitted:
- Qualitative assessment of the sprint's outcome
- Scores for delivery quality
- Notes on what went well and what needs improvement
- Multiple evaluations can be submitted (by different agents or team members)

### Sprint suggestion algorithm

The **"Suggest Sprints"** feature analyzes the spec's tasks, dependencies, and complexity to propose an optimal sprint breakdown. It considers:
- Task dependencies (dependent tasks go in later sprints)
- Task complexity (balance load across sprints)
- Test coverage (each sprint should be independently testable)

### Skip flags per sprint

Each sprint can override validation settings:
- \`skip_test_coverage\` — Skip test scenario coverage checks
- \`skip_rules_coverage\` — Skip business rules coverage checks
- \`skip_qualitative_validation\` — Skip sprint evaluation requirement
`,
    },
    {
      id: 'knowledge-graph',
      title: 'Knowledge Graph',
      icon: <Network size={16} />,
      content: `
## Knowledge Graph — Structured project intelligence

The Knowledge Graph (KG) extracts decisions, constraints, learnings, and relationships from your specs, cards, and sprints into a searchable, interactive graph.

### Accessing the Knowledge Graph

Click the **Knowledge Graph** tab in the main navigation. The KG page has 6 sub-views, selectable from the left panel:

| View | Purpose |
|------|---------|
| **Graph** | Interactive visualization with pan/zoom, node filtering, and edge rendering |
| **Audit Log** | History of all consolidation sessions — who added what and when |
| **Pending Queue** | Consolidation entries waiting to be processed |
| **KG Health** | Provider/model status, schema version, queue depth, **manual tick** trigger, dedup snapshot |
| **Settings** | KG configuration (GraphDB / Event Queue / Decay Tick tabs), provider status, danger zone |
| **Global Search** | Cross-board semantic search by natural language query |

### Node types (11)

| Type | Icon | Description |
|------|------|-------------|
| **Decision** | ⚖️ | Architectural or product decisions |
| **Criterion** | ✓ | Acceptance criteria or success conditions |
| **Constraint** | 🚫 | Technical or business limitations |
| **Assumption** | ❓ | Assumptions that may need validation |
| **Requirement** | 📋 | Functional or technical requirements |
| **Entity** | 🏷️ | Domain entities or concepts |
| **APIContract** | 📡 | API endpoint definitions |
| **TestScenario** | 🧪 | Test cases and verification steps |
| **Bug** | 🐛 | Defects and their learnings |
| **Learning** | 💡 | Lessons learned from bugs or incidents |
| **Alternative** | ↔️ | Considered but not chosen alternatives |

### Edge types (10)

\`supersedes\` · \`contradicts\` · \`derives_from\` · \`relates_to\` · \`mentions\` · \`depends_on\` · \`violates\` · \`implements\` · \`tests\` · \`validates\`

### How consolidation works

1. **Continuous consolidation** — When a card or sprint completes, the system automatically extracts entities and relationships into the graph
2. **Historical consolidation** — Process existing specs and sprints retroactively (enable in Settings → "Enable Historical Consolidation")

Each consolidation session creates an audit trail: nodes added, updated, superseded, and edges created.

### Graph visualization

The graph view uses **React Flow** for interactive exploration:
- **Nodes** are color-coded by type and sized by confidence
- **Edges** are colored by relationship type (red = contradicts, purple = supersedes, gray = other)
- Animated edges indicate contradictions
- **MiniMap** for overview navigation
- Click a node to open the **Node Detail Panel** on the right

### Node Detail Panel

Clicking a node reveals:
- Full content, justification, source artifact reference
- Confidence score and validation status
- **Find Similar** — Semantic search for related decisions
- **Show History** — Supersedence chain showing what replaced what (Decision nodes)

### KG Health & Manual Tick

The **KG Health** sub-view exposes runtime diagnostics:

- **Provider / model** — embedder backend (sentence-transformers or stub fallback)
- **Schema version** — current Kùzu schema (auto-migrated on hot path)
- **Queue depth** — pending consolidation entries
- **\`tick_in_progress\`** — \`true\` when the global advisory lock \`kg_daily_tick\` is held
- **Dedup snapshot** — entity counts and recent dedup actions

#### Run tick now

The **"Run tick now"** button forces a decay/recompute pass without waiting for the scheduler. The button is disabled while a tick is already running (cross-mount/cross-tab safe — backed by the advisory lock + 3s cooldown). Use it after major changes to recompute relevance scores immediately.

The same control surfaces in **Settings → Decay Tick** as **"Save & run now"**, which persists the 3 hot-reload settings (\`interval_minutes\`, \`staleness_days\`, \`max_age_days\`) and triggers a tick in one shot.

### Dead Letter Queue (DLQ) Inspector

When a consolidation entry fails 3 times, it lands in the DLQ. The **DLQ Inspector** modal (accessible from the Pending Queue view) lets you:
- List DLQ rows with full error history (timestamp, exception class, traceback)
- Inspect the original payload that triggered the failure
- Read-only in MVP — reprocess is planned for a future release

Use this to diagnose extractor regressions, embedder timeouts, or schema mismatches without tailing server logs.

### Schema migration self-heal

KG schema evolves across releases (e.g. v0.3.2 added \`human_curated\`, \`last_recomputed_at\`). The hot path **auto-migrates** on first read, so most users never notice. For boards stuck or pre-v0.3.2 graphs, a triplet is exposed:

- **CLI** — \`okto-pulse kg migrate-schema\`
- **MCP tool** — \`kg_migrate_schema\` (gemellar)
- **REST** — \`POST /api/v1/kg/migrate-schema\`

> **Important:** Never delete \`graph.kuzu\` manually — the migration preserves all nodes/edges. Deleting forces a full re-consolidation.

### Cognitive extractors (opt-in)

Beyond the structural extractors (Decision, Constraint, etc.), the KG can extract **Learning**, **Alternative**, and **Assumption** nodes via LLM. This is **opt-in** per board via \`cognitive_llm_config\` (provider + model + API key). When disabled, the system logs a structured event but does not call any LLM. Persistence in Kùzu for cognitive nodes is in v1 (DEFERRED — read the structured logs for now).

### AI agent integration

MCP agents can query the Knowledge Graph via 15+ tools:
- \`kg_get_decision_history\` — Find past decisions on a topic
- \`kg_find_contradictions\` — Detect conflicting decisions
- \`kg_find_similar_decisions\` — Semantic similarity search
- \`kg_explain_constraint\` — Trace constraint to source
- \`kg_get_supersedence_chain\` — See what replaced what
- \`kg_query_cypher\` — Direct Cypher queries (read-only, safety-checked)
- \`kg_query_natural\` — Natural language queries
- \`kg_migrate_schema\` — Trigger schema migration
- \`kg_trigger_tick\` — Force a decay tick (returns 202 + tick id)
- And more (see the full MCP tool list)
`,
    },
    {
      id: 'board-settings',
      title: 'Board Settings',
      icon: <Settings size={16} />,
      content: `
## Board Settings — Configure quality gates and behavior

Each board has configurable settings that control governance rules, validation thresholds, and coverage requirements. Access via **Menu** (☰) → **Settings**.

### Coverage skip flags

These flags bypass specific coverage checks for the entire board:

| Setting | What it skips |
|---------|--------------|
| \`skip_test_coverage_global\` | Test scenario → acceptance criteria coverage |
| \`skip_rules_coverage_global\` | Business rules → functional requirements coverage |
| \`skip_trs_coverage_global\` | Technical requirements → task card linkage |
| \`skip_contract_coverage_global\` | API contract → task card linkage |
| \`skip_test_evidence_global\` | **Test Evidence Gate** (NC-9) — when ON, test scenarios can be marked \`automated/passed/failed\` without proof of execution. A persistent amber banner appears app-wide until disabled. |

### Task Validation Gate thresholds

| Setting | Default | Description |
|---------|---------|-------------|
| \`require_task_validation\` | false | Enable the validation column on the Kanban board |
| \`min_confidence\` | 70 | Minimum confidence score to pass (0–100) |
| \`min_completeness\` | 80 | Minimum completeness score to pass (0–100) |
| \`max_drift\` | 50 | Maximum drift score allowed (0–100) |

### Spec Validation Gate thresholds

| Setting | Default | Description |
|---------|---------|-------------|
| \`require_spec_validation\` | false | Enable the spec validation gate (approved → validated) |
| \`min_spec_completeness\` | 70 | Minimum completeness score |
| \`min_spec_assertiveness\` | 70 | Minimum assertiveness score |
| \`max_spec_ambiguity\` | 30 | Maximum ambiguity score |

### Other settings

| Setting | Default | Description |
|---------|---------|-------------|
| \`max_scenarios_per_card\` | 5 | Maximum test scenarios per card (1–10) |

### Runtime Settings Panel

Beyond the static board settings above, the **Runtime Settings Panel** (Menu → Settings) exposes hot-reload knobs grouped into 3 tabs:

| Tab | Controls |
|-----|----------|
| **GraphDB** | Kùzu connection pool, timeout, query limits |
| **Event Queue** | \`kg_queue_min_interval_ms\` (0–1000), batch size, retry policy |
| **Decay Tick** | \`interval_minutes\` (5–10080), \`staleness_days\` (1–365), \`max_age_days\` (0–365) — plus the **"Save & run now"** action that persists + triggers a tick atomically |

Changes apply without restarting the server. The Decay Tick tab also polls \`/kg/health\` every 5s while open to surface \`tick_in_progress\` and disable conflicting buttons.

### Per-spec overrides

Individual specs can override board-level settings. Toggle the skip flags directly on the spec to bypass specific checks for that spec only.
`,
    },
  ];

  if (isEcosystem) {
    sections.push({
      id: 'permissions',
      title: 'Permissions',
      icon: <CheckCircle size={16} />,
      content: `
## Permissions — Fine-grained access control (Ecosystem)

The Ecosystem edition provides a granular permission system for controlling what agents and users can do.

### Permission presets

Presets define a named set of permissions that can be assigned to agents:
- Open **Menu** (☰) → **Permissions** to manage presets
- **Create** new presets or **clone** existing ones
- Each preset defines per-entity permission flags

### Permission entities

| Entity | Controls access to |
|--------|-------------------|
| **Board** | Board-level operations (view, edit, delete) |
| **Spec** | Spec CRUD, validation, export |
| **Card** | Task CRUD, status transitions, conclusions |
| **Ideation** | Ideation CRUD, evaluation, derivation |
| **Refinement** | Refinement CRUD, derivation |
| **Profile** | Agent/user profile management |
| **Guidelines** | Guideline CRUD, board linking |
| **KG** | Knowledge Graph queries and operations |

### Applying permissions

Permissions are applied when granting board access to an agent:
1. Go to **Agents** → **Board Access**
2. Grant access with a specific permission preset
3. The agent inherits all flags from the preset

Permission overrides can be applied per agent per board for fine-tuning.

### Permission diff view

When editing permissions, a **diff view** shows exactly what changed compared to the base preset — making it easy to audit access changes.
`,
    });
  }

  if (isEcosystem) {
    sections.push({
      id: 'collaboration',
      title: 'Collaboration',
      icon: <Users size={16} />,
      content: `
## Collaboration — Multi-user boards (Ecosystem)

The Ecosystem edition supports **multiple users** working on the same board simultaneously.

### Sharing a board

1. Open the **Share** option from the menu
2. Invite team members by email
3. Each member gets access to the full board: ideations, refinements, specs, and tasks

### Activity tracking

Every action on the board is logged with:
- **Who** did it (user name)
- **What** changed (card moved, spec updated, comment added, etc.)
- **When** it happened

View the activity log from the board's analytics or the activity panel.

### Agents

AI agents can be connected to a board to automate work:
- Agents appear in the **Agents** panel (menu → Agents)
- Each agent has its own identity, objective, and permissions
- Agents can create cards, move tasks, add comments, and more — all tracked in the activity log

### Mentions

Use the comment system to discuss decisions. Mentions are tracked and appear in your notification summary.

### Shared vs personal boards

- **My Boards** — Boards you created (visible only to you and invited members)
- **Shared with me** — Boards others invited you to

Both appear in the sidebar for quick switching.
`,
    });
  }

  return sections;
}

export function HelpPanel({ onClose }: HelpPanelProps) {
  const sections = getSections();
  const [activeSection, setActiveSection] = useState('quickstart');

  const current = sections.find(s => s.id === activeSection) || sections[0];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="relative w-[90vw] max-w-5xl bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-700 flex overflow-hidden"
        style={{ height: '85vh' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Sidebar navigation */}
        <nav className="w-56 shrink-0 border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 flex flex-col">
          <div className="px-4 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2.5">
            <img src={pulseIcon} alt="Okto Pulse" className="h-8 w-8" />
            <div>
              <h2 className="text-sm font-bold text-gray-800 dark:text-gray-200">
                Help Guide
              </h2>
              <p className="text-[10px] text-gray-400 mt-0.5">
                {isEcosystem ? 'Ecosystem Edition' : 'Community Edition'}
              </p>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto py-2">
            {sections.map(section => (
              <button
                key={section.id}
                onClick={() => setActiveSection(section.id)}
                className={`w-full text-left px-4 py-2 text-sm flex items-center gap-2.5 transition-colors ${
                  activeSection === section.id
                    ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 font-medium border-r-2 border-blue-500'
                    : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700/50'
                }`}
              >
                <span className="shrink-0 opacity-70">{section.icon}</span>
                {section.title}
                {activeSection === section.id && <ChevronRight size={12} className="ml-auto opacity-50" />}
              </button>
            ))}
          </div>
        </nav>

        {/* Content area */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="flex items-center justify-between px-6 py-3 border-b border-gray-200 dark:border-gray-700">
            <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200 flex items-center gap-2">
              {current.icon}
              {current.title}
            </h3>
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
            >
              <X size={18} />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto px-6 py-4">
            <MarkdownContent content={current.content} />
          </div>
        </div>
      </div>
    </div>
  );
}
