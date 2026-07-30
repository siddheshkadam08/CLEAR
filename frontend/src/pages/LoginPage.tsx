import { Bot, Lock, Mail, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';

import { errorMessage } from '@/api/errors';
import { ErrorBanner } from '@/components/common/Banner';
import { useAuth } from '@/lib/auth';

export function LoginPage() {
  const { user, login, initialising } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const ssoEnabled = import.meta.env.VITE_ENABLE_MICROSOFT_SSO === 'true';
  const from = (location.state as { from?: string } | null)?.from ?? '/';

  if (!initialising && user) return <Navigate to={from} replace />;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await login(email, password);
      navigate(from, { replace: true });
    } catch (caught) {
      // Deliberately the server's wording: it says "invalid email or password"
      // without revealing which, so the form cannot enumerate accounts.
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-10">
      <div className="grid w-full max-w-5xl overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl lg:grid-cols-[1.05fr_0.95fr]">
        <div className="hidden bg-gradient-to-br from-blue-700 via-blue-600 to-slate-900 p-10 text-white lg:block">
          <div className="flex h-full flex-col justify-between">
            <div>
              <div className="inline-flex rounded-2xl bg-white/10 p-3 backdrop-blur">
                <Bot className="h-8 w-8" />
              </div>
              <h1 className="mt-6 text-4xl font-semibold leading-tight">
                Every contract answer, traced to the clause it came from.
              </h1>
              <p className="mt-4 max-w-lg text-sm leading-7 text-blue-100">
                Ingest PDFs, extract structured legal knowledge, and ask questions across the
                repository — with a page reference and a highlighted passage behind every claim.
              </p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-white/5 p-5 backdrop-blur">
              <p className="text-sm font-medium text-blue-50">
                Answers cite their evidence. When the contracts do not say, the answer says so
                rather than inventing one.
              </p>
            </div>
          </div>
        </div>

        <div className="p-8 sm:p-10">
          <div className="mx-auto max-w-md">
            <div className="mb-8 flex items-center gap-3">
              <div className="rounded-2xl bg-blue-50 p-3 text-blue-600">
                <ShieldCheck className="h-7 w-7" />
              </div>
              <div>
                <p className="text-sm font-semibold uppercase tracking-[0.2em] text-blue-600">
                  AI Workspace
                </p>
                <h2 className="text-2xl font-semibold text-slate-900">Sign in</h2>
              </div>
            </div>

            <form onSubmit={submit} className="space-y-5">
              <label className="block space-y-2 text-sm font-medium text-slate-700">
                <span>Email</span>
                <div className="flex items-center gap-3 rounded-2xl border border-slate-200 px-4 py-3 transition focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-100">
                  <Mail className="h-5 w-5 shrink-0 text-slate-400" />
                  <input
                    type="email"
                    autoComplete="username"
                    required
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="you@company.com"
                    className="w-full border-none bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
                  />
                </div>
              </label>

              <label className="block space-y-2 text-sm font-medium text-slate-700">
                <span>Password</span>
                <div className="flex items-center gap-3 rounded-2xl border border-slate-200 px-4 py-3 transition focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-100">
                  <Lock className="h-5 w-5 shrink-0 text-slate-400" />
                  <input
                    type="password"
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="••••••••"
                    className="w-full border-none bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
                  />
                </div>
              </label>

              {error ? <ErrorBanner message={error} /> : null}

              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-2xl bg-blue-600 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-blue-600/20 transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-70"
              >
                {busy ? 'Signing in…' : 'Sign in'}
              </button>

              {ssoEnabled ? (
                <a
                  href="/api/v1/auth/oidc/login"
                  className="block w-full rounded-2xl border border-slate-200 px-4 py-3 text-center text-sm font-semibold text-slate-700 transition hover:bg-slate-50"
                >
                  Sign in with Microsoft
                </a>
              ) : null}
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}

export default LoginPage;
