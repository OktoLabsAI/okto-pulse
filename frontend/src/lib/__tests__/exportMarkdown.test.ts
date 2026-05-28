import { describe, expect, it } from 'vitest';
import { exportSpec } from '../exportMarkdown';

function specWithArchitecture(architectureDesigns: unknown[]) {
  return {
    title: 'Architecture Warning Spec',
    status: 'review',
    version: 1,
    labels: [],
    architecture_designs: architectureDesigns,
  } as any;
}

describe('exportMarkdown architecture warnings', () => {
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
