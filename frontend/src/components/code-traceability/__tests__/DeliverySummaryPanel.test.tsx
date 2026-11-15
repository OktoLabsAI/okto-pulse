import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { DeliverySummaryPanel } from '../DeliverySummaryPanel';
import { DeliveryEvidencePanel } from '../DeliveryEvidencePanel';
import { deliverySummary, deliveryTask } from './deliverySummaryFixture';
import { persistedProjection } from './readinessFixture';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { isDeliverySummary } from '@/types/delivery-summary';
import { subscribeContextualHelp } from '@/components/help/contextualHelp';

const api = vi.hoisted(() => ({ getDeliverySummary:vi.fn(),getPersistedReadinessProjection:vi.fn(),getDeliveryEvidence:vi.fn(),
  getBoard:vi.fn(),getTaskRequirementOptions:vi.fn(),applyTaskRequirementDecision:vi.fn() }));
vi.mock('@/services/api',()=>({ useDashboardApi:()=>api }));
const details = vi.fn(active => <input aria-label="Detailed draft" disabled={!active} defaultValue="Retained draft" />);
const panel = () => <DeliverySummaryPanel boardId="b" specId="s" canManageTaskRequirements renderDetails={details} />;

beforeEach(()=>{
  vi.resetAllMocks();
  details.mockImplementation(active => <input aria-label="Detailed draft" disabled={!active} defaultValue="Retained draft" />);
  api.getDeliverySummary.mockResolvedValue(deliverySummary());
  api.getPersistedReadinessProjection.mockResolvedValue(persistedProjection());
  api.getBoard.mockResolvedValue({settings:{}});
  api.getTaskRequirementOptions.mockImplementation((_b,_s,ids)=>Promise.resolve({
    contract_version:'task-requirement-options/v1',board_id:'b',spec_id:'s',basis:'a'.repeat(64),can_apply:true,
    allowed_decisions:['required','optional','waived'],cards:ids.map((id:string)=>({card_id:id,title:`Task ${id}`,decision:'required',mutable:true})),
    effects:{dependent_tasks:[],test_scenarios_remain_governed:false} }));
  api.applyTaskRequirementDecision.mockImplementation((_b,_s,body)=>Promise.resolve({
    contract_version:'task-requirement-operation/v1',status:'applied',replayed:false,
    decision:body.decision,affected_cards:body.card_ids }));
});
afterEach(()=>{ cleanup();vi.useRealTimers();vi.unstubAllGlobals(); });

it('shows the three user groups with one compact read and leaves all technical readers unopened',async()=>{
  render(<DeliveryEvidencePanel boardId="b" specId="s" />);
  expect(await screen.findByRole('heading',{name:'Delivered (1)'})).toBeVisible();
  expect(screen.getByRole('heading',{name:'In progress (1)'})).toBeVisible();
  expect(screen.getByRole('heading',{name:'Pending (1)'})).toBeVisible();
  expect(within(screen.getByRole('region',{name:'Pending'})).getByText('Awaiting validation')).toBeVisible();
  expect(api.getDeliverySummary).toHaveBeenCalledOnce();
  expect(api.getDeliverySummary).toHaveBeenCalledWith('b','s',{},expect.any(AbortSignal));
  expect(api.getPersistedReadinessProjection).not.toHaveBeenCalled();
  expect(api.getDeliveryEvidence).not.toHaveBeenCalled(); expect(api.getBoard).not.toHaveBeenCalled();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  expect(screen.queryByText(/Generation/)).not.toBeInTheDocument();
});

it('opens the exact central Help topic from the feature entry without fetching technical data',async()=>{
  const listener=vi.fn();const stop=subscribeContextualHelp(listener);
  render(panel());await screen.findByText('Task 0');
  const help=screen.getByRole('button',{name:'How delivery status works'});help.focus();fireEvent.click(help);
  expect(help).toHaveFocus();expect(listener).toHaveBeenCalledWith({sectionId:'delivery-status'});
  expect(api.getDeliveryEvidence).not.toHaveBeenCalled();expect(details).not.toHaveBeenCalled();stop();
});

