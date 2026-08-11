/**
 * Search results must not outlive the business unit they came from.
 *
 * Every other screen re-scopes because its react-query key contains the project.
 * Search does not use a query at all - it holds results in component state and
 * fills them from a mutation - so nothing invalidated them when the header
 * changed. The rows stayed on screen citing contracts the newly selected unit does
 * not contain, and looked entirely current while doing it.
 *
 * The static key guard in `lib/scope-keys.test.ts` cannot see this: there is no
 * key to inspect. Hence a behavioural test.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { search as searchApi } from '@/api/endpoints';
import { SearchPage } from './SearchPage';

vi.mock('@/api/endpoints', () => ({ search: { query: vi.fn() } }));

// The header's selection, swapped between renders to simulate switching unit.
let currentProject: string | null = 'carbon-unit';
vi.mock('@/lib/scope', () => ({ useProjectScope: () => ({ projectId: currentProject }) }));

const RESULT = {
  total_hits: 1,
  duration_ms: 42,
  warnings: [],
  plan: null,
  contracts: [
    {
      contract_id: '22222222-2222-2222-2222-222222222222',
      title: 'Carbon Master Services Agreement',
      agreement_type: 'msa',
      expiration_date: null,
      risk_band: 'low',
      has_unlimited_liability: false,
    },
  ],
  // Non-empty, or the page renders its "Nothing matched" branch and the contract
  // list never appears - which would make this test pass for the wrong reason.
  hits: [
    {
      level: 'clause',
      ref_id: '33333333-3333-3333-3333-333333333333',
      contract_id: '22222222-2222-2222-2222-222222222222',
      contract_title: 'Carbon Master Services Agreement',
      clause_number: '12.3',
      text: 'Either party may terminate on thirty days written notice.',
      score: 0.91,
      page_number: 18,
    },
  ],
};

// `useMutation` needs a client even though nothing here is cached.
const wrapped = () => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>
      <SearchPage />
    </MemoryRouter>
  </QueryClientProvider>
);

const renderPage = () => render(wrapped());

beforeEach(() => {
  vi.clearAllMocks();
  currentProject = 'carbon-unit';
  vi.mocked(searchApi.query).mockResolvedValue(RESULT as never);
});

describe('SearchPage', () => {
  it('sends the selected business unit with the query', async () => {
    renderPage();
    const user = userEvent.setup();

    await user.type(screen.getByRole('searchbox'), 'termination');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(searchApi.query).toHaveBeenCalled());
    const body = vi.mocked(searchApi.query).mock.calls[0]?.[0] as { project_id?: string | null };
    expect(body.project_id).toBe('carbon-unit');
  });

  it('clears results when the business unit changes', async () => {
    const view = renderPage();
    const user = userEvent.setup();

    await user.type(screen.getByRole('searchbox'), 'termination');
    await user.keyboard('{Enter}');
    // `queryAll`: the title appears twice, once in "Matching contracts" and once
    // on the passage itself.
    await waitFor(() =>
      expect(screen.queryAllByText(/carbon master services agreement/i).length).toBeGreaterThan(0),
    );

    // The header switches to another unit.
    currentProject = 'other-unit';
    view.rerender(wrapped());

    await waitFor(() =>
      expect(screen.queryAllByText(/carbon master services agreement/i)).toHaveLength(0),
    );
  });
});
