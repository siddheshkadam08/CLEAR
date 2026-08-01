/**
 * Where Microsoft sign-in lands.
 *
 * The backend has already done the work that matters: it exchanged the
 * authorization code, verified the id token's signature against Microsoft's
 * published keys, checked the audience, issuer and nonce, and resolved the local
 * account. What arrives here is a CLEAR access token, not a Microsoft one.
 *
 * That distinction is the whole design. A page that read the Microsoft token and
 * trusted the email inside it would be trusting a string the user can edit —
 * anyone can craft a JWT claiming to be anyone, and nothing a browser can do will
 * tell the difference. So this page never sees a Microsoft token, and the only
 * thing it does with what it *does* see is hand it to the API client.
 *
 * The token arrives in the URL fragment rather than the query string because
 * browsers do not send fragments to servers or write them into Referer headers.
 * It is stripped from the address bar immediately so it does not survive in
 * history, a screenshot, or a pasted "here's the page I'm on" message.
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { setAccessToken } from '@/api/client';
import { auth as authApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import { useAuth } from '@/lib/auth';

export function AuthCallbackPage() {
  const navigate = useNavigate();
  const [error, setError] = useState('');
  // React 18 runs effects twice in development StrictMode. The fragment is
  // consumed and cleared on the first pass, so without this guard the second
  // pass finds nothing and reports a failed sign-in that actually succeeded.
  const consumed = useRef(false);

  useEffect(() => {
    if (consumed.current) return;
    consumed.current = true;

    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const accessToken = fragment.get('access_token');
    const failure = fragment.get('error');
    const redirectAfter = fragment.get('redirect_after');

    // Clear the fragment before anything async. A token left in the address bar
    // outlives the page in browser history.
    window.history.replaceState(null, '', window.location.pathname);

    if (failure) {
      setError(failure);
      return;
    }

    if (!accessToken) {
      setError('Microsoft did not return a sign-in result. Please try again.');
      return;
    }

    void (async () => {
      try {
        setAccessToken(accessToken);
        // `/me` resolves memberships live, so this is also the check that the
        // account is still active and still has access — not merely that the
        // token parses.
        const user = await authApi.me();
        useAuth.setState({ user, initialising: false, error: null });
        navigate(safeRedirect(redirectAfter), { replace: true });
      } catch (caught) {
        setAccessToken(null);
        setError(errorMessage(caught));
      }
    })();
  }, [navigate]);

  if (error) {
    return (
      <Centred>
        <h1 style={{ fontSize: 18, fontWeight: 600, margin: '0 0 8px' }}>Sign-in failed</h1>
        <p style={{ fontSize: 14, color: '#5B6478', margin: '0 0 20px', maxWidth: 380 }}>{error}</p>
        <button
          type="button"
          onClick={() => navigate('/login', { replace: true })}
          style={{
            height: 40,
            padding: '0 20px',
            borderRadius: 8,
            border: '1px solid #2563EB',
            background: '#2563EB',
            color: '#fff',
            fontSize: 14,
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          Back to sign in
        </button>
      </Centred>
    );
  }

  return (
    <Centred>
      <div
        aria-hidden
        style={{
          width: 28,
          height: 28,
          border: '3px solid #E4E7EC',
          borderTopColor: '#2563EB',
          borderRadius: '50%',
          animation: 'clear-spin 0.8s linear infinite',
          marginBottom: 16,
        }}
      />
      <style>{'@keyframes clear-spin { to { transform: rotate(360deg) } }'}</style>
      <p style={{ fontSize: 14, color: '#5B6478' }}>Completing sign-in…</p>
    </Centred>
  );
}

function Centred({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#F7F8FA',
        fontFamily: "'Inter', sans-serif",
        color: '#0F172A',
        padding: 24,
        textAlign: 'center',
      }}
    >
      {children}
    </div>
  );
}

/**
 * Only ever redirect within this app.
 *
 * `redirect_after` originates from a query parameter, so an attacker can put
 * anything in it. Without this check a crafted sign-in link would send the user
 * to an external page immediately after authenticating — which is the moment
 * they are least likely to notice, and most likely to re-enter a credential.
 *
 * Rejecting `//evil.com` matters as much as rejecting `https://evil.com`: a
 * protocol-relative URL starts with a slash and would otherwise pass a naive
 * "must start with /" test.
 */
function safeRedirect(target: string | null): string {
  if (!target || !target.startsWith('/') || target.startsWith('//')) return '/';
  return target;
}

export default AuthCallbackPage;
