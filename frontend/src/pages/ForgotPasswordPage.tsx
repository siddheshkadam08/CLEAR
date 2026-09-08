/**
 * Forgot password — step one, requesting the link.
 *
 * The screen is deliberately incurious about whether the address it was given
 * exists. The server answers identically for a registered address, an
 * unregistered one, a deactivated account and a Microsoft-only account, and this
 * page must not undo that by branching on the response: a "no such account"
 * message here would hand an attacker the account list the API is careful not to
 * give them. So the confirmation is shown on *success of the request*, which is
 * unconditional, and says "if that address has an account".
 *
 * Structure follows `ChangePasswordPage` — same `Shell`, `Field` and mutation
 * shape — rather than `LoginPage`, whose inline-styled marketing layout is a
 * different thing entirely.
 */

import { useMutation } from '@tanstack/react-query';
import { MailCheck, KeyRound } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { auth as authApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import { ErrorBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card } from '@/components/common/Card';
import { inputClasses } from '@/components/common/Field';

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [sent, setSent] = useState(false);

  const request = useMutation({
    mutationFn: () => authApi.forgotPassword(email.trim()),
    onSuccess: () => {
      setSent(true);
      setError('');
    },
    onError: (caught) => setError(errorMessage(caught)),
  });

  if (sent) {
    return (
      <Shell>
        <Card>
          <div className="flex items-start gap-3">
            <span className="rounded-xl bg-emerald-50 p-2 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400">
              <MailCheck className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <h1 className="font-display text-xl font-bold text-slate-900 dark:text-slate-100">
                Check your email
              </h1>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                If <span className="font-medium">{email.trim()}</span> has an account, a
                reset link is on its way. It expires shortly and can be used once.
              </p>
            </div>
          </div>
          <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
            Nothing arrived? Check the spam folder, or ask your administrator — accounts
            that sign in with Microsoft do not have a password to reset.
          </p>
          <div className="mt-5">
            <Link to="/login">
              <Button variant="secondary">Back to sign in</Button>
            </Link>
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
              Reset your password
            </h1>
            <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
              Enter the email address you sign in with and we will send you a link.
            </p>
          </div>
        </div>

        <form
          className="mt-5 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            setError('');
            request.mutate();
          }}
        >
          <label className="block space-y-1.5">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Email
            </span>
            <input
              type="email"
              required
              autoFocus
              value={email}
              autoComplete="email"
              placeholder="name@company.com"
              onChange={(event) => setEmail(event.target.value)}
              className={inputClasses}
            />
          </label>

          {error ? <ErrorBanner message={error} /> : null}

          <div className="flex items-center gap-3">
            <Button type="submit" busy={request.isPending} disabled={!email.trim()}>
              Send reset link
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

export default ForgotPasswordPage;
