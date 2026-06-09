import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import mermaid from 'mermaid';
import {
  collectExportWarnings,
  createExportWarningCollector,
  downloadMarkdown,
  exportCard,
  exportIdeation,
  exportRefinement,
  exportSpec,
  exportSprint,
  exportStory,
  markdownFilenameForCard,
  markdownFilenameForSpec,
  renderArchitectureMermaid,
  renderExportWarnings,
  resolveLinkedCriteriaForExport,
  escapeMermaidLabel,
  sanitizeMermaidId,
  sanitizeMarkdownFilename,
} from '../exportMarkdown';

function specWithArchitecture(architectureDesigns: unknown[]) {
  return {
    title: 'Architecture Warning Spec',
    status: 'review',
    version: 1,
    labels: [],
    architecture_designs: architectureDesigns,
  } as any;
}

function extractMermaidBlocks(markdown: string): string[] {
  return Array.from(markdown.matchAll(/```mermaid\n([\s\S]*?)```/g)).map((match) => match[1]);
}

describe('exportMarkdown export warning collector', () => {
  it('normalizes, deduplicates and sorts warning buckets deterministically', () => {
    const warnings = collectExportWarnings({
      asset_warnings: [
        {
          origin: 'mockup:missing-render',
          message: 'Mockup render is unavailable.',
        },
      ],
      broken_links: [
        {
          severity: 'high',
          origin: 'test_scenario:ts-2',
          source_ref: 'acceptance_criterion:AC-999',
          message: 'Linked acceptance criterion does not exist.',
        },
        {
          severity: 'high',
          origin: 'test_scenario:ts-2',
          source_ref: 'acceptance_criterion:AC-999',
          message: 'Linked acceptance criterion does not exist.',
        },
      ],
      architecture_warnings: [
        {
          code: 'entity_without_diagram',
          severity: 'warning',
          message: 'Entity is not represented in any diagram.',
          path: 'entities[2]',
          suggested_fix: 'Add a diagram node linked to this entity.',
          entity_id: 'entity-billing',
        },
      ],
      unresolved_references: [
        {
          severity: 'low',
          origin: 'business_rule:br-1',
          source_ref: 'FR-999',
          message: 'Linked requirement is unavailable.',
        },
      ],
    });

    expect(warnings).toEqual([
      {
        kind: 'broken_link',
        severity: 'high',
        origin: 'test_scenario:ts-2',
        source_ref: 'acceptance_criterion:AC-999',
        message: 'Linked acceptance criterion does not exist.',
      },
      {
        kind: 'architecture_warning',
        severity: 'medium',
        origin: 'entity_without_diagram',
        source_ref: 'entity-billing',
        message: 'Entity is not represented in any diagram.',
        impact: 'Add a diagram node linked to this entity.',
      },
      {
        kind: 'asset_unavailable',
        severity: 'medium',
        origin: 'mockup:missing-render',
        message: 'Mockup render is unavailable.',
      },
      {
        kind: 'unresolved_reference',
        severity: 'low',
        origin: 'business_rule:br-1',
        source_ref: 'FR-999',
        message: 'Linked requirement is unavailable.',
      },
    ]);
  });

  it('supports an incremental collector without losing deterministic output', () => {
    const collector = createExportWarningCollector();
    collector.add({
      kind: 'asset_unavailable',
      origin: 'mockup:one',
      message: 'Render not available.',
    });
    collector.add({
      kind: 'asset_unavailable',
      origin: 'mockup:one',
      message: 'Render not available.',
    });
    collector.collect({
      unresolved_references: [
        {
          severity: 'critical',
          origin: 'scenario:broken',
          source_ref: 'AC-999',
          message: 'Criterion cannot be resolved.',
        },
      ],
    });

    const first = collector.toArray();
    const second = collector.toArray();

    expect(first).toEqual(second);
    expect(first).toHaveLength(2);
    expect(renderExportWarnings(first)).toContain('## Export Warnings');
    expect(renderExportWarnings(first)).toContain('**critical** `unresolved_reference`');
    expect(renderExportWarnings(first)).toContain('**medium** `asset_unavailable`');
  });
});

describe('exportMarkdown linked criteria resolution', () => {
  it('resolves linked criteria by index, string index, AC label, exact text and stable id', () => {
    const resolved = resolveLinkedCriteriaForExport(
      [1, '2', 'AC-3', 'Fourth criterion', 'ac-5'],
      [
        'First criterion',
        'Second criterion',
        'Third criterion',
        'Fourth criterion',
        { id: 'ac-5', text: 'Fifth criterion' },
      ],
    );

    expect(resolved.map((item) => item.status)).toEqual([
      'resolved',
      'resolved',
      'resolved',
      'resolved',
      'resolved',
    ]);
    expect(resolved.map((item) => item.text)).toEqual([
      'First criterion',
      'Second criterion',
      'Third criterion',
      'Fourth criterion',
      'Fifth criterion',
    ]);
  });

  it('renders unresolved linked criteria as visible Export Warnings', () => {
    const md = exportSpec({
      title: 'Criteria warning spec',
      status: 'review',
      version: 1,
      labels: [],
      acceptance_criteria: ['First criterion', 'Second criterion'],
      test_scenarios: [
        {
          id: 'ts-broken',
          title: 'Broken criteria',
          scenario_type: 'unit',
          given: 'A scenario references mixed criteria.',
          when: 'The exporter renders linked criteria.',
          then: 'Resolved criteria and warnings are both visible.',
          linked_criteria: ['2', 'AC-999'],
          notes: null,
          status: 'ready',
          linked_task_ids: [],
        },
      ],
    } as any);

    expect(md).toContain('## Export Warnings');
    expect(md).toContain('AC2: Second criterion');
    expect(md).toContain('Unresolved: AC-999');
    expect(md).toContain('`unresolved_reference`');
    expect(md).toContain('Linked acceptance criterion could not be resolved: AC-999');
  });
});

