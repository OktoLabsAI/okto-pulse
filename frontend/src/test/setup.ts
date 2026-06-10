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

// Sigma's bundle references WebGL2RenderingContext at module scope (for
// instanceof checks). jsdom doesn't define it; an empty class is enough —
// GraphCanvas never instantiates Sigma in tests (no WebGL context available,
// so it renders the accessible fallback).
if (typeof globalThis.WebGL2RenderingContext === 'undefined') {
  // @ts-expect-error — test-only stub
  globalThis.WebGL2RenderingContext = class WebGL2RenderingContextStub {};
}
if (typeof globalThis.WebGLRenderingContext === 'undefined') {
  // @ts-expect-error — test-only stub
  globalThis.WebGLRenderingContext = class WebGLRenderingContextStub {};
}

// useTheme reads window.matchMedia at module-load time. jsdom does not
// implement it; provide a noop stub so any test that imports a component
// using useTheme can mount.
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (_query: string) => ({
      matches: false,
      media: _query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
