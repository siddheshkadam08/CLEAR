/**
 * Routes only.
 *
 * Public `/login` sits outside the shell; everything else nests inside
 * ProtectedRoute -> AppShell. The gate waits for the initial session restore
 * before deciding, so a page refresh does not flash the login screen at someone
 * who is already signed in.
 *
 * Routes are code-split: recharts (~512 kB) is used only by the dashboard and
 * pdf.js (~364 kB plus a 1.4 MB worker) only by the contract viewer. Imported
 * statically they land in the initial bundle, so the login screen would download
 * close to a megabyte before rendering a form with two inputs.
 */

import { Component, Suspense, lazy, useEffect } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';

import { LoadingSpinner } from '@/components/common/LoadingSpinner';
import { Layout } from '@/components/layout/Layout';
import { useAuth } from '@/lib/auth';
import { LoginPage } from '@/pages/LoginPage';

const AdminProjectsPage = lazy(() =>
  import('@/pages/admin/AdminProjectsPage').then((m) => ({ default: m.AdminProjectsPage })),
);
const AdminUsersPage = lazy(() =>
  import('@/pages/admin/AdminUsersPage').then((m) => ({ default: m.AdminUsersPage })),
);
const AlertsPage = lazy(() =>
  import('@/pages/AlertsPage').then((m) => ({ default: m.AlertsPage })),
);
const ClauseMasterPage = lazy(() =>
  import('@/pages/ClauseMasterPage').then((m) => ({ default: m.ClauseMasterPage })),
);
const ContractDetailPage = lazy(() =>
  import('@/pages/ContractDetailPage').then((m) => ({ default: m.ContractDetailPage })),
);
const ContractsPage = lazy(() =>
  import('@/pages/ContractsPage').then((m) => ({ default: m.ContractsPage })),
);
const CopilotPage = lazy(() =>
  import('@/pages/CopilotPage').then((m) => ({ default: m.CopilotPage })),
);
const DashboardPage = lazy(() =>
  import('@/pages/DashboardPage').then((m) => ({ default: m.DashboardPage })),
);
const JobsPage = lazy(() => import('@/pages/JobsPage').then((m) => ({ default: m.JobsPage })));
const SearchPage = lazy(() =>
  import('@/pages/SearchPage').then((m) => ({ default: m.SearchPage })),
);
const UploadPage = lazy(() =>
  import('@/pages/UploadPage').then((m) => ({ default: m.UploadPage })),
);

/**
 * Catches a failed lazy chunk load.
 *
 * Chunk file names are content-hashed, so a deploy while someone has the app open
 * makes the chunk they are about to request a 404. Without this the route renders
 * nothing and the app looks dead; a reload fetches the new index.html and its new
 * hashes, which is genuinely the fix.
 */
class RouteErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Route failed to load', error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
        <p className="font-semibold">This screen could not be loaded.</p>
        <p className="mt-1">
          This usually means the application was updated while you had it open.
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-3 rounded-lg border border-rose-200 px-2.5 py-1.5 text-xs font-medium text-rose-700 transition hover:bg-rose-100"
        >
          Reload
        </button>
      </div>
    );
  }
}

const ProtectedRoute = () => {
  const { user, initialising } = useAuth();
  const location = useLocation();

  if (initialising) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <LoadingSpinner label="Loading workspace..." />
      </div>
    );
  }
  if (!user) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  return <Outlet />;
};

const AdminRoute = () => {
  const { user } = useAuth();
  return user?.is_system_admin ? <Outlet /> : <Navigate to="/" replace />;
};

/**
 * The inverse gate: screens an administrator has no business on.
 *
 * Uploading is project-member work. The server enforces this (an administrator does
 * not hold `contract:upload` - see `ADMIN_EXCLUDED_PERMISSIONS`), and this mirrors
 * it in the router so the screen is never reachable rather than reachable and
 * guaranteed to fail on submit.
 */
const MemberRoute = () => {
  const { user } = useAuth();
  return user?.is_system_admin ? <Navigate to="/contracts" replace /> : <Outlet />;
};

const AppShell = () => (
  <Layout>
    <RouteErrorBoundary>
      <Suspense fallback={<LoadingSpinner label="Loading screen..." />}>
        <Outlet />
      </Suspense>
    </RouteErrorBoundary>
  </Layout>
);

export default function App() {
  const restore = useAuth((state) => state.restore);

  useEffect(() => {
    void restore();
  }, [restore]);

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route element={<MemberRoute />}>
            <Route path="upload" element={<UploadPage />} />
          </Route>
          <Route path="contracts" element={<ContractsPage />} />
          <Route path="contracts/:contractId" element={<ContractDetailPage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="copilot" element={<CopilotPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="alerts" element={<AlertsPage />} />
          <Route element={<AdminRoute />}>
            <Route path="clause-master" element={<ClauseMasterPage />} />
            <Route path="admin/projects" element={<AdminProjectsPage />} />
            <Route path="admin/users" element={<AdminUsersPage />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
