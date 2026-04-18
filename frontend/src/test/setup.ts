import '@testing-library/jest-dom/vitest';

// React Flow calls `new ResizeObserver()` from a useEffect — jsdom has no
// polyfill, so we stub just enough of the API for the component tree to
// mount without blowing up. The observer never fires in tests; we don't
// care about size-driven layout in unit assertions.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

// DOMMatrix is used by React Flow's viewport transform math; jsdom exposes
// it behind a flag. A minimal identity stub is enough for rendering.
if (typeof globalThis.DOMMatrixReadOnly === 'undefined') {
  class DOMMatrixStub {
    m11 = 1;
    m12 = 0;
    m21 = 0;
    m22 = 1;
    m41 = 0;
    m42 = 0;
    constructor(_init?: unknown) {}
    translate() {
      return this;
    }
    scale() {
      return this;
    }
  }
  // @ts-expect-error — test-only stub
  globalThis.DOMMatrixReadOnly = DOMMatrixStub;
  // @ts-expect-error — test-only stub
  globalThis.DOMMatrix = DOMMatrixStub;
}
