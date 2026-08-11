/**
 * The quoted evidence under a citation, clamped until asked to expand.
 *
 * Citations carry whole clauses, and a clause is long: a definitions clause runs
 * past four hundred words. Printed in full, eight of them buried the answer they
 * were supporting - roughly seventy per cent of a reply was evidence nobody had
 * asked to read yet. Clamping inverts that without hiding anything: the answer
 * stays on screen, and the wording is one click away, which is the whole point
 * of showing a citation rather than asserting a fact.
 *
 * Clamped with `line-clamp`, not by truncating the string, so expanding needs no
 * second copy of the text and a mid-word cut never reaches the DOM. The toggle is
 * only rendered when the text actually overflows - a two-line snippet with a
 * "Show more" that reveals nothing is worse than no control at all.
 */

import { ChevronDown } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

export const SourceSnippet = ({
  text,
  className = 'text-xs leading-6 text-slate-500 dark:text-slate-400',
  lines = 3,
}: {
  text: string;
  className?: string;
  /** Lines shown while collapsed. Tailwind needs the literal class, so 2-4. */
  lines?: 2 | 3 | 4;
}) => {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef<HTMLParagraphElement>(null);

  // Measured rather than guessed from character count: whether three lines are
  // enough depends on the column width, which changes between the drawer and the
  // full page, and on the viewport.
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => setOverflows(node.scrollHeight > node.clientHeight + 1);
    measure();
    // Guarded rather than assumed. This runs inside the answer bubble, so a
    // missing constructor would take down the whole reply rather than just the
    // toggle; without an observer the snippet stays collapsed, which is the
    // safe direction.
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [text]);

  if (!text) return null;

  const clamp = { 2: 'line-clamp-2', 3: 'line-clamp-3', 4: 'line-clamp-4' }[lines];

  return (
    <div>
      <p ref={ref} className={`${className} ${expanded ? '' : clamp}`}>
        {text}
      </p>
      {/* Rendered once the text is known to overflow, or once it is expanded -
          the second case matters because an expanded block does not overflow,
          and dropping the control would strand the reader. */}
      {overflows || expanded ? (
        <button
          type="button"
          onClick={() => setExpanded((open) => !open)}
          className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400"
        >
          {expanded ? 'Show less' : 'Show more'}
          <ChevronDown
            className={`h-3 w-3 transition-transform ${expanded ? 'rotate-180' : ''}`}
            aria-hidden
          />
        </button>
      ) : null}
    </div>
  );
};

export default SourceSnippet;
