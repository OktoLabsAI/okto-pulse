import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../../src/index.css';
import { ApiProvider } from '../../src/contexts/ApiContext';
import { DeliveryEvidencePanel } from '../../src/components/code-traceability/DeliveryEvidencePanel';
import { persistedProjection, readiness } from '../../src/components/code-traceability/__tests__/readinessFixture';

function generation(id: string, satisfiedImplementation: number, satisfiedVerification: number) {
  const value = persistedProjection();
  value.projection.generation_id = id;
  value.projection.source_revision = id === 'g1' ? 1 : 2;
  value.projection.target_revision = value.projection.source_revision;
  value.projection.evaluated_at = id === 'g1' ? '2026-10-26T00:00:00Z' : '2026-10-26T00:00:01Z';
  value.units = ['one', 'two', 'three', 'four'].map((ref, index) => ({
    card_id: null, kind: 'obligation' as const, ref: `fr:${ref}`, semantic_sha256: String(index).repeat(64),
    spec_edition: 1, contribution_card_ids: [],
    implementation_state: index < satisfiedImplementation ? 'satisfied' : 'missing',
    verification_state: index < satisfiedVerification ? 'satisfied' : 'missing',
    implementation_reason_codes: [], verification_reason_codes: [],
  }));
  value.page = { offset: 0, limit: 250, total: 4, has_more: false, next_cursor: null };
  for (const summary of value.summaries) {
    for (const [metric, count] of [[summary.implementation, satisfiedImplementation],
      [summary.verification, satisfiedVerification]] as const) {
      metric.total = 4; metric.applicable = 4; metric.satisfied = count; metric.unresolved = 4 - count;
      metric.percent = count * 25;
      metric.counts_by_state = { ...metric.counts_by_state, satisfied: count, missing: 4 - count };
    }
  }
  return value;
}

function initialProjection() {
  const mode = new URLSearchParams(location.search).get('mode');
  const value = generation('g1', 3, 2);
  if (mode === 'pending') {
    value.projection.freshness = 'pending_initial';
    value.projection.generation_id = null;
    value.projection.source_revision = null;
    value.projection.evaluated_at = null;
    value.summaries = [];
    value.units = [];
    value.page = { offset: 0, limit: 250, total: 0, has_more: false, next_cursor: null };
  }
  if (mode === 'unknown' || mode === 'na') for (const summary of value.summaries) {
    summary.implementation.status = mode === 'unknown' ? 'unknown' : 'not_applicable';
    summary.implementation.percent = null;
    summary.implementation.applicable = mode === 'unknown' ? null : 0;
    summary.implementation.total = mode === 'unknown' ? null : 0;
    summary.implementation.satisfied = 0;
    summary.implementation.unresolved = mode === 'unknown' ? null : 0;
  }
  return value;
}

let serverProjection = initialProjection();
const healthyProjection = generation('healthy-g1', 2, 2);
healthyProjection.scope.spec_id = 'healthy';
let primaryFails = false;
let holdNext = false;
let releaseRead: (() => void) | null = null;
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = String(input);
  if (!url.startsWith('/api/v1/')) return originalFetch(input, init);
  if (url.includes('/boards/other/')) return new Response(JSON.stringify({ detail: 'Denied' }), { status: 403 });
  if (url.includes('/delivery-readiness-projection')) {
    if (url.includes('/specs/healthy/'))
      return new Response(JSON.stringify(healthyProjection), { status: 200,
        headers: { 'Content-Type': 'application/json' } });
    if (primaryFails) return new Response(JSON.stringify({ detail: 'Primary Spec unavailable' }), { status: 503 });
    if (holdNext) {
      holdNext = false;
      await new Promise<void>(resolve => { releaseRead = resolve; });
    }
    return new Response(JSON.stringify(serverProjection), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }
  if (url.includes('/delivery-readiness'))
    return new Response(JSON.stringify(readiness(null)), { status: 200, headers: { 'Content-Type': 'application/json' } });
  if (url.endsWith('/boards/b'))
    return new Response(JSON.stringify({ id: 'b', settings: { delivery_evidence_gate: 'blocking' } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  return new Response(JSON.stringify({ detail: 'Fixture route not found' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
};

function App() {
  const [boardId, setBoardId] = useState('b');
  const [showHealthy, setShowHealthy] = useState(false);
  return <ApiProvider><main className="mx-auto max-w-5xl space-y-4 p-5">
    <h1>Readiness browser fixture</h1>
    <div className="flex flex-wrap gap-2">
      <button type="button" onClick={() => { holdNext = true; }}>Hold next read</button>
      <button type="button" onClick={() => {
        serverProjection = generation('g1', 3, 2);
        serverProjection.projection.freshness = 'updating';
        serverProjection.projection.target_revision = 2;
        window.dispatchEvent(new Event('pulse:delivery-evidence-changed'));
      }}>Trigger report</button>
      <button type="button" onClick={() => { releaseRead?.(); releaseRead = null; }}>Release read</button>
      <button type="button" onClick={() => {
        serverProjection = generation('g2', 4, 3);
        (window as Window & { __readinessPublishedAt?: number }).__readinessPublishedAt = performance.now();
        window.dispatchEvent(new Event('pulse:delivery-evidence-changed'));
      }}>Publish g2</button>
      <button type="button" onClick={() => { setShowHealthy(true); }}>Show healthy Spec</button>
      <button type="button" onClick={() => {
        primaryFails = true;
        window.dispatchEvent(new Event('pulse:delivery-evidence-changed'));
      }}>Fail primary Spec</button>
      <button type="button" onClick={() => { setBoardId('other'); }}>Switch board</button>
    </div>
    <DeliveryEvidencePanel boardId={boardId} specId="s" />
    {showHealthy && <DeliveryEvidencePanel boardId={boardId} specId="healthy" />}
  </main></ApiProvider>;
}

createRoot(document.getElementById('root')!).render(<App />);
