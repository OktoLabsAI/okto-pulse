import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import {
  AccessibleTabList,
  AccessibleTabPanel,
  type AccessibleTabItem,
} from '../AccessibleTabs';

type TabId = 'details' | 'resources' | 'activity';

const ITEMS: readonly AccessibleTabItem<TabId>[] = [
  { id: 'details', label: 'Details' },
  { id: 'resources', label: 'Resources', count: 3 },
  { id: 'activity', label: 'Activity' },
];

function Harness({
  items = ITEMS,
  onChanged,
}: {
  items?: readonly AccessibleTabItem<TabId>[];
  onChanged?: (id: TabId) => void;
}) {
  const [value, setValue] = useState<TabId>('details');
  const change = (id: TabId) => {
    setValue(id);
    onChanged?.(id);
  };

  return (
    <>
      <AccessibleTabList
        idBase="entity-tabs"
        ariaLabel="Entity sections"
        items={items}
        value={value}
        onValueChange={change}
      />
      <AccessibleTabPanel
        idBase="entity-tabs"
        tabId="details"
        value={value}
      >
        Details panel
      </AccessibleTabPanel>
      <AccessibleTabPanel
        idBase="entity-tabs"
        tabId="resources"
        value={value}
        mount="lazy-keep"
      >
        <label>
          Resource draft
          <input aria-label="Resource draft" />
        </label>
      </AccessibleTabPanel>
      <AccessibleTabPanel
        idBase="entity-tabs"
        tabId="activity"
        value={value}
      >
        Activity panel
      </AccessibleTabPanel>
    </>
  );
}

describe('AccessibleTabs', () => {
  it('links the tablist, tabs, and active panel with explicit ARIA state', () => {
    render(<Harness />);

    const list = screen.getByRole('tablist', { name: 'Entity sections' });
    const details = screen.getByRole('tab', { name: 'Details' });
    const panel = screen.getByRole('tabpanel', { name: 'Details' });

    expect(list).toHaveAttribute('aria-orientation', 'horizontal');
    expect(list).toHaveClass('overflow-x-auto');
    expect(list).not.toHaveClass('scrollbar-hide');
    expect(details).toHaveAttribute('aria-selected', 'true');
    expect(details).toHaveAttribute('tabindex', '0');
    expect(details).toHaveAttribute('aria-controls', panel.id);
    expect(panel).toHaveAttribute('aria-labelledby', details.id);
  });

  it('uses manual activation: arrows move focus, Enter and Space activate', () => {
    const onChanged = vi.fn();
    render(<Harness onChanged={onChanged} />);

    const details = screen.getByRole('tab', { name: 'Details' });
    const resources = screen.getByRole('tab', { name: /Resources\s*3/ });
    const activity = screen.getByRole('tab', { name: 'Activity' });

    details.focus();
    fireEvent.keyDown(details, { key: 'ArrowRight' });
    expect(resources).toHaveFocus();
    expect(resources).toHaveAttribute('aria-selected', 'false');
    expect(onChanged).not.toHaveBeenCalled();

    fireEvent.keyDown(resources, { key: 'Enter' });
    expect(onChanged).toHaveBeenLastCalledWith('resources');
    expect(resources).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(resources, { key: 'End' });
    expect(activity).toHaveFocus();
    expect(activity).toHaveAttribute('aria-selected', 'false');

    fireEvent.keyDown(activity, { key: ' ' });
    expect(onChanged).toHaveBeenLastCalledWith('activity');
    expect(activity).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(activity, { key: 'Home' });
    expect(details).toHaveFocus();
    expect(details).toHaveAttribute('aria-selected', 'false');
  });

  it('wraps focus and skips disabled tabs without activating them', () => {
    const items: readonly AccessibleTabItem<TabId>[] = [
      { id: 'details', label: 'Details' },
      { id: 'resources', label: 'Resources', disabled: true },
      { id: 'activity', label: 'Activity' },
    ];
    render(<Harness items={items} />);

    const details = screen.getByRole('tab', { name: 'Details' });
    const resources = screen.getByRole('tab', { name: 'Resources' });
    const activity = screen.getByRole('tab', { name: 'Activity' });

    expect(resources).toBeDisabled();
    details.focus();
    fireEvent.keyDown(details, { key: 'ArrowLeft' });
    expect(activity).toHaveFocus();
    fireEvent.keyDown(activity, { key: 'ArrowRight' });
    expect(details).toHaveFocus();
  });

  it('keeps a lazily mounted panel and its draft state after tab changes', () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('tab', { name: /Resources\s*3/ }));
    const draft = screen.getByRole('textbox', { name: 'Resource draft' });
    fireEvent.change(draft, { target: { value: 'unsaved architecture note' } });

    fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
    expect(draft).not.toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: /Resources\s*3/ }));
    expect(screen.getByRole('textbox', { name: 'Resource draft' })).toHaveValue(
      'unsaved architecture note',
    );
  });

  it('supports vertical orientation with Up and Down focus navigation', () => {
    function VerticalHarness() {
      const [value, setValue] = useState<TabId>('details');
      return (
        <AccessibleTabList
          idBase="vertical-tabs"
          ariaLabel="Vertical sections"
          orientation="vertical"
          items={ITEMS}
          value={value}
          onValueChange={setValue}
        />
      );
    }

    render(<VerticalHarness />);
    const list = screen.getByRole('tablist', { name: 'Vertical sections' });
    const details = screen.getByRole('tab', { name: 'Details' });
    const resources = screen.getByRole('tab', { name: /Resources\s*3/ });

    expect(list).toHaveAttribute('aria-orientation', 'vertical');
    details.focus();
    fireEvent.keyDown(details, { key: 'ArrowDown' });
    expect(resources).toHaveFocus();
    expect(resources).toHaveAttribute('aria-selected', 'false');
  });
});
