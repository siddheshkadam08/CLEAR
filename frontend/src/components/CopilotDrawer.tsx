import { Bot, SendHorizonal, Square, Sparkles, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
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

  async function ask() {
    const text = question.trim();
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
      setStreaming(false);
      abortRef.current = null;
    }
  }

  if (!open) return null;

  return (
    <div className="fixed right-0 top-0 z-50 flex h-full justify-end">
      <div className="relative flex h-full w-[28rem] flex-col border-l border-slate-200 bg-white shadow-2xl animate-in slide-in-from-right duration-200">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-slate-200 px-4 py-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-violet-600 text-white">
            <Sparkles className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold text-slate-900">Contract Copilot</h2>
            <p className="truncate text-xs text-slate-500">{contractTitle}</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Thread */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {turns.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-blue-50 to-violet-50">
                <Bot className="h-7 w-7 text-blue-600" />
              </div>
              <p className="text-sm font-medium text-slate-700">Ask about this contract</p>
              <p className="max-w-xs text-xs text-slate-500">
                Try: "Summarise the key risks" or "What are the payment terms?"
              </p>
              <div className="mt-2 flex flex-wrap justify-center gap-2">
                {['Key risks?', 'Payment terms?', 'Termination clauses?'].map((q) => (
                  <button key={q} type="button" onClick={() => { setQuestion(q); inputRef.current?.focus(); }} className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700">
                    {q}
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
                    {turn.plan && turn.streaming && !turn.answer ? (
                      <p className="mb-2 text-xs text-slate-500">
                        Searching {humanise(turn.plan.scope)} · {humanise(turn.plan.strategy)}…
                      </p>
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

                        <div className="whitespace-pre-wrap text-sm leading-7 text-slate-800">
                          {turn.answer}
                          {turn.streaming ? (
                            <span aria-hidden className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-blue-600 align-middle" />
                          ) : null}
                        </div>

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
                          <ol className="mt-3 space-y-2 border-t border-slate-200/70 pt-3">
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
        <form
          className="border-t border-slate-200 bg-white p-3"
          onSubmit={(e) => { e.preventDefault(); void ask(); }}
        >
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
              <Button variant="secondary" size="sm" icon={Square} onClick={() => abortRef.current?.abort()}>Stop</Button>
            ) : (
              <Button type="submit" size="sm" icon={SendHorizonal} disabled={!question.trim()}>Send</Button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
