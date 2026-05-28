import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ArchitectureTab, ArchitectureValidationPanel } from '../ArchitectureTab';
import type { ArchitectureDesign, ArchitectureDesignSummary, ScreenMockup } from '@/types';

const apiMock = vi.hoisted(() => ({
  listArchitectureDesigns: vi.fn(),
  getArchitectureDesign: vi.fn(),
  createArchitectureDesign: vi.fn(),
  updateArchitectureDesign: vi.fn(),
  validateArchitectureDesign: vi.fn(),
  deleteArchitectureDesign: vi.fn(),
  copyArchitectureToCard: vi.fn(),
  importExcalidrawArchitectureDiagram: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const summary: ArchitectureDesignSummary = {
  id: 'arch-1',
  board_id: 'board-1',
  parent_type: 'ideation',
  parent_id: 'ideation-1',
  title: 'Runtime Architecture',
  version: 1,
  source_ref: null,
  source_version: null,
  source_design_id: null,
  stale: false,
  breaking_change_flag: false,
  requires_arch_review: false,
  diagrams_count: 1,
  adapter_payload_refs: ['payload-1'],
  created_at: '2026-04-30T00:00:00Z',
  updated_at: '2026-04-30T00:00:00Z',
};

const design: ArchitectureDesign = {
  ...summary,
  global_description: 'Single-user architecture editing.',
  entities: [],
  interfaces: [],
  diagrams: [
    {
      id: 'diag-1',
      title: 'Context',
      diagram_type: 'context',
      format: 'excalidraw_json',
      adapter_payload_ref: 'payload-1',
      adapter_payload: {
        type: 'excalidraw',
        version: 2,
        elements: [],
        appState: {},
        files: {},
      },
      description: null,
      order_index: 0,
    },
  ],
  created_by: 'user-1',
};

function InlineOnChangedWrapper() {
  const [, setItems] = useState<ArchitectureDesignSummary[]>([]);

  return (
    <ArchitectureTab
      parentType="ideation"
      parentId="ideation-1"
      onChanged={(next) => setItems(next)}
    />
  );
}

describe('ArchitectureTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listArchitectureDesigns.mockResolvedValue([summary]);
    apiMock.getArchitectureDesign.mockResolvedValue(design);
    apiMock.validateArchitectureDesign.mockResolvedValue({
      valid: true,
      issues: [],
      warnings: [],
      suggested_fixes: [],
      summary: {},
    });
  });

  it('loads architecture once when parent passes an inline onChanged callback', async () => {
    render(<InlineOnChangedWrapper />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledTimes(1));

    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });

    expect(apiMock.listArchitectureDesigns).toHaveBeenCalledTimes(1);
    expect(apiMock.getArchitectureDesign).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Stale')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Breaking')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Review')).not.toBeInTheDocument();
  });

  it('shows labeled architecture fields and parent mockup screens', async () => {
    const mockups: ScreenMockup[] = [
      { id: 'mockup-1', title: 'Checkout page', description: null, screen_type: 'page', html_content: '', annotations: null, order: 0 },
    ];
    apiMock.getArchitectureDesign.mockResolvedValue({
      ...design,
      entities: [{ id: 'entity-1', name: 'Checkout API', entity_type: 'service' }],
      interfaces: [{ id: 'interface-1', name: 'Create payment', participants: ['entity-1'] }],
    });

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" screenMockups={mockups} />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    expect(await screen.findByLabelText('Diagram title')).toBeInTheDocument();
    expect(screen.getByText('Checkout API')).toBeInTheDocument();
    expect(screen.getByText('service')).toBeInTheDocument();
    expect(screen.getByText('Create payment')).toBeInTheDocument();
    expect(screen.getByText('Source -> Target')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Edit entity'));
    expect(screen.getByLabelText('Entity name')).toBeInTheDocument();
    expect(screen.getByLabelText('Color')).toBeInTheDocument();
    expect(screen.getByLabelText('Icon')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Edit interface'));
    expect(screen.getByLabelText('Interface name')).toBeInTheDocument();
    expect(screen.getByLabelText('Endpoint / operation')).toBeInTheDocument();
    expect(screen.queryByLabelText('Source entity')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Target entity')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Direction')).toBeInTheDocument();
    expect(screen.getByText('Checkout page')).toBeInTheDocument();
  });

  it('adds catalog components as entities linked to the selected diagram', async () => {
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    fireEvent.click(await screen.findByTitle('Add API component'));

    expect(screen.getAllByText('New API').length).toBeGreaterThan(0);
    expect(screen.getAllByText('API').length).toBeGreaterThan(0);
  });

  it('shows backend architecture design warnings in the UI', async () => {
    apiMock.validateArchitectureDesign.mockResolvedValue({
      valid: true,
      issues: [],
      warnings: ['entities[0].responsibility is empty. Clarify what this component owns.'],
      suggested_fixes: [],
      summary: {},
    });
    apiMock.getArchitectureDesign.mockResolvedValue({
      ...design,
      entities: [{ id: 'entity-1', name: 'Checkout API', entity_type: 'service' }],
    });

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    await waitFor(() => expect(apiMock.validateArchitectureDesign).toHaveBeenCalledTimes(1), { timeout: 1500 });
    expect(screen.getByText('Design review')).toBeInTheDocument();
    expect(screen.getByText('1 warning')).toBeInTheDocument();
    expect(screen.getByText('entities[0].responsibility is empty. Clarify what this component owns.')).toBeInTheDocument();
  });

  it('renders structured topology warnings with an element focus action', () => {
    const onFocusElement = vi.fn();

    render(
      <ArchitectureValidationPanel
        loading={false}
        error={null}
        onFocusElement={onFocusElement}
        result={{
          valid: true,
          issues: [],
          warnings: [],
          structured_warnings: [
            {
              code: 'isolated_entity_node',
              severity: 'warning',
              message: 'Diagram entity node has no incident connector.',
              path: 'diagrams[0].adapter_payload.elements[2]',
              suggested_fix: 'Connect the node to another architecture element.',
              diagram_id: 'diag-1',
              diagram_type: 'runtime',
              element_id: 'node-audit',
            },
          ],
          suppressed_warnings: [],
          suggested_fixes: [],
          summary: {},
        }}
      />,
    );

    expect(screen.getByText('1 warning')).toBeInTheDocument();
    expect(screen.getByText('Connectivity and coverage')).toBeInTheDocument();
    expect(screen.getByText('isolated_entity_node')).toBeInTheDocument();
    expect(screen.getByText('diag-1 / node-audit')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Focus diagram element'));

    expect(onFocusElement).toHaveBeenCalledWith({ diagramId: 'diag-1', elementId: 'node-audit' });
  });

  it('renders entity-scoped structured topology warnings without a broken focus action', () => {
    render(
      <ArchitectureValidationPanel
        loading={false}
        error={null}
        result={{
          valid: true,
          issues: [],
          warnings: [],
          structured_warnings: [
            {
              code: 'entity_without_diagram_node',
              severity: 'warning',
              message: 'Architecture entity is not represented in any diagram.',
              path: 'entities[1]',
              suggested_fix: 'Add a diagram node for this entity or remove it from the model.',
              entity_id: 'entity-billing',
            },
          ],
          suppressed_warnings: [],
          suggested_fixes: [],
          summary: {},
        }}
      />,
    );

    expect(screen.getByText('entity_without_diagram_node')).toBeInTheDocument();
    expect(screen.getByText('entity-billing')).toBeInTheDocument();
    expect(screen.queryByTitle('Focus diagram element')).not.toBeInTheDocument();
  });

  it('keeps legacy string warning rendering for older backend responses', () => {
    render(
      <ArchitectureValidationPanel
        loading={false}
        error={null}
        result={{
          valid: true,
          issues: [],
          warnings: ['entities[0].responsibility is empty. Clarify what this component owns.'],
          structured_warnings: [],
          suppressed_warnings: [],
          suggested_fixes: [],
          summary: {},
        }}
      />,
    );

    expect(screen.getByText('1 warning')).toBeInTheDocument();
    expect(screen.getByText('Authoring warnings')).toBeInTheDocument();
    expect(screen.getByText('entities[0].responsibility is empty. Clarify what this component owns.')).toBeInTheDocument();
  });

  it('keeps the selected diagram payload visible after saving a new version', async () => {
    const designWithElement: ArchitectureDesign = {
      ...design,
      version: 1,
      diagrams: [
        {
          ...design.diagrams[0],
          adapter_payload: {
            type: 'excalidraw',
            version: 2,
            elements: [
              {
                id: 'box-save',
                type: 'rectangle',
                x: 40,
                y: 40,
                width: 160,
                height: 80,
                text: 'Saved API',
              },
            ],
            appState: {},
            files: {},
          },
        },
      ],
    };
    const patchResponse: ArchitectureDesign = {
      ...designWithElement,
      version: 2,
      diagrams: [{ ...designWithElement.diagrams[0], adapter_payload: null }],
    };
    const fullReload: ArchitectureDesign = { ...designWithElement, version: 2 };

    apiMock.getArchitectureDesign
      .mockResolvedValueOnce(designWithElement)
      .mockResolvedValueOnce(fullReload);
    apiMock.updateArchitectureDesign.mockResolvedValue(patchResponse);

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    expect(await screen.findByText('Saved API')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Saved API')).toBeInTheDocument();
    expect(screen.getAllByDisplayValue('v2').length).toBeGreaterThan(0);
  });
});