it('loads exceptions only when opened and keeps waived work outside Delivered',async()=>{
  render(panel()); await screen.findByText('Task 0');
  const value=deliverySummary();value.groups.forEach(row=>{row.loaded=row.id==='optional_or_waived';row.items=[];});
  value.groups[3].items=[deliveryTask('waived','optional_or_waived')];
  api.getDeliverySummary.mockResolvedValueOnce(value);
  const toggle=screen.getByText('Optional / Waived (1)').closest('details')!;
  expect(toggle.open).toBe(false);toggle.open=true;fireEvent(toggle,new Event('toggle'));
  expect(await screen.findByText('Task waived')).toBeVisible();
  expect(api.getDeliverySummary).toHaveBeenLastCalledWith('b','s',{group:'optional_or_waived',limit:10},expect.any(AbortSignal));
  expect(within(screen.getByRole('region',{name:'Delivered'})).queryByText('Task waived')).not.toBeInTheDocument();
  toggle.open=false;fireEvent(toggle,new Event('toggle'));
  expect(api.getDeliverySummary).toHaveBeenCalledTimes(2);
});

it('paginates only the requested group and never follows its cursor automatically',async()=>{
  const value=deliverySummary();value.groups[2].count=2;value.task_count=5;value.required_task_count=4;
  value.groups[2].has_more=true;value.groups[2].next_cursor='signed';api.getDeliverySummary.mockResolvedValueOnce(value);
  render(panel()); await screen.findByText('Task 2');
  expect(api.getDeliverySummary).toHaveBeenCalledOnce();
  const next=structuredClone(value);next.groups.forEach(group=>{group.loaded=group.id==='pending';group.items=[];});
  next.groups[2].items=[deliveryTask('next','pending')];next.groups[2].has_more=false;next.groups[2].next_cursor=null;
  api.getDeliverySummary.mockResolvedValueOnce(next);
  fireEvent.click(screen.getByRole('button',{name:'Load more pending tasks'}));
  expect(await screen.findByText('Task next')).toBeVisible();expect(screen.getByText('Task 2')).toBeVisible();
  expect(screen.getByText('Task 0')).toBeVisible();
  expect(api.getDeliverySummary).toHaveBeenLastCalledWith('b','s',{group:'pending',limit:10,cursor:'signed'},expect.any(AbortSignal));
});

it.each(['conflict','different_population','denied'])('refuses %s page responses without fabricating empty groups',async mode=>{
  const value=deliverySummary();value.groups[2].count=2;value.task_count=5;value.required_task_count=4;
  value.groups[2].has_more=true;value.groups[2].next_cursor='signed';api.getDeliverySummary.mockResolvedValueOnce(value);
  render(panel());await screen.findByText('Task 2');
  if(mode==='different_population')api.getDeliverySummary.mockResolvedValueOnce({...value,population_id:'c'.repeat(64)});
  else api.getDeliverySummary.mockRejectedValueOnce(new AuthenticatedFetchError({message:'opaque technical detail',status:mode==='denied'?403:409}));
  fireEvent.click(screen.getByRole('button',{name:'Load more pending tasks'}));
  expect(await screen.findByRole('alert')).not.toHaveTextContent('opaque technical detail');
  if(mode==='denied')expect(screen.queryByText('Task 2')).not.toBeInTheDocument();
  else expect(screen.getByText('Task 2')).toBeVisible();
  expect(screen.queryByText('No tasks in this Spec.')).not.toBeInTheDocument();
});

it('mounts Details on demand, pauses it when hidden and retains its draft',async()=>{
  render(panel());await screen.findByText('Task 0');expect(details).not.toHaveBeenCalled();
  const trigger=screen.getByRole('button',{name:'Details'});trigger.focus();fireEvent.click(trigger);
  const input=screen.getByLabelText('Detailed draft');fireEvent.change(input,{target:{value:'Draft changed'}});
  const close=screen.getByRole('button',{name:'Hide Details'});close.focus();fireEvent.click(close);
  expect(input).not.toBeVisible();expect(input).toBeDisabled();expect(close).toHaveFocus();
  fireEvent.click(screen.getByRole('button',{name:'Details'}));
  expect(screen.getByLabelText('Detailed draft')).toHaveValue('Draft changed');
  expect(screen.getByLabelText('Detailed draft')).toBeEnabled();
});

