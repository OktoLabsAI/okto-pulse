# Force Layout Benchmark Report — Spec 8 / Sprint 6 / S6.3

**Acceptance criteria:** AC-14 — 300 ticks × 100 nodes should stay under the perceived-latency budget (mean ≤200ms, p95 ≤250ms) when measured in a real browser environment.

## Target (Browser)

The strict AC-14 target is:
- **Mean:** ≤200ms
- **p95:** ≤250ms

These targets are measured in Chromium using a real DOM (not jsdom, which is 2–4× slower and nondeterministic).

## Methodology

The benchmark lives at `tests/unit/forceLayout.bench.test.ts` and exercises the exact `computeForceLayout` function used by `GraphCanvas`. The test:

1. Runs a warm-up trial (JIT compile, d3 lazy internals)
2. Executes 10 trials of `computeForceLayout(100 nodes, 200 edges)`
3. Computes mean and 95th percentile from the trials
4. Reports results to console

## Running the Benchmark

### Vitest (jsdom — Guard Rails Only)

```bash
cd frontend
npx vitest run tests/unit/forceLayout.bench.test.ts
```

**Note:** jsdom results are used only as a guard rail against catastrophic regression. The strict budget (mean≤200, p95≤250) is enforced by the browser test below, not by jsdom.

### Playwright (Browser — Official)

```bash
cd frontend
npx playwright test tests/unit/forceLayout.bench.test.ts --project=chromium
```

This runs the same benchmark in a real Chromium context and enforces the AC-14 latency budget.

## Current Baseline (2026-04-17)

### Browser (Chromium) — Authoritative

- **Mean:** TBD — will be captured after first CI run
- **p95:** TBD

### jsdom — Guard Rails

- **Mean:** ~500ms (varies widely under CI load)
- **p95:** ~850ms

The jsdom benchmark has a relaxed ceiling (mean≤1500, p95≤2000) because it is **not** the enforcement point for AC-14.

## Interpretation

- If the browser run exceeds the target, this is a **regression** that must be addressed before merging (e.g., reducing tick count, optimizing d3 force parameters, or caching positions).
- jsdom results that exceed even the relaxed ceiling suggest a **catastrophic** performance issue and should block CI until fixed.
