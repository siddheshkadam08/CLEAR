/**
 * Markdown renderer for model-generated answers.
 *
 * Answers arrive as markdown - lists of obligations, tables comparing clauses,
 * the occasional quoted definition - and rendering them as pre-wrapped text puts
 * literal asterisks and pipes in front of a lawyer, which reads as a broken
 * product rather than as formatting.
 *
 * Two constraints shape it:
 *
 * - **No raw HTML.** `react-markdown` disallows it by default and nothing here
 *   re-enables it. The input is model output rendered inside an authenticated
 *   session; a permissive renderer would turn a prompt-injected `<img onerror>`
 *   in a contract into script execution.
 * - **Wide content scrolls itself.** A comparison table is wider than the chat
 *   column, and a table that widens its parent makes the whole page scroll
 *   sideways. The overflow is contained here so no caller has to remember.
 */

import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export const Markdown = ({ children }: { children: string }) => (
  <div className="text-sm leading-7 text-slate-800">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children: content }) => <p className="mb-3 last:mb-0">{content}</p>,
        ul: ({ children: content }) => (
          <ul className="mb-3 list-disc space-y-1 pl-5 last:mb-0">{content}</ul>
        ),
        ol: ({ children: content }) => (
          <ol className="mb-3 list-decimal space-y-1 pl-5 last:mb-0">{content}</ol>
        ),
        li: ({ children: content }) => <li className="leading-6">{content}</li>,
        h1: ({ children: content }) => <Heading>{content}</Heading>,
        h2: ({ children: content }) => <Heading>{content}</Heading>,
        h3: ({ children: content }) => <Heading>{content}</Heading>,
        strong: ({ children: content }) => (
          <strong className="font-semibold text-slate-900">{content}</strong>
        ),
        a: ({ children: content, href }) => (
          <a
            href={href}
            // Model output can carry a link the user did not choose to follow;
            // `noreferrer` keeps the referrer off it and `noopener` denies the
            // opened page a handle back to this one.
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 underline underline-offset-2 hover:text-blue-700"
          >
            {content}
          </a>
        ),
        blockquote: ({ children: content }) => (
          <blockquote className="mb-3 border-l-2 border-slate-200 pl-3 text-slate-600 last:mb-0">
            {content}
          </blockquote>
        ),
        code: ({ children: content, className }) =>
          // `react-markdown` uses one component for both; a fenced block carries a
          // `language-*` class, inline code carries none.
          className?.startsWith('language-') ? (
            <code className={`${className} block`}>{content}</code>
          ) : (
            <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[0.85em] text-slate-800">
              {content}
            </code>
          ),
        pre: ({ children: content }) => (
          <pre className="mb-3 overflow-x-auto rounded-xl bg-slate-900 p-3 font-mono text-xs leading-6 text-slate-100 last:mb-0">
            {content}
          </pre>
        ),
        table: ({ children: content }) => (
          <div className="mb-3 overflow-x-auto last:mb-0">
            <table className="w-full border-collapse text-left text-xs">{content}</table>
          </div>
        ),
        th: ({ children: content }) => (
          <th className="border-b border-slate-200 px-2 py-1.5 font-semibold text-slate-700">
            {content}
          </th>
        ),
        td: ({ children: content }) => (
          <td className="border-b border-slate-100 px-2 py-1.5 align-top">{content}</td>
        ),
        hr: () => <hr className="my-4 border-slate-200" />,
      }}
    >
      {children}
    </ReactMarkdown>
  </div>
);

/**
 * One size for every heading level.
 *
 * A model writing inside a chat bubble picks `##` or `###` by habit, not by
 * document structure, so honouring the level would size two equally important
 * headings differently in the same answer.
 */
const Heading = ({ children }: { children: ReactNode }) => (
  <p className="mb-2 mt-3 font-semibold text-slate-900 first:mt-0">{children}</p>
);

export default Markdown;
