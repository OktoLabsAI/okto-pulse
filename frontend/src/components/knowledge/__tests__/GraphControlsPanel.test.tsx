/**
 * Unit coverage for GraphControlsPanel — Spec 8 / Sprint 4 (S4.4, S4.5, S4.6).
 *
 * Verifies the filter wiring: chip toggle emits the correct edgeTypes array,
 * the confidence slider fires with the raw 0..1 value, and the node-limit
 * dropdown bubbles the selected number to `onNodeLimitChange`.
 *
 * These assertions complement the e2e specs (S4.9a / S4.9b) with fast
 * feedback that runs on every CI PR without needing a live dev server.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GraphControlsPanel } from '../GraphControlsPanel';
import type { Filters } from '../GraphControlsPanel';
import { ALL_EDGE_TYPES } from '@/types/knowledge-graph';

function baseFilters(overrides: Partial<Filters> = {}): Filters {
  return {
    types: [],
    edgeTypes: [],
    minRelevance: 0.5,
    searchQuery: '',
    ...overrides,
  };
}

function renderPanel(props: Partial<React.ComponentProps<typeof GraphControlsPanel>> = {}) {
  const onFiltersChange = vi.fn();
  const onSubViewChange = vi.fn();
  const onNodeLimitChange = vi.fn();
  const utils = render(
    <GraphControlsPanel
      filters={baseFilters()}
      onFiltersChange={onFiltersChange}
      subView="graph"
      onSubViewChange={onSubViewChange}
      nodeCount={42}
      nodeLimit={100}
      onNodeLimitChange={onNodeLimitChange}
      {...props}
    />,
  );
  return { ...utils, onFiltersChange, onSubViewChange, onNodeLimitChange };
}

describe('GraphControlsPanel — edge type chips (S4.4, AC-5)', () => {
  it('renders one chip per KGEdgeType', () => {
    renderPanel();
    for (const et of ALL_EDGE_TYPES) {
      expect(screen.getByTestId(`kg-edge-chip-${et}`)).toBeInTheDocument();
    }
    expect(screen.getAllByTestId(/^kg-edge-chip-/)).toHaveLength(ALL_EDGE_TYPES.length);
  });

  it('clicking a chip when all edges are visible hides only that type', () => {
    const { onFiltersChange } = renderPanel();
    fireEvent.click(screen.getByTestId('kg-edge-chip-contradicts'));
    expect(onFiltersChange).toHaveBeenCalledTimes(1);
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.edgeTypes).toHaveLength(ALL_EDGE_TYPES.length - 1);
    expect(next.edgeTypes).not.toContain('contradicts');
  });

  it('clicking an inactive chip re-activates only it', () => {
    const { onFiltersChange } = renderPanel({
      filters: baseFilters({ edgeTypes: ['supersedes'] }),
    });
    fireEvent.click(screen.getByTestId('kg-edge-chip-contradicts'));
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.edgeTypes).toEqual(expect.arrayContaining(['supersedes', 'contradicts']));
    expect(next.edgeTypes).toHaveLength(2);
  });

  it('chips are toggled independently — turning one off leaves others unchanged', () => {
    const { onFiltersChange } = renderPanel({
      filters: baseFilters({ edgeTypes: ['supersedes', 'contradicts', 'tests'] }),
    });
    fireEvent.click(screen.getByTestId('kg-edge-chip-supersedes'));
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.edgeTypes).toEqual(['contradicts', 'tests']);
  });
});

describe('GraphControlsPanel — relevance slider (S4.5, AC-6)', () => {
  it('slider exposes min=0, max=1, step=0.05 and reflects filter value', () => {
    renderPanel({ filters: baseFilters({ minRelevance: 0.25 }) });
    const slider = screen.getByTestId('kg-relevance-slider') as HTMLInputElement;
    expect(slider.min).toBe('0');
    expect(slider.max).toBe('1');
    expect(slider.step).toBe('0.05');
    expect(slider.value).toBe('0.25');
  });

  it('changing the slider emits the raw 0..1 number, not a percent', () => {
    const { onFiltersChange } = renderPanel();
    fireEvent.change(screen.getByTestId('kg-relevance-slider'), {
      target: { value: '0.75' },
    });
    expect(onFiltersChange).toHaveBeenCalledTimes(1);
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.minRelevance).toBeCloseTo(0.75, 5);
  });
});

describe('GraphControlsPanel — node limit dropdown (S4.6, AC-9)', () => {
  it('exposes exactly 50/100/200/500 as options and current value is selected', () => {
    renderPanel({ nodeLimit: 200 });
    const select = screen.getByTestId('kg-node-limit') as HTMLSelectElement;
    const values = [...select.options].map((o) => Number(o.value));
    expect(values).toEqual([50, 100, 200, 500]);
    expect(select.value).toBe('200');
  });

  it('selecting a new option fires onNodeLimitChange with the chosen number', () => {
    const { onNodeLimitChange } = renderPanel();
    fireEvent.change(screen.getByTestId('kg-node-limit'), { target: { value: '500' } });
    expect(onNodeLimitChange).toHaveBeenCalledWith(500);
  });
});
