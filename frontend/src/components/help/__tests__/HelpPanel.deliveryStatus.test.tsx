import { render, screen, cleanup, fireEvent } from '@testing-library/react';
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

it('shares the delivered-version rules between central and contextual Help', () => {
  const view = render(<HelpPanel onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('Help topic'), { target: { value: 'delivery-status' } });
  const central = screen.getByRole('heading', { name: 'Delivery status — See what has been delivered' }).parentElement!;
  const centralText = central.textContent;
  for (const rule of ['different delivered versions independently', 'version B does not replace a valid check for version A',
    'same version', 'without repeating the check', 'does not silently choose the newest passing result',
    'Needs review does not necessarily mean that checks disagree', 'Reset verification',
    'Old results stay in history', 'does not make them current again', 'Fresh verification is required',
    'never need to obtain, select or type a receipt', 'Delivery data is current', 'Graph details are temporarily unavailable']) {
    expect(central).toHaveTextContent(rule);
  }
  expect(central).not.toHaveTextContent('accepted_result_id');
  expect(central).not.toHaveTextContent('ordinal');
  view.unmount();
  render(<HelpPanel initialSectionId="delivery-status" onClose={vi.fn()} />);
  expect(screen.getByRole('heading', { name: 'Delivery status — See what has been delivered' }).parentElement!.textContent).toBe(centralText);
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
