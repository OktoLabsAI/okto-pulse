import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { HelpPanel } from '../HelpPanel';

afterEach(cleanup);

it('explains delivery status, authority, exceptions and next steps at the contextual Help destination', () => {
  render(<HelpPanel initialSectionId="delivery-status" onClose={vi.fn()} />);
  expect(screen.getByLabelText('Help topic')).toHaveValue('delivery-status');
  const content = screen.getByRole('heading', { name: 'Delivery status — See what has been delivered' }).parentElement!;
  for (const rule of ['Delivered','In progress','Pending','Optional / Waived','Awaiting validation',
    'historical Done status is preserved','Unknown counts are not zero','Only an authorized human',
    'note if useful','whole selection is saved together','does not approve delivery','does not erase history',
    'Retry same decision','Details','Task progress','Task requirements','application Help']) {
    expect(content).toHaveTextContent(rule);
  }
  expect(content).not.toHaveTextContent('delivery-summary/v1');
  expect(content).not.toHaveTextContent('population_id');
});
