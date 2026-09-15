# Okto Pulse Frontend

React frontend for the Okto Pulse SDLC workbench. It provides the Kanban board,
specification and validation workflows, analytics, code traceability, and
knowledge-graph exploration used by human operators.

## Development

```bash
cd frontend
npm install
npm run dev
```

The Community build uses the standalone authentication and portal adapters. It
does not require a Clerk publishable key. Other editions can inject their own
adapters without changing this Community setup.

## Quality Checks

```bash
npm run lint
npm run test
npm run build
```

`npm run build` also synchronizes the compiled assets into the Python package at
`src/okto_pulse/community/frontend_dist`.

## Stack

- React and TypeScript
- Vite
- Tailwind CSS
- Zustand
- dnd-kit
- React Flow and Graphology
- Vitest and Playwright

The private package name is `okto-pulse-frontend`; it is an internal build
workspace and is not published to npm.
