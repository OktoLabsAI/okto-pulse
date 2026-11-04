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
    'Retry same decision','Show details','Hide details','Refresh card','task modal header','Task progress','Task requirements','application Help',
    'Requirement / Objective', 'Change task requirements', 'Unknown', 'Not applicable', 'Test Cards',
    'Affected stage', 'What needs attention', 'Next step', 'Task-wide and unassigned checks',
    'Evidence and technical details', 'reuses that page', 'Retry delivery checks']) {
    expect(content).toHaveTextContent(rule);
  }
  expect(content).not.toHaveTextContent('delivery-summary/v1');
  expect(content).not.toHaveTextContent('population_id');
});

it('documents automatic human receipts, immutable correction and explicit retry at the form help destination', () => {
  render(<HelpPanel initialSectionId="delivery-progress" onClose={vi.fn()} />);
  expect(screen.getByLabelText('Help topic')).toHaveValue('delivery-progress');
  const content = screen.getByRole('heading', { name: 'Task progress — What changed and what remains' }).parentElement!;
  for (const rule of ['creates the audit receipt automatically', 'never need to obtain, select or enter a receipt',
    'Test Cards', 'not create a passing run', 'Retry same submission', 'Correct this update',
    'original author, time and content remain unchanged', 'not stored in browser local storage', 'Your text stays in the form'])
    expect(content).toHaveTextContent(rule);
});
