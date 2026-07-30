/**
 * PDF viewer with evidence highlighting.
 *
 * This component is the reason coordinates are mandatory on every positioned
 * element in the canonical document model: a claim about a contract is only
 * checkable if the reader can be taken to the exact words it came from. Clicking a
 * clause, a risk or a citation lands here, on that page, with the passage outlined.
 *
 * Coordinates arrive in the page's own units together with the page size they were
 * measured against, so highlights are placed as a *fraction* of the rendered page
 * rather than by assuming the parser and pdf.js agree on units. They frequently do
 * not - the parser reports inches or points depending on the source - and a scale
 * assumption that is wrong by 72x puts every highlight off-screen.
 */

import { ChevronLeft, ChevronRight, Minus, Plus } from 'lucide-react';
import * as pdfjs from 'pdfjs-dist';
import type { PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import type { BoundingBox } from '@/api/types';

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

export interface PdfViewerProps {
  /** Signed, expiring URL from `GET /contracts/{id}/file`. */
  url: string;
  /** Boxes to outline. Pages are derived from the boxes themselves. */
  highlights?: BoundingBox[];
  /** Page to show. Ignored once the user navigates by hand. */
  page?: number;
  /** Bumping this re-centres the viewer even if `page` is unchanged. */
  focusToken?: number | string;
}

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;

export function PdfViewer({ url, highlights = [], page, focusToken }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<PDFDocumentProxy | null>(null);
  // pdf.js rejects a second render on the same canvas while one is in flight, so
  // the previous task is cancelled rather than left to collide.
  const renderTaskRef = useRef<{ cancel: () => void } | null>(null);

  const [pageCount, setPageCount] = useState(0);
  const [current, setCurrent] = useState(page ?? 1);
  const [zoom, setZoom] = useState(1);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // ---------------------------------------------------------------- document
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    // `withCredentials` is off deliberately: the document URL is pre-signed, so
    // it carries its own authorisation and must not also ride the session cookie.
    const task = pdfjs.getDocument({ url, withCredentials: false });

    task.promise.then(
      (doc) => {
        if (cancelled) {
          void doc.destroy();
          return;
        }
        docRef.current = doc;
        setPageCount(doc.numPages);
        setLoading(false);
      },
      (caught: unknown) => {
        if (cancelled) return;
        setLoading(false);
        setError(
          caught instanceof Error && /expired|403|401/i.test(caught.message)
            ? 'The document link has expired. Reload the page to get a fresh one.'
            : 'This document could not be displayed.',
        );
      },
    );

    return () => {
      cancelled = true;
      void task.destroy();
      docRef.current = null;
    };
  }, [url]);

  // ------------------------------------------------------------------ paging
  useEffect(() => {
    if (page && page >= 1) setCurrent(page);
    // `focusToken` is in the dependency list so clicking the same citation twice
    // still brings the viewer back to it.
  }, [page, focusToken]);

  // ----------------------------------------------------------------- render
  const renderPage = useCallback(async () => {
    const doc = docRef.current;
    const canvas = canvasRef.current;
    if (!doc || !canvas) return;

    const pageNumber = Math.min(Math.max(current, 1), doc.numPages);
    const pdfPage = await doc.getPage(pageNumber);

    // Fit to the container width, then apply the user's zoom on top, so the
    // default view is readable without horizontal scrolling on any screen.
    const unscaled = pdfPage.getViewport({ scale: 1 });
    const available = (containerRef.current?.clientWidth ?? unscaled.width) - 24;
    const fit = available > 0 ? available / unscaled.width : 1;
    const viewport = pdfPage.getViewport({ scale: fit * zoom });

    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;

    const context = canvas.getContext('2d');
    if (!context) return;

    renderTaskRef.current?.cancel();
    const task = pdfPage.render({
      canvasContext: context,
      viewport,
      // Draw at device pixel density so the page is not blurry on a retina
      // display, while the CSS size above keeps the layout in CSS pixels.
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    });
    renderTaskRef.current = task;

    try {
      await task.promise;
      setSize({ width: viewport.width, height: viewport.height });
    } catch (caught) {
      // A cancelled render is the expected outcome of paging quickly; anything
      // else is worth surfacing.
      if ((caught as { name?: string })?.name !== 'RenderingCancelledException') {
        setError('This page could not be rendered.');
      }
    }
  }, [current, zoom]);

  useEffect(() => {
    if (!loading && !error) void renderPage();
  }, [renderPage, loading, error]);

  // Re-render on resize so the fit-to-width scale stays correct.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => void renderPage());
    });
    observer.observe(container);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [renderPage]);

  useEffect(() => () => renderTaskRef.current?.cancel(), []);

  const onPage = highlights.filter((box) => box.page_number === current);
  const pagesWithEvidence = Array.from(new Set(highlights.map((box) => box.page_number))).sort(
    (a, b) => a - b,
  );

  if (error) {
    return (
      <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
        {error}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm"
    >
      {/* The toolbar wraps rather than scrolls: on a phone the page controls end up
          on one line and the zoom controls on the next, which is fine - both stay
          reachable. A horizontally scrolling toolbar hides its right-hand end. */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2">
        <div className="flex items-center gap-1">
          <ViewerButton
            label="Previous page"
            disabled={current <= 1}
            onClick={() => setCurrent((value) => Math.max(1, value - 1))}
          >
            <ChevronLeft className="h-4 w-4" />
          </ViewerButton>
          <span className="min-w-[4.5rem] text-center font-mono text-xs tabular-nums text-slate-600">
            {current} / {pageCount || '—'}
          </span>
          <ViewerButton
            label="Next page"
            disabled={current >= pageCount}
            onClick={() => setCurrent((value) => Math.min(pageCount, value + 1))}
          >
            <ChevronRight className="h-4 w-4" />
          </ViewerButton>
        </div>

        {pagesWithEvidence.length > 0 ? (
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-slate-500">Evidence</span>
            {pagesWithEvidence.slice(0, 6).map((pageNumber) => (
              <button
                key={pageNumber}
                type="button"
                onClick={() => setCurrent(pageNumber)}
                aria-pressed={pageNumber === current}
                className={[
                  'rounded-full px-2 py-1 text-xs font-medium ring-1 ring-inset transition',
                  pageNumber === current
                    ? 'bg-blue-600 text-white ring-blue-600'
                    : 'bg-white text-slate-600 ring-slate-200 hover:bg-slate-100',
                ].join(' ')}
              >
                p{pageNumber}
              </button>
            ))}
          </div>
        ) : null}

        <div className="ml-auto flex items-center gap-1">
          <ViewerButton
            label="Zoom out"
            onClick={() => setZoom((value) => Math.max(MIN_ZOOM, value - 0.25))}
          >
            <Minus className="h-4 w-4" />
          </ViewerButton>
          <span className="w-11 text-center font-mono text-xs tabular-nums text-slate-600">
            {Math.round(zoom * 100)}%
          </span>
          <ViewerButton
            label="Zoom in"
            onClick={() => setZoom((value) => Math.min(MAX_ZOOM, value + 0.25))}
          >
            <Plus className="h-4 w-4" />
          </ViewerButton>
        </div>
      </div>

      <div className="max-h-[70vh] overflow-auto bg-slate-100 p-3 xl:max-h-[calc(100vh-14rem)]">
        {loading ? <div className="h-96 animate-pulse rounded-xl bg-slate-200" /> : null}
        <div
          className="relative mx-auto bg-white shadow-md"
          style={size ? { width: size.width, height: size.height } : undefined}
        >
          <canvas ref={canvasRef} className="block" />
          {size &&
            onPage.map((box, index) => {
              const style = highlightStyle(box, size);
              if (!style) return null;
              return (
                <span
                  key={index}
                  className="pointer-events-none absolute rounded-sm bg-amber-300/35 ring-2 ring-amber-500"
                  style={style}
                />
              );
            })}
        </div>
      </div>
    </div>
  );
}

const ViewerButton = ({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: ReactNode;
}) => (
  <button
    type="button"
    aria-label={label}
    title={label}
    disabled={disabled}
    onClick={onClick}
    className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
  >
    {children}
  </button>
);

/**
 * Place a box as a fraction of the rendered page.
 *
 * A box without its page dimensions cannot be placed safely - the units are then
 * unknowable - so it is dropped rather than drawn somewhere plausible-looking. A
 * highlight over the wrong paragraph is worse than no highlight, because the
 * reader believes it.
 */
function highlightStyle(
  box: BoundingBox,
  size: { width: number; height: number },
): React.CSSProperties | null {
  if (!box.page_width || !box.page_height) return null;
  if (box.page_width <= 0 || box.page_height <= 0) return null;

  const left = (box.x / box.page_width) * size.width;
  const top = (box.y / box.page_height) * size.height;
  const width = (box.width / box.page_width) * size.width;
  const height = (box.height / box.page_height) * size.height;

  return {
    left: Math.max(0, left - 2),
    top: Math.max(0, top - 2),
    width: Math.max(4, width + 4),
    height: Math.max(4, height + 4),
  };
}
