import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { GrafxAdvancedSettings, SettingHelp } from './GrafxAdvancedSettings';
import type { GrafxSettingDescriptor } from '@/services/runtime-settings-api';

const catalog: GrafxSettingDescriptor[] = [
  { name: 'max_result_rows', default: null, nullable: true, editable: true, kind: 'number', description: 'Maximum returned rows. Exhaustion refuses instead of silently truncating.' },
  { name: 'vector_math', default: 'auto', nullable: false, editable: true, kind: 'select', choices: ['auto', 'pure', 'numpy'], description: 'NumPy requires the acceleration dependency and can affect floating-point ties.' },
  { name: 'path', default: ':memory:', nullable: false, editable: false, kind: 'managed', description: 'Pulse owns generation paths; this cannot bypass route authority.' },
];

describe('Grafx advanced settings', () => {
  it('renders editable and managed options with help, preserving sibling overrides', () => {
    const changed = vi.fn();
    const { rerender } = render(<GrafxAdvancedSettings catalog={catalog} value={{ vector_math: 'pure' }} onChange={changed} />);
    fireEvent.click(screen.getByText('Advanced Grafx settings (3)'));
    expect(screen.getByTestId('grafx-option-vector_math')).toHaveValue('pure');
    expect(screen.getByTestId('grafx-option-path')).toHaveTextContent('Managed by Pulse');
    expect(screen.queryByRole('textbox', { name: 'path' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('grafx-option-max_result_rows'), { target: { value: '1500' } });
    expect(changed).toHaveBeenLastCalledWith({ vector_math: 'pure', max_result_rows: 1500 });
    rerender(<GrafxAdvancedSettings catalog={catalog} value={{ vector_math: 'pure', max_result_rows: 1500 }} onChange={changed} />);
    fireEvent.change(screen.getByTestId('grafx-option-max_result_rows'), { target: { value: '' } });
    expect(changed).toHaveBeenLastCalledWith({ vector_math: 'pure', max_result_rows: null });
    fireEvent.focus(screen.getByRole('button', { name: 'About max_result_rows' }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('instead of silently truncating');
  });

  it('exposes tooltip on hover, keyboard focus and touch/click, without dismissing its parent on Escape', () => {
    const parentEscape = vi.fn();
    const { container } = render(<div onKeyDown={parentEscape}><SettingHelp label="page" text="New generations only." /></div>);
    const help = screen.getByRole('button', { name: 'About page' });
    fireEvent.mouseEnter(help);
    expect(screen.getByRole('tooltip')).toHaveTextContent('New generations only');
    expect(container.querySelector('[role="tooltip"]')).toBeNull();
    expect(screen.getByRole('tooltip')).toHaveClass('fixed');
    fireEvent.mouseLeave(help);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.focus(help);
    expect(help).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id);
    fireEvent.keyDown(help, { key: 'Escape' });
    expect(parentEscape).not.toHaveBeenCalled();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.click(help);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
  });
});
