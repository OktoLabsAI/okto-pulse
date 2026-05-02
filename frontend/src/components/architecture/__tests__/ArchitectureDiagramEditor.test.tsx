import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ArchitectureDiagramEditor } from '../ArchitectureDiagramEditor';
import type { ArchitectureDiagram } from '@/types';

const diagram: ArchitectureDiagram = {
  id: 'diag_1',
  title: 'Container',
  diagram_type: 'container',
  format: 'excalidraw_json',
  description: null,
  order_index: 0,
  adapter_payload: {
    type: 'excalidraw',
    version: 2,
    elements: [
      {
        id: 'box_1',
        type: 'rectangle',
        x: 40,
        y: 40,
        width: 160,
        height: 80,
        text: 'API',
      },
    ],
    appState: {},
    files: {},
  },
};

const twoNodeDiagram: ArchitectureDiagram = {
  ...diagram,
  adapter_payload: {
    type: 'excalidraw',
    version: 2,
    elements: [
      {
        id: 'box_1',
        type: 'rectangle',
        x: 40,
        y: 40,
        width: 160,
        height: 80,
        text: 'API',
        displayType: 'API',
      },
      {
        id: 'box_2',
        type: 'rectangle',
        x: 320,
        y: 40,
        width: 160,
        height: 80,
        text: 'Database',
        displayType: 'Database',
      },
    ],
    appState: {},
    files: {},
  },
};

const edgeDiagram: ArchitectureDiagram = {
  ...twoNodeDiagram,
  adapter_payload: {
    type: 'excalidraw',
    version: 2,
    elements: [
      ...((twoNodeDiagram.adapter_payload as { elements: unknown[] }).elements),
      {
        id: 'edge_1',
        type: 'arrow',
        sourceElementId: 'box_1',
        targetElementId: 'box_2',
        linkedInterfaceId: 'interface-1',
        linkedInterfaceIds: ['interface-1'],
        text: '',
        displayType: 'Edge',
        strokeColor: '#94a3b8',
      },
    ],
    appState: {},
    files: {},
  },
};

const detailedDiagram: ArchitectureDiagram = {
  ...edgeDiagram,
  adapter_payload: {
    type: 'excalidraw',
    version: 2,
    elements: [
      {
        id: 'box_1',
        type: 'rectangle',
        x: 40,
        y: 40,
        width: 160,
        height: 80,
        text: 'Checkout API',
        displayType: 'API',
        linkedEntityId: 'entity-api',
      },
      {
        id: 'box_2',
        type: 'rectangle',
        x: 320,
        y: 40,
        width: 160,
        height: 80,
        text: 'Orders DB',
        displayType: 'Database',
        linkedEntityId: 'entity-db',
      },
      {
        id: 'edge_1',
        type: 'arrow',
        sourceElementId: 'box_1',
        targetElementId: 'box_2',
        linkedInterfaceIds: ['interface-1'],
        displayType: 'Edge',
        strokeColor: '#94a3b8',
      },
    ],
    appState: {},
    files: {},
  },
};

