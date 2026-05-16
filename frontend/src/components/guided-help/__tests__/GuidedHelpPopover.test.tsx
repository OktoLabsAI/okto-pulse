import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GuidedHelpPopover } from '../GuidedHelpPopover';
import type { GuidedHelpStep } from '../types';

const step: GuidedHelpStep = {
  id: 'metrics.step.one',
  title: 'Track adoption signals',
  body: 'Metrics summarize usage and health signals without exposing board content.',
  anchor: 'metrics.entry',
  kind: 'feature',
};

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

function callbacks() {
  return {
    onBack: vi.fn(),
    onNext: vi.fn(),
    onDone: vi.fn(),
    onSkipStep: vi.fn(),
    onSkipAll: vi.fn(),
  };
}

beforeEach(() => {
  document.body.innerHTML = '';
  document.documentElement.className = '';
});

describe('GuidedHelpPopover', () => {
  it('renders accessible copy, progress, controls, and Escape skip behavior', () => {
    const props = callbacks();
    render(
      <GuidedHelpPopover
        step={step}
        anchorRect={rect(120, 120, 80, 40)}
        placement="right"
        progress={{ current: 1, total: 2 }}
        canGoBack={false}
        {...props}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Track adoption signals' });
    expect(dialog).toHaveAttribute('aria-describedby');
    expect(dialog).toHaveAttribute('data-fallback', 'false');
    expect(screen.getByText('1 / 2')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Back/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Next/ })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(props.onSkipStep).toHaveBeenCalledTimes(1);
  });

  it('traps focus inside the dialog and calls navigation actions', () => {
    const props = callbacks();
    const view = render(
      <GuidedHelpPopover
        step={step}
        anchorRect={rect(120, 120, 80, 40)}
        placement="bottom"
        progress={{ current: 1, total: 2 }}
        canGoBack
        {...props}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Track adoption signals' });
    const buttons = within(dialog).getAllByRole('button');
    buttons[buttons.length - 1].focus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(document.activeElement).toBe(buttons[0]);

    fireEvent.click(screen.getByRole('button', { name: /Back/ }));
    fireEvent.click(screen.getByRole('button', { name: /Next/ }));
    fireEvent.click(screen.getByRole('button', { name: /Skip all/ }));

    expect(props.onBack).toHaveBeenCalledTimes(1);
    expect(props.onNext).toHaveBeenCalledTimes(1);
    expect(props.onSkipAll).toHaveBeenCalledTimes(1);

    view.rerender(
      <GuidedHelpPopover
        step={step}
        anchorRect={rect(120, 120, 80, 40)}
        placement="bottom"
        progress={{ current: 2, total: 2 }}
        canGoBack
        {...props}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Done/ }));
    expect(props.onDone).toHaveBeenCalledTimes(1);
  });

  it('renders compact fallback when the anchor is missing', () => {
    const props = callbacks();
    render(
      <GuidedHelpPopover
        step={step}
        anchorRect={null}
        placement="bottom"
        progress={{ current: 1, total: 1 }}
        {...props}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Track adoption signals' });
    expect(dialog).toHaveAttribute('data-placement', 'fallback');
    expect(dialog).toHaveAttribute('data-fallback', 'true');
    expect(screen.getByText(/anchor is not visible/i)).toBeTruthy();
  });

  it('keeps mobile-width and dark-theme constraints on the dialog shell', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 640 });
    document.documentElement.classList.add('dark');
    const props = callbacks();

    render(
      <GuidedHelpPopover
        step={step}
        anchorRect={null}
        placement="bottom"
        progress={{ current: 1, total: 1 }}
        {...props}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Track adoption signals' });
    expect(dialog).toHaveClass('max-w-[calc(100vw-32px)]');
    expect(dialog).toHaveClass('dark:bg-gray-950');
    expect(dialog).toHaveStyle({ width: '320px' });
  });
});
