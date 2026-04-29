/**
 * exportMarkdown.ts — Markdown export utilities for all Okto Pulse entities.
 *
 * Each entity type has a dedicated generator that compiles structured data
 * into a well-formatted Markdown string. Tasks resolve references from
 * their parent spec (TRs, BRs, FRs, ACs, test scenarios, API contracts).
 */

import type {
  Ideation,
  Refinement,
  Spec,
  Card,
  TestScenario,
  BusinessRule,
  ApiContract,
  ScreenMockup,
  TechnicalRequirement,
  SpecKnowledgeSummary,
  ConclusionEntry,
  ValidationEntry,
} from '@/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Create a filename-safe slug from a title. */
export function slugify(title: string, max = 50): string {
  return title
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '') // strip accents
    .replace(/[^a-z0-9\s-]/g, '')   // remove special chars
    .replace(/\s+/g, '-')           // spaces → hyphens
    .replace(/-+/g, '-')            // collapse hyphens
    .replace(/^-|-$/g, '')          // trim edges
    .slice(0, max);
}

/** Format an ISO date string to YYYY-MM-DD HH:mm */
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Trigger browser download of a text file. */
export function downloadMarkdown(content: string, filename: string): void {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/** Add a section only if content is non-empty. */
function section(heading: string, body: string | null | undefined, level = 2): string {
  if (!body?.trim()) return '';
  const prefix = '#'.repeat(level);
  return `${prefix} ${heading}\n\n${body.trim()}\n\n`;
}

/** Render a string[] as a numbered list. */
function numberedList(items: (string | TechnicalRequirement)[]): string {
  return items
    .map((item, i) => {
      const text = typeof item === 'string' ? item : item.text;
      return `${i + 1}. ${text}`;
    })
    .join('\n');
}

/** Render a string[] as bullets. */
function bulletList(items: string[]): string {
  return items.map(item => `- ${item}`).join('\n');
}

/** Render metadata table at the top of the document. */
function metaTable(rows: [string, string][]): string {
  const filtered = rows.filter(([, v]) => !!v);
  if (filtered.length === 0) return '';
  return filtered.map(([k, v]) => `| **${k}** | ${v} |`).join('\n') + '\n\n';
}

// ---------------------------------------------------------------------------
// Mockups
// ---------------------------------------------------------------------------

function renderMockups(mockups: ScreenMockup[] | null | undefined): string {
  if (!mockups?.length) return '';
  const items = mockups.map((m, i) => {
    let entry = `### ${i + 1}. ${m.title || 'Untitled mockup'}\n\n`;
    if (m.description) entry += `${m.description}\n\n`;
    if (m.screen_type) entry += `**Type:** ${m.screen_type}\n\n`;
    entry += `*[HTML mockup — render in Okto Pulse UI]*\n`;
    return entry;
  }).join('\n');
  return `## Screen Mockups\n\n${items}\n`;
}

// ---------------------------------------------------------------------------
// Q&A
// ---------------------------------------------------------------------------

function renderQA(items: { question: string; answer?: string | null; asked_by?: string | null; answered_by?: string | null }[]): string {
  if (!items?.length) return '';
  const entries = items.map((q, i) => {
    let entry = `### Q${i + 1}: ${q.question}\n\n`;
    if (q.asked_by) entry += `*Asked by: ${q.asked_by}*\n\n`;
    if (q.answer) {
      entry += `**A:** ${q.answer}\n\n`;
      if (q.answered_by) entry += `*Answered by: ${q.answered_by}*\n\n`;
    } else {
      entry += `*Unanswered*\n\n`;
    }
    return entry;
  }).join('');
  return `## Q&A\n\n${entries}`;
}

// ---------------------------------------------------------------------------
// Test Scenarios
// ---------------------------------------------------------------------------

function renderTestScenarios(scenarios: TestScenario[] | null | undefined, criteria?: string[] | null): string {
  if (!scenarios?.length) return '';
  const items = scenarios.map((ts, i) => {
    let entry = `### ${i + 1}. ${ts.title}\n\n`;
    if (ts.scenario_type) entry += `**Type:** ${ts.scenario_type}\n\n`;
    entry += `- **Given:** ${ts.given}\n`;
    entry += `- **When:** ${ts.when}\n`;
    entry += `- **Then:** ${ts.then}\n\n`;
    if (ts.linked_criteria?.length && criteria?.length) {
      const names = ts.linked_criteria
        .map(c => criteria.indexOf(c) >= 0 ? `AC${criteria.indexOf(c) + 1}: ${c}` : c)
        .map(c => `  - ${c}`);
      entry += `**Linked criteria:**\n${names.join('\n')}\n\n`;
    }
    if (ts.notes) entry += `**Notes:** ${ts.notes}\n\n`;
    return entry;
  }).join('');
  return `## Test Scenarios\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Business Rules
// ---------------------------------------------------------------------------

function renderBusinessRules(rules: BusinessRule[] | null | undefined, frs?: string[] | null): string {
  if (!rules?.length) return '';
  const items = rules.map((br, i) => {
    let entry = `### ${i + 1}. ${br.title}\n\n`;
    entry += `**Rule:** ${br.rule}\n\n`;
    entry += `- **When:** ${br.when}\n`;
    entry += `- **Then:** ${br.then}\n\n`;
    if (br.linked_requirements?.length && frs?.length) {
      const names = br.linked_requirements
        .map(r => frs.indexOf(r) >= 0 ? `FR${frs.indexOf(r) + 1}: ${r}` : r)
        .map(r => `  - ${r}`);
      entry += `**Linked requirements:**\n${names.join('\n')}\n\n`;
    }
    if (br.notes) entry += `**Notes:** ${br.notes}\n\n`;
    return entry;
  }).join('');
  return `## Business Rules\n\n${items}`;
}

