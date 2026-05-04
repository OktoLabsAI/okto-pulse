/**
 * Sprint D1 — TC-1, TC-2, TC-4, TC-5 vitest aggregated suite for the
 * HierarchicalList + ViewModeToggle + useViewMode trio.
 *
 * - TC-1: HierarchicalList grouping + Standalone bucket
 * - TC-2: ViewModeToggle + useViewMode persists per-panel preference in localStorage
 * - TC-4: group header shows count + summary
 * - TC-5: aggregated suite (this file)
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, act } from '@testing-library/react';
import { renderHook } from '@testing-library/react';
import { HierarchicalList } from '../HierarchicalList';
import { ViewModeToggle } from '../ViewModeToggle';
import { useViewMode } from '@/hooks/useViewMode';

interface SampleItem {
  id: string;
  title: string;
  parentId: string | null;
}

const items: SampleItem[] = [
  { id: 'a1', title: 'Alpha 1', parentId: 'P1' },
  { id: 'a2', title: 'Alpha 2', parentId: 'P1' },
  { id: 'b1', title: 'Beta 1', parentId: 'P2' },
  { id: 'orphan', title: 'No parent', parentId: null },
];

beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = '';
});

describe('HierarchicalList — TC-1 grouping + Standalone', () => {
  it('renders 3 groups (P1, P2, Standalone) when grouping enabled', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="list"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={(it) => it.parentId}
        getGroupTitle={(k) => `Group ${k}`}
        ungroupedLabel="Standalone"
      />,
    );
    expect(screen.getByTestId('hierarchical-list-group-P1')).toBeTruthy();
    expect(screen.getByTestId('hierarchical-list-group-P2')).toBeTruthy();
    expect(screen.getByTestId('hierarchical-list-group-__ungrouped__')).toBeTruthy();
    expect(screen.getByText('Standalone')).toBeTruthy();
  });

  it('renders flat (no group headers) when grouping disabled', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="list"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={(it) => it.parentId}
        groupingEnabled={false}
      />,
    );
    expect(screen.queryByTestId('hierarchical-list-group-P1')).toBeNull();
    expect(screen.queryByTestId('hierarchical-list-group-__ungrouped__')).toBeNull();
    // All items should render
    expect(screen.getByText('Alpha 1')).toBeTruthy();
    expect(screen.getByText('No parent')).toBeTruthy();
  });

  it('can render ungrouped items flat while grouped items keep headers', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="list"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span data-testid={`row-${it.id}`}>{it.title}</span>}
        getGroupKey={(it) => it.parentId}
        ungroupedMode="flat"
      />,
    );

    expect(screen.getByTestId('hierarchical-list-group-P1')).toBeTruthy();
    expect(screen.getByTestId('hierarchical-list-group-P2')).toBeTruthy();
    expect(screen.queryByTestId('hierarchical-list-group-__ungrouped__')).toBeNull();
    expect(screen.getByTestId('row-orphan')).toBeTruthy();
  });

  it('toggling grid view switches to grid container; list view back to list', () => {
    const { rerender } = render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="grid"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={() => null}
        groupingEnabled={false}
      />,
    );
    expect(screen.getByTestId('hierarchical-list-grid')).toBeTruthy();

    rerender(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="list"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={() => null}
        groupingEnabled={false}
      />,
    );
    expect(screen.getByTestId('hierarchical-list-list')).toBeTruthy();
  });
});

describe('HierarchicalList — TC-4 group header count + collapse', () => {
  it('group header shows the per-group count badge', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="list"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={(it) => it.parentId}
      />,
    );
    expect(screen.getByTestId('hierarchical-list-group-count-P1').textContent).toContain('(2)');
    expect(screen.getByTestId('hierarchical-list-group-count-P2').textContent).toContain('(1)');
    expect(screen.getByTestId('hierarchical-list-group-count-__ungrouped__').textContent).toContain('(1)');
  });

  it('clicking the header collapses items and clicking again restores them', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="list"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span data-testid={`row-${it.id}`}>{it.title}</span>}
        getGroupKey={(it) => it.parentId}
      />,
    );
    expect(screen.getByTestId('row-a1')).toBeTruthy();
    fireEvent.click(screen.getByTestId('hierarchical-list-group-header-P1'));
    expect(screen.queryByTestId('row-a1')).toBeNull();
    expect(screen.queryByTestId('row-a2')).toBeNull();
    // Other group still visible
    expect(screen.getByTestId('row-b1')).toBeTruthy();
    fireEvent.click(screen.getByTestId('hierarchical-list-group-header-P1'));
    expect(screen.getByTestId('row-a1')).toBeTruthy();
  });
});

describe('useViewMode — TC-2 per-panel persistence', () => {
  it('defaults to provided mode when storage is empty', () => {
    const { result } = renderHook(() => useViewMode('panelA', 'grid'));
    expect(result.current.viewMode).toBe('grid');
  });

  it('persists changes per panelKey and reads them back', () => {
    const { result } = renderHook(() => useViewMode('ideations', 'list'));
    act(() => result.current.setViewMode('grid'));
    expect(localStorage.getItem('okto.view-mode.ideations')).toBe('grid');

    // Independent panel does NOT inherit the change
    const { result: other } = renderHook(() => useViewMode('specs', 'list'));
    expect(other.current.viewMode).toBe('list');
  });

  it('toggle() flips list ↔ grid and persists', () => {
    const { result } = renderHook(() => useViewMode('panelB', 'list'));
    act(() => result.current.toggle());
    expect(result.current.viewMode).toBe('grid');
    expect(localStorage.getItem('okto.view-mode.panelB')).toBe('grid');
    act(() => result.current.toggle());
    expect(result.current.viewMode).toBe('list');
  });
});

describe('HierarchicalList — TC-3 responsive grid classes', () => {
  it('grid container carries Tailwind responsive col classes', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="grid"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={() => null}
        groupingEnabled={false}
        gridCols={3}
      />,
    );
    const grid = screen.getByTestId('hierarchical-list-grid');
    const cls = grid.className;
    expect(cls).toContain('grid-cols-1');
    expect(cls).toContain('sm:grid-cols-2');
    expect(cls).toContain('md:grid-cols-3');
  });

  it('gridCols=4 swaps in lg:grid-cols-4', () => {
    render(
      <HierarchicalList<SampleItem>
        items={items}
        viewMode="grid"
        getItemKey={(it) => it.id}
        renderItem={(it) => <span>{it.title}</span>}
        getGroupKey={() => null}
        groupingEnabled={false}
        gridCols={4}
      />,
    );
    expect(screen.getByTestId('hierarchical-list-grid').className).toContain('lg:grid-cols-4');
  });
});

describe('ViewModeToggle — TC-2 UI surface', () => {
  it('renders both buttons and signals current mode via aria-pressed', () => {
    const onChange = vi.fn();
    render(<ViewModeToggle value="grid" onChange={onChange} />);
    const list = screen.getByTestId('view-mode-toggle-list') as HTMLButtonElement;
    const grid = screen.getByTestId('view-mode-toggle-grid') as HTMLButtonElement;
    expect(list.getAttribute('aria-pressed')).toBe('false');
    expect(grid.getAttribute('aria-pressed')).toBe('true');
  });

  it('clicks dispatch the correct mode', () => {
    const onChange = vi.fn();
    render(<ViewModeToggle value="list" onChange={onChange} />);
    fireEvent.click(screen.getByTestId('view-mode-toggle-grid'));
    expect(onChange).toHaveBeenCalledWith('grid');
    fireEvent.click(screen.getByTestId('view-mode-toggle-list'));
    expect(onChange).toHaveBeenCalledWith('list');
  });
});
