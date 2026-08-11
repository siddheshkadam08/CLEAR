/**
 * The clamp exists to stop citations burying the answer, so the properties worth
 * pinning are that it hides nothing and that it does not offer a control which
 * reveals nothing.
 *
 * jsdom reports every element as zero-height, so `scrollHeight > clientHeight` is
 * always false there and the overflow probe cannot fire. That is fine for what
 * these assert - the text is always in the DOM, which is the safety property -
 * and the toggle's own behaviour is tested by forcing the expanded state through
 * a stubbed measurement rather than by pretending jsdom does layout.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SourceSnippet } from './SourceSnippet';

const CLAUSE =
  'Except as provided herein, neither Party shall, directly or indirectly, disclose ' +
  'any part of the Confidential Information provided by the Disclosing Party to any ' +
  'other party, corporation, affiliate, subsidiary, organization or person of any kind.';

/** Make the element look taller than its box, which is what a clamp produces. */
function stubOverflow() {
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(60);
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('SourceSnippet', () => {
  it('keeps the full text in the DOM while collapsed', () => {
    // Clamped by CSS, never truncated: the evidence a citation points at must be
    // selectable and searchable even before anyone expands it.
    stubOverflow();
    render(<SourceSnippet text={CLAUSE} />);

    expect(screen.getByText(CLAUSE)).toBeInTheDocument();
  });

  it('offers no toggle when the text already fits', () => {
    // jsdom reports 0/0, so nothing overflows - the case this asserts.
    render(<SourceSnippet text="Clause 12 caps liability at fees paid." />);

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('expands and collapses', async () => {
    stubOverflow();
    render(<SourceSnippet text={CLAUSE} />);

    const toggle = screen.getByRole('button', { name: /show more/i });
    await userEvent.click(toggle);

    expect(screen.getByRole('button', { name: /show less/i })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /show less/i }));
    expect(screen.getByRole('button', { name: /show more/i })).toBeInTheDocument();
  });

  it('renders nothing for an empty citation', () => {
    const { container } = render(<SourceSnippet text="" />);

    expect(container).toBeEmptyDOMElement();
  });
});