describe('ArchitectureDiagramEditor', () => {
  it('renders diagram elements on the visual canvas', () => {
    render(<ArchitectureDiagramEditor diagram={diagram} onChange={vi.fn()} />);
    expect(screen.getByTestId('architecture-element-box_1')).toHaveTextContent('API');
  });

  it('moves visual elements with pointer drag', () => {
    const onChange = vi.fn();
    render(<ArchitectureDiagramEditor diagram={diagram} onChange={onChange} />);

    fireEvent.pointerDown(screen.getByTestId('architecture-element-box_1'), { clientX: 10, clientY: 20, pointerId: 1 });
    fireEvent.pointerMove(screen.getByTestId('architecture-canvas'), { clientX: 34, clientY: 56, pointerId: 1 });
    fireEvent.pointerUp(screen.getByTestId('architecture-canvas'), { pointerId: 1 });

    const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0] as ArchitectureDiagram;
    const payload = updated.adapter_payload as { elements: Array<{ id: string; x: number; y: number }> };
    expect(payload.elements.find((item) => item.id === 'box_1')).toMatchObject({ x: 72, y: 72 });
  });

  it('does not render diagram type tools or a connect toolbar action', () => {
    render(<ArchitectureDiagramEditor diagram={{ ...diagram, diagram_type: 'sequence' }} onChange={vi.fn()} />);

    expect(screen.queryByTitle('Add message')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Add database')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Connect nodes')).not.toBeInTheDocument();
  });

  it('shows edge settings only for selected edges', () => {
    const onChange = vi.fn();
    render(
      <ArchitectureDiagramEditor
        diagram={edgeDiagram}
        interfaces={[{ id: 'interface-1', name: 'Create invoice', direction: 'bidirectional', protocol: 'REST' }]}
        onChange={onChange}
      />,
    );

    expect(screen.queryByText('Linked Interfaces')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('architecture-element-box_1'));
    expect(screen.queryByText('Linked Interfaces')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('architecture-element-edge_1'));
    expect(screen.getAllByText('REST').length).toBeGreaterThan(0);
    expect(screen.getByRole('checkbox', { name: /Create invoice/i })).toBeChecked();
    fireEvent.change(screen.getByLabelText('Connection type'), { target: { value: 'elbow' } });

    const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0] as ArchitectureDiagram;
    const payload = updated.adapter_payload as { elements: Array<{ id: string; linkedInterfaceIds?: string[] | null; connectionType?: string }> };
    expect(payload.elements.find((item) => item.id === 'edge_1')).toMatchObject({ connectionType: 'elbow' });
  });

  it('clears canvas selection when the empty canvas is clicked', () => {
    render(
      <ArchitectureDiagramEditor
        diagram={edgeDiagram}
        interfaces={[{ id: 'interface-1', name: 'Create invoice', direction: 'bidirectional' }]}
        onChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId('architecture-element-edge_1'));
    expect(screen.getByText('Linked Interfaces')).toBeInTheDocument();

    fireEvent.pointerDown(screen.getByTestId('architecture-canvas'), { clientX: 3, clientY: 3, pointerId: 1 });
    expect(screen.queryByText('Linked Interfaces')).not.toBeInTheDocument();
  });

  it('uses interface direction to render connection arrow heads', () => {
    const { container, rerender } = render(
      <ArchitectureDiagramEditor
        diagram={edgeDiagram}
        interfaces={[{ id: 'interface-1', name: 'Create invoice', direction: 'target_to_source' }]}
        onChange={vi.fn()}
      />,
    );

    expect(container.querySelector('svg path[marker-start]')).toBeInTheDocument();
    expect(container.querySelector('svg path[marker-end]')).not.toBeInTheDocument();

    rerender(
      <ArchitectureDiagramEditor
        diagram={edgeDiagram}
        interfaces={[{ id: 'interface-1', name: 'Create invoice', direction: 'bidirectional' }]}
        onChange={vi.fn()}
      />,
    );

    expect(container.querySelector('svg path[marker-start]')).toBeInTheDocument();
    expect(container.querySelector('svg path[marker-end]')).toBeInTheDocument();
  });

  it('creates connections using explicit node border anchors', () => {
    const onChange = vi.fn();
    render(<ArchitectureDiagramEditor diagram={twoNodeDiagram} onChange={onChange} />);

    expect(screen.getByTestId('architecture-anchor-box_1-top')).toBeInTheDocument();
    expect(screen.getByTestId('architecture-anchor-box_1-right')).toBeInTheDocument();
    expect(screen.getByTestId('architecture-anchor-box_1-bottom')).toBeInTheDocument();
    expect(screen.getByTestId('architecture-anchor-box_1-left')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('architecture-anchor-box_1-bottom'));
    fireEvent.click(screen.getByTestId('architecture-anchor-box_2-top'));

    const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0] as ArchitectureDiagram;
    const payload = updated.adapter_payload as { elements: Array<{ type: string; sourceAnchor?: string; targetAnchor?: string }> };
    expect(payload.elements).toContainEqual(expect.objectContaining({
      type: 'arrow',
      sourceAnchor: 'bottom',
      targetAnchor: 'top',
    }));
  });

  it('deletes the selected element with the delete key', () => {
    const onChange = vi.fn();
    render(<ArchitectureDiagramEditor diagram={twoNodeDiagram} onChange={onChange} />);

    fireEvent.click(screen.getByTestId('architecture-element-box_2'));
    fireEvent.keyDown(window, { key: 'Delete' });

    const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0] as ArchitectureDiagram;
    const payload = updated.adapter_payload as { elements: Array<{ id: string }> };
    expect(payload.elements.some((item) => item.id === 'box_2')).toBe(false);
  });

  it('opens detail modals for entities and connections with double click', () => {
    render(
      <ArchitectureDiagramEditor
        diagram={detailedDiagram}
        entities={[
          {
            id: 'entity-api',
            name: 'Checkout API',
            entity_type: 'api',
            responsibility: 'Persists checkout commands.',
          },
          {
            id: 'entity-db',
            name: 'Orders DB',
            entity_type: 'database',
          },
        ]}
        interfaces={[
          {
            id: 'interface-1',
            name: 'Create order',
            endpoint: 'POST /orders',
            direction: 'source_to_target',
            protocol: 'REST',
            contract_type: 'OpenAPI',
          },
        ]}
        onChange={vi.fn()}
      />,
    );

    fireEvent.doubleClick(screen.getByTestId('architecture-element-box_1'));
    expect(screen.getByRole('dialog', { name: 'Entity details' })).toBeInTheDocument();
    expect(screen.getByText('Persists checkout commands.')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Close'));
    fireEvent.doubleClick(screen.getByTestId('architecture-element-edge_1'));
    expect(screen.getByRole('dialog', { name: 'Connection details' })).toBeInTheDocument();
    expect(screen.getAllByText('Create order').length).toBeGreaterThan(0);
    expect(screen.getByText('POST /orders')).toBeInTheDocument();
  });
});
