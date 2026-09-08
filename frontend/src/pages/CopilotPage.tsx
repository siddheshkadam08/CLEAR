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
import {
  Bot,
  Check,
  Copy,
  FileQuestion,
  MessageSquarePlus,
  PanelLeft,
  RotateCcw,
  SendHorizonal,
  Square,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { copilot as copilotApi, dashboard as dashboardApi } from '@/api/endpoints';
import { errorMessage } from '@/api/errors';
import type {
  ChatMessage,
  Citation,
  CopilotQueryMetadata,
  CopilotSource,
  CopilotStreamDone,
  PlanExplanation,
  ResponseFormat,
  UUID,
} from '@/api/types';
import { Badge } from '@/components/common/Badge';
import { getRiskVariant } from '@/lib/badges';
import { ErrorBanner, NoticeBanner } from '@/components/common/Banner';
import { Button } from '@/components/common/Button';
import { Card, PageHeader } from '@/components/common/Card';
import { EmptyState } from '@/components/common/EmptyState';
import { inputClasses, selectClasses, SelectChevron } from '@/components/common/Field';
import { Markdown } from '@/components/common/Markdown';
import { SourceSnippet } from '@/components/common/SourceSnippet';
import { formatDateTime, formatPercent, humanise } from '@/lib/format';
import { useProjectScope } from '@/lib/scope';

interface Turn {
  id: string;
  question: string;
  answer: string;
  citations: Citation[];
  /** Supporting passages with their similarity, from the `done` event. */
  sources: CopilotSource[];
  metadata?: CopilotQueryMetadata;
  confidence?: number;
  confidenceBand?: string;
  needsReview: boolean;
  refused: boolean;
  warnings: string[];
  plan?: PlanExplanation | null;
  streaming: boolean;
  error?: string;
}

/** Answer shapes the model is asked for. Empty means prose, the default. */
/** Answer shapes the API will actually accept.
 *
 * `bullet_points` and `table` used to be offered here. Neither is a member of the
 * backend `ResponseFormat` enum, so picking either returned 422 and the Copilot
 * answered nothing - two of the three options were dead. `response_format` was
 * typed as a bare `string`, which is why the compiler had nothing to say about
 * it. An empty value sends `null`, letting the server pick from the question's
 * intent, which is the better default for most questions. */
const FORMATS: { value: '' | ResponseFormat; label: string }[] = [
  { value: '', label: 'Automatic' },
  { value: 'executive_summary', label: 'Executive summary' },
  { value: 'risk_report', label: 'Risk report' },
  { value: 'action_items', label: 'Action items' },
  { value: 'timeline', label: 'Timeline' },
];

/**
 * Starter questions - one per answer format, shown and sent verbatim.
 *
 * Whole sentences, because the search matches on meaning and two words carry very
 * little of it: a bare "Key risks?" scores 0.2936 against a 0.35 floor and is
 * refused, while the sentence below clears it comfortably. Offering keywords would
 * teach exactly the habit that makes the product look broken.
 *
 * Each is paired with the format it suits and *sets that dropdown when clicked*, so
 * the control is discovered by using it rather than by reading five labels and
 * guessing which changes what.
 *
 * Every one was asked against the live corpus before being listed, and all five came
 * back answered with citations. That check is the point: a suggested question that
 * refuses is the product proposing something it cannot do. Two earlier drafts were
 * cut by it - "When do these agreements expire?" (no expiry dates extracted) and
 * "What obligations do these agreements place on us?", which fails on the word *us*:
 * the evidence never says which party the reader is.
 *
 * They are phrased across the corpus - "these agreements", not "this agreement" -
 * because that is what this page does and the contract drawer does not.
 */
const SUGGESTIONS: { question: string; format: '' | ResponseFormat }[] = [
  { question: 'What confidentiality obligations do these agreements impose?', format: '' },
  { question: 'Summarise the commercial terms of these agreements.', format: 'executive_summary' },
  { question: 'What are the key risks across these agreements?', format: 'risk_report' },
  { question: 'Which obligations fall due under these agreements?', format: 'action_items' },
  { question: 'What notice deadlines apply under these agreements?', format: 'timeline' },
];

export function CopilotPage() {
  const { projectId } = useProjectScope();
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState('');
  const [format, setFormat] = useState<'' | ResponseFormat>('');
  const [sessionId, setSessionId] = useState<UUID | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const sessionsQuery = useQuery({
    // Keyed on the business unit, which is what makes the list refetch when the
    // header changes. Without it you keep the previous unit's conversations, and
    // opening one gives a thread whose follow-ups search a different corpus.
    queryKey: ['copilot-sessions', projectId],
    queryFn: () => copilotApi.sessions(projectId),
  });

  /**
   * Whether this scope has anything to answer from.
   *
   * A business unit whose contracts have not finished processing has nothing
   * indexed, so every question is refused - correctly, and in wording that reads
   * exactly like broken retrieval. Better to say so before the question than to
   * take one and answer it with a shrug.
   *
   * Deliberately the *same* query key the dashboard uses, so this is usually
   * served from cache rather than costing a request. `clauses_extracted` is the
   * signal: no clause was extracted from anything here, so nothing was embedded
   * either. It is a proxy - clause detection can succeed where embedding failed -
   * which is why the message below talks about processing rather than promising
   * anything about the index. In that case this simply does not fire and the
   * user gets the ordinary refusal.
   */
  const { data: scopeOverview } = useQuery({
    queryKey: ['dashboard', projectId],
    queryFn: () => dashboardApi.overview(projectId),
    staleTime: 60_000,
  });
  const nothingIndexed =
    scopeOverview !== undefined &&
    (scopeOverview.kpis.find((kpi) => kpi.key === 'clauses_extracted')?.value ?? 0) === 0;

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

  /**
   * Ask a question and stream the answer.
   *
   * `turnId` is passed when retrying: the existing turn is reset and refilled in
   * place, so a retry replaces the failed answer rather than repeating the
   * question further down the thread.
   */
  const ask = useCallback(
    async (text: string, retryTurnId?: string, formatOverride?: '' | ResponseFormat) => {
      if (!text || streaming) return;

      // `formatOverride` exists for the starter questions, which set the dropdown
      // and ask in the same click: `setFormat` is asynchronous, so reading `format`
      // here would send the format selected *before* the click.
      const chosenFormat = formatOverride ?? format;

      const turnId = retryTurnId ?? `${Date.now()}:${text.slice(0, 24)}`;
      const blank: Turn = {
        id: turnId,
        question: text,
        answer: '',
        citations: [],
        sources: [],
        needsReview: false,
        refused: false,
        warnings: [],
        streaming: true,
      };

      setStreaming(true);
      setTurns((current) =>
        retryTurnId
          ? current.map((turn) => (turn.id === retryTurnId ? blank : turn))
          : [...current, blank],
      );

      const controller = new AbortController();
      abortRef.current = controller;

      const patch = (changes: Partial<Turn>) =>
        setTurns((current) =>
          current.map((turn) => (turn.id === turnId ? { ...turn, ...changes } : turn)),
        );

      try {
        // Open a session on the first question rather than waiting for "New chat".
        //
        // Without one the API is sent `session_id: null`, which it accepts and
        // answers - from *no history*. So the first answer looked right and every
        // follow-up was silently stateless: "what notice period does that require?"
        // resolved against nothing, and the transcript was never saved. Failing
        // visibly would have been better; answering plausibly is the bad case.
        let sid = sessionId;
        if (!sid) {
          sid = (await copilotApi.createSession({ project_id: projectId })).id;
          setSessionId(sid);
          await queryClient.invalidateQueries({ queryKey: ['copilot-sessions'] });
        }

        await copilotApi.stream(
          {
            query: text,
            project_id: projectId,
            session_id: sid,
            response_format: chosenFormat || null,
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
                const payload = data as CopilotStreamDone;
                patch({
                  citations: payload.citations ?? [],
                  sources: payload.sources ?? [],
                  metadata: payload.metadata,
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
    },
    [format, projectId, queryClient, sessionId, streaming],
  );

  function submit() {
    const text = question.trim();
    if (!text || streaming) return;
    setQuestion('');
    void ask(text);
  }

  const sessionList = (
    <>
      <p className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
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
                    : 'text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800',
                ].join(' ')}
              >
                <span className="block truncate text-sm font-medium">
                  {session.title ?? 'Untitled'}
                </span>
                <span className="mt-0.5 block text-xs text-slate-500 dark:text-slate-400">
                  {formatDateTime(session.updated_at ?? session.created_at)}
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          No conversations yet. Ask a question and one starts automatically.
        </p>
      )}
    </>
  );

  return (
    <div className="space-y-5">
      <PageHeader
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
            {turns.length === 0 && nothingIndexed ? (
              <EmptyState
                icon={FileQuestion}
                title="Nothing to search here yet"
                description="No contract in this business unit has finished processing, so there is nothing for the Copilot to read. Switch business unit, or upload a contract and wait for it to finish."
              />
            ) : turns.length === 0 ? (
              <Card className="text-center">
                <Bot className="mx-auto h-8 w-8 text-slate-300 dark:text-slate-600" aria-hidden />
                <p className="mt-2 text-sm font-medium text-slate-700 dark:text-slate-200">
                  Ask across every contract in this business unit
                </p>
                <p className="mx-auto mt-1 max-w-md text-xs text-slate-500 dark:text-slate-400">
                  Whole questions work better than keywords — the search matches on meaning,
                  and two words carry very little of it. Answers are drawn only from
                  contracts in the projects you belong to.
                </p>
                {/* Stacked rows rather than pills: these are sentences, and a pill
                    that wraps to three lines stops looking like one control. Each
                    shows exactly what it will ask, so the advice above and the
                    buttons below say the same thing. */}
                <div className="mx-auto mt-4 flex max-w-md flex-col gap-2">
                  {SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion.question}
                      type="button"
                      onClick={() => {
                        setFormat(suggestion.format);
                        void ask(suggestion.question, undefined, suggestion.format);
                      }}
                      className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-3 py-2 text-left text-xs font-medium leading-5 text-slate-600 transition hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
                    >
                      <span className="min-w-0">{suggestion.question}</span>
                      {/* The format each one will switch to, so the dropdown is
                          explained by using it rather than by guesswork. */}
                      <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500 dark:bg-slate-700 dark:text-slate-400">
                        {FORMATS.find((f) => f.value === suggestion.format)?.label}
                      </span>
                    </button>
                  ))}
                </div>
              </Card>
            ) : (
              turns.map((turn) => (
                <TurnView
                  key={turn.id}
                  turn={turn}
                  busy={streaming}
                  onRetry={() => void ask(turn.question, turn.id)}
                />
              ))
            )}
            <div ref={bottomRef} />
          </div>

          {/* Sticky composer: on a phone the thread scrolls under it, so the input
              is always where the thumb already is. */}
          <form
            className="sticky bottom-0 rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-3 shadow-lg sm:p-4"
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            {/* Disabled when there is nothing indexed: the only honest action in
                that state is to change business unit or wait for processing, and
                offering a question box implies one of them would be answered. */}
            <textarea
              rows={2}
              placeholder={
                nothingIndexed
                  ? 'No processed contracts in this business unit'
                  : 'Ask a question about your contracts'
              }
              aria-label="Your question"
              disabled={nothingIndexed}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  submit();
                }
              }}
              className={`${inputClasses} resize-none disabled:cursor-not-allowed disabled:bg-slate-50 dark:disabled:bg-slate-900`}
            />
            <div className="mt-2 flex items-center gap-2">
              <div className="relative w-36 sm:w-44">
                <select
                  value={format}
                  onChange={(event) => setFormat(event.target.value as '' | ResponseFormat)}
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
              </div>
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
                <Button
                  type="submit"
                  icon={SendHorizonal}
                  disabled={!question.trim() || nothingIndexed}
                >
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
function TurnView({
  turn,
  busy,
  onRetry,
}: {
  turn: Turn;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-br-md bg-blue-600 px-4 py-2.5 text-sm leading-6 text-white shadow-sm">
          {turn.question}
        </p>
      </div>

      <Card dense>
        {turn.plan && turn.streaming && !turn.answer ? (
          <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
            Searching {humanise(turn.plan.scope)} · {humanise(turn.plan.strategy)}…
          </p>
        ) : null}

        {turn.error ? (
          <ErrorBanner message={turn.error} onRetry={busy ? undefined : onRetry} />
        ) : (
          <>
            {turn.needsReview && !turn.streaming && !turn.metadata?.generationFailed ? (
              <div className="mb-3">
                <NoticeBanner message="This answer needs review: a citation could not be tied back to the retrieved evidence. Verify against the source before relying on it." />
              </div>
            ) : null}

            {/* Both of these describe a search that is not the one the reader would
                assume from their question. A thin answer from a widened or
                unnarrowed search reads exactly like a thin answer from a precise
                one unless it says so. */}
            {turn.metadata?.relaxedFilters && !turn.streaming ? (
              <div className="mb-3">
                <NoticeBanner message="Nothing matched within the document type this question appeared to be about, so the search was widened to every document type." />
              </div>
            ) : null}

            {turn.metadata?.scopeTruncated && !turn.streaming ? (
              <div className="mb-3">
                <NoticeBanner message="This question matched more contracts than can be ranked individually. Results are drawn from across the project, so a specific agreement may not be represented." />
              </div>
            ) : null}

            {turn.refused ? (
              <div className="mb-3 rounded-2xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 px-4 py-3 text-sm text-slate-600 dark:text-slate-300">
                The Copilot declined to answer from the evidence available.
              </div>
            ) : null}

            <div className="text-sm leading-7 text-slate-800 dark:text-slate-200">
              {/* Streaming text is rendered raw: a half-arrived `**` or an unclosed
                  table row makes the markdown parser reflow the whole answer on
                  every token. It is parsed once the stream closes. */}
              {turn.streaming ? (
                <span className="whitespace-pre-wrap">{turn.answer}</span>
              ) : (
                <Markdown>{turn.answer}</Markdown>
              )}
              {turn.streaming ? (
                <span
                  aria-hidden
                  className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-blue-600 align-middle"
                />
              ) : null}
            </div>

            {turn.warnings.length && !turn.streaming ? (
              <ul className="mt-3 space-y-1 text-xs text-slate-500 dark:text-slate-400">
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
                {turn.metadata?.generationFailed ? (
                  <Badge text="Not summarised" variant="warning" />
                ) : turn.metadata?.insufficientContext ? (
                  <Badge text="Not enough context" variant="warning" />
                ) : turn.citations.length === 0 && turn.answer && !turn.refused ? (
                  <Badge text="No citations" variant="warning" />
                ) : null}
                {turn.metadata?.documentTypeDetected && turn.metadata.documentType ? (
                  <Badge text={humanise(turn.metadata.documentType)} variant="neutral" />
                ) : null}

                <span className="ml-auto flex items-center gap-1">
                  <CopyAnswerButton text={turn.answer} />
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={RotateCcw}
                    disabled={busy}
                    onClick={onRetry}
                  >
                    Retry
                  </Button>
                </span>
              </div>
            ) : null}

            <SourceList turn={turn} />
          </>
        )}
      </Card>
    </div>
  );
}