it('selects tasks once for a single human save and retains the accepted form across summary refresh',async()=>{
  render(panel());await screen.findByText('Task 0');
  fireEvent.click(screen.getByRole('button',{name:'Change task requirements'}));
  fireEvent.click(screen.getByRole('checkbox',{name:'Select Task 0'}));fireEvent.click(screen.getByRole('checkbox',{name:'Select Task 1'}));
  fireEvent.click(screen.getByRole('button',{name:'Continue with 2 selected'}));
  expect(screen.getByRole('group',{name:'Selected task requirements'})).toHaveFocus();
  await waitFor(()=>expect(screen.getByLabelText('Requirement')).toBeEnabled());
  expect(api.getTaskRequirementOptions).toHaveBeenCalledWith('b','s',['0','1'],expect.any(AbortSignal));
  fireEvent.change(screen.getByLabelText('Requirement'),{target:{value:'waived'}});
  fireEvent.click(screen.getByRole('button',{name:'Save task requirement'}));
  expect(await screen.findByText('Task requirement saved. Work status and proof are unchanged.')).toBeVisible();
  expect(api.applyTaskRequirementDecision).toHaveBeenCalledOnce();
  expect(api.applyTaskRequirementDecision.mock.calls[0][2]).toMatchObject({card_ids:['0','1'],decision:'waived',note:null});
  await waitFor(()=>expect(api.getDeliverySummary).toHaveBeenCalledTimes(2));
  expect(screen.getByRole('group',{name:'Selected task requirements'})).toHaveFocus();
  expect(api.getDeliveryEvidence).not.toHaveBeenCalled();
});

it('does not label a stale Done task as Delivered and keeps unknown counts unknown',async()=>{
  const value=deliverySummary();value.complete=false;value.task_count=value.required_task_count=value.unique_obligation_count=null;
  value.projection.freshness='updating';value.groups.forEach(group=>{group.count=null;group.items=[];});
  value.groups[2].items=[{...deliveryTask('historical','delivered'),group:'pending',reason:'updating',requirement:'unknown'}];
  api.getDeliverySummary.mockResolvedValue(value);render(panel());
  expect(await screen.findByText('Completion awaiting an updated check')).toBeVisible();
  expect(screen.getByRole('heading',{name:'Delivered'})).toBeVisible();expect(screen.queryByText('Delivered (0)')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('counts are not yet confirmed');
  expect(screen.queryByText('No tasks in this Spec.')).not.toBeInTheDocument();
});

it.each([403,404,503])('shows unavailability for HTTP %s instead of a successful empty list',async status=>{
  api.getDeliverySummary.mockRejectedValueOnce(new AuthenticatedFetchError({message:'internal',status}));render(panel());
  expect(await screen.findByRole('alert')).not.toHaveTextContent('internal');
  if ([403,404].includes(status)) expect(screen.getByRole('button',{name:'Details'})).toBeDisabled();
  expect(screen.queryByText('No tasks in this Spec.')).not.toBeInTheDocument();expect(details).not.toHaveBeenCalled();
});

it('bounds automatic pending reads and stops them on unmount',async()=>{
  vi.useFakeTimers();const value=deliverySummary();value.complete=false;value.projection.freshness='pending_initial';
  value.task_count=value.required_task_count=value.unique_obligation_count=null;value.groups.forEach(group=>{group.count=null;group.items=[];});
  api.getDeliverySummary.mockResolvedValue(value);const view=render(panel());
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});expect(api.getDeliverySummary).toHaveBeenCalledTimes(4);
  view.unmount();await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});expect(api.getDeliverySummary).toHaveBeenCalledTimes(4);
});

it.each(['count','duplicate','group','contract','complete','waived_delivered'])('rejects inconsistent %s DTOs',kind=>{
  const value=deliverySummary();
  if(kind==='count')value.task_count=0;
  if(kind==='duplicate')value.groups[2].items[0].card_id='0';
  if(kind==='group')value.groups[2].items[0].group='delivered';
  if(kind==='contract')(value as any).contract_version='old';
  if(kind==='complete')value.complete=false;
  if(kind==='waived_delivered')value.groups[0].items[0].requirement='waived';
  expect(isDeliverySummary(value)).toBe(false);
});
