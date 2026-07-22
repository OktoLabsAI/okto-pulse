import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useEscapeToClose } from '../useEscapeToClose';
import { EditableField } from '@/components/shared/EditableField';

function Layer({
  onClose,
  enabled = true,
  canClose = true,
}: {
  onClose: () => void;
  enabled?: boolean;
  canClose?: boolean;
}) {
  useEscapeToClose(onClose, { enabled, canClose });
  return <div />;
}

function NestedLayers({
  closeParent,
  closeChild,
}: {
  closeParent: () => void;
  closeChild: () => void;
}) {
  useEscapeToClose(closeParent);
  return <PriorityLayer onClose={closeChild} priority={1} />;
}

function PriorityLayer({ onClose, priority }: { onClose: () => void; priority: number }) {
  useEscapeToClose(onClose, { priority });
  return <div />;
}

describe('useEscapeToClose', () => {
  it('closes the active modal on Escape and ignores other keys', () => {
    const onClose = vi.fn();
    render(<Layer onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Enter' });
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes only the uppermost nested modal', () => {
    const closeParent = vi.fn();
    const closeChild = vi.fn();
    const view = render(<Layer onClose={closeParent} />);

    view.rerender(
      <>
        <Layer onClose={closeParent} />
        <Layer onClose={closeChild} />
      </>,
    );

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(closeChild).toHaveBeenCalledTimes(1);
    expect(closeParent).not.toHaveBeenCalled();

    view.rerender(<Layer onClose={closeParent} />);
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(closeParent).toHaveBeenCalledTimes(1);
  });

  it('honors child priority when parent and child mount in the same commit', () => {
    const closeParent = vi.fn();
    const closeChild = vi.fn();
    render(<NestedLayers closeParent={closeParent} closeChild={closeChild} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(closeChild).toHaveBeenCalledTimes(1);
    expect(closeParent).not.toHaveBeenCalled();
  });

  it('blocks fall-through while the uppermost modal cannot close', () => {
    const closeParent = vi.fn();
    const closeChild = vi.fn();
    render(
      <>
        <Layer onClose={closeParent} />
        <Layer onClose={closeChild} canClose={false} />
      </>,
    );

    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    document.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(closeChild).not.toHaveBeenCalled();
    expect(closeParent).not.toHaveBeenCalled();
  });

  it('does not handle Escape already consumed by an inline editor', () => {
    const onClose = vi.fn();
    const view = render(
      <div>
        <Layer onClose={onClose} />
        <EditableField value="Original title" onSave={vi.fn()} />
      </div>,
    );

    fireEvent.click(view.getByText('Original title'));
    const editor = view.getByDisplayValue('Original title');
    fireEvent.change(editor, { target: { value: 'Unsaved title' } });
    fireEvent.keyDown(editor, { key: 'Escape' });

    expect(onClose).not.toHaveBeenCalled();
    expect(view.getByText('Original title')).toBeInTheDocument();
  });

  it('still closes from a regular form field that did not consume Escape', () => {
    const onClose = vi.fn();
    const view = render(
      <div>
        <Layer onClose={onClose} />
        <input aria-label="regular field" />
      </div>,
    );

    fireEvent.keyDown(view.getByLabelText('regular field'), { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
