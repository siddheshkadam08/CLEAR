import { Bot, SendHorizonal, Square, Sparkles, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';

import { copilot as copilotApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { Citation, PlanExplanation, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { getRiskVariant } from '@/lib/badges';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { inputClasses } from '@/components/common/Field';
import { formatPercent, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

interface Turn {
  id: string;
  question: string;
  answer: string;
  citations: Citation[];
  confidence?: number;
  confidenceBand?: string;
  needsReview: boolean;
  refused: boolean;
  warnings: string[];
  plan?: PlanExplanation | null;
  streaming: boolean;
  error?: string;
}

interface CopilotDrawerProps {
  open: boolean;
  onClose: () => void;
  contractId: string;
  contractTitle: string;
}

/** What the wait says, and from which second. An answer takes 18-22s end to end:
 *  four of those pass before the first event arrives, so a single fixed line sits
 *  unchanged long enough to read as a hung request. These track the work actually
 *  happening, and the last one sets an expectation rather than pretending. */
const WAIT_STAGES: readonly { after: number; text: string }[] = [
  { after: 0, text: 'Reading the contract' },
  { after: 4, text: 'Searching the clauses' },
  { after: 9, text: 'Weighing the evidence' },
  { after: 15, text: 'Writing the answer' },
  { after: 25, text: 'Still working — long contracts take a little longer' },
];

function WaitingIndicator({ scope }: { scope: string | null }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  // Latest stage whose threshold has passed. `WAIT_STAGES` starts at 0 so one
  // always matches, but the fallback is spelled out rather than asserted -
  // indexing is only sound here because of a property of the data.
  const stage = [...WAIT_STAGES].reverse().find((s) => elapsed >= s.after);
  // Once the plan has arrived the scope is known and worth saying, but only
  // while it is still the search that is running - after that it is stale.
  const label =
    scope && elapsed < 15 ? `Searching ${scope}` : (stage?.text ?? 'Reading the contract');

  return (
    <div className="flex items-center gap-2 text-xs text-slate-500" role="status" aria-live="polite">
      <span className="flex gap-1" aria-hidden>
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" />
      </span>
      <span>
        {label}…{elapsed >= 5 ? <span className="ml-1 tabular-nums text-slate-400">{elapsed}s</span> : null}
      </span>
    </div>
  );
}

/** Starter questions, shown and sent verbatim.
 *
 *  Every one of these was asked against the three contracts in the corpus before
 *  being listed, and all twelve came back answered with citations. That check is
 *  the point of the list: a suggested question that refuses is the product
 *  proposing something it cannot do, which is worse than suggesting nothing.
 *
 *  The shape matters as much as the topic - one clause named outright, six to
 *  eight words, and no "and". A question covering two subjects lands between them
 *  in embedding space and matches neither strongly, the same dilution that sinks
 *  a bare "Key risks?" to 0.2936 against a 0.35 gate. An earlier draft here asked
 *  "What are the payment terms and when is payment due?" for that reason. */
const SUGGESTIONS = [
  'What are the key risks in this agreement?',
  'What is the limitation of liability?',
  'What are the payment terms under this agreement?',
  'What is the notice period for termination?',
] as const;

export function CopilotDrawer({ open, onClose, contractId, contractTitle }: CopilotDrawerProps) {
  const { projectId } = useProjectScope();
  const [question, setQuestion] = useState('');
  const [sessionId, setSessionId] = useState<UUID | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('keydown', onKey); };
  }, [open, onClose]);

  // `override` exists for the starter questions: `setQuestion` is asynchronous, so
  // setting state and calling `ask()` in the same handler sends whatever was in
  // the box beforehand - usually the empty string, which returns silently.
  async function ask(override?: string) {
    const text = (override ?? question).trim();
    if (!text || streaming) return;

    let sid = sessionId;
    if (!sid) {
      const session = await copilotApi.createSession({ project_id: projectId, contract_id: contractId, title: contractTitle });
      sid = session.id;
      setSessionId(sid);
    }

    const turnId = `${turns.length}:${text.slice(0, 24)}`;
    setQuestion('');
    setStreaming(true);
    setTurns((cur) => [...cur, { id: turnId, question: text, answer: '', citations: [], needsReview: false, refused: false, warnings: [], streaming: true }]);

    const controller = new AbortController();
    abortRef.current = controller;

    const patch = (changes: Partial<Turn>) =>
      setTurns((cur) => cur.map((t) => (t.id === turnId ? { ...t, ...changes } : t)));

    try {
      await copilotApi.stream(
        { query: text, project_id: projectId, contract_ids: [contractId], session_id: sid, scope: 'contract' },
        {
          signal: controller.signal,
          onEvent: (event, data) => {
            if (event === 'plan') {
              patch({ plan: data as PlanExplanation });
            } else if (event === 'token') {
              const token = (data as { text?: string }).text ?? '';
              setTurns((cur) => cur.map((t) => (t.id === turnId ? { ...t, answer: t.answer + token } : t)));
            } else if (event === 'done') {
              const p = data as { citations?: Citation[]; confidence?: number; confidence_band?: string; needs_review?: boolean; warnings?: string[]; text?: string | null };
              patch({ citations: p.citations ?? [], confidence: p.confidence, confidenceBand: p.confidence_band, needsReview: Boolean(p.needs_review), warnings: p.warnings ?? [], streaming: false, ...(p.text ? { answer: p.text } : {}) });
            } else if (event === 'error') {
              patch({ streaming: false, error: (data as { message?: string }).message ?? 'The answer stream failed.' });
            }
          },
          onError: (err) => patch({ streaming: false, error: err.message }),
        },
      );
    } catch (caught) {
      patch({ streaming: false, error: errorMessage(caught) });
    } finally {
      // Clear the turn as well as the composer. Only the `done` and `error`
      // handlers used to do it, so a stream that closed without dispatching
      // either left this turn marked streaming for ever - the spinner ran past
      // four minutes on a request the server had long since finished. Whatever
      // the stream did, once the reader is exhausted the turn is not streaming.
      patch({ streaming: false });
      setStreaming(false);
      abortRef.current = null;
    }
  }

  if (!open) return null;

  // Portalled to `document.body` rather than rendered where it is declared. The
  // drawer is `position: fixed`, and `fixed` is only relative to the viewport
  // while no ancestor establishes a containing block - any `transform`,
  // `filter`, `backdrop-filter`, `will-change` or `contain` on the chain from
  // the page down silently re-anchors it, which is what left a strip of page
  // showing above the header. A portal takes it out of that chain for good, and
  // out of reach of any ancestor `overflow` that would clip it.
  return createPortal(
    // `inset-y-0` rather than `top-0 h-full`: a fixed element's percentage height
    // resolves against the viewport *unless* an ancestor establishes a containing
    // block (a `transform`, `filter` or `will-change` anywhere above it does), and
    // then `h-full` silently collapses. Pinning both edges cannot be defeated that
    // way.
    //
    // The panel takes `h-full`, not `100dvh`. The two are not the same box: the
    // wrapper is already pinned to both edges of whatever contains it, while
    // `dvh` always measures the viewport. Where those disagree - and they do
    // whenever an ancestor establishes a containing block - the panel is taller
    // than the space it sits in and the composer runs off the bottom edge, which
    // is exactly how the drawer was clipping.
    <div className="fixed inset-y-0 right-0 z-50 flex justify-end">
      <div
        className="relative flex h-full max-h-full w-screen max-w-[28rem] flex-col overflow-hidden border-l border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-2xl animate-in slide-in-from-right duration-200"
      >
        {/* Header. `shrink-0` because a flex item's default `min-height: auto`
            lets it be compressed by a long thread - the title and close button
            are the last things that should give way. */}
        <div className="flex shrink-0 items-center gap-3 border-b border-slate-200 dark:border-slate-700 px-4 py-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-violet-600 text-white">
            <Sparkles className="h-4 w-4" />
          </div>
          {/* Title only. The subtitle carried `contractTitle`, which for an
              untitled contract falls back to the agreement type in caps
              ("SPONSORSHIP AGREEMENT") - a label the user already sees on the
              page behind the drawer, shouting a second time. */}
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">Contract Copilot</h2>
          </div>
          <button type="button" onClick={onClose} className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Thread. `min-h-0` is what actually makes this scroll: a flex item's
            default `min-height: auto` refuses to shrink below its content, so
            `flex-1 overflow-y-auto` alone grows the column instead of scrolling
            and pushes the composer off the bottom of the drawer. */}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
          {turns.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-blue-50 to-violet-50">
                <Bot className="h-7 w-7 text-blue-600" />
              </div>
              <p className="text-sm font-medium text-slate-700">Ask about this contract</p>
              <p className="max-w-xs text-xs text-slate-500">
                Whole questions work better than keywords — the search matches on
                meaning, and two words carry very little of it.
              </p>
              {/* Stacked rows, not pills: these are sentences, and a pill that
                  wraps to three lines stops looking like one control. Each shows
                  exactly what it will ask, so the advice above and the buttons
                  below say the same thing - the previous short labels read as the
                  keyword prompts the panel had just warned against. */}
              <div className="mt-2 flex w-full max-w-xs flex-col gap-2">
                {SUGGESTIONS.map((suggested) => (
                  <button
                    key={suggested}
                    type="button"
                    onClick={() => void ask(suggested)}
                    className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-2 text-left text-xs font-medium leading-5 text-slate-600 dark:text-slate-300 transition hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700"
                  >
                    {suggested}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              {turns.map((turn) => (
                <div key={turn.id} className="space-y-3">
                  {/* User bubble */}
                  <div className="flex justify-end">
                    <p className="max-w-[85%] rounded-2xl rounded-br-md bg-gradient-to-r from-blue-600 to-blue-700 px-4 py-2.5 text-sm leading-6 text-white shadow-sm">
                      {turn.question}
                    </p>
                  </div>

                  {/* Assistant */}
                  <div className="rounded-2xl border border-slate-100 bg-slate-50/50 px-4 py-3">
                    {/* Waiting state. Shown from the moment the question is sent,
                        not from the moment the plan arrives - retrieval and query
                        analysis take a second or two before the first event, and
                        an empty bubble in that window reads as a broken app. */}
                    {turn.streaming && !turn.answer ? (
                      <WaitingIndicator scope={turn.plan ? humanise(turn.plan.scope) : null} />
                    ) : null}

                    {turn.error ? (
                      <ErrorBanner message={turn.error} />
                    ) : (
                      <>
                        {turn.needsReview && !turn.streaming ? (
                          <div className="mb-2">
                            <NoticeBanner message="Verify this answer against the source — a citation could not be confirmed." />
                          </div>
                        ) : null}

                        {/* An empty bubble is the worst outcome: it reads as a
                            crash. A refusal that arrives with no text - or an
                            answer the stream dropped - gets said out loud here
                            rather than rendering as nothing. */}
                        {!turn.streaming && !turn.answer ? (
                          <div className="space-y-2 text-sm leading-7 text-slate-600">
                            <p>
                              I could not find wording in this contract that answers
                              that, so I would rather say so than guess.
                            </p>
                            <p className="text-xs leading-6 text-slate-500">
                              Asking in a full sentence usually fixes it — “What is
                              the limitation of liability?” finds an answer where
                              “liability?” does not. If the contract was uploaded
                              recently, it may still be processing.
                            </p>
                          </div>
                        ) : (
                          <div className="whitespace-pre-wrap text-sm leading-7 text-slate-800">
                            {turn.answer}
                            {turn.streaming && turn.answer ? (
                              <span aria-hidden className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-blue-600 align-middle" />
                            ) : null}
                          </div>
                        )}

                        {!turn.streaming && (turn.confidence !== undefined || turn.citations.length === 0) ? (
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            {turn.confidence !== undefined ? (
                              <Badge text={`Confidence ${formatPercent(turn.confidence)}`} variant={getRiskVariant(turn.confidenceBand === 'high' ? 'low' : turn.confidenceBand === 'low' ? 'high' : turn.confidenceBand)} />
                            ) : null}
                            {turn.citations.length === 0 && turn.answer && !turn.refused ? (
                              <Badge text="No citations" variant="warning" />
                            ) : null}
                          </div>
                        ) : null}

                        {turn.citations.length ? (
                          <ol className="mt-3 space-y-2 border-t border-slate-200/70 pt-3 dark:border-slate-700/70">
                            {turn.citations.map((c) => (
                              <li key={c.label} className="flex gap-2">
                                <span className="shrink-0 rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-blue-700">[{c.label}]</span>
                                <div className="min-w-0">
                                  <Link to={`/contracts/${c.contract_id}`} className="text-xs font-medium text-blue-600 hover:text-blue-700">
                                    {c.contract_title ?? 'Contract'}{c.clause_number ? ` · ${c.clause_number}` : ''}{c.page_range ? ` · p${c.page_range}` : ''}
                                  </Link>
                                  <p className="mt-0.5 text-[11px] leading-5 text-slate-500">{c.text}</p>
                                </div>
                              </li>
                            ))}
                          </ol>
                        ) : null}
                      </>
                    )}
                  </div>
                </div>
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* Composer */}
        {/* `env(safe-area-inset-bottom)` keeps the send row clear of the home
            indicator on iOS and of any browser UI that overlays the bottom edge;
            it is 0 on a desktop, so the `max()` keeps the normal padding there. */}
        <form
          className="shrink-0 border-t border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
          onSubmit={(e) => { e.preventDefault(); void ask(); }}
        >
          {/* `items-end` so the button stays on the last line as the textarea
              grows, and a matching `h-10` so the two are the same height on the
              first line - `size="sm"` is `h-8` against a 2.5rem box, which read
              as the button sitting low. */}
          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              rows={1}
              placeholder="Ask about this contract…"
              aria-label="Your question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void ask(); } }}
              className={`${inputClasses} max-h-24 min-h-[2.5rem] flex-1 resize-none`}
            />
            {streaming ? (
              <Button className="h-10 shrink-0" variant="secondary" size="sm" icon={Square} onClick={() => abortRef.current?.abort()}>Stop</Button>
            ) : (
              <Button className="h-10 shrink-0" type="submit" size="sm" icon={SendHorizonal} disabled={!question.trim()}>Send</Button>
            )}
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}
