import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';

import { errorMessage } from '@/api/errors';
import { ErrorBanner } from '@/components/common/Banner';
import { useAuth } from '@/lib/auth';

const FEATURES = [
  { strong: 'Upload once', label: '— PDF, scanned copies, or Word' },
  { strong: 'Risk flagged automatically', label: '— matched to your clause master' },
  { strong: 'Search in seconds', label: '— not the usual hour-long PDF hunt' },
];

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
    <>
      <style>{`
        @keyframes clear-scan {
          0%   { transform: translateY(0);      opacity: 0; }
          8%   {                                 opacity: 1; }
          50%  { transform: translateY(290px);  opacity: 1; }
          58%  {                                 opacity: 0; }
          100% { transform: translateY(290px);  opacity: 0; }
        }
        @media (prefers-reduced-motion: reduce) {
          .clear-scan-line { animation: none !important; opacity: 0.6; top: 150px !important; }
        }
        .clear-field-input:focus {
          border-color: #2563EB !important;
          box-shadow: 0 0 0 3px rgba(37,99,235,0.14) !important;
        }
        .clear-btn-secondary:hover { background: #F7F8FA !important; }
        .clear-btn-primary:hover:not(:disabled) { background: #1D4ED8 !important; }
        .clear-panel-foot a:hover { color: #E7EAF0 !important; }
      `}</style>

      <div style={{ fontFamily: "'Inter', sans-serif", display: 'flex', minHeight: '100vh', background: '#F7F8FA', color: '#0F172A' }}>

        {/* ── Left panel ── */}
        <section style={{
          position: 'relative', flex: '0 0 44%', minWidth: '380px',
          background: '#0F172A', color: '#E7EAF0',
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
          padding: '56px 56px 40px', overflow: 'hidden',
        }}>
          {/* Brand */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', position: 'relative', zIndex: 2 }}>
            <img src="/image/irisclear.png" width="200" height="150" alt="C.L.E.A.R." style={{ flexShrink: 0, objectFit: 'contain' }} />
          </div>

          {/* Hero */}
          <div style={{ position: 'relative', zIndex: 2, marginTop: '64px', flexGrow: 1 }}>
            <div style={{
              fontFamily: "'IBM Plex Mono', monospace", fontSize: '11px',
              letterSpacing: '1.5px', color: '#10B981', textTransform: 'uppercase',
              marginBottom: '18px', display: 'flex', alignItems: 'center', gap: '8px',
            }}>
              <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#10B981', boxShadow: '0 0 0 4px rgba(16,185,129,0.18)', display: 'inline-block', flexShrink: 0 }} />
              Contract intelligence, live
            </div>
            <h1 style={{
              fontFamily: "'Manrope', sans-serif", fontWeight: 600,
              fontSize: '38px', lineHeight: 1.18, letterSpacing: '-0.5px',
              maxWidth: '460px', marginBottom: '18px',
            }}>
              Every clause,<br /><span style={{ color: '#7FA8F5' }}>accounted for.</span>
            </h1>
            <p style={{ fontSize: '15.5px', lineHeight: 1.65, color: '#8B96AC', maxWidth: '400px' }}>
              CLEAR reads your legacy agreements, checks each clause against your clause master,
              and flags what&apos;s risky before it becomes a problem.
            </p>

            {/* Feature list */}
            <div style={{ marginTop: '40px', display: 'flex', flexDirection: 'column', gap: '16px', position: 'relative', zIndex: 2 }}>
              {FEATURES.map(({ strong, label }) => (
                <div key={strong} style={{ display: 'flex', alignItems: 'flex-start', gap: '12px', fontSize: '14px' }}>
                  <span style={{
                    flexShrink: 0, width: '18px', height: '18px', borderRadius: '5px',
                    background: 'rgba(37,99,235,0.18)', border: '0.5px solid rgba(127,168,245,0.35)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center', marginTop: '1px',
                  }}>
                    <svg viewBox="0 0 10 10" width="10" height="10">
                      <path d="M1 5h8M5 1l4 4-4 4" stroke="#7FA8F5" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </span>
                  <div><strong>{strong}</strong> <span style={{ color: '#8B96AC' }}>{label}</span></div>
                </div>
              ))}
            </div>
          </div>

          {/* Decorative doc stack */}
          <div aria-hidden="true" style={{
            position: 'absolute', right: '-60px', top: '50%', transform: 'translateY(-46%)',
            width: '340px', height: '420px', zIndex: 1, opacity: 0.9,
          }}>
            <div style={{ position: 'absolute', width: '230px', height: '300px', borderRadius: '14px', background: '#22315A', border: '0.5px solid rgba(255,255,255,0.10)', top: '10px', left: '110px', transform: 'rotate(-1deg)' }} />
            <div style={{ position: 'absolute', width: '230px', height: '300px', borderRadius: '14px', background: '#1E2B4D', border: '0.5px solid rgba(255,255,255,0.10)', top: '20px', left: '100px', transform: 'rotate(2deg)' }} />
            <div style={{ position: 'absolute', width: '230px', height: '300px', borderRadius: '14px', background: '#16213B', border: '0.5px solid rgba(255,255,255,0.10)', top: '40px', left: '70px', transform: 'rotate(-6deg)' }}>
              <div style={{ position: 'absolute', top: '36px', left: '24px', right: '24px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                <i style={{ display: 'block', height: '6px', borderRadius: '3px', background: 'rgba(255,255,255,0.08)' }} />
                <i style={{ display: 'block', height: '6px', borderRadius: '3px', background: 'rgba(255,255,255,0.08)' }} />
                <i style={{ display: 'block', height: '6px', borderRadius: '3px', background: 'rgba(255,255,255,0.08)', width: '60%' }} />
                <i style={{ display: 'block', height: '6px', borderRadius: '3px', background: 'rgba(239,158,39,0.55)', width: '80%' }} />
                <i style={{ display: 'block', height: '6px', borderRadius: '3px', background: 'rgba(255,255,255,0.08)' }} />
              </div>
            </div>
            <div className="clear-scan-line" style={{
              position: 'absolute', left: '110px', top: '10px',
              width: '230px', height: '2px',
              background: 'linear-gradient(90deg, transparent, #7FA8F5, transparent)',
              filter: 'drop-shadow(0 0 6px #2563EB)',
              animation: 'clear-scan 3.6s ease-in-out infinite',
            }} />
          </div>

          {/* Footer links */}
          <div className="clear-panel-foot" style={{ position: 'relative', zIndex: 2, fontSize: '12.5px', color: '#8B96AC', display: 'flex', gap: '18px' }}>
            {['Privacy', 'Terms', 'Help'].map((label) => (
              <a key={label} href="#" style={{ color: 'inherit', textDecoration: 'none', transition: 'color .15s' }}>{label}</a>
            ))}
          </div>
        </section>

        {/* ── Right panel ── */}
        <section style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '40px 24px' }}>
          <div style={{ width: '100%', maxWidth: '380px' }}>
            {/* Header */}
            <div style={{ marginBottom: '28px' }}>
              <span style={{
                fontFamily: "'IBM Plex Mono', monospace", fontSize: '11px',
                letterSpacing: '1.5px', textTransform: 'uppercase',
                color: '#5B6478', marginBottom: '10px', display: 'block',
              }}>Legal workspace</span>
              <h2 style={{ fontFamily: "'Manrope', sans-serif", fontWeight: 600, fontSize: '24px', marginBottom: '6px', color: '#0F172A' }}>
                Sign in to CLEAR
              </h2>
              <p style={{ fontSize: '14px', color: '#5B6478' }}>Enter your work email to access your contracts.</p>
            </div>

            {/* Form */}
            <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: '16px', marginBottom: '18px' }}>
              <div>
                <label htmlFor="login-email" style={{ display: 'block', fontSize: '13px', fontWeight: 500, color: '#0F172A', marginBottom: '6px' }}>
                  Work email
                </label>
                <input
                  id="login-email"
                  className="clear-field-input"
                  type="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="name@company.com"
                  style={{
                    width: '100%', height: '42px', padding: '0 14px', borderRadius: '8px',
                    border: '1px solid #E4E7EC', background: '#fff', fontSize: '14px',
                    fontFamily: "'Inter', sans-serif", color: '#0F172A', outline: 'none',
                    transition: 'border-color .15s, box-shadow .15s',
                  }}
                />
              </div>

              <div>
                <label htmlFor="login-password" style={{ display: 'block', fontSize: '13px', fontWeight: 500, color: '#0F172A', marginBottom: '6px' }}>
                  Password
                </label>
                <input
                  id="login-password"
                  className="clear-field-input"
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter your password"
                  style={{
                    width: '100%', height: '42px', padding: '0 14px', borderRadius: '8px',
                    border: '1px solid #E4E7EC', background: '#fff', fontSize: '14px',
                    fontFamily: "'Inter', sans-serif", color: '#0F172A', outline: 'none',
                    transition: 'border-color .15s, box-shadow .15s',
                  }}
                />
              </div>

              {error ? <ErrorBanner message={error} /> : null}

              <button
                type="submit"
                disabled={busy}
                className="clear-btn-primary"
                style={{
                  height: '44px', borderRadius: '8px', border: '1px solid #2563EB',
                  background: '#2563EB', fontFamily: "'Inter', sans-serif",
                  fontSize: '14px', fontWeight: 500, color: '#fff',
                  cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.7 : 1,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  transition: 'background .15s',
                }}
              >
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </form>

            {ssoEnabled ? (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', margin: '6px 0', color: '#A6ACBB', fontSize: '12px' }}>
                  <span style={{ flex: 1, height: '1px', background: '#E4E7EC' }} />
                  or
                  <span style={{ flex: 1, height: '1px', background: '#E4E7EC' }} />
                </div>
                <a
                  href="/api/v1/auth/oidc/login"
                  className="clear-btn-secondary"
                  style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
                    height: '44px', borderRadius: '8px', border: '1px solid #E4E7EC',
                    background: '#fff', fontFamily: "'Inter', sans-serif",
                    fontSize: '14px', fontWeight: 500, color: '#0F172A',
                    textDecoration: 'none', marginBottom: '18px', width: '100%',
                    transition: 'background .15s',
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 21 21" fill="none">
                    <rect x="1" y="1" width="9" height="9" fill="#F25022" rx="1" />
                    <rect x="11" y="1" width="9" height="9" fill="#7FBA00" rx="1" />
                    <rect x="1" y="11" width="9" height="9" fill="#00A4EF" rx="1" />
                    <rect x="11" y="11" width="9" height="9" fill="#FFB900" rx="1" />
                  </svg>
                  Sign in with Microsoft
                </a>
              </>
            ) : null}
          </div>
        </section>
      </div>
    </>
  );
}

export default LoginPage;