describe('exportMarkdown markdown download filenames', () => {
  it('builds deterministic sanitized filenames for specs tasks tests and bugs', () => {
    expect(markdownFilenameForSpec({ title: 'Árvore / Billing?', version: 12 } as any))
      .toBe('spec_arvore-billing_v12.md');
    expect(markdownFilenameForCard({ title: 'Run E2E: / KG?', card_type: 'normal' } as any))
      .toBe('task_run-e2e-kg.md');
    expect(markdownFilenameForCard({ title: 'Run E2E: / KG?', card_type: 'test' } as any))
      .toBe('test_run-e2e-kg.md');
    expect(markdownFilenameForCard({ title: 'Crash on "save"', card_type: 'bug' } as any))
      .toBe('bug_crash-on-save.md');
    expect(markdownFilenameForCard({ title: '!!!', card_type: 'normal' } as any))
      .toBe('task_untitled.md');
  });

  it('sanitizes raw filenames before assigning the browser download name', () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const createObjectURL = vi.fn().mockReturnValue('blob:markdown-export');
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const appendChild = vi.spyOn(document.body, 'appendChild');
    (URL as any).createObjectURL = createObjectURL;
    (URL as any).revokeObjectURL = revokeObjectURL;

    try {
      downloadMarkdown('# Export', '../Spec E2E?.md');

      const anchor = appendChild.mock.calls[0]?.[0] as HTMLAnchorElement;
      expect(anchor.download).toBe('spec-e2e.md');
      expect(click).toHaveBeenCalledTimes(1);
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:markdown-export');
      expect(sanitizeMarkdownFilename('../../')).toBe('export.md');
    } finally {
      click.mockRestore();
      appendChild.mockRestore();
      (URL as any).createObjectURL = originalCreateObjectURL;
      (URL as any).revokeObjectURL = originalRevokeObjectURL;
    }
  });
});

describe('exportMarkdown output determinism', () => {
  it('produces byte-identical spec and card Markdown for repeated equivalent inputs', () => {
    const spec = {
      title: 'Deterministic Export Spec',
      status: 'review',
      version: 3,
      labels: ['markdown', 'export'],
      description: 'Spec export should be stable.',
      context: 'Used for diff-based handoff.',
      functional_requirements: ['FR1: Stable output'],
      technical_requirements: [{ id: 'tr-1', text: 'Sort and render consistently' }],
      acceptance_criteria: ['AC1: Same input produces same bytes'],
      test_scenarios: [
        {
          id: 'ts-stable',
          title: 'Stable scenario',
          scenario_type: 'unit',
          given: 'A loaded spec',
          when: 'Export is called twice',
          then: 'Both outputs are byte-identical',
          linked_criteria: ['1'],
        },
      ],
      business_rules: [],
      api_contracts: [],
      integration_requirements: [],
      observability_requirements: [],
      decisions: [],
      knowledge_bases: [],
      screen_mockups: [],
      architecture_designs: [],
      qa_items: [],
    } as any;
    const card = {
      id: 'task-stable',
      title: 'Stable task export',
      description: 'Card export should be stable.',
      details: 'No random timestamps or generated ids.',
      status: 'validation',
      priority: 'high',
      card_type: 'normal',
      test_scenario_ids: ['ts-stable'],
      depends_on: [{ id: 'task-parent', title: 'Parent task', status: 'done' }],
      comments: [{ content: 'Reviewed once.', created_at: '2026-05-28T10:00:00Z' }],
    } as any;

    const specFirst = exportSpec(spec);
    const specSecond = exportSpec({ ...spec, labels: [...spec.labels] });
    const cardFirst = exportCard(card, spec);
    const cardSecond = exportCard({ ...card, test_scenario_ids: [...card.test_scenario_ids] }, { ...spec });

    expect(specSecond).toBe(specFirst);
    expect(cardSecond).toBe(cardFirst);
  });
});

