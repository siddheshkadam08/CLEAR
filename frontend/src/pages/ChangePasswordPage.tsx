/**
 * Change password.
 *
 * Serves two audiences that need different framing:
 *
 * - **Forced.** `must_change_password` is set on every account an administrator
 *   provisions, and `require_password_current` then rejects *every* endpoint
 *   except this one and logout. Until this screen existed a provisioned user
 *   could sign in and reach nothing at all, with no way out of the state. The
 *   copy says so rather than presenting an ordinary settings form.
 * - **Voluntary.** Someone changing a password they still know, from a link.
 *
 * The server does the confirmation check. Sending `confirm_password` rather than
 * comparing locally keeps one implementation of the rule, and the client's copy
 * of it cannot drift or be skipped.
 *
 * A successful change revokes every other session, so the user is signed out here
 * too - a browser left holding a rotated credential would fail its next refresh
 * and look like a bug rather than the security measure it is.
 */

import { useMutation } from '@tanstack/react-query';
import { KeyRound, ShieldAlert } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { auth as authApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card } from '@/components/common/Card';
import { inputClasses } from '@/components/common/Field';
import { useAuth } from '@/lib/auth';

export function ChangePasswordPage() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const forced = Boolean(user?.must_change_password);

  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);

  const change = useMutation({
    mutationFn: () => authApi.changePassword(current, next, confirm),
    onSuccess: () => {
      setDone(true);
      setError('');
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  async function finish() {
    // Clears the in-memory token as well as the cookie the server just dropped,
    // so the next screen starts from a clean unauthenticated state.
    await logout();
    navigate('/login', { replace: true });
  }

  if (done) {
    return (
      <Shell>
        <Card>
          <h1 className="font-display text-xl font-bold text-slate-900 dark:text-slate-100">
            Password changed
          </h1>
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
            Every other session has been signed out. Sign in again with your new
            password.
          </p>
          <div className="mt-5">
            <Button onClick={() => void finish()}>Go to sign in</Button>
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
            {forced ? <ShieldAlert className="h-5 w-5" /> : <KeyRound className="h-5 w-5" />}
          </span>
          <div className="min-w-0">
            <h1 className="font-display text-xl font-bold text-slate-900 dark:text-slate-100">
              {forced ? 'Choose a new password' : 'Change your password'}
            </h1>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              {forced
                ? 'Your account was created with a temporary password. Choose your own before continuing.'
                : 'You will be signed out of every other session.'}
            </p>
          </div>
        </div>

        {forced ? (
          <div className="mt-4">
            <NoticeBanner message="Until this is done, the rest of the workspace is unavailable." />
          </div>
        ) : null}

        <form
          className="mt-5 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            setError('');
            change.mutate();
          }}
        >
          <Field
            label={forced ? 'Temporary password' : 'Current password'}
            value={current}
            onChange={setCurrent}
            autoComplete="current-password"
          />
          <Field
            label="New password"
            value={next}
            onChange={setNext}
            autoComplete="new-password"
            hint="At least 8 characters."
          />
          <Field
            label="Confirm new password"
            value={confirm}
            onChange={setConfirm}
            autoComplete="new-password"
          />

          {error ? <ErrorBanner message={error} /> : null}

          <div className="flex items-center gap-3">
            <Button
              type="submit"
              busy={change.isPending}
              disabled={!current || !next || !confirm}
            >
              Change password
            </Button>
            {/* No "cancel" when forced: there is nowhere to cancel to. */}
            {forced ? null : (
              <Button variant="ghost" type="button" onClick={() => navigate(-1)}>
                Cancel
              </Button>
            )}
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
  autoComplete,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: string;
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
        autoComplete={autoComplete}
        onChange={(event) => onChange(event.target.value)}
        className={inputClasses}
      />
      {hint ? <span className="block text-xs text-slate-500">{hint}</span> : null}
    </label>
  );
}

export default ChangePasswordPage;
