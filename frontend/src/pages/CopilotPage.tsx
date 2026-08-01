/**
 * Contract Copilot.
 *
 * Grounding is the entire product here, so three states that a chat UI normally
 * hides are given first-class treatment:
 *
 * - **Refusal.** "The supplied contracts do not say" is a correct answer, not a
 *   failure. It is shown plainly rather than dressed up as an error.
 * - **Needs review.** Set when a citation could not be tied back to retrieved
 *   evidence. The answer is still shown - withholding it helps nobody - but it is
 *   marked, because an unverifiable citation is exactly the failure mode a lawyer
 *   must not have to detect for themselves.
 * - **Citations.** Numbered inline and listed underneath, each linking to the
 *   contract and page it came from.
 *
 * The conversation list is a sidebar above `lg` and a drawer below it: on a phone
 * the thread is the screen, and a permanent list would halve it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, MessageSquarePlus, PanelLeft, SendHorizonal, Square } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { copilot as copilotApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type { ChatMessage, Citation, PlanExplanation, UUID } from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { getRiskVariant } from '@/lib/badges';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses } from '@/components/common/Field';
import { formatDateTime, formatPercent, humanise } from '@/lib/format';
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

export function CopilotPage() {
  const { projectId } = useProjectScope();
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState('');
  // No control sets this any more - the format picker was removed from the
  // markup - but the value is still sent with the question.
  const [format] = useState('');
  const [sessionId, setSessionId] = useState<UUID | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const sessionsQuery = useQuery({
    queryKey: ['copilot-sessions'],
    queryFn: () => copilotApi.sessions(),
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns]);

  // Abort an in-flight stream when the user navigates away, otherwise the reader
  // keeps consuming the response after the component is gone.
  useEffect(() => () => abortRef.current?.abort(), []);

  const createSession = useMutation({
    mutationFn: () => copilotApi.createSession({ project_id: projectId }),
    onSuccess: async (session) => {
      setSessionId(session.id);
      setTurns([]);
      setSessionsOpen(false);
      await queryClient.invalidateQueries({ queryKey: ['copilot-sessions'] });
    },
  });

  async function loadSession(id: UUID) {
    const session = await copilotApi.session(id);
    setSessionId(id);
    setTurns(rebuildTurns(session.messages));
    setSessionsOpen(false);
  }

  async function ask() {
    const text = question.trim();
    if (!text || streaming) return;

    const turnId = `${turns.length}:${text.slice(0, 24)}`;
    setQuestion('');
    setStreaming(true);
    setTurns((current) => [
      ...current,
      {
        id: turnId,
        question: text,
        answer: '',
        citations: [],
        needsReview: false,
        refused: false,
        warnings: [],
        streaming: true,
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    const patch = (changes: Partial<Turn>) =>
      setTurns((current) =>
        current.map((turn) => (turn.id === turnId ? { ...turn, ...changes } : turn)),
      );

    try {
      await copilotApi.stream(
        {
          query: text,
          project_id: projectId,
          session_id: sessionId,
          response_format: format || null,
        },
        {
          signal: controller.signal,
          onEvent: (event, data) => {
            if (event === 'plan') {
              patch({ plan: data as PlanExplanation });
            } else if (event === 'token') {
              const token = (data as { text?: string }).text ?? '';
              setTurns((current) =>
                current.map((turn) =>
                  turn.id === turnId ? { ...turn, answer: turn.answer + token } : turn,
                ),
              );
            } else if (event === 'done') {
              const payload = data as {
                citations?: Citation[];
                confidence?: number;
                confidence_band?: string;
                needs_review?: boolean;
                warnings?: string[];
                text?: string | null;
              };
              patch({
                citations: payload.citations ?? [],
                confidence: payload.confidence,
                confidenceBand: payload.confidence_band,
                needsReview: Boolean(payload.needs_review),
                warnings: payload.warnings ?? [],
                streaming: false,
                // The server sends `text` only when it had to strip a fabricated
                // citation label. What was streamed is then stale and must be
                // replaced, not appended to.
                ...(payload.text ? { answer: payload.text } : {}),
              });
            } else if (event === 'error') {
              patch({
                streaming: false,
                error: (data as { message?: string }).message ?? 'The answer stream failed.',
              });
            }
          },
          onError: (error) => patch({ streaming: false, error: error.message }),
        },
      );
    } catch (caught) {
      patch({ streaming: false, error: errorMessage(caught) });
    } finally {
      setStreaming(false);
      abortRef.current = null;
      await queryClient.invalidateQueries({ queryKey: ['copilot-sessions'] });
    }
  }

  const sessionList = (
    <>
      <p className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
        Conversations
      </p>
      {sessionsQuery.data?.length ? (
        <div className="space-y-1">
          {sessionsQuery.data.map((session) => {
            const active = session.id === sessionId;
            return (
              <button
                key={session.id}
                type="button"
                onClick={() => void loadSession(session.id)}
                className={[
                  'block w-full rounded-xl px-3 py-2.5 text-left transition',
                  active
                    ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-100'
                    : 'text-slate-600 hover:bg-slate-100',
                ].join(' ')}
              >
                <span className="block truncate text-sm font-medium">
                  {session.title ?? 'Untitled'}
                </span>
                <span className="mt-0.5 block text-xs text-slate-500">
                  {formatDateTime(session.updated_at ?? session.created_at)}
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-slate-500">
          No conversations yet. Ask a question and one starts automatically.
        </p>
      )}
    </>
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Copilot"
        subtitle="Answers come only from your contracts, with a citation for every claim. When the contracts do not say, the answer says so."
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              icon={PanelLeft}
              className="lg:hidden"
              onClick={() => setSessionsOpen((open) => !open)}
            >
              History
            </Button>
            <Button
              variant="secondary"
              size="sm"
              icon={MessageSquarePlus}
              busy={createSession.isPending}
              onClick={() => createSession.mutate()}
            >
              New conversation
            </Button>
          </>
        }
      />

      {sessionsOpen ? (
        <Card dense className="lg:hidden">
          {sessionList}
        </Card>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[16rem_minmax(0,1fr)] lg:items-start">
        <Card
          dense
          className="hidden lg:sticky lg:top-24 lg:block lg:max-h-[calc(100vh-8rem)] lg:overflow-y-auto"
        >
          {sessionList}
        </Card>

        <div className="min-w-0 space-y-4">
          <div className="space-y-4">
            {turns.length === 0 ? (
              <EmptyState
                icon={Bot}
                title="Ask about your contracts"
                description="Try: “Which agreements let the counterparty terminate for convenience?” or “Summarise the indemnities in the Acme MSA.” Answers are drawn only from contracts in the projects you belong to."
              />
            ) : (
              turns.map((turn) => <TurnView key={turn.id} turn={turn} />)
            )}
            <div ref={bottomRef} />
          </div>

          {/* Sticky composer: on a phone the thread scrolls under it, so the input
              is always where the thumb already is. */}
          <form
            className="sticky bottom-0 rounded-2xl border border-slate-200 bg-white p-3 shadow-lg sm:p-4"
            onSubmit={(event) => {
              event.preventDefault();
              void ask();
            }}
          >
            <textarea
              rows={2}
              placeholder="Ask a question about your contracts"
              aria-label="Your question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  void ask();
                }
              }}
              className={`${inputClasses} resize-none`}
            />
            <div className="mt-2 flex items-center gap-2">
              {/* <div className="relative w-36 sm:w-44">
              <select
                value={format}
                onChange={(event) => setFormat(event.target.value)}
                aria-label="Answer format"
                className={selectClasses}
              >
                {FORMATS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <SelectChevron />
              </div> */}
              <span className="ml-auto" />
              {streaming ? (
                <Button
                  variant="secondary"
                  icon={Square}
                  onClick={() => abortRef.current?.abort()}
                >
                  Stop
                </Button>
              ) : (
                <Button type="submit" icon={SendHorizonal} disabled={!question.trim()}>
                  Ask
                </Button>
              )}
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Turn
// =============================================================================
function TurnView({ turn }: { turn: Turn }) {
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-br-md bg-blue-600 px-4 py-2.5 text-sm leading-6 text-white shadow-sm">
          {turn.question}
        </p>
      </div>

      <Card dense>
        {turn.plan && turn.streaming && !turn.answer ? (
          <p className="mb-3 text-xs text-slate-500">
            Searching {humanise(turn.plan.scope)} · {humanise(turn.plan.strategy)}…
          </p>
        ) : null}

        {turn.error ? (
          <ErrorBanner message={turn.error} />
        ) : (
          <>
            {turn.needsReview && !turn.streaming ? (
              <div className="mb-3">
                <NoticeBanner message="This answer needs review: a citation could not be tied back to the retrieved evidence. Verify against the source before relying on it." />
              </div>
            ) : null}

            {turn.refused ? (
              <div className="mb-3 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
                The Copilot declined to answer from the evidence available.
              </div>
            ) : null}

            <div className="whitespace-pre-wrap text-sm leading-7 text-slate-800">
              {turn.answer}
              {turn.streaming ? (
                <span
                  aria-hidden
                  className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-blue-600 align-middle"
                />
              ) : null}
            </div>

            {turn.warnings.length && !turn.streaming ? (
              <ul className="mt-3 space-y-1 text-xs text-slate-500">
                {turn.warnings.map((warning, index) => (
                  <li key={index}>{warning}</li>
                ))}
              </ul>
            ) : null}

            {!turn.streaming ? (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {turn.confidence !== undefined ? (
                  <Badge
                    text={`Confidence ${formatPercent(turn.confidence)}`}
                    variant={getRiskVariant(
                      turn.confidenceBand === 'high'
                        ? 'low'
                        : turn.confidenceBand === 'low'
                          ? 'high'
                          : turn.confidenceBand,
                    )}
                  />
                ) : null}
                {turn.citations.length === 0 && turn.answer && !turn.refused ? (
                  <Badge text="No citations" variant="warning" />
                ) : null}
              </div>
            ) : null}

            {turn.citations.length ? (
              <ol className="mt-4 space-y-3 border-t border-slate-100 pt-4">
                {turn.citations.map((citation) => (
                  <li key={citation.label} className="flex gap-3">
                    <span className="shrink-0 rounded-lg bg-blue-50 px-2 py-0.5 font-mono text-xs font-semibold text-blue-700">
                      [{citation.label}]
                    </span>
                    <div className="min-w-0">
                      <Link
                        to={`/contracts/${citation.contract_id}`}
                        className="text-sm font-medium text-blue-600 hover:text-blue-700"
                      >
                        {citation.contract_title ?? 'Contract'}
                        {citation.clause_number ? ` · ${citation.clause_number}` : ''}
                        {citation.page_range ? ` · p${citation.page_range}` : ''}
                      </Link>
                      <p className="mt-1 text-xs leading-6 text-slate-500">{citation.text}</p>
                    </div>
                  </li>
                ))}
              </ol>
            ) : null}
          </>
        )}
      </Card>
    </div>
  );
}

/** Rebuild the display turns from a stored session's flat message list. */
function rebuildTurns(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      turns.push({
        id: message.id,
        question: message.content,
        answer: '',
        citations: [],
        needsReview: false,
        refused: false,
        warnings: [],
        streaming: false,
      });
      continue;
    }

    const last = turns[turns.length - 1];
    // An assistant message with no preceding user message would be a malformed
    // session; skip it rather than inventing a question to attach it to.
    if (!last) continue;

    last.answer = message.content;
    last.citations = message.citations;
    last.confidence = message.confidence ?? undefined;
    last.needsReview = message.needs_review;
  }
  return turns;
}

export default CopilotPage;
