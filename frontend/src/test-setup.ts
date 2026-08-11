import '@testing-library/jest-dom/vitest';

// jsdom implements no layout, so it has no `scrollIntoView`. Components that keep
// a view pinned to the bottom - the Copilot thread - call it on every update and
// would otherwise fail on the effect rather than on anything under test.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Same gap, same reason: jsdom ships no `ResizeObserver`. Anything that measures
// itself to decide what to render - the clamped citation snippet deciding whether
// a "Show more" is worth offering - constructs one in an effect.
if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
