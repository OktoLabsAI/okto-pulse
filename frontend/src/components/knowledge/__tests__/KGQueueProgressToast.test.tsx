import { render, screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';

import { KGQueueProgressToast } from '../KGQueueProgressToast';

describe('KGQueueProgressToast', () => {
  test('uses processed and stable total instead of shrinking pending count', () => {
    render(
      <KGQueueProgressToast
        progress={{
          pending: 8,
          claimed: 1,
          done: 0,
          processed: 3,
          failed: 0,
          paused: 0,
          total: 12,
        }}
      />,
    );

    expect(screen.getByText('25%')).toBeInTheDocument();
    expect(screen.getByText('Processed')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});
