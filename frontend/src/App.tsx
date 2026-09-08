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
import { useAuth, useHasPermissionAnywhere } from '@/lib/auth';
import { AuthCallbackPage } from '@/pages/AuthCallbackPage';
// Static, like the two above: this is the only screen a user with
// `must_change_password` can reach, so a failed lazy chunk would strand them with
// no way to clear the flag and no way to use anything else.
import { ChangePasswordPage } from '@/pages/ChangePasswordPage';
// Static for the same reason: these two are reached from an email by someone who
// cannot sign in, so a failed lazy chunk would leave them with no route back.
import { ForgotPasswordPage } from '@/pages/ForgotPasswordPage';
import { LoginPage } from '@/pages/LoginPage';
import { ResetPasswordPage } from '@/pages/ResetPasswordPage';

const AdminProjectsPage = lazy(() =>
  import('@/pages/admin/AdminProjectsPage').then((m) => ({ default: m.AdminProjectsPage })),
);
const EvaluationPage = lazy(() =>
  import('@/pages/admin/EvaluationPage').then((m) => ({ default: m.EvaluationPage })),
);
const AuditPage = lazy(() =>
  import('@/pages/admin/AuditPage').then((m) => ({ default: m.AuditPage })),
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
const ExportsPage = lazy(() =>
  import('@/pages/ExportsPage').then((m) => ({ default: m.ExportsPage })),
);
const JobsPage = lazy(() => import('@/pages/JobsPage').then((m) => ({ default: m.JobsPage })));
const ClauseCoveragePage = lazy(() =>
  import('@/pages/ClauseCoveragePage').then((m) => ({ default: m.ClauseCoveragePage })),
);
const PortfolioPage = lazy(() =>
  import('@/pages/PortfolioPage').then((m) => ({ default: m.PortfolioPage })),
);
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

  // A forced password change is a hard gate, mirroring the server's.
  // `require_password_current` rejects every endpoint except change-password and
  // logout, so without this redirect a provisioned user signs in successfully and
  // then meets a 403 on whatever screen they land on - which reads as a broken
  // deployment rather than an action they need to take.
  if (user.must_change_password && location.pathname !== '/change-password') {
    return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
};

/** Anyone holding `audit:read` on a project, plus administrators. */
const AuditRoute = () => {
  const allowed = useHasPermissionAnywhere('audit:read');
  return allowed ? <Outlet /> : <Navigate to="/" replace />;
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
      {/* Outside ProtectedRoute: this is where a user arrives *before* they have
          a session, and guarding it would bounce them back to the login screen
          in a loop. */}
      <Route path="/auth/callback" element={<AuthCallbackPage />} />
      {/* Also public, and for the same reason: someone who has forgotten their
          password has no session to guard. The reset route must stay declared —
          the catch-all below redirects anything unknown to `/`, which would make
          a perfectly good link from an email look broken. */}
      <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      <Route element={<ProtectedRoute />}>
        {/* Outside AppShell: a user who must change their password cannot use the
            navigation the shell renders, and showing it would offer links that
            all answer 403. */}
        <Route path="change-password" element={<ChangePasswordPage />} />
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route element={<MemberRoute />}>
            <Route path="upload" element={<UploadPage />} />
          </Route>
          <Route path="contracts" element={<ContractsPage />} />
          <Route path="contracts/:contractId" element={<ContractDetailPage />} />
          <Route path="portfolio" element={<PortfolioPage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="copilot" element={<CopilotPage />} />
          <Route path="jobs" element={<JobsPage />} />
          {/* Not admin-gated: the endpoint scopes rows to the requester, so this
              screen only ever shows the caller their own exports. */}
          <Route path="exports" element={<ExportsPage />} />
          <Route path="clause-coverage" element={<ClauseCoveragePage />} />
          {/* Was `/doc-pipeline`. A bookmark is a promise; see LEGACY_PATHS in
              components/layout/navigation.ts. */}
          <Route path="doc-pipeline" element={<Navigate to="/clause-coverage" replace />} />
          <Route path="alerts" element={<AlertsPage />} />
          {/* Not under AdminRoute: AUDIT_READ belongs to Project Manager as
              well, so admin-only here would withdraw a permission the seeded
              roles grant. The endpoint enforces it and scopes the rows. */}
          <Route element={<AuditRoute />}>
            <Route path="admin/audit" element={<AuditPage />} />
          </Route>
          <Route element={<AdminRoute />}>
            <Route path="clause-master" element={<ClauseMasterPage />} />
            <Route path="admin/projects" element={<AdminProjectsPage />} />
            <Route path="admin/users" element={<AdminUsersPage />} />
            <Route path="admin/evaluation" element={<EvaluationPage />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