// ---------------------------------------------------------------------------
// API Contracts
// ---------------------------------------------------------------------------

function renderApiContracts(contracts: ApiContract[] | null | undefined): string {
  if (!contracts?.length) return '';
  const items = contracts.map((ac, i) => {
    let entry = `### ${i + 1}. ${ac.method} ${ac.path}\n\n`;
    entry += `\`${ac.method} ${ac.path}\`\n\n`;
    if (ac.description) entry += `${ac.description}\n\n`;
    if (ac.request_body) {
      entry += `**Request:**\n\`\`\`json\n${JSON.stringify(ac.request_body, null, 2)}\n\`\`\`\n\n`;
    }
    if (ac.response_success) {
      entry += `**Response (success):**\n\`\`\`json\n${JSON.stringify(ac.response_success, null, 2)}\n\`\`\`\n\n`;
    }
    if (ac.response_errors?.length) {
      entry += `**Response (errors):**\n\`\`\`json\n${JSON.stringify(ac.response_errors, null, 2)}\n\`\`\`\n\n`;
    }
    return entry;
  }).join('');
  return `## API Contracts\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Knowledge Bases
// ---------------------------------------------------------------------------

function renderKnowledgeBases(kbs: (SpecKnowledgeSummary | { title: string; content?: string; source_type?: string })[]): string {
  if (!kbs?.length) return '';
  const items = kbs.map((kb, i) => {
    let entry = `### ${i + 1}. ${kb.title}\n\n`;
    if ('source_type' in kb && kb.source_type) entry += `**Source:** ${kb.source_type}\n\n`;
    if ('content' in kb && kb.content) entry += `${kb.content}\n\n`;
    return entry;
  }).join('');
  return `## Knowledge Base\n\n${items}`;
}

// ---------------------------------------------------------------------------
// Entity Generators
// ---------------------------------------------------------------------------

