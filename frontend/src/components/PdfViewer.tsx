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

import { fetchAuthenticatedBlob } from '@/api/client';
import { ApiError } from '@/api/errors';
import type { BoundingBox } from '@/api/types';

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/** The content URL answered with something that is not a PDF - see the throw site. */
class NotPdfError extends Error {
  constructor(readonly contentType: string) {
    super(`Expected PDF bytes, received ${contentType}`);
    this.name = 'NotPdfError';
  }
}

/**
 * Development-only tracing for the two-request document load.
 *
 * Stripped from the production bundle: `import.meta.env.DEV` is a compile-time
 * constant, so Vite removes both the call and this function from the build.
 *
 * Deliberately never given a URL containing credentials, a token, or any document
 * content - only the path, the status and the content type, which is what
 * identifies a misrouted or unauthenticated request.
 */
function traceLoad(stage: string, detail: Record<string, unknown>): void {
  if (!import.meta.env.DEV) return;
  // eslint-disable-next-line no-console
  console.debug(`[PdfViewer] ${stage}`, detail);
}

/**
 * Turn a load failure into something a reader can act on.
 *
 * The proxied route now fails with a real `ApiError` carrying a status, so the
 * message can name the actual problem instead of guessing from the text of an
 * exception. That guessing is what produced "the document link has expired" for a
 * deployment that signs nothing and therefore has no link to expire - advice
 * whose only instruction, reload the page, reproduced the error.
 *
 * A 401 here has already survived one refresh attempt inside
 * `fetchAuthenticatedBlob`, so the session really is gone.
 */
function describeFailure(caught: unknown, proxied: boolean): string {
  if (caught instanceof NotPdfError) {
    // Deliberately blunt: this is a deployment fault, not something the reader
    // did, and the alternative is pdf.js's "Invalid PDF structure" sending someone
    // to look for a corrupt upload that is perfectly fine.
    return 'The server returned a page instead of the document. The API route serving document content is not reachable.';
  }

  if (caught instanceof ApiError) {
    if (caught.status === 401) {
      return 'Your session has expired. Sign in again to view this document.';
    }
    if (caught.status === 403) {
      return 'You do not have permission to view this document.';
    }
    if (caught.status === 404) {
      return 'This document is no longer available.';
    }
    return 'This document could not be displayed.';
  }

  // Signed object-storage URLs are fetched by pdf.js itself, so their failures
  // arrive as pdf.js errors with no status to read.
  if (!proxied && caught instanceof Error && /expired|403|401/i.test(caught.message)) {
    return 'The document link has expired. Reload the page to get a fresh one.';
  }

  return 'This document could not be displayed.';
}

export interface PdfViewerProps {
  /**
   * Document URL from `GET /contracts/{id}/file`.
   *
   * Either a pre-signed object-storage URL, or - when the backend cannot sign,
   * which is every local-storage deployment - an API path that carries the
   * bearer token instead. The loader below tells them apart.
   */
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
  //: The first highlight drawn on the current page - the scroll target.
  const firstHighlight = useRef<HTMLSpanElement>(null);
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
    let task: ReturnType<typeof pdfjs.getDocument> | null = null;
    let objectUrl: string | null = null;
    const controller = new AbortController();

    setLoading(true);
    setError(null);

    // Two kinds of URL arrive here, and they authorise in opposite ways.
    //
    // A real object-storage URL is pre-signed: the signature *is* the
    // authorisation, and it is handed straight to pdf.js. Attaching our own
    // credentials to that cross-origin request would break the CORS preflight for
    // no gain.
    //
    // A local or otherwise unsignable backend has nothing to sign against, so
    // `file_access` returns an API path instead - a route behind the normal
    // session guard. Those bytes are fetched HERE, by the application, and handed
    // to pdf.js as a blob URL.
    //
    // Fetching rather than letting pdf.js do it, for three reasons:
    //
    //  * The token. pdf.js would need `httpHeaders`, which pins whatever token was
    //    current when the document was opened. A token that expires between then
    //    and the request 401s, and the reader is told to sign in again while the
    //    HttpOnly refresh cookie that would have recovered it goes unused.
    //    `fetchAuthenticatedBlob` runs the same refresh-and-retry as every other
    //    call, so an expired token is invisible.
    //  * Range requests. pdf.js fetches a large PDF in pieces, so the same
    //    authorisation problem recurs per range for as long as the document is
    //    open - a long read can start failing to render pages part-way through.
    //    One fetch up front has one outcome.
    //  * The URL stops being a credential. A blob URL is origin-local and dies
    //    with the page; nothing that could be copied out of devtools and replayed.
    //
    // A blob URL rather than passing the ArrayBuffer as `data:` - pdf.js transfers
    // that buffer to its worker and leaves it detached, so StrictMode's second
    // effect invocation in development would hand it an empty buffer and fail.
    const proxied = url.startsWith('/api/');

