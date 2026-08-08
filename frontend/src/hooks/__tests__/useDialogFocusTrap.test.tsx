import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useDialogFocusTrap } from '../useDialogFocusTrap';

function TrappedDialog() {
  const trap = useDialogFocusTrap(true, '[data-initial]');
  return (
    <div
      ref={trap.dialogRef}
      role="dialog"
      tabIndex={-1}
      onKeyDown={trap.onKeyDown}
    >
      <button type="button" data-initial>First</button>
      <button type="button">Last</button>
    </div>
  );
}

describe('useDialogFocusTrap', () => {
  it('sets initial focus, wraps Tab in both directions, and restores the opener', async () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<TrappedDialog />);
    const dialog = screen.getByRole('dialog');
    const first = screen.getByRole('button', { name: 'First' });
    const last = screen.getByRole('button', { name: 'Last' });

    await waitFor(() => expect(first).toHaveFocus());
    last.focus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(first).toHaveFocus();
    first.focus();
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(last).toHaveFocus();

    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });
});