/** Generate Markdown for an Ideation. */
export function exportIdeation(ideation: Ideation): string {
  let md = `# ${ideation.title}\n\n`;

  md += metaTable([
    ['Status', ideation.status],
    ['Version', `v${ideation.version}`],
    ['Complexity', ideation.complexity || ''],
    ['Assignee', ideation.assignee_id || ''],
    ['Created', fmtDate(ideation.created_at)],
    ['Updated', fmtDate(ideation.updated_at)],
    ['Labels', ideation.labels?.join(', ') || ''],
  ]);

  md += section('Description', ideation.description);
  md += section('Problem Statement', ideation.problem_statement);
  md += section('Proposed Approach', ideation.proposed_approach);

  // Scope assessment
  if (ideation.scope_assessment) {
    const sa = ideation.scope_assessment as Record<string, unknown>;
    let table = '| Dimension | Score | Justification |\n|-----------|-------|---------------|\n';
    for (const dim of ['domains', 'ambiguity', 'dependencies']) {
      const score = sa[dim] ?? '-';
      const just = sa[`${dim}_justification`] ?? '';
      table += `| **${dim.charAt(0).toUpperCase() + dim.slice(1)}** | ${score}/5 | ${just} |\n`;
    }
    md += `## Scope Assessment\n\n${table}\n`;
  }

  md += renderMockups(ideation.screen_mockups);
  md += renderQA(ideation.qa_items || []);

  return md;
}

/** Generate Markdown for a Refinement. */
export function exportRefinement(refinement: Refinement): string {
  let md = `# ${refinement.title}\n\n`;

  md += metaTable([
    ['Status', refinement.status],
    ['Version', `v${refinement.version}`],
    ['Assignee', refinement.assignee_id || ''],
    ['Created', fmtDate(refinement.created_at)],
    ['Updated', fmtDate(refinement.updated_at)],
    ['Labels', refinement.labels?.join(', ') || ''],
  ]);

  md += section('Description', refinement.description);

  if (refinement.in_scope?.length) {
    md += `## In Scope\n\n${bulletList(refinement.in_scope)}\n\n`;
  }
  if (refinement.out_of_scope?.length) {
    md += `## Out of Scope\n\n${bulletList(refinement.out_of_scope)}\n\n`;
  }

  md += section('Analysis', refinement.analysis);

  if (refinement.decisions?.length) {
    md += `## Decisions\n\n${numberedList(refinement.decisions)}\n\n`;
  }

  md += renderKnowledgeBases(refinement.knowledge_bases || []);
  md += renderMockups(refinement.screen_mockups);
  md += renderQA(refinement.qa_items || []);

  return md;
}

/** Generate Markdown for a Sprint with parent spec context. */
export function exportSprint(sprint: any, parentSpec: any): string {
  let md = `# Sprint: ${sprint.title}\n\n`;

  md += metaTable([
    ['Status', sprint.status],
    ['Version', `v${sprint.version}`],
    ['Spec Version', `v${sprint.spec_version}`],
    ['Spec', parentSpec?.title || sprint.spec_id || 'N/A'],
    ...(sprint.start_date ? [['Start Date', sprint.start_date] as [string, string]] : []),
    ...(sprint.end_date ? [['End Date', sprint.end_date] as [string, string]] : []),
  ]);

  if (sprint.objective) md += `## Objective\n\n${sprint.objective}\n\n`;
  if (sprint.expected_outcome) md += `## Expected Outcome\n\n${sprint.expected_outcome}\n\n`;
  if (sprint.description) md += `## Description\n\n${sprint.description}\n\n`;

  // Progress
  const cards = sprint.cards || [];
  const done = cards.filter((c: any) => c.status === 'done').length;
  const total = cards.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  md += `## Progress\n\n**${pct}%** complete (${done}/${total} cards done)\n\n`;

  // Cards
  if (cards.length > 0) {
    md += `## Cards\n\n| Title | Status | Type |\n|-------|--------|------|\n`;
    for (const c of cards) {
      md += `| ${c.title} | ${c.status} | ${c.card_type || 'normal'} |\n`;
    }
    md += '\n';
  }

  // Scoped Test Scenarios
  if (sprint.test_scenario_ids?.length && parentSpec?.test_scenarios?.length) {
    const scoped = parentSpec.test_scenarios.filter((ts: any) =>
      sprint.test_scenario_ids.includes(ts.id) ||
      ts.linked_task_ids?.some((id: string) => cards.some((c: any) => c.id === id))
    );
    if (scoped.length > 0) {
      md += `## Scoped Test Scenarios\n\n`;
      for (const ts of scoped) {
        md += `### ${ts.title}\n- **Given:** ${ts.given}\n- **When:** ${ts.when}\n- **Then:** ${ts.then}\n- **Status:** ${ts.status}\n\n`;
      }
    }
  }

  // Scoped Business Rules
  if (sprint.business_rule_ids?.length && parentSpec?.business_rules?.length) {
    const scoped = parentSpec.business_rules.filter((br: any) =>
      sprint.business_rule_ids.includes(br.id) ||
      br.linked_task_ids?.some((id: string) => cards.some((c: any) => c.id === id))
    );
    if (scoped.length > 0) {
      md += `## Scoped Business Rules\n\n`;
      for (const br of scoped) {
        md += `### ${br.title}\n- **When:** ${br.when}\n- **Then:** ${br.then}\n\n`;
      }
    }
  }

  // Evaluations
  if (sprint.evaluations?.length) {
    md += `## Evaluations\n\n`;
    for (const ev of sprint.evaluations) {
      md += `### ${ev.evaluator_name} — ${ev.recommendation} (${ev.overall_score}/100)\n`;
      if (ev.overall_justification) md += `${ev.overall_justification}\n`;
      md += '\n';
    }
  }

  // Labels
  if (sprint.labels?.length) {
    md += `## Labels\n\n${sprint.labels.join(', ')}\n\n`;
  }

  return md;
}

