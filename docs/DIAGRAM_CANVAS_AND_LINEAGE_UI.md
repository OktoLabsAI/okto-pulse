# Diagram canvas and lineage UI corrections

## Architecture canvas

The visual editor reserves at least **420 diagram units on every side** of the
element bounds. A view-only origin offset makes negative X/Y positions reachable
through ordinary scroll/pan. Existing payload coordinates are not rewritten.
The margin scales with zoom; large diagrams still require scrolling when they
cannot fit within the editor's supported zoom range.

Dragging can cross X=0/Y=0. Canvas extents grow during editing but do not shrink
within the same diagram session, avoiding scrollbar clamps and viewport jumps.
Origin changes compensate scroll and pointer coordinates before paint. Focus,
fit-to-view, connected edges, grid snapping and read-only navigation use the same
coordinate system. Nothing is written merely by opening or navigating a diagram.

## SDLC lineage

Forward connections explicitly select `lineage-source-right` and
`lineage-target-left`. Reverse lineage paths retain the opposite handles when
the displayed stage order requires them. Dependencies also name their forward
handles explicitly. No edge identity, direction or relationship is rewritten.

Previously, omitting the source handle let React Flow choose the first registered
source, which was the reverse/left handle. This caused forward lines to leave the
wrong side even though the node-level source position was Right.

Entity type remains represented by the header/icon. Status has a separate,
stable color in the node border, thicker left rail, status badge and minimap.
Status labels remain visible; unknown statuses receive a neutral fallback.
The collapsible **Status colors** legend lists statuses present in the graph.
The palette covers current Story, Ideation, Refinement, Spec, Sprint and Card
lifecycles, with a test guarding coverage and distinct color values.

React Flow dimension changes are retained in controlled nodes so the minimap can
actually draw their rectangles. This accepts measurements only, never position,
selection or deletion changes; those continue to follow existing application
policies. No database, API or Core changes are required.

## Isolated validation

2026-09-13 checkpoint: **66 unit/component tests and 4 Chromium browser tests
passed**. Production TypeScript/build and packaged-asset verification passed.
Browser assertions cover negative-coordinate margins, drift-free repeated drag,
zoom/focus/fullscreen/read-only, rendered edge endpoints and status/minimap colors.
The active Pulse process was not stopped, reinstalled or pointed at test data.

Run unit/component regression with:

```sh
npm test -- src/components/architecture src/components/traceability
npx playwright test --config playwright.architecture.config.ts
```

The browser suite launches a dedicated frontend on `127.0.0.1:5189`, without
an API proxy or backend workers. Fixtures use the actual editors and synthetic
in-memory data only; they cannot change the running Pulse's boards or databases.
Manual fixture pages: `/tests/fixtures/architecture-canvas.html` and
`/tests/fixtures/lineage-canvas.html`. These test entry points are not bundled
into the production SPA. Build with `npm run build`, then verify packaged assets
with `npm run verify:frontend-dist`.