describe('exportMarkdown architecture warnings', () => {
  it('renders a deterministic Mermaid flowchart from linked diagram connections', () => {
    const design = {
      id: 'arch-runtime',
      title: 'Runtime',
      version: 3,
      global_description: 'Runtime flow.',
      entities: [
        { id: 'worker/service', name: 'Worker "Service"' },
        { id: 'api gateway', name: 'API\nGateway' },
      ],
      interfaces: [
        { id: 'http-calls', name: 'HTTP "calls"', participants: ['api gateway', 'worker/service'] },
      ],
      diagrams: [
        {
          id: 'diag-runtime',
          title: 'Runtime diagram',
          order_index: 0,
          content_hash: 'hash-runtime',
          adapter_payload: {
            elements: [
              { id: 'node-worker', linkedEntityId: 'worker/service' },
              { id: 'edge-api-worker', sourceElementId: 'node-api', targetElementId: 'node-worker', linkedInterfaceId: 'http-calls' },
              { id: 'node-api', linkedEntityId: 'api gateway' },
            ],
          },
        },
      ],
    } as any;

    const first = renderArchitectureMermaid(design);
    const second = renderArchitectureMermaid({
      ...design,
      entities: [...design.entities].reverse(),
      interfaces: [...design.interfaces],
      diagrams: [{ ...design.diagrams[0], adapter_payload: { elements: [...design.diagrams[0].adapter_payload.elements].reverse() } }],
    });

    expect(first).toEqual(second);
    expect(first.metadata).toEqual({
      renderedFrom: 'diagram_connections',
      diagramIds: ['diag-runtime'],
      designId: 'arch-runtime',
      designVersion: 3,
      sourceHash: 'hash-runtime',
    });
    expect(first.warnings).toEqual([]);
    expect(first.mermaid).toContain('flowchart TD');
    expect(first.mermaid).toContain('api_gateway["API Gateway"]');
    expect(first.mermaid).toContain('worker_service["Worker #quot;Service#quot;"]');
    expect(first.mermaid).toContain('api_gateway -- "HTTP #quot;calls#quot;" --> worker_service');
    expect(first.mermaid).not.toContain('adapter_payload');
    expect(first.mermaid).not.toContain('elements');
  });

  it('falls back to deterministic interface participants when diagram payload is unavailable', () => {
    const rendered = renderArchitectureMermaid({
      id: 'arch-fallback',
      title: 'Fallback',
      version: 1,
      entities: [
        { id: 'ui', name: 'Web UI' },
        { id: 'api', name: 'API' },
      ],
      interfaces: [
        { id: 'fetch', name: 'fetch boards', participants: ['ui', 'api'] },
      ],
      diagrams: [{ id: 'diag-summary', title: 'Summary only', order_index: 0, adapter_payload_ref: 'payload-ref' }],
    } as any);

    expect(rendered.metadata.renderedFrom).toBe('entity_interface_fallback');
    expect(rendered.warnings).toEqual([]);
    expect(rendered.mermaid).toContain('ui["Web UI"]');
    expect(rendered.mermaid).toContain('api["API"]');
    expect(rendered.mermaid).toContain('ui -- "fetch boards" --> api');
  });

  it('emits node-only Mermaid with a lossy warning when relationships are not reconstructable', () => {
    const rendered = renderArchitectureMermaid({
      id: 'arch-node-only',
      title: 'Node-only',
      version: 1,
      entities: [
        { id: 'one', name: 'One' },
        { id: 'two', name: 'Two' },
      ],
      interfaces: [{ id: 'unknown', name: 'Unknown participant', participants: ['one'] }],
      diagrams: [],
    } as any);

    expect(rendered.metadata.renderedFrom).toBe('entity_interface_fallback');
    expect(rendered.mermaid).toContain('one["One"]');
    expect(rendered.mermaid).toContain('two["Two"]');
    expect(rendered.mermaid).not.toContain('--');
    expect(rendered.warnings).toMatchObject([
      {
        kind: 'architecture_warning',
        code: 'relationships_not_reconstructable',
      },
    ]);
  });

  it('omits Mermaid and warns when architecture has no renderable entities', () => {
    const rendered = renderArchitectureMermaid({
      id: 'arch-empty',
      title: 'Empty',
      version: 1,
      entities: [],
      interfaces: [],
      diagrams: [],
    } as any);

    expect(rendered.mermaid).toBe('');
    expect(rendered.metadata.renderedFrom).toBe('empty');
    expect(rendered.warnings).toMatchObject([
      {
        kind: 'architecture_warning',
        code: 'architecture_not_renderable',
      },
    ]);
  });

  it('sanitizes Mermaid ids and escapes labels without leaking syntax delimiters', () => {
    expect(sanitizeMermaidId(' 123 API | Gateway[prod] ')).toBe('n_123_API_Gateway_prod');
    expect(sanitizeMermaidId('áéí worker/service')).toBe('aei_worker_service');
    expect(sanitizeMermaidId('', '9 fallback')).toBe('n_9_fallback');
    expect(sanitizeMermaidId('valid_Id_2')).toBe('valid_Id_2');
    expect(sanitizeMermaidId('end')).toBe('n_end');
    expect(sanitizeMermaidId('class')).toBe('n_class');

    const label = escapeMermaidLabel('API "Gateway"\n| `danger` \\ path');
    expect(label).toBe('API #quot;Gateway#quot; | \'danger\' \\ path');
    expect(label).not.toContain('\n');
    expect(label).not.toContain('`');
  });

  it('emits Mermaid labels accepted by the real Mermaid parser for adversarial input', async () => {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
    const labels = [
      'API "Gateway" | x',
      'evil"] --> y[z',
      '""""',
      'Cache [Redis]',
      '"]%%{init}%%',
      '"; click n1 callback "x"',
      'plain label',
    ];

    for (const raw of labels) {
      const nodeId = sanitizeMermaidId(raw, 'label_node');
      const diagram = `flowchart TD\n  ${nodeId}["${escapeMermaidLabel(raw)}"]\n  ${nodeId} -- "${escapeMermaidLabel(raw)}" --> target["Target"]\n`;
      await expect(mermaid.parse(diagram), raw).resolves.toBeDefined();
    }
  });

  it('prefixes Mermaid reserved node ids before rendering diagrams', async () => {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
    const reservedNames = ['end', 'class', 'classDef', 'style', 'linkStyle', 'graph', 'subgraph', 'flowchart', 'click', 'call', 'href'];

    for (const name of reservedNames) {
      const rendered = renderArchitectureMermaid({
        id: `arch-${name}`,
        title: name,
        version: 1,
        entities: [{ id: name, name }],
        interfaces: [],
        diagrams: [],
      } as any);

      expect(rendered.mermaid).toContain(`n_${name}["${name}"]`);
      await expect(mermaid.parse(rendered.mermaid), name).resolves.toBeDefined();
    }
  });

  it('renders structured architecture warnings with deterministic fields and ordering', () => {
    const md = exportSpec(specWithArchitecture([
      {
        title: 'Runtime flow',
        version: 2,
        entities: [],
        interfaces: [],
        diagrams: [{ id: 'diag-runtime', title: 'Runtime' }],
        structured_warnings: [
          {
            code: 'isolated_entity_node',
            severity: 'warning',
            message: 'Node is isolated.',
            path: 'diagrams[0].adapter_payload.elements[3]',
            suggested_fix: 'Connect the node to another architecture element.',
            diagram_id: 'diag-runtime',
            element_id: 'node-api',
          },
          {
            code: 'dangling_connector',
            severity: 'warning',
            message: 'Connector has an unresolved endpoint.',
            path: 'diagrams[0].adapter_payload.elements[1]',
            suggested_fix: 'Attach the connector to valid source and target elements or remove it.',
            diagram_id: 'diag-runtime',
            element_id: 'edge-missing-target',
          },
          {
            code: 'entity_without_diagram',
            severity: 'warning',
            message: 'Entity is not represented in any diagram.',
            path: 'entities[2]',
            suggested_fix: 'Add a diagram node linked to this entity or remove the entity.',
            entity_id: 'entity-billing',
          },
        ],
      },
    ]));

    expect(md).toContain('#### Connectivity and Coverage Warnings');
    expect(md).toContain('- **Code:** `dangling_connector`');
    expect(md).toContain('  **Location:** `diag-runtime / edge-missing-target`');
    expect(md).toContain('  **Suggested fix:** Attach the connector to valid source and target elements or remove it.');
    expect(md).toContain('- **Code:** `entity_without_diagram`');
    expect(md).toContain('  **Location:** `entity-billing`');
    expect(md).toContain('- **Code:** `isolated_entity_node`');
    expect(md).toContain('  **Location:** `diag-runtime / node-api`');

    expect(md.indexOf('`dangling_connector`')).toBeLessThan(md.indexOf('`entity_without_diagram`'));
    expect(md.indexOf('`entity_without_diagram`')).toBeLessThan(md.indexOf('`isolated_entity_node`'));
  });

  it('integrates Mermaid before architecture tables and keeps raw payload out of Markdown', () => {
    const md = exportSpec(specWithArchitecture([
      {
        id: 'arch-connected',
        title: 'Connected runtime',
        version: 1,
        entities: [
          { id: 'ui', name: 'Web UI' },
          { id: 'api', name: 'API' },
        ],
        interfaces: [
          { id: 'uses-api', name: 'uses API', participants: ['ui', 'api'] },
        ],
        diagrams: [
          {
            id: 'diag-connected',
            title: 'Connected diagram',
            adapter_payload: {
              elements: [
                { id: 'node-ui', linkedEntityId: 'ui' },
                { id: 'node-api', linkedEntityId: 'api' },
                { id: 'edge-ui-api', sourceElementId: 'node-ui', targetElementId: 'node-api', linkedInterfaceId: 'uses-api' },
              ],
              appState: { viewBackgroundColor: '#fff' },
              files: { asset: { id: 'file-1' } },
            },
          },
        ],
      },
    ]));

    const architectureIndex = md.indexOf('## Architecture Designs');
    const mermaidIndex = md.indexOf('```mermaid');
    const entitiesIndex = md.indexOf('#### Entities');
    expect(architectureIndex).toBeGreaterThan(-1);
    expect(mermaidIndex).toBeGreaterThan(architectureIndex);
    expect(mermaidIndex).toBeLessThan(entitiesIndex);
    expect(md).toContain('flowchart TD');
    expect(md).toContain('ui["Web UI"]');
    expect(md).toContain('api["API"]');
    expect(md).toContain('ui -- "uses API" --> api');
    expect(md).not.toContain('adapter_payload');
    expect(md).not.toContain('elements');
    expect(md).not.toContain('appState');
    expect(md).not.toContain('files');
  });

  it('keeps adversarial raw architecture payload fields out of spec and card Markdown', async () => {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
    const adversarialDesign = {
      id: 'arch-boundary',
      title: 'Boundary runtime',
      version: 1,
      entities: [
        { id: 'portal', name: 'Portal "] --> leaked["' },
        { id: 'service', name: 'Service `secret`' },
      ],
      interfaces: [
        { id: 'call-service', name: 'calls service "] --> escaped["', participants: ['portal', 'service'] },
      ],
      diagrams: [
        {
          id: 'diag-boundary',
          title: 'Boundary diagram',
          adapter_payload_ref: 'payload-safe-ref',
          content_hash: 'hash-safe',
          adapter_payload: {
            elements: [
              {
                id: 'node-portal',
                linkedEntityId: 'portal',
                secret: 'RAW_PAYLOAD_SECRET',
                appState: { viewBackgroundColor: 'RAW_APP_STATE_SECRET' },
              },
              {
                id: 'node-service',
                linkedEntityId: 'service',
                files: { attached: { content: 'RAW_FILE_SECRET' } },
              },
              {
                id: 'edge-portal-service',
                sourceElementId: 'node-portal',
                targetElementId: 'node-service',
                linkedInterfaceId: 'call-service',
                customData: { password: 'RAW_EDGE_SECRET' },
              },
            ],
            appState: { currentItemStrokeColor: 'RAW_TOP_LEVEL_APP_STATE' },
            files: { one: { dataURL: 'RAW_TOP_LEVEL_FILE' } },
          },
        },
      ],
      structured_warnings: [
        {
          code: 'diagram_payload_path',
          path: 'diagrams[0].adapter_payload.elements[2].appState.files',
          message: 'adapter_payload elements appState files must not be serialized',
          suggested_fix: 'Review adapter_payload elements appState files without exporting raw data',
        },
      ],
    } as any;
    const spec = specWithArchitecture([adversarialDesign]);
    const card = {
      id: 'task-boundary',
      title: 'Boundary export card',
      status: 'validation',
      priority: 'high',
      labels: [],
      card_type: 'normal',
      architecture_designs: [adversarialDesign],
      knowledge_bases: [],
      conclusions: [],
      validations: [],
      comments: [],
      attachments: [],
    } as any;

    for (const markdown of [exportSpec(spec), exportCard(card, spec)]) {
      expect(markdown).toContain('payload_ref=payload-safe-ref');
      expect(markdown).toContain('hash=hash-safe');
      expect(markdown).toContain('portal -- "calls service #quot;#93; --#gt; escaped#91;#quot;" --> service');
      for (const forbidden of [
        'adapter_payload',
        'elements',
        'appState',
        'files',
        'RAW_PAYLOAD_SECRET',
        'RAW_APP_STATE_SECRET',
        'RAW_FILE_SECRET',
        'RAW_EDGE_SECRET',
        'RAW_TOP_LEVEL_APP_STATE',
        'RAW_TOP_LEVEL_FILE',
      ]) {
        expect(markdown).not.toContain(forbidden);
      }
      for (const block of extractMermaidBlocks(markdown)) {
        await expect(mermaid.parse(block), block).resolves.toBeDefined();
      }
    }
  });

  it('redacts Architecture Critic payload key paths without requiring adapter_payload in the same warning', () => {
    const md = exportSpec(specWithArchitecture([
      {
        id: 'arch-warning-paths',
        title: 'Warning paths',
        version: 1,
        entities: [],
        interfaces: [],
        diagrams: [],
        structured_warnings: [
          {
            code: 'nondeterministic_appState',
            path: 'appState.viewBackgroundColor',
            message: 'appState.viewBackgroundColor must be deterministic',
            suggested_fix: 'pin files.theme.dataURL after reviewing source and target elements',
          },
          {
            code: 'external_file_reference',
            path: 'files.one.dataURL',
            message: 'files.one.dataURL must not be exported',
            suggested_fix: 'connect the source and target elements and attach files later',
          },
        ],
      },
    ]));

    expect(md).toContain('diagram_state.viewBackgroundColor must be deterministic');
    expect(md).toContain('pin asset_refs.theme.dataURL after reviewing source and target elements');
    expect(md).toContain('connect the source and target elements and attach files later');
    expect(md).not.toContain('appState');
    expect(md).not.toContain('files.');
  });

  it('keeps architecture Mermaid export on a read-only source boundary', () => {
    const source = readFileSync('src/lib/exportMarkdown.ts', 'utf8');

    expect(source).not.toMatch(/\bJSON\.stringify\s*\(\s*(design|diagram|architecture|entity|itf|interface)\b/);
    expect(source).not.toMatch(/\b(create|update|delete|save)Architecture(Design)?\b/);
    expect(source).not.toMatch(/\buseMutation\b/);
    expect(source).not.toMatch(/from\s+['"].*services\/api['"]/);
    expect(source).not.toMatch(/\b(fetch|axios)\s*\(/);
  });

  it('keeps exportSpec architecture Mermaid output deterministic across multiple designs', () => {
    const designs = [
      {
        id: 'arch-beta',
        title: 'Beta runtime',
        version: 2,
        entities: [
          { id: 'worker', name: 'Worker' },
          { id: 'queue', name: 'Queue' },
        ],
        interfaces: [
          { id: 'consume', name: 'consume jobs', participants: ['queue', 'worker'] },
        ],
        diagrams: [],
      },
      {
        id: 'arch-alpha',
        title: 'Alpha runtime',
        version: 1,
        entities: [
          { id: 'ui', name: 'UI' },
          { id: 'api', name: 'API' },
        ],
        interfaces: [
          { id: 'call', name: 'call API', participants: ['ui', 'api'] },
        ],
        diagrams: [],
      },
    ];

    const first = exportSpec(specWithArchitecture(designs));
    const second = exportSpec(specWithArchitecture(designs.map((design) => ({ ...design }))));

    expect(second).toBe(first);
    expect(first.match(/```mermaid/g)).toHaveLength(2);
    expect(first.indexOf('### 1. Beta runtime')).toBeLessThan(first.indexOf('### 2. Alpha runtime'));
    expect(first.indexOf('### 1. Beta runtime')).toBeLessThan(first.indexOf('queue -- "consume jobs" --> worker'));
    expect(first.indexOf('queue -- "consume jobs" --> worker')).toBeLessThan(first.indexOf('### 2. Alpha runtime'));
    expect(first.indexOf('### 2. Alpha runtime')).toBeLessThan(first.indexOf('ui -- "call API" --> api'));
  });

  it('surfaces Mermaid conversion warnings locally and through global export warnings', () => {
    const md = exportSpec(specWithArchitecture([
      {
        id: 'arch-node-only',
        title: 'Node-only architecture',
        version: 1,
        entities: [
          { id: 'api', name: 'API' },
          { id: 'worker', name: 'Worker' },
        ],
        interfaces: [],
        diagrams: [],
      },
    ]));

    expect(md).toContain('## Export Warnings');
    expect(md).toContain('#### Mermaid Conversion Warnings');
    expect(md).toContain('`relationships_not_reconstructable`');
    expect(md).toContain('Architecture Design entities were rendered, but no deterministic relationship edges could be reconstructed.');
    expect(md).toContain('```mermaid');
    expect(md).toContain('api["API"]');
    expect(md).toContain('worker["Worker"]');
  });

  it('omits empty Mermaid code fences for non-renderable architecture and warns explicitly', () => {
    const md = exportSpec(specWithArchitecture([
      {
        id: 'arch-empty',
        title: 'Empty architecture',
        version: 1,
        entities: [],
        interfaces: [],
        diagrams: [],
      },
    ]));

    expect(md).toContain('## Export Warnings');
    expect(md).toContain('#### Mermaid Conversion Warnings');
    expect(md).toContain('`architecture_not_renderable`');
    expect(md).not.toContain('```mermaid');
    expect(md).not.toContain('flowchart TD');
  });

  it('does not render the connectivity warnings subsection when no structured warnings are present', () => {
    const md = exportSpec(specWithArchitecture([
      {
        title: 'Complete runtime flow',
        version: 1,
        entities: [],
        interfaces: [],
        diagrams: [{ id: 'diag-runtime', title: 'Runtime' }],
      },
    ]));

    expect(md).toContain('## Architecture Designs');
    expect(md).not.toContain('Connectivity and Coverage Warnings');
  });

  it('can consume nested backend validation output without running topology logic in export', () => {
    const md = exportSpec(specWithArchitecture([
      {
        title: 'Validated architecture',
        validation_result: {
          structured_warnings: [
            {
              code: 'disconnected_subgraph',
              severity: 'warning',
              message: 'Diagram has disconnected components.',
              path: 'diagrams[0]',
              suggested_fix: 'Connect the subgraphs or document the separation.',
              diagram_id: 'diag-context',
              node_ref: 'subgraph:2',
            },
          ],
        },
      },
    ]));

    expect(md).toContain('#### Connectivity and Coverage Warnings');
    expect(md).toContain('- **Code:** `disconnected_subgraph`');
    expect(md).toContain('  **Location:** `diag-context / subgraph:2`');
  });
});

describe('exportMarkdown visual asset fallback warnings', () => {
  it('keeps mockup and diagram summaries while warning when visual renders are unavailable', () => {
    const md = exportSpec({
      title: 'Visual fallback spec',
      status: 'review',
      version: 1,
      labels: [],
      screen_mockups: [
        {
          id: 'mockup-1',
          title: 'Export preview drawer',
          description: 'Shows Markdown preview.',
          screen_type: 'drawer',
          html_content: '<div>preview</div>',
          annotations: [{ id: 'a-1', text: 'Keep warnings visible', author_id: null }],
          order: 0,
        },
      ],
      architecture_designs: [
        {
          id: 'arch-1',
          title: 'Export runtime',
          version: 1,
          global_description: 'Runtime slice.',
          entities: [{ id: 'exporter', name: 'Markdown Exporter', responsibility: 'Serialize Markdown' }],
          interfaces: [{ id: 'export-spec', name: 'exportSpec', protocol: 'TypeScript function' }],
          diagrams: [
            {
              id: 'diag-1',
              title: 'Runtime diagram',
              diagram_type: 'component',
              format: 'excalidraw_json',
              order_index: 0,
              adapter_payload_ref: 'payload-1',
            },
          ],
        },
      ],
    } as any);

    expect(md).toContain('## Export Warnings');
    expect(md).toContain('`asset_unavailable`');
    expect(md).toContain('Mockup visual render is unavailable');
    expect(md).toContain('Architecture diagram visual render is unavailable');
    expect(md).toContain('### 1. Export preview drawer');
    expect(md).toContain('#### Entities');
    expect(md).toContain('**Markdown Exporter**');
    expect(md).toContain('#### Diagrams');
    expect(md).toContain('payload_ref=payload-1');
  });
});

describe('exportMarkdown complete spec export', () => {
  it('renders all structured spec sections without object placeholders', () => {
    const md = exportSpec({
      title: 'Complete spec',
      status: 'review',
      version: 7,
      labels: ['export'],
      description: 'Full description.',
      context: 'Full context.',
      functional_requirements: [{ id: 'fr-1', text: 'FR1 body', status: 'active' }],
      technical_requirements: [
        {
          id: 'tr-1',
          text: 'TR1 body',
          status: 'active',
          linked_task_ids: ['task-1'],
          notes: 'TR notes',
        },
      ],
      acceptance_criteria: ['AC1 body'],
      test_scenarios: [
        {
          id: 'ts-1',
          title: 'Scenario one',
          scenario_type: 'unit',
          given: 'Given state',
          when: 'When action',
          then: 'Then outcome',
          linked_criteria: ['1'],
          notes: 'Scenario notes',
          status: 'ready',
          linked_task_ids: ['task-1'],
        },
      ],
      business_rules: [
        {
          id: 'br-1',
          title: 'Business rule',
          rule: 'Rule text',
          when: 'When condition',
          then: 'Then behavior',
          linked_requirements: ['fr-1'],
          linked_task_ids: ['task-1'],
          status: 'active',
          notes: 'Rule notes',
        },
      ],
      api_contracts: [
        {
          id: 'api-1',
          method: 'GET',
          path: '/export',
          description: 'Contract description',
          request_body: { type: 'object', properties: { id: { type: 'string' } } },
          response_success: { type: 'object', required: ['ok'] },
          response_errors: [{ status: 400, detail: 'bad request' }],
          linked_requirements: ['fr-1'],
          linked_rules: ['br-1'],
          linked_task_ids: ['task-1'],
          status: 'active',
          notes: 'API notes',
        },
      ],
      integration_requirements: [
        {
          id: 'ir-1',
          title: 'Integration requirement',
          integration_type: 'data_contract',
          description: 'IR description',
          provider: 'provider',
          consumer: 'consumer',
          contract_ref: 'api-1',
          endpoint: '/export',
          method: 'GET',
          data_contract: { source: 'payload' },
          linked_requirements: ['fr-1'],
          linked_api_contracts: ['api-1'],
          linked_task_ids: ['task-1'],
          status: 'active',
          notes: 'IR notes',
        },
      ],
      observability_requirements: [
        {
          id: 'or-1',
          title: 'Observability requirement',
          signal_type: 'metric',
          description: 'OR description',
          target: 'exporter',
          metric_name: 'markdown_export_total',
          threshold: '0 failures',
          severity: 'high',
          owner: 'frontend',
          linked_requirements: ['fr-1'],
          linked_integration_requirements: ['ir-1'],
          linked_task_ids: ['task-1'],
          status: 'active',
          notes: 'OR notes',
        },
      ],
      decisions: [
        {
          id: 'dec-1',
          title: 'Decision one',
          rationale: 'Decision rationale',
          context: 'Decision context',
          alternatives_considered: ['Alternative A'],
          supersedes_decision_id: null,
          linked_requirements: ['fr-1'],
          linked_task_ids: ['task-1'],
          status: 'active',
          notes: 'Decision notes',
        },
      ],
      knowledge_bases: [{ title: 'KB one', content: 'KB body', source_type: 'spec' }],
      screen_mockups: [],
      architecture_designs: [],
      qa_items: [{ question: 'Question?', answer: 'Answer.', asked_by: 'agent', answered_by: 'user' }],
      cards: [{ id: 'task-1', title: 'Implementation task', status: 'done' }],
    } as any);

    for (const heading of [
      '## Functional Requirements',
      '## Technical Requirements',
      '## Acceptance Criteria',
      '## Test Scenarios',
      '## Business Rules',
      '## API Contracts',
      '## Integration Requirements',
      '## Observability Requirements',
      '## Decisions',
      '## Knowledge Base',
      '## Q&A',
    ]) {
      expect(md).toContain(heading);
    }
    expect(md).toContain('TR1 body');
    expect(md).toContain('- FR1 body (fr-1)');
    expect(md).toContain('- Business rule (br-1)');
    expect(md).toContain('- GET /export (api-1)');
    expect(md).toContain('- Integration requirement (ir-1)');
    expect(md).toContain('- Implementation task (task-1)');
    expect(md).toContain('```json');
    expect(md).not.toContain('[object Object]');
  });
});

describe('exportMarkdown complete task family export', () => {
  const parentSpec = {
    title: 'Parent spec',
    status: 'in_progress',
    version: 1,
    labels: [],
    functional_requirements: ['FR1 body'],
    technical_requirements: ['TR1 body'],
    acceptance_criteria: ['AC1 body'],
    test_scenarios: [
      {
        id: 'ts-linked',
        title: 'Linked test scenario',
        scenario_type: 'unit',
        given: 'Given linked test',
        when: 'When exported',
        then: 'Then scenario is present',
        linked_criteria: ['1'],
        notes: null,
        status: 'ready',
        linked_task_ids: ['test-card'],
      },
    ],
    business_rules: [],
    api_contracts: [],
    integration_requirements: [],
    observability_requirements: [],
    decisions: [],
    cards: [
      { id: 'task-1', title: 'Origin implementation task', status: 'done' },
      { id: 'test-card', title: 'Regression test task', status: 'done' },
    ],
    knowledge_bases: [],
    screen_mockups: [],
    architecture_designs: [],
    qa_items: [],
  } as any;

  it('exports inherited spec architecture and card-owned architecture with Mermaid', () => {
    const specWithRuntimeArchitecture = {
      ...parentSpec,
      title: 'Parent spec with architecture',
      architecture_designs: [
        {
          id: 'arch-spec-runtime',
          title: 'Spec runtime',
          entities: [
            { id: 'spec-ui', name: 'Spec UI' },
            { id: 'spec-api', name: 'Spec API' },
          ],
          interfaces: [
            { id: 'spec-call', name: 'calls spec API', participants: ['spec-ui', 'spec-api'] },
          ],
          diagrams: [],
        },
      ],
    } as any;
    const md = exportCard({
      id: 'task-with-card-architecture',
      title: 'Task with card architecture',
      description: 'Exports both architecture scopes.',
      details: null,
      status: 'validation',
      priority: 'high',
      labels: [],
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      due_date: null,
      assignee_id: null,
      card_type: 'normal',
      test_scenario_ids: null,
      screen_mockups: [],
      architecture_designs: [
        {
          id: 'arch-card-runtime',
          title: 'Card runtime',
          entities: [
            { id: 'card-worker', name: 'Card Worker' },
            { id: 'card-store', name: 'Card Store' },
          ],
          interfaces: [
            { id: 'card-write', name: 'writes card output', participants: ['card-worker', 'card-store'] },
          ],
          diagrams: [],
        },
      ],
      knowledge_bases: [],
      conclusions: [],
      validations: [],
      comments: [],
      attachments: [],
    } as any, specWithRuntimeArchitecture);

    const specContextIndex = md.indexOf('## Spec Context: Parent spec with architecture');
    const inheritedArchitectureIndex = md.indexOf('## Architecture Designs', specContextIndex);
    const cardArchitectureIndex = md.indexOf('## Architecture Designs', inheritedArchitectureIndex + 1);
    expect(specContextIndex).toBeGreaterThan(-1);
    expect(inheritedArchitectureIndex).toBeGreaterThan(specContextIndex);
    expect(cardArchitectureIndex).toBeGreaterThan(inheritedArchitectureIndex);
    expect(md.match(/```mermaid/g)).toHaveLength(2);
    expect(md).toContain('spec_ui -- "calls spec API" --> spec_api');
    expect(md).toContain('card_worker -- "writes card output" --> card_store');
    expect(md.indexOf('Spec runtime')).toBeLessThan(md.indexOf('Card runtime'));
  });

  it('deduplicates repeated architecture warning origins in exportCard global warnings', () => {
    const repeatedNodeOnlyArchitecture = {
      id: 'arch-shared-node-only',
      title: 'Shared node-only architecture',
      entities: [
        { id: 'solo-a', name: 'Solo A' },
        { id: 'solo-b', name: 'Solo B' },
      ],
      interfaces: [],
      diagrams: [],
    };
    const specWithRepeatedWarning = {
      ...parentSpec,
      title: 'Parent spec repeated warning',
      architecture_designs: [repeatedNodeOnlyArchitecture],
    } as any;
    const md = exportCard({
      id: 'task-repeated-warning',
      title: 'Task repeated warning',
      description: null,
      details: null,
      status: 'validation',
      priority: 'medium',
      labels: [],
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      due_date: null,
      assignee_id: null,
      card_type: 'normal',
      test_scenario_ids: null,
      screen_mockups: [],
      architecture_designs: [repeatedNodeOnlyArchitecture],
      knowledge_bases: [],
      conclusions: [],
      validations: [],
      comments: [],
      attachments: [],
    } as any, specWithRepeatedWarning);

    const globalWarningStart = md.indexOf('## Export Warnings');
    const bodyStart = md.indexOf('---\n\n## Spec Context');
    const globalWarnings = md.slice(globalWarningStart, bodyStart);
    expect(globalWarningStart).toBeGreaterThan(-1);
    expect(globalWarnings.match(/relationships_not_reconstructable/g)).toHaveLength(1);
    expect(md.match(/#### Mermaid Conversion Warnings/g)).toHaveLength(2);
    expect(md.match(/```mermaid/g)).toHaveLength(2);
  });

  it('exports normal task data, dependencies, conclusions, validations and comments', () => {
    const md = exportCard({
      id: 'task-1',
      title: 'Normal task',
      description: 'Task description',
      details: 'Task details',
      status: 'validation',
      priority: 'high',
      labels: ['task'],
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      due_date: null,
      assignee_id: 'agent',
      card_type: 'normal',
      test_scenario_ids: null,
      screen_mockups: [],
      architecture_designs: [],
      knowledge_bases: [{ title: 'Card KB', content: 'Card KB body', source: 'card' }],
      conclusions: [
        {
          text: 'Implemented task',
          author_id: 'agent',
          created_at: '2026-05-28T12:00:00Z',
          completeness: 96,
          completeness_justification: 'Complete',
          drift: 3,
          drift_justification: 'Small drift',
          source: 'move_to_validation',
        },
      ],
      validations: [
        {
          id: 'val-1',
          verdict: 'pass',
          confidence: 95,
          completeness: 96,
          drift: 3,
          summary: 'Looks good',
          evaluator_id: 'validator',
          created_at: '2026-05-28T12:30:00Z',
        },
      ],
      comments: [{ author_id: 'agent', content: 'Comment body', created_at: '2026-05-28T12:45:00Z' }],
      attachments: [
        {
          id: 'att-1',
          filename: 'evidence.txt',
          original_filename: 'evidence.txt',
          mime_type: 'text/plain',
          size: 42,
          uploaded_by: 'agent',
          created_at: '2026-05-28T12:50:00Z',
        },
      ],
      depends_on: [{ id: 'dep-1', title: 'Dependency card', status: 'done' }],
    } as any, parentSpec);

    expect(md).toContain('| **Type** | Task |');
    expect(md).toContain('## Dependencies');
    expect(md).toContain('Dependency card');
    expect(md).toContain('## Conclusions');
    expect(md).toContain('Implemented task');
    expect(md).toContain('## Validations');
    expect(md).toContain('Looks good');
    expect(md).toContain('## Card Knowledge Bases');
    expect(md).toContain('## Attachments');
    expect(md).toContain('## Comments');
    expect(md).not.toContain('[object Object]');
  });

  it('exports test card linked scenarios with acceptance criteria resolved', () => {
    const md = exportCard({
      id: 'test-card',
      title: 'Test card',
      description: null,
      details: null,
      status: 'in_progress',
      priority: 'medium',
      labels: [],
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      due_date: null,
      assignee_id: null,
      card_type: 'test',
      test_scenario_ids: ['ts-linked'],
      screen_mockups: [],
      architecture_designs: [],
      knowledge_bases: [],
      conclusions: [],
      validations: [],
      comments: [],
      attachments: [],
    } as any, parentSpec);

    expect(md).toContain('| **Type** | Test |');
    expect(md).toContain('## Test Details');
    expect(md).toContain('ts-linked');
    expect(md).toContain('## Test Scenarios');
    expect(md).toContain('Linked test scenario');
    expect(md).toContain('AC1: AC1 body');
    expect(md).not.toContain('[object Object]');
  });

  it('exports bug card fields and regression links without collapsing to a generic task', () => {
    const md = exportCard({
      id: 'bug-1',
      title: 'Bug card',
      description: 'Bug description',
      details: null,
      status: 'in_progress',
      priority: 'critical',
      labels: ['bug'],
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      due_date: null,
      assignee_id: null,
      card_type: 'bug',
      origin_task_id: 'task-1',
      severity: 'major',
      expected_behavior: 'Expected behavior',
      observed_behavior: 'Observed behavior',
      steps_to_reproduce: '1. Reproduce',
      action_plan: 'Fix it',
      linked_test_task_ids: ['test-card'],
      test_scenario_ids: null,
      screen_mockups: [],
      architecture_designs: [],
      knowledge_bases: [],
      conclusions: [],
      validations: [],
      comments: [],
      attachments: [],
    } as any, parentSpec);

    expect(md).toContain('# [BUG] Bug card');
    expect(md).toContain('| **Type** | Bug |');
    expect(md).toContain('## Bug Details');
    expect(md).toContain('**Severity:** major');
    expect(md).toContain('**Origin task:** Origin implementation task (task-1)');
    expect(md).toContain('### Expected Behavior');
    expect(md).toContain('### Observed Behavior');
    expect(md).toContain('### Steps to Reproduce');
    expect(md).toContain('**Linked test tasks:**');
    expect(md).toContain('- Regression test task (test-card)');
    expect(md).not.toContain('[object Object]');
  });
});

describe('exportMarkdown existing entity export regressions', () => {
  it('exports ideation refinement story and sprint without exceptions or object placeholders', () => {
    const commonMockup = {
      id: 'mockup-1',
      title: 'Export mockup',
      description: 'Shows the export drawer.',
      screen_type: 'drawer',
      render_ref: 'mockup-preview.png',
    };
    const architectureDesign = {
      id: 'arch-1',
      title: 'Existing export architecture',
      global_description: 'Existing entity export should keep architecture summaries readable.',
      entities: [{ id: 'exporter', name: 'Exporter', responsibility: 'Write Markdown.' }],
      interfaces: [{ id: 'iface-1', name: 'Download', endpoint: 'downloadMarkdown' }],
      diagrams: [{ id: 'diag-1', title: 'Runtime diagram', preview_ref: 'diag.png' }],
    };
    const storyMarkdown = exportStory({
      title: 'Story export',
      status: 'accepted',
      topic_id: 'topic-1',
      topic: { name: 'Markdown' },
      actor: 'Agent',
      goal: 'Export stories',
      benefit: 'Offline review',
      description: 'Story description.',
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      labels: ['export'],
      ideation_links: [{ ideation_id: 'idea-1', created_at: '2026-05-28T10:30:00Z' }],
      screen_mockups: [commonMockup],
    } as any);
    const ideationMarkdown = exportIdeation({
      title: 'Ideation export',
      status: 'review',
      version: 2,
      complexity: 'medium',
      assignee_id: 'agent',
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      labels: ['export'],
      description: 'Ideation description.',
      problem_statement: 'Problem statement.',
      proposed_approach: 'Proposed approach.',
      scope_assessment: {
        domains: 2,
        domains_justification: 'Limited domain.',
        ambiguity: 1,
        ambiguity_justification: 'Clear.',
        dependencies: 2,
        dependencies_justification: 'Some dependencies.',
      },
      screen_mockups: [commonMockup],
      architecture_designs: [architectureDesign],
      qa_items: [{ question: 'Ready?', answer: 'Yes.' }],
    } as any);
    const refinementMarkdown = exportRefinement({
      title: 'Refinement export',
      status: 'approved',
      version: 3,
      assignee_id: 'agent',
      created_at: '2026-05-28T10:00:00Z',
      updated_at: '2026-05-28T11:00:00Z',
      labels: ['export'],
      description: 'Refinement description.',
      in_scope: ['Markdown output'],
      out_of_scope: ['PDF output'],
      analysis: 'Analysis text.',
      decisions: ['Keep frontend-first export.'],
      knowledge_bases: [{ title: 'Export KB', content: 'KB body.' }],
      screen_mockups: [commonMockup],
      architecture_designs: [architectureDesign],
      qa_items: [{ question: 'Validated?', answer: 'Yes.' }],
    } as any);
    const sprintMarkdown = exportSprint({
      title: 'Sprint export',
      status: 'closed',
      version: 1,
      spec_version: 3,
      spec_id: 'spec-1',
      start_date: '2026-05-28',
      end_date: '2026-05-29',
      objective: 'Ship export regression.',
      expected_outcome: 'All existing exports still render.',
      description: 'Sprint description.',
      cards: [
        { id: 'card-1', title: 'Done card', status: 'done', card_type: 'normal' },
        { id: 'card-2', title: 'Validation card', status: 'validation', card_type: 'test' },
      ],
      test_scenario_ids: ['ts-1'],
      business_rule_ids: ['br-1'],
      evaluations: [{ evaluator_name: 'Validator', recommendation: 'approve', overall_score: 96 }],
      labels: ['export'],
    }, {
      title: 'Parent spec',
      test_scenarios: [
        {
          id: 'ts-1',
          title: 'Sprint scenario',
          given: 'Existing exports',
          when: 'Regression suite runs',
          then: 'Markdown remains readable',
          status: 'passed',
        },
      ],
      business_rules: [{ id: 'br-1', title: 'Export rule', when: 'Export runs', then: 'It is readable' }],
    });

    const outputs = [storyMarkdown, ideationMarkdown, refinementMarkdown, sprintMarkdown];
    for (const md of outputs) {
      expect(md).not.toContain('[object Object]');
      expect(md.trim().startsWith('#')).toBe(true);
    }
    expect(storyMarkdown).toContain('## Linked Ideation');
    expect(ideationMarkdown).toContain('## Scope Assessment');
    expect(refinementMarkdown).toContain('## In Scope');
    expect(refinementMarkdown).toContain('## Decisions');
    expect(sprintMarkdown).toContain('## Progress');
    expect(sprintMarkdown).toContain('## Scoped Test Scenarios');
    expect(sprintMarkdown).toContain('## Scoped Business Rules');
  });
});

describe('exportMarkdown structured-entity + revoked handling', () => {
  it('excludes revoked FR/AC from the spec export and renders structured .text', () => {
    const md = exportSpec({
      title: 'Revoked handling spec',
      status: 'review',
      version: 1,
      labels: [],
      functional_requirements: [
        { id: 'fr_a', text: 'Active FR stays', status: 'active' },
        { id: 'fr_b', text: 'Revoked FR is hidden', status: 'revoked' },
      ],
      acceptance_criteria: [
        { id: 'ac_a', text: 'Active AC stays', status: 'active' },
        { id: 'ac_b', text: 'Revoked AC is hidden', status: 'revoked' },
      ],
    } as any);
    // structured dicts render their .text (never [object Object])
    expect(md).toContain('Active FR stays');
    expect(md).toContain('Active AC stays');
    expect(md).not.toContain('[object Object]');
    // revoked entries are filtered out — consistent with the SpecModal display
    expect(md).not.toContain('Revoked FR is hidden');
    expect(md).not.toContain('Revoked AC is hidden');
  });

  it('renders resolved-reference FR/AC text without "undefined" for legacy strings', () => {
    const md = exportSpec({
      title: 'Resolved refs spec',
      status: 'review',
      version: 1,
      labels: [],
      functional_requirements: ['FR active'],
      acceptance_criteria: ['AC active'],
      resolved_references: {
        functional_requirements: ['Legacy string FR'],
        acceptance_criteria: [{ id: 'ac_x', text: 'Resolved AC text' }],
      },
    } as any);
    expect(md).toContain('## Resolved References');
    expect(md).toContain('Legacy string FR'); // string-safe (was "undefined")
    expect(md).toContain('Resolved AC text');
    expect(md).not.toContain('- undefined');
  });
});