/** Generate Markdown for a Spec. */
export function exportSpec(spec: Spec): string {
  let md = `# ${spec.title}\n\n`;

  md += metaTable([
    ['Status', spec.status],
    ['Version', `v${spec.version}`],
    ['Assignee', spec.assignee_id || ''],
    ['Created', fmtDate(spec.created_at)],
    ['Updated', fmtDate(spec.updated_at)],
    ['Labels', spec.labels?.join(', ') || ''],
  ]);

  md += section('Description', spec.description);
  md += section('Context', spec.context);

  if (spec.functional_requirements?.length) {
    md += `## Functional Requirements\n\n${numberedList(spec.functional_requirements)}\n\n`;
  }
  if (spec.technical_requirements?.length) {
    md += `## Technical Requirements\n\n${numberedList(spec.technical_requirements)}\n\n`;
  }
  if (spec.acceptance_criteria?.length) {
    md += `## Acceptance Criteria\n\n${numberedList(spec.acceptance_criteria)}\n\n`;
  }

  md += renderTestScenarios(spec.test_scenarios, spec.acceptance_criteria);
  md += renderBusinessRules(spec.business_rules, spec.functional_requirements);
  md += renderApiContracts(spec.api_contracts);
  md += renderKnowledgeBases(spec.knowledge_bases || []);
  md += renderMockups(spec.screen_mockups);
  md += renderQA(spec.qa_items || []);

  return md;
}

