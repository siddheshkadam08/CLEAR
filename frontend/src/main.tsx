import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import App from './App';
import { ApiError } from './api/errors';
import './lib/theme';
import './styles/index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      // Refetching on every window focus makes a review tool feel jumpy while
      // someone is reading a clause.
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // Never retry an auth or permission failure: the answer will not change,
        // and each attempt is another audited denial.
        if (error instanceof ApiError && !error.isRetryable) return false;
        return failureCount < 2;
      },
    },
  },
});

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element is missing from index.html.');
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
