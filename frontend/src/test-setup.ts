import '@testing-library/jest-dom/vitest';

// jsdom implements no layout, so it has no `scrollIntoView`. Components that keep
// a view pinned to the bottom - the Copilot thread - call it on every update and
// would otherwise fail on the effect rather than on anything under test.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