    const load = async (): Promise<void> => {
      let source = url;

      if (proxied) {
        traceLoad('content request started', { url });
        const { blob, contentType, status } = await fetchAuthenticatedBlob(url, {
          signal: controller.signal,
        });
        traceLoad('content request completed', {
          url,
          status,
          contentType,
          bytes: blob.size,
        });

        // A 200 is not proof that these are PDF bytes.
        //
        // The SPA fallback (`try_files $uri $uri/ /index.html`) answers any path
        // the proxy does not claim with the application's own HTML, at 200. So a
        // content URL that has lost its `/api/v1` prefix - or a proxy that stops
        // routing it - returns a perfectly successful page of HTML, pdf.js reports
        // "Invalid PDF structure", and nothing appears in the backend's log at all
        // because the request never reached it. Naming that here turns a confusing
        // parser error into the routing problem it actually is.
        if (contentType && !/pdf|octet-stream/i.test(contentType)) {
          throw new NotPdfError(contentType);
        }

        // Checked before creating the URL: a revoke in the cleanup below cannot
        // run for an object that did not exist when the cleanup was scheduled.
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        source = objectUrl;
      }

      task = pdfjs.getDocument({ url: source });
      const doc = await task.promise;

      if (cancelled) {
        void doc.destroy();
        return;
      }
      traceLoad('document ready', { pages: doc.numPages });
      docRef.current = doc;
      setPageCount(doc.numPages);
      setLoading(false);
    };

    load().catch((caught: unknown) => {
      // An abort is this effect being superseded, not a failure. Reporting it
      // would flash an error over the document the reader just switched to.
      if (cancelled || (caught instanceof DOMException && caught.name === 'AbortError')) return;
      traceLoad('load failed', { url, error: String(caught) });
      setLoading(false);
      setError(describeFailure(caught, proxied));
    });

    return () => {
      cancelled = true;
      // Aborts the fetch itself, not just its result. Switching contracts while a
      // 40MB document is in flight would otherwise leave it downloading to
      // nowhere, competing for bandwidth with the one now on screen.
      controller.abort();
      void task?.destroy();
      // Without this the decoded PDF stays resident for the lifetime of the page.
      // Contracts here run to tens of megabytes and the viewer is remounted on
      // every document a reviewer opens, so the leak is measured in hundreds of
      // megabytes over a session, not kilobytes.
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      docRef.current = null;
    };
  }, [url]);

  // ------------------------------------------------------------------ paging
  useEffect(() => {
    if (page && page >= 1) setCurrent(page);
    // `focusToken` is in the dependency list so clicking the same citation twice
    // still brings the viewer back to it.
  }, [page, focusToken]);

  // --------------------------------------------------------------- centring
  //
  // Changing the page is not the same as showing the highlight. The canvas sits
  // in a `max-h-[70vh] overflow-auto` box, so a clause near the top of page 9 is
  // drawn correctly and then left off-screen if the reader was scrolled down.
  // The prop above promised this ("re-centres the viewer") and only ever paged.
  //
  // Deferred to the next frame because the page render is async: at the moment
  // `current` changes the highlight for the new page does not exist yet, and
  // scrolling to a stale node would be worse than not scrolling at all.
  useEffect(() => {
    if (!highlights.length) return;
    const frame = requestAnimationFrame(() => {
      firstHighlight.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [current, focusToken, size, highlights.length]);

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
      className="overflow-hidden rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-sm"
    >
      {/* The toolbar wraps rather than scrolls: on a phone the page controls end up
          on one line and the zoom controls on the next, which is fine - both stay
          reachable. A horizontally scrolling toolbar hides its right-hand end. */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 px-3 py-2">
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
                    : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 ring-slate-200 hover:bg-slate-100',
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
          className="relative mx-auto bg-white dark:bg-slate-800 shadow-md"
          style={size ? { width: size.width, height: size.height } : undefined}
        >
          <canvas ref={canvasRef} className="block" />
          {size &&
            onPage.map((box, index) => {
              const style = highlightStyle(box, size);
              if (!style) return null;
              return (
                <span
                  // The first highlight on the page is the scroll target. Without
                  // a ref here the viewer changed page and left the box wherever
                  // the previous scroll position happened to be - which, for a
                  // clause near the top of a long page, is off-screen.
                  ref={index === 0 ? firstHighlight : undefined}
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
    className="rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-1.5 text-slate-600 dark:text-slate-300 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
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
