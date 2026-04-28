/**
 * SearchInput — TC-2 (TS2) covering the `/` focus shortcut, Esc clear,
 * and the inline X button.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SearchInput } from '../SearchInput';

describe('SearchInput', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('renders with placeholder and current value', () => {
    render(<SearchInput value="hello" onChange={() => {}} placeholder="Search foo…" />);
    const input = screen.getByPlaceholderText('Search foo…') as HTMLInputElement;
    expect(input.value).toBe('hello');
  });

  it('calls onChange on typing', () => {
    const onChange = vi.fn();
    render(<SearchInput value="" onChange={onChange} />);
    const input = screen.getByTestId('list-search-input');
    fireEvent.change(input, { target: { value: 'abc' } });
    expect(onChange).toHaveBeenCalledWith('abc');
  });

  it('focus moves to input when "/" is pressed outside of an input/textarea', () => {
    render(<SearchInput value="" onChange={() => {}} />);
    const input = screen.getByTestId('list-search-input') as HTMLInputElement;
    expect(document.activeElement).not.toBe(input);
    fireEvent.keyDown(window, { key: '/' });
    expect(document.activeElement).toBe(input);
  });

  it('does NOT hijack "/" while typing in another input', () => {
    render(
      <>
        <input data-testid="other-input" />
        <SearchInput value="" onChange={() => {}} />
      </>,
    );
    const other = screen.getByTestId('other-input') as HTMLInputElement;
    other.focus();
    fireEvent.keyDown(other, { key: '/' });
    // Slash should NOT have stolen focus
    expect(document.activeElement).toBe(other);
  });

  it('Esc clears the value when value is non-empty', () => {
    const onChange = vi.fn();
    render(<SearchInput value="hello" onChange={onChange} />);
    const input = screen.getByTestId('list-search-input');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onChange).toHaveBeenCalledWith('');
  });

  it('Esc on empty input blurs instead of dispatching clear', () => {
    const onChange = vi.fn();
    render(<SearchInput value="" onChange={onChange} />);
    const input = screen.getByTestId('list-search-input') as HTMLInputElement;
    input.focus();
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onChange).not.toHaveBeenCalled();
    expect(document.activeElement).not.toBe(input);
  });

  it('clear button (X) is rendered when value is set and clears on click', () => {
    const onChange = vi.fn();
    render(<SearchInput value="hello" onChange={onChange} />);
    const clearBtn = screen.getByTestId('list-search-input-clear');
    fireEvent.click(clearBtn);
    expect(onChange).toHaveBeenCalledWith('');
  });

  it('clear button is hidden when value is empty', () => {
    render(<SearchInput value="" onChange={() => {}} />);
    expect(screen.queryByTestId('list-search-input-clear')).toBeNull();
  });

  it('respects enableSlashShortcut={false}', () => {
    render(<SearchInput value="" onChange={() => {}} enableSlashShortcut={false} />);
    const input = screen.getByTestId('list-search-input');
    expect(document.activeElement).not.toBe(input);
    fireEvent.keyDown(window, { key: '/' });
    expect(document.activeElement).not.toBe(input);
  });
});
