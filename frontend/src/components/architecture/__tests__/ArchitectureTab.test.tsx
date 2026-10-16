import { workspaceItem, workspacePage } from '@/testFixtures/effectiveResources';
import { useState } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ArchitectureTab, ArchitectureValidationPanel } from '../ArchitectureTab';
import type { ArchitectureDesign, ArchitectureDesignSummary, ScreenMockup } from '@/types';

const apiMock = vi.hoisted(() => ({
  listArchitectureDesigns: vi.fn(),
  getArchitectureDesign: vi.fn(),
  createArchitectureDesign: vi.fn(),
  updateArchitectureDesign: vi.fn(),
  validateArchitectureDesign: vi.fn(),
  deleteArchitectureDesign: vi.fn(),
  copyArchitectureToCard: vi.fn(),
  importExcalidrawArchitectureDiagram: vi.fn(),
  getEffectiveResources: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const summary: ArchitectureDesignSummary = {
  id: 'arch-1',
  board_id: 'board-1',
  parent_type: 'ideation',
  parent_id: 'ideation-1',
  title: 'Runtime Architecture',
  version: 1,
  source_ref: null,
  source_version: null,
  source_design_id: null,
  stale: false,
  breaking_change_flag: false,
  requires_arch_review: false,
  diagrams_count: 1,
  adapter_payload_refs: ['payload-1'],
  created_at: '2026-04-30T00:00:00Z',
  updated_at: '2026-04-30T00:00:00Z',
};

const design: ArchitectureDesign = {
  ...summary,
  global_description: 'Single-user architecture editing.',
  entities: [],
  interfaces: [],
  diagrams: [
    {
      id: 'diag-1',
      title: 'Context',
      diagram_type: 'context',
      format: 'excalidraw_json',
      adapter_payload_ref: 'payload-1',
      adapter_payload: {
        type: 'excalidraw',
        version: 2,
        elements: [],
        appState: {},
        files: {},
      },
      description: null,
      order_index: 0,
    },
  ],
  created_by: 'user-1',
};

function InlineOnChangedWrapper() {
  const [, setItems] = useState<ArchitectureDesignSummary[]>([]);

  return (
    <ArchitectureTab
      parentType="ideation"
      parentId="ideation-1"
      onChanged={(next) => setItems(next)}
    />
  );
}

describe('ArchitectureTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.listArchitectureDesigns.mockResolvedValue([summary]);
    apiMock.getArchitectureDesign.mockResolvedValue(design);
    apiMock.validateArchitectureDesign.mockResolvedValue({
      valid: true,
      issues: [],
      warnings: [],
      suggested_fixes: [],
      summary: {},
    });
    apiMock.getEffectiveResources.mockImplementation((boardId, entityType, entityId, options) =>
    Promise.resolve(workspacePage(boardId, entityType, entityId, options.resource_type, [], options.profile)));
  });

  it('keeps the authored draft after an external version and requires review before saving', async () => {
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={0} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Local intent' } });
    const changed = { ...design, title: 'Another writer', version: 2 };
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, title: changed.title, version: 2 }]);
    apiMock.getArchitectureDesign.mockResolvedValue(changed);
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={1} />);
    await screen.findByRole('button', { name: 'Review architecture base' });
    expect(screen.getByLabelText('Name')).toHaveValue('Local intent');
    expect(screen.getByRole('button', { name: /^Save$/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review architecture base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Keep draft on reviewed base' }));
    apiMock.updateArchitectureDesign.mockResolvedValue({ ...changed, title: 'Local intent', version: 3 });
    apiMock.getArchitectureDesign.mockResolvedValue({ ...changed, title: 'Local intent', version: 3 });
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }));
    await waitFor(() => expect(apiMock.updateArchitectureDesign).toHaveBeenCalledWith('arch-1', expect.objectContaining({
      expected_version: 2, title: 'Local intent',
    })));
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('v3'));
  }, 15000);

  it('preserves a dirty selected design when the authoritative list removes it', async () => {
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={0} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Unsaved removed design' } });
    apiMock.listArchitectureDesigns.mockResolvedValue([]);
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={1} />);
    await screen.findByText('This architecture is no longer in the current resource list. Your draft has not been saved.');
    expect(screen.getByLabelText('Name')).toHaveValue('Unsaved removed design');
    expect(screen.getByRole('button', { name: /^Save$/ })).toBeDisabled();
    expect(apiMock.updateArchitectureDesign).not.toHaveBeenCalled();
  });

  it('recovers only the read after a confirmed save whose detail request fails', async () => {
    const onChanged = vi.fn();
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" onChanged={onChanged} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Confirmed intent' } });
    apiMock.updateArchitectureDesign.mockResolvedValue({ ...design, title: 'Confirmed intent', version: 2 });
    apiMock.getArchitectureDesign.mockRejectedValueOnce(new Error('Detail read unavailable'));
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }));
    await screen.findByRole('button', { name: 'Reload saved architecture' });
    expect(screen.getByRole('button', { name: /^Save$/ })).toBeDisabled();
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, title: 'Confirmed intent', version: 2 });
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, title: 'Confirmed intent', version: 2 }]);
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved architecture' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Reload saved architecture' })).not.toBeInTheDocument());
    expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1);
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Version')).toHaveValue('v2');
  }, 15000);

  it('loads architecture once when parent passes an inline onChanged callback', async () => {
    render(<InlineOnChangedWrapper />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledTimes(1));

    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });

    expect(apiMock.listArchitectureDesigns).toHaveBeenCalledTimes(1);
    expect(apiMock.getArchitectureDesign).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Stale')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Breaking')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Review')).not.toBeInTheDocument();
  });

  it('recovers a confirmed create without another POST even when its version is below the old selection', async () => {
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, version: 3 }]);
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, version: 3 });
    const onChanged = vi.fn().mockRejectedValueOnce(new Error('Parent read unavailable')).mockResolvedValue(undefined);
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" onChanged={onChanged} />);
    await screen.findByDisplayValue(design.title);
    expect(screen.getByLabelText('Version')).toHaveValue('v3');
    fireEvent.click(screen.getByText('New', { selector: 'button' }));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Created design' } });
    fireEvent.change(screen.getByLabelText('Global description'), { target: { value: 'New authoring fixture' } });
    const created = { ...design, id: 'created-design', title: 'Created design', version: 1 };
    apiMock.createArchitectureDesign.mockResolvedValue(created);
    apiMock.getArchitectureDesign.mockResolvedValue(created);
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, ...created }]);
    fireEvent.click(screen.getByText('Create', { selector: 'button' }));
    await screen.findByText('Reload saved architecture', { selector: 'button' });
    await screen.findByText('Parent read unavailable');
    expect(screen.getByText('Create', { selector: 'button' })).toBeDisabled();
    fireEvent.click(screen.getByText('Reload saved architecture', { selector: 'button' }));
    await waitFor(() => expect(screen.queryByText('Reload saved architecture', { selector: 'button' })).not.toBeInTheDocument());
    expect(apiMock.createArchitectureDesign).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Name')).toHaveValue('Created design');
    expect(onChanged).toHaveBeenCalledTimes(2);
  }, 15000);

  it('keeps an uncertain creation blocked until the resource list is inspected and the attempt discarded', async () => {
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);
    await screen.findByDisplayValue(design.title);
    fireEvent.click(screen.getByText('New', { selector: 'button' }));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Uncertain creation' } });
    fireEvent.change(screen.getByLabelText('Global description'), { target: { value: 'Local draft' } });
    apiMock.createArchitectureDesign.mockRejectedValueOnce(new Error('Response lost'));
    fireEvent.click(screen.getByText('Create', { selector: 'button' }));
    await screen.findByText('Reload current architecture', { selector: 'button' });
    expect(screen.getByLabelText('Title')).toHaveValue('Uncertain creation');
    expect(screen.getByText('Create', { selector: 'button' })).toBeDisabled();
    expect(screen.getByText('I reviewed resources; discard local attempt')).toBeDisabled();
    fireEvent.click(screen.getByText('Reload current architecture', { selector: 'button' }));
    await waitFor(() => expect(screen.getByText('I reviewed resources; discard local attempt')).not.toBeDisabled());
    expect(apiMock.createArchitectureDesign).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('I reviewed resources; discard local attempt'));
    expect(screen.queryByLabelText('Title')).not.toBeInTheDocument();
    expect(apiMock.createArchitectureDesign).toHaveBeenCalledTimes(1);
  }, 15000);

  it('recovers only the list after a confirmed delete and sends the reviewed version', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);
      await screen.findByDisplayValue(design.title);
      apiMock.deleteArchitectureDesign.mockResolvedValue(undefined);
      apiMock.listArchitectureDesigns.mockRejectedValueOnce(new Error('List read unavailable')).mockResolvedValue([]);
      fireEvent.click(screen.getByText('Delete', { selector: 'button' }));
      await screen.findByText('Reload after deletion', { selector: 'button' });
      expect(apiMock.deleteArchitectureDesign).toHaveBeenCalledWith('arch-1', 1);
      fireEvent.click(screen.getByText('Reload after deletion', { selector: 'button' }));
      await waitFor(() => expect(screen.queryByText('Reload after deletion', { selector: 'button' })).not.toBeInTheDocument());
      expect(apiMock.deleteArchitectureDesign).toHaveBeenCalledTimes(1);
      expect(screen.queryByDisplayValue(design.title)).not.toBeInTheDocument();
    } finally { confirm.mockRestore(); }
  }, 15000);

  it('preserves import input across an external change and imports once on the explicitly reviewed base', async () => {
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={0} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.click(screen.getByTitle('Import Excalidraw'));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Local import' } });
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, version: 2 }]);
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, version: 2 });
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={1} />);
    await screen.findByText('Use reviewed architecture base for import', { selector: 'button' });
    expect(screen.getByLabelText('Title')).toHaveValue('Local import');
    expect(screen.getByText('Import', { selector: 'button' })).toBeDisabled();
    fireEvent.click(screen.getByText('Use reviewed architecture base for import', { selector: 'button' }));
    const imported = { ...design, version: 3 };
    apiMock.importExcalidrawArchitectureDiagram.mockResolvedValue(imported);
    apiMock.getArchitectureDesign.mockRejectedValueOnce(new Error('Imported detail read unavailable'));
    fireEvent.click(screen.getByText('Import', { selector: 'button' }));
    await screen.findByText('Imported detail read unavailable');
    expect(apiMock.importExcalidrawArchitectureDiagram).toHaveBeenCalledWith('arch-1', expect.objectContaining({
      title: 'Local import', expected_version: 2,
    }));
    apiMock.getArchitectureDesign.mockResolvedValue(imported);
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, version: 3 }]);
    fireEvent.click(screen.getByText('Reload saved architecture', { selector: 'button' }));
    await waitFor(() => expect(screen.queryByText('Reload saved architecture', { selector: 'button' })).not.toBeInTheDocument());
    expect(apiMock.importExcalidrawArchitectureDiagram).toHaveBeenCalledTimes(1);
  }, 15000);

  it('discards late creation results after a parent identity change', async () => {
    let resolveCreate!: (value: ArchitectureDesign) => void;
    apiMock.createArchitectureDesign.mockImplementationOnce(() => new Promise(resolve => { resolveCreate = resolve; }));
    const onChanged = vi.fn();
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" onChanged={onChanged} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.click(screen.getByText('New', { selector: 'button' }));
    fireEvent.change(screen.getByLabelText('Global description'), { target: { value: 'Old parent creation' } });
    fireEvent.click(screen.getByText('Create', { selector: 'button' }));
    const other = { ...design, id: 'arch-other', parent_id: 'ideation-other', title: 'Other parent' };
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, ...other }]);
    apiMock.getArchitectureDesign.mockResolvedValue(other);
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-other" onChanged={onChanged} />);
    await screen.findByDisplayValue('Other parent');
    await act(async () => resolveCreate({ ...design, id: 'late-create', version: 1 }));
    expect(screen.getByLabelText('Name')).toHaveValue('Other parent');
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('retains an uncommitted entity form across an external revision', async () => {
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={0} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.click(screen.getAllByText('Add', { selector: 'button' })[0]);
    fireEvent.change(screen.getByLabelText('Entity name'), { target: { value: 'Uncommitted component' } });
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, version: 2 }]);
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, version: 2 });
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={1} />);
    await screen.findByText('Review architecture base', { selector: 'button' });
    expect(screen.getByLabelText('Entity name')).toHaveValue('Uncommitted component');
    expect(screen.getByLabelText('Version')).toHaveValue('v1');
    expect(screen.getByText('Save', { selector: 'button' })).toBeDisabled();
  });

  it('does not retry an unconfirmed write or infer success from matching content', async () => {
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);
    await screen.findByDisplayValue(design.title);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Uncertain intent' } });
    apiMock.updateArchitectureDesign.mockRejectedValueOnce(new Error('Response lost'));
    fireEvent.click(screen.getByText('Save', { selector: 'button' }));
    await screen.findByText('Reload current architecture', { selector: 'button' });
    expect(screen.getByText('Save', { selector: 'button' })).toBeDisabled();
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, version: 2, title: 'Uncertain intent' }]);
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, version: 2, title: 'Uncertain intent' });
    fireEvent.click(screen.getByText('Reload current architecture', { selector: 'button' }));
    await waitFor(() => expect(apiMock.getArchitectureDesign.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByText('Save', { selector: 'button' })).toBeDisabled();
    expect(screen.getByText(/The save response was not confirmed/)).toBeInTheDocument();
    expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1);
  });

  it('ignores a confirmed save received after architecture access was denied', async () => {
    let resolveSave!: (value: ArchitectureDesign) => void;
    apiMock.updateArchitectureDesign.mockImplementationOnce(() => new Promise(resolve => { resolveSave = resolve; }));
    const onChanged = vi.fn();
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={0} onChanged={onChanged} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Protected intent' } });
    fireEvent.click(screen.getByText('Save', { selector: 'button' }));
    apiMock.listArchitectureDesigns.mockRejectedValueOnce(Object.assign(new Error('Denied'), { status: 403 }));
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-1" revision={1} onChanged={onChanged} />);
    await screen.findByText('Architecture access is no longer available.');
    await act(async () => resolveSave({ ...design, title: 'Protected intent', version: 2 }));
    expect(screen.queryByDisplayValue('Protected intent')).not.toBeInTheDocument();
    expect(screen.queryByText('Reload saved architecture', { selector: 'button' })).not.toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('does not publish a late save into another parent workspace', async () => {
    let resolveSave!: (value: ArchitectureDesign) => void;
    apiMock.updateArchitectureDesign.mockImplementationOnce(() => new Promise(resolve => { resolveSave = resolve; }));
    const onChanged = vi.fn();
    const view = render(<ArchitectureTab parentType="ideation" parentId="ideation-1" onChanged={onChanged} />);
    await screen.findByDisplayValue(design.title);
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Old workspace intent' } });
    fireEvent.click(screen.getByText('Save', { selector: 'button' }));
    const other = { ...design, id: 'arch-other', parent_id: 'ideation-other', title: 'Other workspace' };
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, ...other }]);
    apiMock.getArchitectureDesign.mockResolvedValue(other);
    view.rerender(<ArchitectureTab parentType="ideation" parentId="ideation-other" onChanged={onChanged} />);
    await screen.findByDisplayValue('Other workspace');
    await act(async () => resolveSave({ ...design, title: 'Old workspace intent', version: 2 }));
    expect(screen.getByLabelText('Name')).toHaveValue('Other workspace');
    expect(screen.queryByText('Reload saved architecture', { selector: 'button' })).not.toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('does not notify onChanged for a pure read so the parent modal never remounts it', async () => {
    // Regression: notifying onChanged on every read made the parent modal
    // reload and flip its loading flag, remounting this tab and re-reading in
    // an infinite loop across spec/refinement/ideation/task modals.
    const onChanged = vi.fn();
    render(
      <ArchitectureTab
        parentType="ideation"
        parentId="ideation-1"
        onChanged={onChanged}
      />,
    );

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });

    expect(apiMock.listArchitectureDesigns).toHaveBeenCalledTimes(1);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('renders card architecture as a read-only snapshot with copy refresh', async () => {
    apiMock.listArchitectureDesigns.mockResolvedValue([
      { ...summary, parent_type: 'card', parent_id: 'card-1' },
    ]);
    apiMock.getArchitectureDesign.mockResolvedValue({
      ...design,
      parent_type: 'card',
      parent_id: 'card-1',
      source_design_id: 'source-arch-1',
    });

    render(<ArchitectureTab parentType="card" parentId="card-1" specIdForCopy="spec-1" />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    expect(screen.getByText('Card architecture snapshots are read-only')).toBeInTheDocument();
    expect(screen.getByTitle('Copy from spec')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /New/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Save/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Delete/i })).not.toBeInTheDocument();
  });

  it('refreshes a visible Card snapshot on revision without notifying a mutation', async () => {
    const onChanged = vi.fn();
    const view = render(<ArchitectureTab parentType="card" parentId="card-1" revision={0} onChanged={onChanged} />);
    await screen.findByText('Runtime Architecture');
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, title: 'Updated snapshot', version: 2 }]);
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, title: 'Updated snapshot', version: 2 });
    view.rerender(<ArchitectureTab parentType="card" parentId="card-1" revision={1} onChanged={onChanged} />);
    await screen.findByText('Updated snapshot');
    expect(onChanged).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /^Save$/ })).not.toBeInTheDocument();
  });

  it('defers Card architecture list and effective-resource reads while hidden', async () => {
    const view = render(<ArchitectureTab parentType="card" parentId="card-1" boardId="board-1" active={false} revision={0} />);
    await act(async () => {});
    expect(apiMock.listArchitectureDesigns).not.toHaveBeenCalled();
    expect(apiMock.getEffectiveResources).not.toHaveBeenCalled();
    view.rerender(<ArchitectureTab parentType="card" parentId="card-1" boardId="board-1" active revision={2} />);
    await screen.findByText('Runtime Architecture');
    expect(apiMock.listArchitectureDesigns).toHaveBeenCalledTimes(1);
    expect(apiMock.getEffectiveResources).toHaveBeenCalledTimes(2);
  });

  it('ignores a late architecture list from another Card', async () => {
    let resolveOld!: (value: ArchitectureDesignSummary[]) => void;
    apiMock.listArchitectureDesigns.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
    const view = render(<ArchitectureTab parentType="card" parentId="card-1" />);
    apiMock.listArchitectureDesigns.mockResolvedValue([{ ...summary, title: 'Current snapshot' }]);
    apiMock.getArchitectureDesign.mockResolvedValue({ ...design, title: 'Current snapshot' });
    view.rerender(<ArchitectureTab parentType="card" parentId="card-2" />);
    await screen.findByText('Current snapshot');
    await act(async () => resolveOld([summary]));
    expect(screen.getByText('Current snapshot')).toBeInTheDocument();
    expect(screen.queryByText('Runtime Architecture')).not.toBeInTheDocument();
  });

  it('clears a Card snapshot after a denied list refresh', async () => {
    const view = render(<ArchitectureTab parentType="card" parentId="card-1" revision={0} />);
    await screen.findByText('Runtime Architecture');
    apiMock.listArchitectureDesigns.mockRejectedValue({ status: 403 });
    view.rerender(<ArchitectureTab parentType="card" parentId="card-1" revision={1} />);
    await screen.findByText('Architecture access is no longer available.');
    expect(screen.queryByText('Runtime Architecture')).not.toBeInTheDocument();
  });

  it('renders inherited effective architecture as read-only with provenance', async () => {
    const inheritedDesign: ArchitectureDesign = {
      ...design,
      id: 'arch-parent-1',
      parent_type: 'ideation',
      parent_id: 'idea-1',
      title: 'Parent Architecture',
      global_description: 'Inherited parent architecture.',
    };
    apiMock.listArchitectureDesigns.mockResolvedValue([]);
    apiMock.getEffectiveResources.mockImplementation((boardId, entityType, entityId, options) =>
      Promise.resolve(workspacePage(boardId, entityType, entityId, options.resource_type,
        options.resource_type === 'architecture' ? [workspaceItem('architecture', 'arch-parent-1', 'Parent Architecture', {
          inherited: true, attachment_kind: 'inherited_reference', body: inheritedDesign,
          provenance: { source_entity_type: 'ideation', source_entity_id: 'idea-1',
            source_entity_title: 'Source idea', origin_class: 'v2', source_revision: '1', source_content_sha256: null },
        })] : [], options.profile)));

    render(
      <ArchitectureTab
        parentType="refinement"
        parentId="refinement-1"
        boardId="board-1"
        entityType="refinement"
        entityId="refinement-1"
      />,
    );

    expect(await screen.findByText('Parent Architecture')).toBeInTheDocument();
    expect(screen.getByTestId('architecture-inherited-origin').textContent).toMatch(
      /Read-only inherited from ideation: Source idea/,
    );
    expect(screen.getByRole('button', { name: /New/i })).toBeInTheDocument();
    expect(screen.getByTitle('Import Excalidraw')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Save/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Delete/i })).not.toBeInTheDocument();
    expect(apiMock.getArchitectureDesign).not.toHaveBeenCalled();
  });

  it('shows labeled architecture fields and parent mockup screens', async () => {
    const mockups: ScreenMockup[] = [
      { id: 'mockup-1', title: 'Checkout page', description: null, screen_type: 'page', html_content: '', annotations: null, order: 0 },
    ];
    apiMock.getArchitectureDesign.mockResolvedValue({
      ...design,
      entities: [{ id: 'entity-1', name: 'Checkout API', entity_type: 'service' }],
      interfaces: [{ id: 'interface-1', name: 'Create payment', participants: ['entity-1'] }],
    });

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" screenMockups={mockups} />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    expect(await screen.findByLabelText('Diagram title')).toBeInTheDocument();
    expect(screen.getByText('Checkout API')).toBeInTheDocument();
    expect(screen.getByText('service')).toBeInTheDocument();
    expect(screen.getByText('Create payment')).toBeInTheDocument();
    expect(screen.getByText('Source -> Target')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Edit entity'));
    expect(screen.getByLabelText('Entity name')).toBeInTheDocument();
    expect(screen.getByLabelText('Color')).toBeInTheDocument();
    expect(screen.getByLabelText('Icon')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Edit interface'));
    expect(screen.getByLabelText('Interface name')).toBeInTheDocument();
    expect(screen.getByLabelText('Endpoint / operation')).toBeInTheDocument();
    expect(screen.queryByLabelText('Source entity')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Target entity')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Direction')).toBeInTheDocument();
    expect(screen.getByText('Checkout page')).toBeInTheDocument();
  });

  it('adds catalog components as entities linked to the selected diagram', async () => {
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    await waitFor(() => expect(screen.getByText('Runtime Architecture')).toBeInTheDocument());
    fireEvent.click(await screen.findByTitle('Add API component'));

    expect(screen.getAllByText('New API').length).toBeGreaterThan(0);
    expect(screen.getAllByText('API').length).toBeGreaterThan(0);
  });

  it('saves new catalog entities with an empty boundaries array', async () => {
    apiMock.updateArchitectureDesign.mockResolvedValue(design);
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);
    await screen.findByText('Runtime Architecture');
    fireEvent.click(await screen.findByTitle('Add API component'));
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1));
    expect(apiMock.updateArchitectureDesign.mock.calls[0][1].entities[0].boundaries).toEqual([]);
  });

  it('edits and saves boundaries as the same ordered array sent to validation', async () => {
    const initial = { ...design, entities: [{ id: 'checkout', name: 'Checkout API', entity_type: 'service', boundaries: ['Tenant data, isolated'] }] };
    apiMock.getArchitectureDesign.mockResolvedValue(initial);
    apiMock.updateArchitectureDesign.mockResolvedValue(initial);
    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);
    await screen.findByText('Checkout API');
    fireEvent.click(screen.getByTitle('Edit entity'));
    expect(screen.getByRole('textbox', { name: /Boundary 1/ })).toHaveValue('Tenant data, isolated');
    fireEvent.click(screen.getByRole('button', { name: 'Add boundary' }));
    fireEvent.change(screen.getByRole('textbox', { name: /Boundary 2/ }), { target: { value: 'Public ports\nNo private imports' } });
    const expected = ['Tenant data, isolated', 'Public ports\nNo private imports'];
    await waitFor(() => expect(apiMock.validateArchitectureDesign.mock.calls.some(([payload]) => JSON.stringify(payload.entities?.[0]?.boundaries) === JSON.stringify(expected))).toBe(true));
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1));
    expect(apiMock.updateArchitectureDesign.mock.calls[0][1].entities[0].boundaries).toEqual(expected);
    expect(apiMock.validateArchitectureDesign.mock.calls.some(([payload]) => JSON.stringify(payload.entities?.[0]?.boundaries) === JSON.stringify(expected))).toBe(true);
  });

  it('shows backend architecture design warnings in the UI', async () => {
    apiMock.validateArchitectureDesign.mockResolvedValue({
      valid: true,
      issues: [],
      warnings: ['entities[0].responsibility is empty. Clarify what this component owns.'],
      suggested_fixes: [],
      summary: {},
    });
    apiMock.getArchitectureDesign.mockResolvedValue({
      ...design,
      entities: [{ id: 'entity-1', name: 'Checkout API', entity_type: 'service' }],
    });

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    await waitFor(() => expect(apiMock.validateArchitectureDesign).toHaveBeenCalledTimes(1), { timeout: 1500 });
    expect(screen.getByText('Design review')).toBeInTheDocument();
    expect(screen.getByText('1 warning')).toBeInTheDocument();
    expect(screen.getByText('entities[0].responsibility is empty. Clarify what this component owns.')).toBeInTheDocument();
  });

  it('renders structured topology warnings with an element focus action', () => {
    const onFocusElement = vi.fn();

    render(
      <ArchitectureValidationPanel
        loading={false}
        error={null}
        onFocusElement={onFocusElement}
        result={{
          valid: true,
          issues: [],
          warnings: [],
          structured_warnings: [
            {
              code: 'isolated_entity_node',
              severity: 'warning',
              message: 'Diagram entity node has no incident connector.',
              path: 'diagrams[0].adapter_payload.elements[2]',
              suggested_fix: 'Connect the node to another architecture element.',
              diagram_id: 'diag-1',
              diagram_type: 'runtime',
              element_id: 'node-audit',
            },
          ],
          suppressed_warnings: [],
          suggested_fixes: [],
          summary: {},
        }}
      />,
    );

    expect(screen.getByText('1 warning')).toBeInTheDocument();
    expect(screen.getByText('Connectivity and coverage')).toBeInTheDocument();
    expect(screen.getByText('isolated_entity_node')).toBeInTheDocument();
    expect(screen.getByText('diag-1 / node-audit')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Focus diagram element'));

    expect(onFocusElement).toHaveBeenCalledWith({ diagramId: 'diag-1', elementId: 'node-audit' });
  });

  it('renders entity-scoped structured topology warnings without a broken focus action', () => {
    render(
      <ArchitectureValidationPanel
        loading={false}
        error={null}
        result={{
          valid: true,
          issues: [],
          warnings: [],
          structured_warnings: [
            {
              code: 'entity_without_diagram_node',
              severity: 'warning',
              message: 'Architecture entity is not represented in any diagram.',
              path: 'entities[1]',
              suggested_fix: 'Add a diagram node for this entity or remove it from the model.',
              entity_id: 'entity-billing',
            },
          ],
          suppressed_warnings: [],
          suggested_fixes: [],
          summary: {},
        }}
      />,
    );

    expect(screen.getByText('entity_without_diagram_node')).toBeInTheDocument();
    expect(screen.getByText('entity-billing')).toBeInTheDocument();
    expect(screen.queryByTitle('Focus diagram element')).not.toBeInTheDocument();
  });

  it('renders current authoring warnings separately from structured critic findings', () => {
    render(
      <ArchitectureValidationPanel
        loading={false}
        error={null}
        result={{
          valid: true,
          issues: [],
          warnings: ['entities[0].responsibility is empty. Clarify what this component owns.'],
          structured_warnings: [],
          suppressed_warnings: [],
          suggested_fixes: [],
          summary: {},
        }}
      />,
    );

    expect(screen.getByText('1 warning')).toBeInTheDocument();
    expect(screen.getByText('Authoring warnings')).toBeInTheDocument();
    expect(screen.getByText('entities[0].responsibility is empty. Clarify what this component owns.')).toBeInTheDocument();
  });

  it('keeps the selected diagram payload visible after saving a new version', async () => {
    const designWithElement: ArchitectureDesign = {
      ...design,
      version: 1,
      diagrams: [
        {
          ...design.diagrams[0],
          adapter_payload: {
            type: 'excalidraw',
            version: 2,
            elements: [
              {
                id: 'box-save',
                type: 'rectangle',
                x: 40,
                y: 40,
                width: 160,
                height: 80,
                text: 'Saved API',
              },
            ],
            appState: {},
            files: {},
          },
        },
      ],
    };
    const patchResponse: ArchitectureDesign = {
      ...designWithElement,
      version: 2,
      diagrams: [{ ...designWithElement.diagrams[0], adapter_payload: null }],
    };
    const fullReload: ArchitectureDesign = { ...designWithElement, version: 2 };

    apiMock.getArchitectureDesign
      .mockResolvedValueOnce(designWithElement)
      .mockResolvedValueOnce(fullReload);
    apiMock.updateArchitectureDesign.mockResolvedValue(patchResponse);

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    expect(await screen.findByText('Saved API')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Saved API')).toBeInTheDocument();
    expect(screen.getAllByDisplayValue('v2').length).toBeGreaterThan(0);
  });

  it('requires explicit acknowledgement before saving structured architecture warnings', async () => {
    apiMock.validateArchitectureDesign.mockResolvedValue({
      valid: true,
      issues: [],
      warnings: [],
      structured_warnings: [
        {
          code: 'entity_without_diagram',
          severity: 'warning',
          message: 'Architecture entity is not represented in any diagram.',
          path: 'entities[1]',
          suggested_fix: 'Add a diagram node for this entity.',
          entity_id: 'entity-billing',
          finding_key: 'finding-key-1',
        },
      ],
      suppressed_warnings: [],
      suggested_fixes: [],
      summary: {},
    });

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    await waitFor(() => expect(apiMock.validateArchitectureDesign).toHaveBeenCalledTimes(1), { timeout: 1500 });
    expect(screen.getByText('entity_without_diagram')).toBeInTheDocument();
    expect(
      screen.getByText(
        'I reviewed these architecture warnings. Save may continue, but active warnings still block moving the owner to Done until they are resolved.',
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('Save'));

    expect(apiMock.updateArchitectureDesign).not.toHaveBeenCalled();
  });

  it('sends acknowledgement keys when saving reviewed structured warnings', async () => {
    const patchResponse: ArchitectureDesign = {
      ...design,
      version: 2,
      diagrams: [{ ...design.diagrams[0], adapter_payload: null }],
    };
    const fullReload: ArchitectureDesign = { ...design, version: 2 };
    apiMock.validateArchitectureDesign.mockResolvedValue({
      valid: true,
      issues: [],
      warnings: [],
      structured_warnings: [
        {
          code: 'entity_without_diagram',
          severity: 'warning',
          message: 'Architecture entity is not represented in any diagram.',
          path: 'entities[1]',
          suggested_fix: 'Add a diagram node for this entity.',
          entity_id: 'entity-billing',
          finding_key: 'finding-key-1',
        },
      ],
      suppressed_warnings: [],
      suggested_fixes: [],
      summary: {},
    });
    apiMock.getArchitectureDesign
      .mockResolvedValueOnce(design)
      .mockResolvedValueOnce(fullReload);
    apiMock.updateArchitectureDesign.mockResolvedValue(patchResponse);

    render(<ArchitectureTab parentType="ideation" parentId="ideation-1" />);

    const acknowledgement = await screen.findByRole('checkbox', {
      name: /Save may continue, but active warnings still block moving the owner to Done until they are resolved/i,
    });
    fireEvent.click(acknowledgement);
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(apiMock.updateArchitectureDesign).toHaveBeenCalledTimes(1));
    expect(apiMock.updateArchitectureDesign).toHaveBeenCalledWith(
      'arch-1',
      expect.objectContaining({
        architecture_warning_acknowledgement: {
          accepted: true,
          warning_keys: ['finding-key-1'],
          statement: 'Reviewed in Architecture tab before save.',
        },
      }),
    );
  });
});
