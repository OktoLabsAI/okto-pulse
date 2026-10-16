import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ArchitectureBoundariesEditor } from '../ArchitectureBoundariesEditor';

function Editor() {
  const [value, onChange] = useState<string[]>([]);
  return <ArchitectureBoundariesEditor value={value} onChange={onChange} />;
}

describe('ArchitectureBoundariesEditor', () => {
  it('adds, edits and removes individual items without splitting punctuation or lines', () => {
    render(<Editor />);
    fireEvent.click(screen.getByRole('button', { name: 'Add boundary' }));
    const first = screen.getByRole('textbox', { name: /Boundary 1/ });
    expect(first).toHaveAttribute('aria-invalid', 'true');
    fireEvent.change(first, { target: { value: 'Tenant data, isolated\nNo direct SQL' } });
    expect(first).toHaveAttribute('aria-invalid', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Add boundary' }));
    fireEvent.change(screen.getByRole('textbox', { name: /Boundary 2/ }), { target: { value: 'Public ports only' } });
    expect(screen.getAllByRole('textbox')).toHaveLength(2);
    expect(first).toHaveValue('Tenant data, isolated\nNo direct SQL');
    fireEvent.click(screen.getByRole('button', { name: 'Remove boundary 1' }));
    expect(screen.getByRole('textbox', { name: /Boundary 1/ })).toHaveValue('Public ports only');
    fireEvent.click(screen.getByRole('button', { name: 'Remove boundary 1' }));
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('renders read-only lists without exposing editing controls', () => {
    render(<ArchitectureBoundariesEditor value={['Backend, internal', 'Public ports only']} onChange={vi.fn()} readOnly />);
    expect(screen.getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Backend, internal', 'Public ports only']);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});
