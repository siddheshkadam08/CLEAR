/**
 * Forgot password — step two, redeeming the link.
 *
 * The token arrives in the query string because the user gets here by clicking a
 * link in an email, which is a plain navigation carrying no headers. It is read
 * once into state rather than re-read from the URL on every render, so the field
 * stays valid if something later rewrites the location.
 *
 * A missing token is handled before the form is shown. Without that check the
 * user would fill in two password fields and only then be told the link was
 * malformed.
 *
 * As on `ChangePasswordPage`, `confirm_password` goes to the server rather than
 * being compared here: one implementation of the rule, in the place that cannot
 * be bypassed.
 */

import { useMutation } from '@tanstack/react-query';
import { CheckCircle2, KeyRound, ShieldAlert } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { auth as authApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card } from '@/components/common/Card';
import { inputClasses } from '@/components/common/Field';

export function ResetPasswordPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [token] = useState(() => params.get('token') ?? '');

  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);

  const reset = useMutation({
    mutationFn: () => authApi.resetPassword(token, next, confirm),
    onSuccess: () => {
      setDone(true);
      setError('');
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  if (!token) {
    return (
      <Shell>
        <Card>
          <div className="flex items-start gap-3">
            <span className="rounded-xl bg-rose-50 p-2 text-rose-600 dark:bg-rose-950 dark:text-rose-400">
              <ShieldAlert className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <h1 className="font-display text-xl font-bold text-slate-900 dark:text-slate-100">
                This link is incomplete
              </h1>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Some mail clients break long links across lines. Request a new one and
                open it in a single click.
              </p>
            </div>
          </div>
          <div className="mt-5">
            <Link to="/forgot-password">
              <Button>Request a new link</Button>
            </Link>
          </div>
        </Card>
      </Shell>
    );
  }

  if (done) {
    return (
      <Shell>
        <Card>
          <div className="flex items-start gap-3">
            <span className="rounded-xl bg-emerald-50 p-2 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400">
              <CheckCircle2 className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <h1 className="font-display text-xl font-bold text-slate-900 dark:text-slate-100">
                Password updated
              </h1>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Every existing session has been signed out. Sign in with your new
                password.
              </p>
            </div>
          </div>
          <div className="mt-5">
            <Button onClick={() => navigate('/login', { replace: true })}>
              Go to sign in
            </Button>
          </div>
        </Card>
      </Shell>
    );
  }

  return (
    <Shell>
      <Card>
        <div className="flex items-start gap-3">
          <span className="rounded-xl bg-blue-50 p-2 text-blue-600 dark:bg-blue-950 dark:text-blue-400">
            <KeyRound className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <h1 className="font-display text-xl font-bold text-slate-900 dark:text-slate-100">
              Choose a new password
            </h1>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              This link works once. Everyone signed in as you will be signed out.
            </p>
          </div>
        </div>

        <form
          className="mt-5 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            setError('');
            reset.mutate();
          }}
        >
          <Field
            label="New password"
            value={next}
            onChange={setNext}
            hint="At least 8 characters, with upper and lower case, a digit and a symbol."
          />
          <Field label="Confirm new password" value={confirm} onChange={setConfirm} />

          {error ? <ErrorBanner message={error} /> : null}

          <div className="flex items-center gap-3">
            <Button type="submit" busy={reset.isPending} disabled={!next || !confirm}>
              Set new password
            </Button>
            <Link
              to="/login"
              className="text-sm font-medium text-slate-500 transition hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
            >
              Back to sign in
            </Link>
          </div>
        </form>
      </Card>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4 dark:bg-slate-900">
      <div className="w-full max-w-md">{children}</div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
        {label}
      </span>
      <input
        type="password"
        required
        value={value}
        autoComplete="new-password"
        onChange={(event) => onChange(event.target.value)}
        className={inputClasses}
      />
      {hint ? <span className="block text-xs text-slate-500">{hint}</span> : null}
    </label>
  );
}

export default ResetPasswordPage;
