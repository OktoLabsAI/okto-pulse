/**
 * CognitivePendingBadge + useCognitivePendingBadges tests — KG-03.6
 * frontend rework (Codex audit val_f87b8844).
 *
 * Coverage:
 *   - active items show badge (pending / in_progress / failed);
 *   - terminal-only items hide badge;
 *   - refinement is eligible and renders;
 *   - ideation / other are ineligible (badge hidden);
 *   - no mutation affordance on the badge;
 *   - hook makes ONE batch HTTP request, not one per source_ref.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { renderHook } from '@testing-library/react';

import { CognitivePendingBadge } from '../CognitivePendingBadge';
import {
  KG_BADGE_LABEL_ACTIVE,
  type KGCognitivePendingBadgeView,
  type KGCognitivePendingBadgesResponse,
} from '@/services/kg-health-api';
import * as kgHealthApi from '@/services/kg-health-api';
import { useCognitivePendingBadges } from '@/hooks/useCognitivePendingBadges';

vi.mock('@/services/kg-health-api', async () => {
  const actual = await vi.importActual<typeof kgHealthApi>(
    '@/services/kg-health-api',
  );
  return {
    ...actual,
    getKGCognitivePendingBadges: vi.fn(),
  };
});


beforeEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});


function badge(partial: Partial<KGCognitivePendingBadgeView>): KGCognitivePendingBadgeView {
  return {
    show_badge: false,
    label: '',
    status: null,
    item_id: null,
    updated_at: null,
    reason: 'not_found',
    ...partial,
  };
}


// -------- CognitivePendingBadge component (visual contract) -------------


describe('CognitivePendingBadge component', () => {
  it('renders the badge label when show_badge=true with active status', () => {
    render(
      <CognitivePendingBadge
        badge={badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        })}
      />,
    );
    const el = screen.getByTestId('cognitive-pending-badge');
    expect(el).toBeInTheDocument();
    expect(el.textContent).toContain(KG_BADGE_LABEL_ACTIVE);
    expect(el.getAttribute('data-status')).toBe('pending');
  });

  it.each(['pending', 'in_progress', 'failed'] as const)(
    'renders for active status=%s',
    (status) => {
      render(
        <CognitivePendingBadge
          badge={badge({
            show_badge: true,
            status,
            reason: 'active_cognitive_item',
            label: KG_BADGE_LABEL_ACTIVE,
          })}
        />,
      );
      const el = screen.getByTestId('cognitive-pending-badge');
      expect(el.getAttribute('data-status')).toBe(status);
    },
  );

  it.each(['consolidated', 'skipped'] as const)(
    'hides badge for terminal status=%s (reason=terminal_status)',
    (status) => {
      const { container } = render(
        <CognitivePendingBadge
          badge={badge({
            show_badge: false,
            status,
            reason: 'terminal_status',
            label: '',
          })}
        />,
      );
      expect(container.firstChild).toBeNull();
    },
  );

  it('renders nothing when badge is undefined', () => {
    const { container } = render(<CognitivePendingBadge badge={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when show_badge=false (ineligible_entity_type)', () => {
    const { container } = render(
      <CognitivePendingBadge
        badge={badge({
          show_badge: false,
          status: null,
          reason: 'ineligible_entity_type',
        })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('exposes NO mutation affordance (no button / input / select)', () => {
    const { container } = render(
      <CognitivePendingBadge
        badge={badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        })}
      />,
    );
    expect(container.querySelector('button')).toBeNull();
    expect(container.querySelector('input')).toBeNull();
    expect(container.querySelector('textarea')).toBeNull();
    expect(container.querySelector('select')).toBeNull();
  });

  it('uses role=status for accessibility', () => {
    render(
      <CognitivePendingBadge
        badge={badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        })}
      />,
    );
    const el = screen.getByTestId('cognitive-pending-badge');
    expect(el.getAttribute('role')).toBe('status');
  });

  it('compact mode keeps label in title for accessibility but hides text', () => {
    render(
      <CognitivePendingBadge
        compact
        badge={badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        })}
      />,
    );
    const el = screen.getByTestId('cognitive-pending-badge');
    expect(el.getAttribute('title')).toContain(KG_BADGE_LABEL_ACTIVE);
    // Compact mode → only the icon renders, not the label text.
    expect(el.textContent).not.toContain(KG_BADGE_LABEL_ACTIVE);
  });
});


// -------- useCognitivePendingBadges hook (batch semantics) --------------


describe('useCognitivePendingBadges hook', () => {
  function mockResponse(badges: Record<string, KGCognitivePendingBadgeView>): KGCognitivePendingBadgesResponse {
    return {
      board_id: 'b1',
      selected_kg_generation_id: 'gen-xxx',
      readonly: true,
      eligible_entity_types: ['spec', 'decision', 'refinement', 'task', 'test', 'bug'],
      badges,
    };
  }

  it('makes exactly ONE batch HTTP request for multiple source_refs', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockResolvedValue(
      mockResponse({
        'spec:s1': badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        }),
        'refinement:r1': badge({
          show_badge: true,
          status: 'in_progress',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        }),
        'ideation:i1': badge({
          show_badge: false,
          status: null,
          reason: 'ineligible_entity_type',
        }),
      }),
    );

    const { result } = renderHook(() =>
      useCognitivePendingBadges('b1', [
        'spec:s1',
        'refinement:r1',
        'ideation:i1',
      ]),
    );

    await waitFor(() =>
      expect(Object.keys(result.current.badges).length).toBe(3),
    );
    expect(mockFn).toHaveBeenCalledTimes(1);
    expect(mockFn).toHaveBeenCalledWith(
      'b1',
      ['spec:s1', 'refinement:r1', 'ideation:i1'],
      { kgGenerationId: null },
      expect.any(AbortSignal),
    );
  });

  it('dedupes source_refs before the HTTP call', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockResolvedValue(mockResponse({}));
    renderHook(() =>
      useCognitivePendingBadges('b1', ['spec:s1', 'spec:s1', 'spec:s2']),
    );
    await waitFor(() => expect(mockFn).toHaveBeenCalled());
    const args = mockFn.mock.calls[0];
    // Deduped to two unique refs.
    expect(args[1]).toHaveLength(2);
    expect(new Set(args[1])).toEqual(new Set(['spec:s1', 'spec:s2']));
  });

  it('does NOT call the API for empty source_refs list', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockResolvedValue(mockResponse({}));
    renderHook(() => useCognitivePendingBadges('b1', []));
    // Give the effect a tick to settle.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockFn).not.toHaveBeenCalled();
  });

  it('does NOT call the API when boardId is null', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockResolvedValue(mockResponse({}));
    renderHook(() => useCognitivePendingBadges(null, ['spec:s1']));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(mockFn).not.toHaveBeenCalled();
  });

  it('refinement is eligible — the resolver/hook never returns ineligible for it', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockResolvedValue(
      mockResponse({
        'refinement:r1': badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        }),
      }),
    );
    const { result } = renderHook(() =>
      useCognitivePendingBadges('b1', ['refinement:r1']),
    );
    await waitFor(() =>
      expect(result.current.badges['refinement:r1']).toBeDefined(),
    );
    expect(result.current.badges['refinement:r1'].show_badge).toBe(true);
    expect(result.current.badges['refinement:r1'].reason).toBe(
      'active_cognitive_item',
    );
  });

  it('exposes the resolved generation id for hook consumers', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockResolvedValue(
      mockResponse({
        'spec:s1': badge({
          show_badge: true,
          status: 'pending',
          reason: 'active_cognitive_item',
          label: KG_BADGE_LABEL_ACTIVE,
        }),
      }),
    );
    const { result } = renderHook(() =>
      useCognitivePendingBadges('b1', ['spec:s1']),
    );
    await waitFor(() =>
      expect(result.current.selectedKgGenerationId).toBe('gen-xxx'),
    );
  });

  it('propagates API errors so the consumer can surface a fallback', async () => {
    const mockFn = vi.mocked(kgHealthApi.getKGCognitivePendingBadges);
    mockFn.mockRejectedValue(new Error('cognitive_badges_unavailable'));
    const { result } = renderHook(() =>
      useCognitivePendingBadges('b1', ['spec:s1']),
    );
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
    expect(result.current.error?.message).toBe('cognitive_badges_unavailable');
  });
});


// -------- Integration sanity: ineligible card surfaces hide badge -------


describe('ineligible entity types do not render the badge (visual)', () => {
  it.each([
    {
      label: 'ideation',
      view: badge({
        show_badge: false,
        status: null,
        reason: 'ineligible_entity_type',
      }),
    },
    {
      label: 'other',
      view: badge({
        show_badge: false,
        status: null,
        reason: 'ineligible_entity_type',
      }),
    },
  ])('hides badge for %s entity type', ({ view }) => {
    const { container } = render(<CognitivePendingBadge badge={view} />);
    expect(container.firstChild).toBeNull();
  });
});
