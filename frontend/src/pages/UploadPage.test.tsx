/**
 * The upload destination must follow the business-unit selector.
 *
 * This is the one place where getting the scope wrong *writes*. `target` was
 * seeded by a `useState` initialiser, which runs on the first render only - so
 * changing the business unit afterwards left the destination pointing at whichever
 * unit happened to be selected when the page was opened. The header said one thing,
 * the upload posted to another, and the file landed somewhere nobody would think to
 * look for it.
 *
 * The in-page selector must still win for the current batch: choosing a different
 * destination is a deliberate act, and a re-render that overwrote it would make the
 * control unusable.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { UploadPage } from './UploadPage';

vi.mock('@/api/endpoints', () => ({
  contracts: { upload: vi.fn() },
}));

const CARBON = '11111111-1111-1111-1111-111111111111';
const QUEUE = '22222222-2222-2222-2222-222222222222';

const PROJECTS = [
  { id: CARBON, name: 'Carbon' },
  { id: QUEUE, name: 'Queue E2E' },
];

let currentProject: string | null = CARBON;
vi.mock('@/lib/scope', () => ({
  useProjectScope: () => ({ projects: PROJECTS, projectId: currentProject }),
}));

const wrapped = () => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>
      <UploadPage />
    </MemoryRouter>
  </QueryClientProvider>
);

/** The destination `<select>` - the only combobox on the page. */
const destination = () => screen.getByRole('combobox') as HTMLSelectElement;

beforeEach(() => {
  vi.clearAllMocks();
  currentProject = CARBON;
});

describe('UploadPage destination', () => {
  it('starts on the business unit the header has selected', () => {
    render(wrapped());
    expect(destination().value).toBe(CARBON);
  });

  it('follows the header when the business unit changes', async () => {
    const view = render(wrapped());
    expect(destination().value).toBe(CARBON);

    currentProject = QUEUE;
    view.rerender(wrapped());

    await waitFor(() => expect(destination().value).toBe(QUEUE));
  });

  it('keeps a destination the user chose by hand', async () => {
    render(wrapped());
    const user = userEvent.setup();

    await user.selectOptions(destination(), QUEUE);

    // No header change, so nothing should overwrite the deliberate choice.
    await waitFor(() => expect(destination().value).toBe(QUEUE));
  });
});
