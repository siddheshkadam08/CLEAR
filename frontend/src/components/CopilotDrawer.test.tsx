/**
 * The drawer is where Copilot actually lives - the sidebar entry was removed and
 * the feature moved onto the contract - so it is the surface most people see, and
 * it had been left behind by two changes made to the full page.
 *
 * It rendered answers as pre-wrapped text, so a lawyer asking "what are the key
 * risks?" got a wall with literal `*` and `**` in it. And it printed every cited
 * clause in full beneath, which for eight citations was several times the length
 * of the answer they were supporting.
 *
 * Both are pinned here because both are invisible to typechecking: the wrong
 * thing still compiles and still renders, it just renders badly.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { copilot as copilotApi } from '@/api/endpoints';
import type { Citation } from '@/api/types';
import { CopilotDrawer } from './CopilotDrawer';

vi.mock('@/api/endpoints', () => ({
  copilot: {
    // The drawer opens a session before its first question, so this has to
    // resolve or `ask` throws before a single token arrives.
    createSession: vi.fn(async () => ({ id: 'session-1' })),
    stream: vi.fn(),
  },
}));
vi.mock('@/lib/scope', () => ({ useProjectScope: () => ({ projectId: null }) }));

const LONG_CLAUSE =
  'Except as provided herein, neither Party shall, directly or indirectly, disclose ' +
  'any part of the Confidential Information provided by the Disclosing Party to any ' +
  'other party, corporation, affiliate, subsidiary, organization or person of any ' +
  'kind without the prior written consent of the Disclosing Party.';

const CITATION: Citation = {
  label: 1,
  contract_id: '22222222-2222-2222-2222-222222222222',
  contract_title: 'Sub-Contractor Agreement',
  clause_number: '9',
  page_range: '5-6',
  text: LONG_CLAUSE,
} as Citation;

type StreamHandlers = { onEvent: (event: string, data: unknown) => void };

function answersWith(text: string, citations: Citation[] = []) {
  vi.mocked(copilotApi.stream).mockImplementation((async (
    _body: unknown,
    handlers: StreamHandlers,
  ) => {
    handlers.onEvent('token', { text });
    handlers.onEvent('done', {
      citations,
      confidence: 0.68,
      confidence_band: 'medium',
      needs_review: false,
      warnings: [],
      sources: [],
      metadata: {},
    });
  }) as never);
}

const renderDrawer = () =>
  render(
    <MemoryRouter>
      <CopilotDrawer
        open
        onClose={() => {}}
        contractId="11111111-1111-1111-1111-111111111111"
        contractTitle="Sub-Contractor Agreement"
      />
    </MemoryRouter>,
  );

async function ask(question: string) {
  const { default: userEvent } = await import('@testing-library/user-event');
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/your question/i), question);
  await user.click(screen.getByRole('button', { name: /send/i }));
  return user;
}

describe('CopilotDrawer', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the answer as markdown, not as literal asterisks', async () => {
    answersWith('The cap is **the fees paid**.');
    renderDrawer();
    await ask('What is the liability cap?');

    await waitFor(() => {
      expect(screen.getByText('the fees paid').tagName).toBe('STRONG');
    });
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it('renders a risk list as list items rather than a wall of text', async () => {
    // The shape the risk format actually produces. As pre-wrapped text every one
    // of these bullets kept its `*`.
    answersWith('Key risks:\n\n- Uncapped flow-down\n- Conflicting confidentiality\n- One-sided liability');
    renderDrawer();
    await ask('What are the key risks in this agreement?');

    await waitFor(() => {
      expect(screen.getAllByRole('listitem')).toHaveLength(3);
    });
  });

  it('keeps a cited clause in the DOM but does not print it unbounded', async () => {
    answersWith('Confidentiality is mutual [1].', [CITATION]);
    renderDrawer();
    await ask('Is confidentiality mutual?');

    // Present and selectable - clamped by CSS, never truncated, because the
    // evidence behind a citation must be verifiable without a round trip.
    await waitFor(() => {
      expect(screen.getByText(LONG_CLAUSE)).toBeInTheDocument();
    });
    // ...and the clamp is what keeps it from burying the answer.
    expect(screen.getByText(LONG_CLAUSE).className).toMatch(/line-clamp-/);
  });
});
