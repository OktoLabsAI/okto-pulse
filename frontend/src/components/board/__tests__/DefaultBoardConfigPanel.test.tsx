// Spec 9df814bc / card 7da43521 / ts_fca679a0 — the Menu > Board default config
// panel renders the active template, defaults, design system, and override diff
// from the REAL administrative API (mocked at the api-service boundary, never a
// local disconnected mock state), and the admin actions hit the real API.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  getActiveDefaultBoardConfig: vi.fn(),
  listDefaultBoardConfigVersions: vi.fn(),
  getBoardDefaultConfigDiff: vi.fn(),
  createDefaultBoardConfigVersion: vi.fn(),
  activateDefaultBoardConfigVersion: vi.fn(),
  deactivateDefaultBoardConfigVersion: vi.fn(),
  listDefaultGuidelineCandidates: vi.fn(),
  updateDefaultGuidelineRefs: vi.fn(),
}));
const permissionState = vi.hoisted(() => ({
  isLoading: false,
  error: null as Error | null,
  ownerReviewRequired: false,
  allowed: new Set<string>(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Custom',
    isLoading: permissionState.isLoading,
    error: permissionState.error,
    ownerReviewRequired: permissionState.ownerReviewRequired,
    has: (flag: string) => permissionState.allowed.has(flag),
  }),
}));
// The Export/Import buttons pull the import-export service through the API
// context; stub the hook so this panel test does not need an ApiProvider.
vi.mock('@/services/import-export-api', () => ({
  useImportExportApi: () => ({
    exportGuidelines: vi.fn(),
    importGuidelines: vi.fn(),
    exportDesignSystems: vi.fn(),
    exportDesignSystem: vi.fn(),
    importDesignSystems: vi.fn(),
    exportPresets: vi.fn(),
    importPresets: vi.fn(),
    exportBoardConfig: vi.fn(),
    importBoardConfig: vi.fn(),
  }),
  importExportFilename: (kind: string) => `${kind}-00000000.json`,
  downloadJsonFile: vi.fn(),
}));


import { CONTEXTUAL_HELP_EVENT } from '@/components/help';
import { DefaultBoardConfigPanel } from '../DefaultBoardConfigPanel';

function tmpl(over: Record<string, unknown> = {}) {
  return {
    id: 't', version: 1, status: 'active', is_active: true, scope: 'global',
    settings_payload: {}, guideline_default_refs: [], design_system_default_ref: null,
    spec_checklist_mode: 'advisory',
    created_by: 'u', created_at: null, updated_at: null, ...over,
  };
}

function revisionPin(
  guidelineId: 'g1' | 'g2',
  revisionNumber = 1,
) {
  return {
    revision_id: `${guidelineId}-revision-${revisionNumber}`,
    revision_number: revisionNumber,
    semantic_version: `${revisionNumber}.0.0`,
    revision_digest: (guidelineId === 'g1' ? 'a' : 'b').repeat(64),
  };
}

function guidelineRef(
  guidelineId: 'g1' | 'g2',
  priority: number,
  revisionNumber = 1,
) {
  return {
    guideline_id: guidelineId,
    priority,
    ...revisionPin(guidelineId, revisionNumber),
  };
}

function candidate(
  guidelineId: 'g1' | 'g2',
  isDefault: boolean,
  priority: number | null,
  {
    headRevisionNumber = 1,
    defaultRevisionNumber = headRevisionNumber,
  }: {
    headRevisionNumber?: number;
    defaultRevisionNumber?: number;
  } = {},
) {
  const head = revisionPin(guidelineId, headRevisionNumber);
  return {
    guideline_id: guidelineId,
    title: guidelineId === 'g1' ? 'Already default' : 'Not default yet',
    scope: 'global',
    guideline_version: 1,
    ...head,
    head_revision: head,
    default_revision: isDefault
      ? revisionPin(guidelineId, defaultRevisionNumber)
      : null,
    retired: false,
    eligible: true,
    eligibility_reason: null,
    is_default: isDefault,
    priority,
  };
}

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

