/**
 * HTTP client for the platform API.
 *
 * Two things here are load-bearing:
 *
 * * **The access token lives in memory, not localStorage.** A token in
 *   localStorage is readable by any script that gets injected into the page. The
 *   refresh token is an HttpOnly cookie the browser sends automatically and no
 *   script can read, so a page reload recovers the session without ever exposing
 *   a long-lived credential to JavaScript.
 * * **A 401 triggers exactly one refresh.** Concurrent requests that all 401 share
 *   a single in-flight refresh and then retry; without that, a dashboard firing six
 *   parallel queries would trigger six refreshes and invalidate its own tokens in a
 *   race.
 */

import { ApiError } from './errors';

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string) ?? '/api/v1';

/** In-memory only. Deliberately not persisted - see the module docstring. */
let accessToken: string | null = null;

/** Shared across concurrent 401s so only one refresh is ever in flight. */
let refreshInFlight: Promise<boolean> | null = null;

type Listener = (authenticated: boolean) => void;
const listeners = new Set<Listener>();

export function setAccessToken(token: string | null): void {
  accessToken = token;
  listeners.forEach((listener) => listener(token !== null));
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function onAuthChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  /** Skip the automatic refresh-and-retry. Used by the refresh call itself. */
  skipRefresh?: boolean;
  query?: Record<string, string | number | boolean | string[] | undefined | null>;
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${BASE_URL}${path}`;
  if (!query) return url;

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    // Repeated keys rather than a comma-joined string: FastAPI reads list query
    // params as `?state=queued&state=running`.
    if (Array.isArray(value)) {
      value.forEach((entry) => params.append(key, String(entry)));
    } else {
      params.append(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

async function refreshAccessToken(): Promise<boolean> {
  refreshInFlight ??= (async () => {
    try {
      const response = await fetch(`${BASE_URL}/auth/refresh`, {
        method: 'POST',
        // The HttpOnly refresh cookie has to be sent for this to work at all.
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!response.ok) {
        setAccessToken(null);
        return false;
      }
      const data = (await response.json()) as { access_token?: string };
      if (!data.access_token) {
        setAccessToken(null);
        return false;
      }
      setAccessToken(data.access_token);
      return true;
    } catch {
      setAccessToken(null);
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, skipRefresh, query, headers, ...rest } = options;

  const send = async (): Promise<Response> =>
    fetch(buildUrl(path, query), {
      ...rest,
      credentials: 'include',
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

  let response = await send();

  if (response.status === 401 && !skipRefresh) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      response = await send();
    }
  }

  if (!response.ok) {
    throw await ApiError.fromResponse(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) {
    return (await response.text()) as unknown as T;
  }
  return (await response.json()) as T;
}

/** Multipart upload. Content-Type is left to the browser so the boundary is right. */
export async function apiUpload<T>(
  path: string,
  formData: FormData,
  onProgress?: (percent: number) => void,
): Promise<T> {
  // XMLHttpRequest rather than fetch: fetch still has no upload progress, and a
  // 40 MB contract uploading with no feedback reads as a hung page.
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', buildUrl(path));
    xhr.withCredentials = true;
    if (accessToken) {
      xhr.setRequestHeader('Authorization', `Bearer ${accessToken}`);
    }

    xhr.upload.addEventListener('progress', (event) => {
      if (onProgress && event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    });

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.responseText ? (JSON.parse(xhr.responseText) as T) : (undefined as T));
      } else {
        reject(ApiError.fromXhr(xhr));
      }
    });
    xhr.addEventListener('error', () =>
      reject(new ApiError('network_error', 'The upload could not be sent.', 0)),
    );
    xhr.addEventListener('abort', () =>
      reject(new ApiError('aborted', 'The upload was cancelled.', 0)),
    );

    xhr.send(formData);
  });
}

/**
 * Open an SSE stream for the Copilot.
 *
 * `fetch` rather than `EventSource`: EventSource cannot send an Authorization
 * header or a POST body, and the ask endpoint needs both.
 */
export async function apiStream(
  path: string,
  body: unknown,
  handlers: {
    onEvent: (event: string, data: unknown) => void;
    onError?: (error: Error) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const response = await fetch(buildUrl(path), {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: JSON.stringify(body),
    signal: handlers.signal,
  });

  if (!response.ok || !response.body) {
    throw await ApiError.fromResponse(response);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      // Normalise line endings before looking for frame boundaries. The spec
      // allows CRLF, LF or CR, and this server sends CRLF - so splitting on
      // "\n\n" alone finds nothing in "\r\n\r\n", every frame stays in the
      // buffer, and not one event is ever dispatched. The stream then ends
      // cleanly having delivered nothing, which is indistinguishable from a
      // request that hung.
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, '\n');

      // SSE frames are separated by a blank line. A frame can arrive split across
      // reads, so anything after the last separator stays buffered.
      const frames = buffer.split('\n\n');
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        let eventName = 'message';
        const dataLines: string[] = [];
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) eventName = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        try {
          handlers.onEvent(eventName, JSON.parse(dataLines.join('\n')));
        } catch {
          handlers.onEvent(eventName, dataLines.join('\n'));
        }
      }
    }
  } catch (error) {
    if ((error as Error).name !== 'AbortError') {
      handlers.onError?.(error as Error);
    }
  } finally {
    reader.releaseLock();
  }
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'POST', body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'PATCH', body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'PUT', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'DELETE' }),
  refresh: refreshAccessToken,
};
