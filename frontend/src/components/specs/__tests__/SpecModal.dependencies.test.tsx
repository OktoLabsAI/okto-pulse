import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import axe from 'axe-core';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { Spec } from '@/types';
import type {
  SpecDependencyItem,
  SpecDependencyPage,
  SpecDependencyReadiness,
} from '@/types/spec-dependencies';
import { SpecModal } from '../SpecModal';

const apiMock = vi.hoisted(() => ({
  getSpec: vi.fn(),
  getAllowedTransitions: vi.fn(),
  listSprints: vi.fn(),
  listSpecDependencies: vi.fn(),
  lookupSpecs: vi.fn(),
  addSpecDependency: vi.fn(),
  removeSpecDependency: vi.fn(),
}));
const permissionMock = vi.hoisted(() => ({
  denied: new Set<string>(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({ id: 'board-1', owner_id: null, agents: [] }),
}));

vi.mock('@/hooks/usePermissions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/hooks/usePermissions')>();
  return {
    ...actual,
    usePermissions: () => ({
      preset: null,
      isLoading: false,
      error: null,
      has: (permission: string) => !permissionMock.denied.has(permission),
    }),
  };
});

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div />,
}));

vi.mock('@/components/shared/ValidationGateOverride', () => ({
  ValidationGateOverride: () => <div />,
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: () => <div />,
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const readiness: SpecDependencyReadiness = {
  board_id: 'board-1',
  spec_id: 'spec-1',
  can_start: false,
  reason_code: 'spec_dependencies_incomplete',
  current_edition: 2,
  last_started_edition: null,
  current_edition_started: false,
  active_dependency_count: 2,
  unmet_count: 2,
  blocking_count: 2,
  archived_blocking_count: 0,
  unfinished_blocking_count: 2,
  blockers_truncated: true,
  blockers: [
    {
      dependency_id: 'dependency-1',
      dependent_spec_id: 'spec-1',
      prerequisite_spec_id: 'spec-2',
      target_title: 'Platform prerequisite',
      target_status: 'approved',
      target_edition: 1,
      target_version: 3,
      target_archived: false,
    },
  ],
  ready: false,
};

const spec: Spec = {
  id: 'spec-1',
  board_id: 'board-1',
  ideation_id: 'ideation-1',
  refinement_id: null,
  title: 'Dependent specification',
  description: null,
  context: null,
  functional_requirements: [],
  technical_requirements: [],
  acceptance_criteria: [],
  test_scenarios: [],
  business_rules: [],
  api_contracts: [],
  integration_requirements: [],
  observability_requirements: [],
  decisions: [],
  screen_mockups: [],
  architecture_designs: [],
  skip_test_coverage: false,
  status: 'validated',
  edition: 2,
  version: 4,
  assignee_id: null,
  created_by: 'user-1',
  created_at: '2026-08-12T10:00:00Z',
  updated_at: '2026-08-12T10:00:00Z',
  labels: [],
  cards: [],
  knowledge_bases: [],
  qa_items: [],
  dependency_readiness: readiness,
};

const dependencyItem: SpecDependencyItem = {
  id: 'dependency-1',
  dependent_spec_id: 'spec-1',
  prerequisite_spec_id: 'spec-2',
  created_at: '2026-08-12T10:00:00Z',
  created_by: 'user-1',
  created_by_type: 'user',
  created_by_name: 'Jo',
  introduced_at_spec_version: 3,
  source_status_on_create: 'draft',
  target_status_on_create: 'approved',
  target_version_on_create: 2,
  removed_at_spec_version: null,
  resolved_on_create: false,
  active: true,
  removed_at: null,
  removed_by: null,
  removed_by_type: null,
  removed_by_name: null,
  removal_reason: null,
  direction: 'depends_on',
  related_spec: {
    id: 'spec-2',
    title: 'Platform prerequisite',
    status: 'approved',
    edition: 1,
    version: 3,
    archived: false,
  },
  satisfied: false,
  retrospective: false,
  lineage: 'same_ideation',
  capabilities: {
    can_remove: true,
    can_navigate: true,
    remove_reason_code: null,
  },
};

function dependencyPage(items = [dependencyItem]): SpecDependencyPage {
  return {
    items,
    direction: 'depends_on',
    total: items.length,
    has_more: false,
    readiness,
  };
}

function transitionEnvelope() {
  return {
    board_id: 'board-1',
    entity_type: 'spec',
    entity_id: 'spec-1',
    current_status: 'validated',
    source: 'core_sdlc_registry_v1',
    allowed_transitions: [
      {
        to_status: 'in_progress',
        label: 'In Progress',
        gate: 'validated_to_in_progress',
        blocked_reason: 'Two prerequisites are unfinished.',
        blocked_facts: {
          spec_id: 'spec-1',
          blocking_count: 2,
        },
        preconditions: [],
        capabilities: [],
        effects: [],
        reason_codes: ['spec_dependencies_incomplete'],
        policy_compliance: false,
        policy_compliance_decision: null,
      },
    ],
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((fulfill) => {
    resolve = fulfill;
  });
  return { promise, resolve };
}

function renderModal(onChanged = vi.fn()) {
  return render(
    <SpecModal
      specId={spec.id}
      boardId={spec.board_id}
      onClose={vi.fn()}
      onChanged={onChanged}
    />,
  );
}

describe('SpecModal Dependencies workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionMock.denied.clear();
    apiMock.getSpec.mockResolvedValue(spec);
    apiMock.getAllowedTransitions.mockResolvedValue(transitionEnvelope());
    apiMock.listSprints.mockResolvedValue([]);
    apiMock.listSpecDependencies.mockResolvedValue(dependencyPage());
    apiMock.lookupSpecs.mockResolvedValue({
      items: [{ id: 'spec-3', title: 'Done prerequisite', status: 'done' }],
      total: 1,
      offset: 0,
      limit: 50,
    });
    apiMock.addSpecDependency.mockResolvedValue({
      operation: 'added',
      dependency: dependencyItem,
      spec_version: 5,
      replayed: false,
    });
    apiMock.removeSpecDependency.mockResolvedValue({
      operation: 'removed',
      dependency: { ...dependencyItem, active: false },
      spec_version: 5,
      replayed: false,
    });
  });

  it('uses context readiness in the header without eagerly listing rows', async () => {
    renderModal();

    await screen.findByText(spec.title);
    expect(apiMock.listSpecDependencies).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'In Progress' })).toBeDisabled();
    expect(screen.getByRole('tab', { name: /Dependencies/ })).toHaveTextContent('2');

    fireEvent.click(screen.getByRole('button', { name: '2 dependencies unfinished' }));
    await waitFor(() => expect(apiMock.listSpecDependencies).toHaveBeenCalledTimes(1));
    expect(apiMock.listSpecDependencies).toHaveBeenCalledWith(
      'board-1',
      'spec-1',
      expect.objectContaining({
        direction: 'depends_on',
        satisfaction: 'all',
        active_state: 'active',
        lineage: 'all',
        limit: 25,
        signal: expect.any(AbortSignal),
      }),
    );
    expect(await screen.findByText('Platform prerequisite')).toBeInTheDocument();
    expect(screen.getAllByText('Unfinished')).toHaveLength(2);
    expect(screen.getAllByText('Same ideation')).toHaveLength(2);
    expect(screen.getByText('Introduced in Spec v3')).toBeInTheDocument();
  });

  it('ignores an older dependency reload after a newer manual reload resolves first', async () => {
    const dependencyReload = deferred<Spec>();
    const manualReload = deferred<Spec>();
    const staleSnapshot = {
      ...spec,
      title: 'Stale dependency snapshot',
      version: 5,
    };
    const latestSnapshot = {
      ...spec,
      ideation_id: null,
      title: 'Latest manual snapshot',
      version: 6,
    };
    const onChanged = vi.fn();
    apiMock.getSpec
      .mockResolvedValueOnce(spec)
      .mockReturnValueOnce(dependencyReload.promise)
      .mockReturnValueOnce(manualReload.promise);

    renderModal(onChanged);
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');
    fireEvent.click(screen.getByRole('button', { name: 'Add dependency' }));
    const addDialog = await screen.findByRole('dialog', { name: 'Add dependency' });
    fireEvent.click(await within(addDialog).findByRole('radio', { name: /Done prerequisite/ }));
    fireEvent.click(within(addDialog).getByRole('button', { name: 'Add dependency' }));
    await waitFor(() => expect(apiMock.getSpec).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(apiMock.getSpec).toHaveBeenCalledTimes(3));
    await act(async () => {
      manualReload.resolve(latestSnapshot);
      await manualReload.promise;
    });
    expect(await screen.findByText(latestSnapshot.title)).toBeInTheDocument();

    await act(async () => {
      dependencyReload.resolve(staleSnapshot);
      await dependencyReload.promise;
    });
    await waitFor(() => {
      expect(screen.getByText(latestSnapshot.title)).toBeInTheDocument();
      expect(screen.queryByText(staleSnapshot.title)).not.toBeInTheDocument();
      expect(onChanged).not.toHaveBeenCalled();
      expect(apiMock.getAllowedTransitions).toHaveBeenCalledTimes(2);
      expect(apiMock.listSprints).toHaveBeenCalledTimes(2);
    });
  });

  it('keeps separate direction filters and pages incoming relationships lazily', async () => {
    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');

    fireEvent.change(screen.getByLabelText('Lifecycle'), { target: { value: 'all' } });
    await waitFor(() => expect(apiMock.listSpecDependencies).toHaveBeenLastCalledWith(
      'board-1',
      'spec-1',
      expect.objectContaining({ direction: 'depends_on', active_state: 'all' }),
    ));

    const dependsOnTab = screen.getByRole('tab', { name: /Depends on/ });
    const requiredByTab = screen.getByRole('tab', { name: /Required by/ });
    dependsOnTab.focus();
    fireEvent.keyDown(dependsOnTab, { key: 'ArrowRight' });
    expect(requiredByTab).toHaveFocus();
    expect(requiredByTab).toHaveAttribute('aria-selected', 'false');
    fireEvent.keyDown(requiredByTab, { key: 'Enter' });
    await waitFor(() => expect(apiMock.listSpecDependencies).toHaveBeenLastCalledWith(
      'board-1',
      'spec-1',
      expect.objectContaining({ direction: 'required_by', active_state: 'active' }),
    ));
    expect(screen.getByLabelText('Lifecycle')).toHaveValue('active');
    fireEvent.change(screen.getByLabelText('Related Spec status'), {
      target: { value: 'done' },
    });
    await waitFor(() => expect(apiMock.listSpecDependencies).toHaveBeenLastCalledWith(
      'board-1',
      'spec-1',
      expect.objectContaining({
        direction: 'required_by',
        related_statuses: ['done'],
      }),
    ));

    fireEvent.click(screen.getByRole('tab', { name: /Depends on/ }));
    expect(screen.getByLabelText('Lifecycle')).toHaveValue('all');
    expect(screen.getByLabelText('Related Spec status')).toHaveValue('');
  });

  it('distinguishes an archived Done prerequisite from an unfinished prerequisite', async () => {
    const archivedReadiness: SpecDependencyReadiness = {
      ...readiness,
      active_dependency_count: 7,
      unmet_count: 7,
      blocking_count: 7,
      archived_blocking_count: 4,
      unfinished_blocking_count: 3,
      blockers_truncated: true,
      blockers: [{
        ...readiness.blockers[0],
        target_status: 'done',
        target_archived: true,
      }],
    };
    const archivedDependency: SpecDependencyItem = {
      ...dependencyItem,
      satisfied: false,
      related_spec: {
        ...dependencyItem.related_spec,
        status: 'done',
        archived: true,
      },
      capabilities: {
        ...dependencyItem.capabilities,
        can_navigate: false,
      },
    };
    apiMock.getSpec.mockResolvedValue({
      ...spec,
      dependency_readiness: archivedReadiness,
    });
    apiMock.listSpecDependencies.mockResolvedValue({
      ...dependencyPage([archivedDependency]),
      readiness: archivedReadiness,
    });

    const { container } = renderModal();
    await screen.findByText(spec.title);
    expect(screen.getByRole('button', { name: '7 dependencies blocked · 4 archived' }))
      .toHaveAttribute(
        'title',
        'Restore archived prerequisites or remove their dependencies before starting.',
      );

    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    const archivedTitle = await screen.findByText('Platform prerequisite');
    const archivedRow = archivedTitle.closest('article');
    expect(archivedRow).not.toBeNull();
    expect(screen.getByText('Start is blocked by archived or unfinished dependencies'))
      .toBeInTheDocument();
    expect(screen.getByText(/4 archived prerequisites must be restored or removed/))
      .toBeInTheDocument();
    expect(screen.getByText(/3 other blockers still need attention/)).toBeInTheDocument();
    expect(screen.getByText(/rows below show only a bounded sample of blockers/))
      .toBeInTheDocument();
    expect(within(archivedRow!).getByText('Archived')).toBeInTheDocument();
    expect(within(archivedRow!).getByText('Restore required')).toBeInTheDocument();
    expect(within(archivedRow!).queryByText('Satisfied')).not.toBeInTheDocument();

    const result = await axe.run(container);
    expect(result.violations.filter((violation) => (
      violation.impact === 'serious' || violation.impact === 'critical'
    ))).toEqual([]);
  }, 10_000);

  it('distinguishes duplicate and idempotency conflicts from version conflicts', async () => {
    apiMock.addSpecDependency
      .mockRejectedValueOnce(new AuthenticatedFetchError({
        message: 'duplicate',
        status: 409,
        code: 'spec_dependency_state_conflict',
        details: { facts: { conflict_kind: 'active_duplicate' } },
      }))
      .mockRejectedValueOnce(new AuthenticatedFetchError({
        message: 'idempotency conflict',
        status: 409,
        code: 'spec_dependency_state_conflict',
        details: { facts: { conflict_kind: 'idempotency_key_reuse' } },
      }));

    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');
    fireEvent.click(screen.getByRole('button', { name: 'Add dependency' }));
    const addDialog = await screen.findByRole('dialog', { name: 'Add dependency' });
    fireEvent.click(await within(addDialog).findByRole('radio', { name: /Done prerequisite/ }));
    const submit = within(addDialog).getByRole('button', { name: 'Add dependency' });

    fireEvent.click(submit);
    expect(await within(addDialog).findByText(
      'This prerequisite is already an active dependency.',
    )).toBeInTheDocument();

    fireEvent.click(submit);
    expect(await within(addDialog).findByText(
      'This request key was already used for a different dependency operation. Close the dialog and try again.',
    )).toBeInTheDocument();
  });

  it('refreshes the edition fence after validated-to-draft reentry without changing the idempotency key', async () => {
    const freshSpec = {
      ...spec,
      status: 'draft' as const,
      version: spec.version,
      edition: spec.edition + 1,
      dependency_readiness: {
        ...readiness,
        current_edition: readiness.current_edition + 1,
        active_dependency_count: 1,
        unmet_count: 1,
        blocking_count: 1,
        archived_blocking_count: 0,
        unfinished_blocking_count: 1,
        blockers_truncated: false,
      },
    };
    apiMock.getSpec
      .mockResolvedValueOnce(spec)
      .mockResolvedValue(freshSpec);
    apiMock.addSpecDependency
      .mockRejectedValueOnce(new AuthenticatedFetchError({
        message: 'Spec lifecycle edition changed after the dependency form was loaded.',
        status: 409,
        code: 'spec_dependency_state_conflict',
        details: {
          facts: {
            expected_spec_edition: spec.edition,
            current_spec_edition: freshSpec.edition,
          },
        },
      }))
      .mockResolvedValueOnce({
        dependency: dependencyItem,
        spec_version: 6,
        replayed: false,
      });

    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');
    fireEvent.click(screen.getByRole('button', { name: 'Add dependency' }));
    const addDialog = await screen.findByRole('dialog', { name: 'Add dependency' });
    fireEvent.click(await within(addDialog).findByRole('radio', { name: /Done prerequisite/ }));
    const submit = within(addDialog).getByRole('button', { name: 'Add dependency' });
    fireEvent.click(submit);

    expect(await within(addDialog).findByText(
      'Spec lifecycle edition changed after the dependency form was loaded.',
    )).toBeInTheDocument();
    expect(addDialog).toBeInTheDocument();
    await waitFor(() => expect(apiMock.getSpec).toHaveBeenCalledTimes(2));
    const firstRequest = apiMock.addSpecDependency.mock.calls[0][2];
    expect(firstRequest).toEqual({
      prerequisite_spec_id: 'spec-3',
      expected_spec_version: spec.version,
      expected_spec_edition: spec.edition,
      idempotency_key: expect.any(String),
    });

    fireEvent.click(submit);
    await waitFor(() => expect(apiMock.addSpecDependency).toHaveBeenCalledTimes(2));
    expect(apiMock.addSpecDependency.mock.calls[1][2]).toEqual({
      prerequisite_spec_id: 'spec-3',
      expected_spec_version: spec.version,
      expected_spec_edition: freshSpec.edition,
      idempotency_key: firstRequest.idempotency_key,
    });
  });

  it('submits add with CAS/idempotency and requires a reason before removal', async () => {
    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');

    fireEvent.click(screen.getByRole('button', { name: 'Add dependency' }));
    const addDialog = await screen.findByRole('dialog', { name: 'Add dependency' });
    fireEvent.click(await within(addDialog).findByRole('radio', { name: /Done prerequisite/ }));
    apiMock.getSpec.mockResolvedValue({ ...spec, version: 5 });
    fireEvent.click(within(addDialog).getByRole('button', { name: 'Add dependency' }));
    await waitFor(() => expect(apiMock.addSpecDependency).toHaveBeenCalledWith(
      'board-1',
      'spec-1',
      {
        prerequisite_spec_id: 'spec-3',
        expected_spec_version: 4,
        expected_spec_edition: 2,
        idempotency_key: expect.any(String),
      },
    ));

    const removeButton = await screen.findByRole('button', { name: 'Remove' });
    removeButton.focus();
    fireEvent.click(removeButton);
    let removeDialog = await screen.findByRole('alertdialog', { name: 'Remove dependency' });
    await waitFor(() => expect(
      within(removeDialog).getByRole('button', { name: 'Close remove dependency dialog' }),
    ).toHaveFocus());
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole(
      'alertdialog',
      { name: 'Remove dependency' },
    )).not.toBeInTheDocument());
    expect(removeButton).toHaveFocus();

    fireEvent.click(removeButton);
    removeDialog = await screen.findByRole('alertdialog', { name: 'Remove dependency' });
    const confirmRemoval = within(removeDialog).getByRole('button', { name: 'Remove dependency' });
    expect(confirmRemoval).toBeDisabled();
    fireEvent.change(within(removeDialog).getByLabelText(/Removal reason/), {
      target: { value: 'This prerequisite was superseded.' },
    });
    fireEvent.click(confirmRemoval);
    await waitFor(() => expect(apiMock.removeSpecDependency).toHaveBeenCalledWith(
      'board-1',
      'spec-1',
      'dependency-1',
      {
        expected_spec_version: 5,
        expected_spec_edition: 2,
        idempotency_key: expect.any(String),
        reason: 'This prerequisite was superseded.',
      },
    ));
    await waitFor(() => expect(screen.queryByRole(
      'alertdialog',
      { name: 'Remove dependency' },
    )).not.toBeInTheDocument());
    expect(screen.getByRole('heading', { name: 'Spec dependencies' })).toHaveFocus();
  });

  it('withholds dependency mutation affordances without interact_in for the current Spec state', async () => {
    permissionMock.denied.add('spec.interact_in.validated');

    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');

    const addButton = screen.getByRole('button', { name: 'Add dependency' });
    expect(addButton).toBeDisabled();
    expect(addButton).toHaveAttribute(
      'title',
      'Adding dependencies is not authorized.',
    );
    fireEvent.click(addButton);
    expect(apiMock.lookupSpecs).not.toHaveBeenCalled();

    const removeButton = screen.getByRole('button', { name: 'Remove' });
    expect(removeButton).toBeDisabled();
    expect(removeButton).toHaveAttribute(
      'title',
      'Removal is not authorized for this dependency.',
    );
    fireEvent.click(removeButton);
    expect(screen.queryByRole('alertdialog', { name: 'Remove dependency' }))
      .not.toBeInTheDocument();
  });

  it('disables adding from an archived source and explains capability denials', async () => {
    apiMock.getSpec.mockResolvedValue({ ...spec, archived: true });
    apiMock.listSpecDependencies.mockResolvedValue(dependencyPage([{
      ...dependencyItem,
      capabilities: {
        ...dependencyItem.capabilities,
        can_remove: false,
        remove_reason_code: 'source_archived',
      },
    }]));

    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');

    const addButton = screen.getByRole('button', { name: 'Add dependency' });
    expect(addButton).toBeDisabled();
    expect(addButton).toHaveAttribute(
      'title',
      'Restore this Spec before adding dependencies.',
    );
    expect(addButton).toHaveAttribute(
      'aria-describedby',
      'spec-spec-1-dependency-add-disabled-reason',
    );
    expect(screen.getByText('Restore this Spec before adding dependencies.'))
      .toBeInTheDocument();
    fireEvent.click(addButton);
    expect(apiMock.lookupSpecs).not.toHaveBeenCalled();

    const removeButton = screen.getByRole('button', { name: 'Remove' });
    expect(removeButton).toBeDisabled();
    expect(removeButton).toHaveAttribute(
      'title',
      'Restore the dependent Spec before removing this dependency.',
    );
    expect(removeButton).toHaveAttribute(
      'aria-describedby',
      'dependency-dependency-1-remove-disabled-reason',
    );
    expect(screen.getByText('Restore the dependent Spec before removing this dependency.'))
      .toBeInTheDocument();
  });

  it('keeps removed dependencies legible as lifecycle history with the removal Spec version', async () => {
    apiMock.listSpecDependencies.mockResolvedValue(dependencyPage([{
      ...dependencyItem,
      active: false,
      removed_at_spec_version: 5,
      removed_at: '2026-08-12T12:30:00Z',
      removed_by: 'agent-1',
      removed_by_type: 'agent',
      removed_by_name: 'Codex',
      removal_reason: 'The prerequisite was superseded.',
      capabilities: {
        ...dependencyItem.capabilities,
        can_remove: false,
        remove_reason_code: 'dependency_removed',
      },
    }]));

    renderModal();
    await screen.findByText(spec.title);
    fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
    await screen.findByText('Platform prerequisite');

    expect(screen.getByText(/Removed in Spec v5 ·/)).toBeInTheDocument();
    expect(screen.getByText('The prerequisite was superseded.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'View lifecycle details' }));
    expect(screen.getByText(/Spec v5 · .* by Codex/)).toBeInTheDocument();
  });

  it.each(['light', 'dark'] as const)(
    'has no serious or critical accessibility violations in %s mode',
    async (theme) => {
      const { container } = render(
        <div className={theme === 'dark' ? 'dark bg-surface-950' : 'bg-white'}>
          <SpecModal
            specId={spec.id}
            boardId={spec.board_id}
            onClose={vi.fn()}
            onChanged={vi.fn()}
          />
        </div>,
      );
      await screen.findByText(spec.title);
      fireEvent.click(screen.getByRole('tab', { name: /Dependencies/ }));
      await screen.findByText('Platform prerequisite');

      const result = await axe.run(container);
      expect(result.violations.filter((violation) => (
        violation.impact === 'serious' || violation.impact === 'critical'
      ))).toEqual([]);
    },
  );
});