/** Generate Markdown for a Card/Task, resolving spec references. */
export function exportCard(card: Card, spec?: Spec | null): string {
  const isBug = card.card_type === 'bug';
  let md = `# ${isBug ? '[BUG] ' : ''}${card.title}\n\n`;

  md += metaTable([
    ['Status', card.status],
    ['Priority', card.priority !== 'none' ? card.priority : ''],
    ['Type', isBug ? 'Bug' : 'Task'],
    ['Assignee', card.assignee_id || ''],
    ['Created', fmtDate(card.created_at)],
    ['Updated', fmtDate(card.updated_at)],
    ['Due date', card.due_date ? fmtDate(card.due_date) : ''],
    ['Labels', card.labels?.join(', ') || ''],
  ]);

  md += section('Description', card.description);
  md += section('Details', card.details);

  // Bug-specific fields
  if (isBug) {
    let bugSection = `## Bug Details\n\n`;
    bugSection += `**Severity:** ${card.severity || 'unknown'}\n\n`;
    if (card.origin_task_id) bugSection += `**Origin task:** ${card.origin_task_id}\n\n`;
    if (card.expected_behavior) bugSection += `### Expected Behavior\n\n${card.expected_behavior}\n\n`;
    if (card.observed_behavior) bugSection += `### Observed Behavior\n\n${card.observed_behavior}\n\n`;
    if (card.steps_to_reproduce) bugSection += `### Steps to Reproduce\n\n${card.steps_to_reproduce}\n\n`;
    if (card.action_plan) bugSection += `### Action Plan\n\n${card.action_plan}\n\n`;
    if (card.linked_test_task_ids?.length) {
      bugSection += `**Linked test tasks:** ${card.linked_test_task_ids.join(', ')}\n\n`;
    }
    md += bugSection;
  }

  // Conclusions
  if (card.conclusions?.length) {
    const entries = card.conclusions.map((c: ConclusionEntry, i: number) => {
      let e = `### Conclusion ${i + 1}\n\n${c.text}\n\n`;
      if (c.completeness != null) e += `**Completeness:** ${c.completeness}%${c.completeness_justification ? ` — ${c.completeness_justification}` : ''}\n\n`;
      if (c.drift != null) e += `**Drift:** ${c.drift}%${c.drift_justification ? ` — ${c.drift_justification}` : ''}\n\n`;
      return e;
    }).join('');
    md += `## Conclusions\n\n${entries}`;
  }

  // Validations
  if (card.validations?.length) {
    const entries = card.validations.map((v: ValidationEntry, i: number) => {
      let e = `### Validation ${i + 1} — ${v.verdict === 'pass' ? 'PASSED' : 'FAILED'}\n\n`;
      e += `| Metric | Score |\n|--------|-------|\n`;
      e += `| Confidence | ${v.confidence} |\n`;
      e += `| Completeness | ${v.completeness} |\n`;
      e += `| Drift | ${v.drift} |\n\n`;
      if (v.summary) e += `**Summary:** ${v.summary}\n\n`;
      e += `*Reviewer: ${v.evaluator_id} | ${fmtDate(v.created_at)}*\n\n`;
      return e;
    }).join('');
    md += `## Validations\n\n${entries}`;
  }

  // Dependencies
  // Note: Card type doesn't include dependency data directly; we show what we have
  // from comments/QA context

  // Resolved spec context
  if (spec) {
    md += `---\n\n## Spec Context: ${spec.title}\n\n`;

    // Linked test scenarios (resolve from IDs)
    if (card.test_scenario_ids?.length && spec.test_scenarios?.length) {
      const linked = spec.test_scenarios.filter(ts => card.test_scenario_ids!.includes(ts.id));
      if (linked.length) {
        md += renderTestScenarios(linked, spec.acceptance_criteria);
      }
    }

    if (spec.functional_requirements?.length) {
      md += `## Functional Requirements\n\n${numberedList(spec.functional_requirements)}\n\n`;
    }
    if (spec.technical_requirements?.length) {
      md += `## Technical Requirements\n\n${numberedList(spec.technical_requirements)}\n\n`;
    }
    if (spec.acceptance_criteria?.length) {
      md += `## Acceptance Criteria\n\n${numberedList(spec.acceptance_criteria)}\n\n`;
    }

    md += renderBusinessRules(spec.business_rules, spec.functional_requirements);
    md += renderApiContracts(spec.api_contracts);
    md += renderKnowledgeBases(spec.knowledge_bases || []);
  }

  // Card-own knowledge bases
  if (card.knowledge_bases?.length) {
    md += `## Card Knowledge Bases\n\n`;
    for (const kb of card.knowledge_bases) {
      md += `### ${kb.title}${kb.source === 'spec' ? ' (from spec)' : ''}\n\n`;
      md += `${kb.content}\n\n`;
    }
  }

  md += renderMockups(card.screen_mockups);
  md += renderQA(card.qa_items || []);

  // Comments
  if (card.comments?.length) {
    const entries = card.comments.map(c =>
      `**${c.author_id || 'Unknown'}** (${fmtDate(c.created_at)}):\n\n${c.content}\n`
    ).join('\n---\n\n');
    md += `## Comments\n\n${entries}\n`;
  }

  return md;
}
