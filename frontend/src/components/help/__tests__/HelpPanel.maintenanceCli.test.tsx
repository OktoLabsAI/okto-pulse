import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { HelpPanel } from '../HelpPanel';

describe('HelpPanel after maintenance CLI retirement', () => {
  it('explains schema unavailability without directing the user to a removed CLI', () => {
    render(<HelpPanel initialSectionId="knowledge-graph" onClose={vi.fn()} />);
    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(screen.getByRole('heading', { name: 'Schema availability' })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/schema incompatibility makes the affected graph operation unavailable/i);
    expect(dialog).toHaveTextContent(/health observations are not authority to migrate or repair storage/i);
    expect(dialog).toHaveTextContent(/project knowledge queries and semantic consolidation retain their existing authorization/i);
    expect(dialog).not.toHaveTextContent(/okto-pulse kg\b/);
    expect(dialog).not.toHaveTextContent(/a triplet is exposed/i);
    expect(dialog).not.toHaveTextContent(/run tick now|save & run now|kg_tick_run_now|manual tick|danger zone/i);
    expect(dialog).not.toHaveTextContent(/kg_migrate_schema|kg_orphan_report|kg_orphan_backfill|sprints?/i);
    expect(dialog).toHaveTextContent(/source records remain authoritative/i);
    expect(dialog).toHaveTextContent(/read-only configuration and provider information/i);
    expect(dialog).toHaveTextContent(/open KG Health from the main menu/i);
    expect(screen.getByRole('heading', { name: 'Component availability' })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/unavailable Global Discovery cache can coexist with a healthy Board graph/i);
    expect(dialog).toHaveTextContent(/health offers no rebuild or quarantine restore control/i);
    expect(dialog).not.toHaveTextContent(/ceremonial rebuild|single-use, TTL-bound confirmation|explicit recovery flow below/i);
    expect(screen.queryByRole('heading', { name: 'Recovery & deterministic rebuild' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Cognitive consolidation (KG-03)' })).toBeInTheDocument();
  });

  it('falls back from the retired Sprint help deep link without offering Sprint actions', () => {
    render(<HelpPanel initialSectionId="sprints" onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Sprints' })).not.toBeInTheDocument();
    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(dialog).not.toHaveTextContent(/creating sprints|suggest sprints|skip flags per sprint/i);
    expect(screen.getByRole('button', { name: 'Knowledge Graph' })).toBeInTheDocument();
  });
});