/**
 * The passages behind an answer.
 *
 * Prefers `sources` - which carry the similarity score and are what the server
 * says the answer used - and falls back to `citations` for a turn rebuilt from a
 * stored session, where only the citations were persisted.
 */
function SourceList({ turn }: { turn: Turn }) {
  if (turn.streaming) return null;

  const rows = turn.sources.length
    ? turn.sources.map((source) => ({
        key: `s${source.label}`,
        label: source.label,
        contractId: source.contractId,
        title: source.contractName ?? 'Contract',
        heading: source.clauseHeading,
        section: source.sectionNumber,
        page: source.pageNumber != null ? `p.${source.pageNumber}` : null,
        similarity: source.similarityScore ?? null,
        matchType: source.matchType,
        text: source.text,
      }))
    : turn.citations.map((citation) => ({
        key: `c${citation.label}`,
        label: citation.label,
        contractId: citation.contract_id,
        title: citation.contract_title ?? 'Contract',
        heading: citation.section_title,
        section: citation.clause_number,
        page: citation.page_range || null,
        // A stored citation kept the fusion score, which is not a similarity and
        // must not be shown as one.
        similarity: null as number | null,
        matchType: 'semantic',
        text: citation.text,
      }));

  if (!rows.length) return null;

  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      <p className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
        Sources
      </p>
      <ol className="space-y-3">
        {rows.map((row) => (
          <li key={row.key} className="flex gap-3">
            <span className="shrink-0 rounded-lg bg-blue-50 px-2 py-0.5 font-mono text-xs font-semibold text-blue-700">
              [{row.label}]
            </span>
            <div className="min-w-0 flex-1">
              <Link
                to={`/contracts/${row.contractId}`}
                className="text-sm font-medium text-blue-600 hover:text-blue-700"
              >
                {row.title}
              </Link>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-slate-500 dark:text-slate-400">
                {row.heading ? (
                  <span className="font-medium text-slate-600 dark:text-slate-300">{row.heading}</span>
                ) : null}
                {row.section ? <span>Section {row.section}</span> : null}
                {row.page ? <span>{row.page}</span> : null}
                {row.similarity != null ? (
                  <span title="Cosine similarity between your question and this passage">
                    Similarity {row.similarity.toFixed(2)}
                  </span>
                ) : row.matchType === 'keyword' ? (
                  // A keyword hit has no similarity. Saying which search found it
                  // is honest; "Similarity 0.00" would not be.
                  <span title="Found by exact wording rather than by meaning">
                    Keyword match
                  </span>
                ) : null}
              </p>
              <SourceSnippet text={row.text} className="mt-1 text-xs leading-6 text-slate-500 dark:text-slate-400" />
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Copy the answer, confirming in place rather than with a toast. */
function CopyAnswerButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard access is denied outside a secure context and in some
      // browsers' permission settings. Nothing was copied, so the button simply
      // does not report success - an error banner over a failed copy would be
      // louder than the action itself.
    }
  }

  return (
    <Button
      variant="ghost"
      size="sm"
      icon={copied ? Check : Copy}
      disabled={!text}
      onClick={() => void copy()}
    >
      {copied ? 'Copied' : 'Copy'}
    </Button>
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
        sources: [],
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
