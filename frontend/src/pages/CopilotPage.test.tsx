/**
 * The three things a reader does with an answer they cannot fully trust: check
 * the source it came from, copy it somewhere else, and ask again when it broke.
 *
 * The source list is the one that carries weight. An answer about a termination
 * clause is only actionable if the section and page are on screen next to it -
 * otherwise verifying it means opening the PDF and searching by hand, which is
 * the work the product exists to remove.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { copilot as copilotApi } from '@/api/endpoints';
import type { CopilotSource, CopilotStreamDone } from '@/api/types';
import { CopilotPage } from './CopilotPage';

vi.mock('@/api/endpoints', () => ({
  copilot: {
    sessions: vi.fn(async () => []),
    createSession: vi.fn(),
    session: vi.fn(),
    stream: vi.fn(),
  },
}));

vi.mock('@/lib/scope', () => ({ useProjectScope: () => ({ projectId: null }) }));

const SOURCE: CopilotSource = {
  contractId: '22222222-2222-2222-2222-222222222222',
  contractName: 'Acme Master Services Agreement',
  clauseHeading: 'Termination',
  sectionNumber: '12.3',
  pageNumber: 18,
  similarityScore: 0.91,
  matchType: 'semantic',
  text: 'Either party may terminate on thirty days written notice.',
  label: 1,
};

const DONE: CopilotStreamDone = {
  citations: [],
  confidence: 0.82,
  confidence_band: 'high',
  needs_review: false,
  warnings: [],
  sources: [SOURCE],
  metadata: {
    documentTypeDetected: true,
    documentType: 'MSA',
    documentTypeConfidence: 0.94,
    retrievalMode: 'DocumentTypeFiltered',
    retrievedChunks: 8,
    topSimilarity: 0.91,
    insufficientContext: false,
    generationFailed: false,
    relaxedFilters: false,
    scopeTruncated: false,
    confidence: 0.82,
    confidenceBand: 'high',
    needsReview: false,
    refused: false,
    warnings: [],
    tokens: 0,
    costUsd: 0,
    timings: {},
  },
};

type StreamHandlers = { onEvent: (event: string, data: unknown) => void };

/** Drive the streaming API double through one complete answer. */
function answersWith(text: string, done: CopilotStreamDone = DONE) {
  vi.mocked(copilotApi.stream).mockImplementation((async (
    _body: unknown,
    handlers: StreamHandlers,
  ) => {
    handlers.onEvent('token', { text });
    handlers.onEvent('done', done);
  }) as never);
}

const renderPage = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <CopilotPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );

async function ask(question: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/your question/i), question);
  await user.click(screen.getByRole('button', { name: /^ask$/i }));
  return user;
}

describe('CopilotPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(copilotApi.sessions).mockResolvedValue([]);
  });

  it('renders the answer as markdown once the stream closes', async () => {
    answersWith('Either party may terminate on **thirty days** written notice.');
    renderPage();

    await ask('What is the notice period?');

    await waitFor(() => expect(screen.getByText('thirty days').tagName).toBe('STRONG'));
  });

  it('lists the source with its section, page and similarity', async () => {
    answersWith('Thirty days written notice.');
    renderPage();

    await ask('What is the notice period?');

    await waitFor(() =>
      expect(screen.getByRole('link', { name: /acme master services agreement/i })).toBeInTheDocument(),
    );
    expect(screen.getByText('Termination')).toBeInTheDocument();
    expect(screen.getByText(/section 12\.3/i)).toBeInTheDocument();
    expect(screen.getByText('p.18')).toBeInTheDocument();
    expect(screen.getByText(/similarity 0\.91/i)).toBeInTheDocument();
  });

  it('copies the answer', async () => {
    answersWith('Thirty days written notice.');
    renderPage();

    // Read back through `user-event`'s own clipboard stub rather than a hand-rolled
    // one: `setup()` replaces `navigator.clipboard`, so a double installed here
    // would be the thing that got overwritten and the test would pass on nothing.
    const user = await ask('What is the notice period?');
    await waitFor(() => screen.getByRole('button', { name: /copy/i }));
    await user.click(screen.getByRole('button', { name: /copy/i }));

    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe('Thirty days written notice.'),
    );
    expect(screen.getByRole('button', { name: /copied/i })).toBeInTheDocument();
  });

  it('replaces the answer in place when retried, rather than repeating the question', async () => {
    answersWith('First attempt.');
    renderPage();

    const user = await ask('What is the notice period?');
    await waitFor(() => screen.getByText('First attempt.'));

    answersWith('Second attempt.');
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => expect(screen.getByText('Second attempt.')).toBeInTheDocument());
    expect(screen.queryByText('First attempt.')).not.toBeInTheDocument();
    expect(screen.getAllByText('What is the notice period?')).toHaveLength(1);
  });

  it('marks an answer the guardrail produced', async () => {
    answersWith("I couldn't find sufficient information within the selected contract or project to answer this question.", {
      ...DONE,
      sources: [],
      metadata: { ...DONE.metadata!, insufficientContext: true, retrievedChunks: 0 },
    });
    renderPage();

    await ask('What is the notice period?');

    await waitFor(() => expect(screen.getByText(/not enough context/i)).toBeInTheDocument());
    expect(screen.queryByText(/^sources$/i)).not.toBeInTheDocument();
  });

  it('says so when the search was widened past the detected document type', async () => {
    // A thin answer from a widened search reads exactly like a thin answer from a
    // precise one unless the widening is stated.
    answersWith('Nothing specific found.', {
      ...DONE,
      metadata: { ...DONE.metadata!, relaxedFilters: true },
    });
    renderPage();

    await ask('What is the notice period in the MSA?');

    await waitFor(() => expect(screen.getByText(/widened/i)).toBeInTheDocument());
  });

  it('says so when more contracts matched than can be ranked individually', async () => {
    answersWith('Some answer.', {
      ...DONE,
      metadata: { ...DONE.metadata!, scopeTruncated: true },
    });
    renderPage();

    await ask('Which agreements have an uncapped indemnity?');

    await waitFor(() =>
      expect(screen.getByText(/may not be represented/i)).toBeInTheDocument(),
    );
  });

  it('shows a keyword match rather than a similarity of zero', async () => {
    answersWith('Some answer.', {
      ...DONE,
      sources: [{ ...SOURCE, similarityScore: null, matchType: 'keyword' }],
    });
    renderPage();

    await ask('net 30');

    await waitFor(() => expect(screen.getByText(/keyword match/i)).toBeInTheDocument());
    expect(screen.queryByText(/similarity 0\.00/i)).not.toBeInTheDocument();
  });

  it('keeps the sources when generation failed', async () => {
    answersWith('The answer could not be generated just now.', {
      ...DONE,
      metadata: { ...DONE.metadata!, generationFailed: true, needsReview: true },
    });
    renderPage();

    await ask('What is the notice period?');

    await waitFor(() => expect(screen.getByText(/not summarised/i)).toBeInTheDocument());
    expect(screen.getByRole('link', { name: /acme master services agreement/i })).toBeInTheDocument();
  });

  it('offers a retry when the stream fails', async () => {
    vi.mocked(copilotApi.stream).mockImplementation((async (
      _body: unknown,
      handlers: StreamHandlers,
    ) => {
      handlers.onEvent('error', { message: 'The answer stream was interrupted.' });
    }) as never);
    renderPage();

    await ask('What is the notice period?');

    await waitFor(() => expect(screen.getByText(/interrupted/i)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });
});