describe('DefaultBoardConfigPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.isLoading = false;
    permissionState.error = null;
    permissionState.ownerReviewRequired = false;
    grant(
      'guidelines.revisions.read',
      'guidelines.adoption.manage',
    );
    apiMock.getActiveDefaultBoardConfig.mockResolvedValue({
      scope: 'global',
      active: tmpl({
        id: 't2', version: 2,
        settings_payload: {
          max_scenarios_per_card: 5,
          require_task_validation: true,
          min_confidence: 70,
          require_spec_validation: true,
          design_system_gate_mode: 'advisory',
        },
        guideline_default_refs: [guidelineRef('g1', 1)],
        design_system_default_ref: { design_system_id: 'ds-1', gate_mode: 'advisory' },
      }),
    });
    apiMock.listDefaultBoardConfigVersions.mockResolvedValue({
      scope: 'global', active_id: 't2',
      versions: [
        tmpl({ id: 't2', version: 2, status: 'active', is_active: true }),
        tmpl({ id: 't1', version: 1, status: 'inactive', is_active: false }),
      ],
    });
    apiMock.getBoardDefaultConfigDiff.mockResolvedValue({
      board_id: 'b1', snapshot_state: 'applied',
      applied_template_id: 't1', applied_template_version: 1,
      active_template_id: 't2', active_template_version: 2, is_outdated: true,
      fields: [{ field: 'max_scenarios_per_card', template_value: 3, current_value: 8, state: 'overridden' }],
    });
    apiMock.activateDefaultBoardConfigVersion.mockResolvedValue(tmpl());
    apiMock.deactivateDefaultBoardConfigVersion.mockResolvedValue(tmpl());
    apiMock.createDefaultBoardConfigVersion.mockResolvedValue(tmpl({ id: 'new-template' }));
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global', template_id: 't2', template_version: 2,
      candidates: [
        candidate('g1', true, 1),
        candidate('g2', false, null),
      ],
    });
    apiMock.updateDefaultGuidelineRefs.mockResolvedValue(tmpl());
  });

  it.each([
    {
      label: 'loading',
      isLoading: true,
      error: null,
      ownerReviewRequired: false,
      permissions: ['guidelines.revisions.read'],
    },
    {
      label: 'permission error',
      isLoading: false,
      error: new Error('permission service unavailable'),
      ownerReviewRequired: false,
      permissions: ['guidelines.revisions.read'],
    },
    {
      label: 'owner review',
      isLoading: false,
      error: null,
      ownerReviewRequired: true,
      permissions: ['guidelines.revisions.read'],
    },
    {
      label: 'explicit deny',
      isLoading: false,
      error: null,
      ownerReviewRequired: false,
      permissions: ['guidelines.adoption.manage'],
    },
  ])(
    'fails closed for guideline defaults while authority is $label and keeps Help visible',
    async ({
      isLoading,
      error,
      ownerReviewRequired,
      permissions,
    }) => {
      permissionState.isLoading = isLoading;
      permissionState.error = error;
      permissionState.ownerReviewRequired = ownerReviewRequired;
      grant(...permissions);

      render(<DefaultBoardConfigPanel boardId="b1" />);

      expect(
        await screen.findByTestId('dbc-guideline-authority-unavailable'),
      ).toHaveTextContent('guidelines.revisions.read');
      expect(
        screen.getByTestId('default-guideline-policy-help'),
      ).toHaveTextContent('How exact pins work');
      expect(apiMock.listDefaultGuidelineCandidates).not.toHaveBeenCalled();
    },
  );

  it('loads exact pins for a read-only actor but disables every adoption affordance', async () => {
    grant('guidelines.revisions.read');

    render(<DefaultBoardConfigPanel boardId="b1" />);

    expect(
      await screen.findByTestId('dbc-guideline-candidates'),
    ).toBeInTheDocument();
    expect(apiMock.listDefaultGuidelineCandidates).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('dbc-toggle-default-g1')).toBeDisabled();
    expect(screen.getByTestId('dbc-toggle-default-g2')).toBeDisabled();
    expect(screen.getByTestId('default-guideline-policy-help'))
      .toBeInTheDocument();
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
  });

  it('enables guideline-default management only with both capabilities and opens canonical Help', async () => {
    const helpListener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, helpListener, {
      once: true,
    });

    render(<DefaultBoardConfigPanel boardId="b1" />);

    expect(
      await screen.findByTestId('dbc-toggle-default-g2'),
    ).toBeEnabled();
    fireEvent.click(screen.getByTestId('default-guideline-policy-help'));

    expect(helpListener).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { sectionId: 'policy-governance' },
      }),
    );
  });

  it('renders active template, defaults, design system, and override diff from REAL api data', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    await screen.findByTestId('default-board-config-panel');

    expect(screen.getByTestId('dbc-active-version').textContent).toBe('v2');
    expect(screen.getByTestId('dbc-guideline-count').textContent).toBe('1');
    expect(screen.getByTestId('dbc-design-system-detail').textContent).toMatch(/ds-1/);
    expect(screen.getByTestId('dbc-design-system-detail').textContent).toMatch(/advisory/);
    expect(screen.getByTestId('dbc-spec-checklist')).toHaveTextContent('advisory');
    expect(screen.getByTestId('dbc-outdated')).toBeInTheDocument();
    expect(screen.getByTestId('dbc-diff-fields').textContent).toMatch(/max_scenarios_per_card/);
    // The board diff was fetched from the REAL API with the board id.
    expect(apiMock.getBoardDefaultConfigDiff).toHaveBeenCalledWith('b1');
  });

  it('admin activate action hits the real API and reloads', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    const activate = await screen.findByTestId('dbc-activate-1');
    fireEvent.click(activate);
    await waitFor(() => expect(apiMock.activateDefaultBoardConfigVersion).toHaveBeenCalledWith('t1'));
  });

  it('paginates version history at 5 per page', async () => {
    // 7 versions -> page 1 shows v7..v3, page 2 shows v2..v1; no overflow on one page.
    apiMock.listDefaultBoardConfigVersions.mockResolvedValue({
      scope: 'global', active_id: 't7',
      versions: [7, 6, 5, 4, 3, 2, 1].map((v) =>
        tmpl({ id: `t${v}`, version: v, status: v === 7 ? 'active' : 'inactive', is_active: v === 7 }),
      ),
    });

    render(<DefaultBoardConfigPanel boardId="b1" />);
    await screen.findByTestId('dbc-versions');

    // Page 1: exactly 5 rows (v7..v3), the older two are not rendered yet.
    for (const v of [7, 6, 5, 4, 3]) {
      expect(screen.getByTestId(`dbc-version-${v}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId('dbc-version-2')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dbc-version-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('dbc-versions-page')).toHaveTextContent('Page 1 of 2');
    expect(screen.getByTestId('dbc-versions-prev')).toBeDisabled();

    // Advance: page 2 shows the remaining two and drops the first five.
    fireEvent.click(screen.getByTestId('dbc-versions-next'));

    expect(screen.getByTestId('dbc-version-2')).toBeInTheDocument();
    expect(screen.getByTestId('dbc-version-1')).toBeInTheDocument();
    expect(screen.queryByTestId('dbc-version-7')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dbc-version-3')).not.toBeInTheDocument();
    expect(screen.getByTestId('dbc-versions-page')).toHaveTextContent('Page 2 of 2');
    expect(screen.getByTestId('dbc-versions-next')).toBeDisabled();
  });

  it('hides version pagination when there are 5 or fewer versions', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />); // default mock has 2 versions
    await screen.findByTestId('dbc-versions');
    expect(screen.queryByTestId('dbc-versions-pagination')).not.toBeInTheDocument();
  });

  it('shows legacy/no-snapshot state when the board has no applied snapshot', async () => {
    apiMock.getBoardDefaultConfigDiff.mockResolvedValue({
      board_id: 'b1', snapshot_state: 'legacy_no_snapshot',
      applied_template_id: null, applied_template_version: null,
      active_template_id: null, active_template_version: null, is_outdated: false, fields: [],
    });
    render(<DefaultBoardConfigPanel boardId="b1" />);
    expect(await screen.findByTestId('dbc-legacy')).toBeInTheDocument();
  });

  it('shows no-active-template state gracefully', async () => {
    apiMock.getActiveDefaultBoardConfig.mockResolvedValue({ scope: 'global', active: null });
    render(<DefaultBoardConfigPanel boardId="b1" />);
    expect(await screen.findByTestId('dbc-no-active')).toBeInTheDocument();
  });

  it('stages guideline default changes and commits them in one version on Save', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    await screen.findByTestId('dbc-guideline-candidates');
    expect(screen.getByTestId('dbc-cand-default-g1')).toBeInTheDocument();

    // Setting g2 as default stages locally (live badge) — no API call yet.
    fireEvent.click(screen.getByTestId('dbc-toggle-default-g2'));
    expect(screen.getByTestId('dbc-cand-default-g2')).toBeInTheDocument();
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();

    // Save commits the FULL rebuilt ref list (keeps g1, adds g2) as ONE new version.
    fireEvent.click(screen.getByTestId('dbc-save-template'));
    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1));
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      guideline_default_refs: [
        guidelineRef('g1', 1),
        guidelineRef('g2', 2),
      ],
    }));
    // The unified Save path no longer uses the standalone guideline-refs endpoint.
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();
  });

  it('does not silently promote an existing default when its guideline has a newer head', async () => {
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 't2',
      template_version: 2,
      candidates: [
        candidate('g1', true, 1, {
          headRevisionNumber: 2,
          defaultRevisionNumber: 1,
        }),
        candidate('g2', false, null),
      ],
    });

    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(
      await screen.findByTestId('dbc-toggle-default-g2'),
    );
    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() =>
      expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(
      expect.objectContaining({
        guideline_default_refs: [
          guidelineRef('g1', 1, 1),
          guidelineRef('g2', 2, 1),
        ],
      }),
    );
  });

  it('shows pinned and latest versions separately and updates the pin only after explicit Use latest plus Save', async () => {
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 't2',
      template_version: 2,
      candidates: [
        candidate('g1', true, 1, {
          headRevisionNumber: 2,
          defaultRevisionNumber: 1,
        }),
      ],
    });

    render(<DefaultBoardConfigPanel boardId="b1" />);

    const row = await screen.findByTestId('dbc-cand-g1');
    expect(row).toHaveTextContent('Default v1.0.0');
    expect(row).toHaveTextContent('Latest v2.0.0');
    expect(screen.getByTestId('dbc-cand-update-g1'))
      .toHaveTextContent('Update available');

    fireEvent.click(screen.getByTestId('dbc-use-latest-g1'));

    expect(row).toHaveTextContent('Default v2.0.0');
    expect(screen.queryByTestId('dbc-cand-update-g1')).not.toBeInTheDocument();
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('dbc-save-template'));
    await waitFor(() =>
      expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(
      expect.objectContaining({
        guideline_default_refs: [guidelineRef('g1', 1, 2)],
      }),
    );
  });

  it('requires a retired default to be unset before another default is added', async () => {
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 't2',
      template_version: 2,
      candidates: [
        {
          ...candidate('g1', true, 1),
          retired: true,
          eligible: false,
          eligibility_reason: 'guideline_retired',
        },
        candidate('g2', false, null),
      ],
    });

    render(<DefaultBoardConfigPanel boardId="b1" />);

    expect(
      await screen.findByTestId('dbc-retired-default-warning'),
    ).toHaveTextContent('Unset the retired default');
    expect(
      screen.getByTestId('dbc-toggle-default-g2'),
    ).toBeDisabled();
    expect(screen.getByTestId('dbc-toggle-default-g1')).not.toBeDisabled();

    fireEvent.click(screen.getByTestId('dbc-toggle-default-g1'));
    expect(screen.getByTestId('dbc-toggle-default-g2')).not.toBeDisabled();
  });

  it('unsetting a default stages an empty ref list, committed on Save', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    await screen.findByTestId('dbc-guideline-candidates');
    fireEvent.click(screen.getByTestId('dbc-toggle-default-g1'));
    expect(screen.queryByTestId('dbc-cand-default-g1')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dbc-save-template'));
    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalled());
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      guideline_default_refs: [], // g1 removed; g2 was not a default
    }));
  });

  it('keeps the board settings UI visible when guideline candidates fail to load', async () => {
    apiMock.listDefaultGuidelineCandidates.mockRejectedValue(new Error('Guideline not found'));

    render(<DefaultBoardConfigPanel boardId="b1" />);

    expect(await screen.findByTestId('default-board-config-panel')).toBeInTheDocument();
    expect(screen.getByTestId('dbc-candidates-error')).toHaveTextContent('Default guideline candidates are unavailable');
    expect(screen.queryByText('Guideline not found')).not.toBeInTheDocument();
  });

  it('preserves authoritative exact guideline pins when candidates are unavailable', async () => {
    apiMock.listDefaultGuidelineCandidates.mockRejectedValue(
      new Error('catalog unavailable'),
    );

    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(
      await screen.findByRole('switch', {
        name: 'Require task validation',
      }),
    );
    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() =>
      expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(
      expect.objectContaining({
        guideline_default_refs: [guidelineRef('g1', 1)],
      }),
    );
  });

  it('creates the first template with the staged guideline default on Save when none exists', async () => {
    apiMock.getActiveDefaultBoardConfig.mockResolvedValue({ scope: 'global', active: null });
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global', template_id: null, template_version: null,
      candidates: [
        candidate('g2', false, null),
      ],
    });

    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(await screen.findByTestId('dbc-toggle-default-g2'));
    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalled());
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      guideline_default_refs: [guidelineRef('g2', 1)],
    }));
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();
  });

  it('stages gate edits in a draft and creates ONE version only on Save', async () => {
    // The template editor reuses the shared BoardSettingsForm. Editing must NOT
    // create a version per change — it stages a draft; Save commits one version.
    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(await screen.findByRole('switch', { name: 'Require task validation' }));

    // No version yet — the change is only staged locally.
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
    expect(screen.getByTestId('dbc-template-dirty')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1));
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      guideline_default_refs: [guidelineRef('g1', 1)],
      design_system_default_ref: { design_system_id: 'ds-1', gate_mode: 'advisory' },
      settings_payload: expect.objectContaining({
        require_task_validation: false,
        min_confidence: 70,
        design_system_gate_mode: 'advisory',
      }),
    }));
  });

  it('versions the refinement ambiguity policy and bounded threshold in Global Default', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(await screen.findByTestId('toggle-refinement-ambiguity-gate'));
    fireEvent.click(await screen.findByTestId('button-max-refinement-ambiguity-5'));

    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1));
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      settings_payload: expect.objectContaining({
        require_refinement_ambiguity_gate: true,
        max_refinement_ambiguity: 5,
      }),
    }));
  });

  it('versions the curated checklist default and applies it only on Save', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);

    expect(await screen.findByTestId('dbc-checklist-mode-advisory')).toHaveAttribute(
      'aria-checked',
      'true',
    );
    fireEvent.click(screen.getByTestId('dbc-checklist-mode-blocking'));

    expect(screen.getByTestId('dbc-template-dirty')).toBeInTheDocument();
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() =>
      expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(
      expect.objectContaining({
        activate: true,
        spec_checklist_mode: 'blocking',
      }),
    );
  });

  it('opens contextual checklist help from Global Default', async () => {
    const onOpenHelp = vi.fn();
    render(
      <DefaultBoardConfigPanel boardId="b1" onOpenHelp={onOpenHelp} />,
    );

    fireEvent.click(await screen.findByTestId('dbc-checklist-help-link'));

    expect(onOpenHelp).toHaveBeenCalledTimes(1);
  });

  it('stages task requirement link gate skip in the default board config payload', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(await screen.findByRole('switch', { name: 'Skip task requirement link gate' }));

    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1));
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      settings_payload: expect.objectContaining({
        skip_task_requirement_link_gate_global: true,
      }),
    }));
  });

  it('discards staged gate edits without creating a version', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(await screen.findByRole('switch', { name: 'Require task validation' }));
    expect(screen.getByTestId('dbc-template-dirty')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('dbc-discard-template'));

    expect(screen.queryByTestId('dbc-template-dirty')).not.toBeInTheDocument();
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
  });

  it('commits a numeric gate default change on Save', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    const input = await screen.findByTestId('bsf-num-min_confidence');

    fireEvent.change(input, { target: { value: '85' } });
    fireEvent.blur(input);
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledTimes(1));
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      settings_payload: expect.objectContaining({
        min_confidence: 85,
        require_task_validation: true,
      }),
    }));
    const payload = apiMock.createDefaultBoardConfigVersion.mock.calls[0][0].settings_payload;
    expect(Object.prototype.hasOwnProperty.call(payload, 'skip_task_requirement_link_gate_global')).toBe(false);
  });

  it('keeps the Design System default ref gate mirrored when the default gate mode changes', async () => {
    render(<DefaultBoardConfigPanel boardId="b1" />);
    fireEvent.click(await screen.findByTestId('design-system-gate-mode-blocking'));
    fireEvent.click(screen.getByTestId('dbc-save-template'));

    await waitFor(() => expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalled());
    expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith(expect.objectContaining({
      activate: true,
      design_system_default_ref: { design_system_id: 'ds-1', gate_mode: 'blocking' },
      settings_payload: expect.objectContaining({
        design_system_gate_mode: 'blocking',
      }),
    }));
  });
});
