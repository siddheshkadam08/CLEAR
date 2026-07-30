/**
 * API error handling.
 *
 * The backend returns one envelope for every failure:
 * `{ error: { code, message, details, trace_id } }`. Preserving `trace_id` matters
 * - it is what turns "it broke" into a searchable incident.
 */

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly details: Record<string, unknown> = {},
    readonly traceId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  static async fromResponse(response: Response): Promise<ApiError> {
    let code = 'http_error';
    let message = `The request failed (${response.status}).`;
    let details: Record<string, unknown> = {};
    let traceId: string | undefined;

    try {
      const body = (await response.json()) as {
        error?: {
          code?: string;
          message?: string;
          details?: Record<string, unknown>;
          trace_id?: string;
        };
        detail?: unknown;
      };
      if (body.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        details = body.error.details ?? {};
        traceId = body.error.trace_id;
      } else if (typeof body.detail === 'string') {
        message = body.detail;
      }
    } catch {
      // A non-JSON error body (a proxy 502, say) still has to produce a usable
      // message rather than an unhandled parse failure.
    }

    return new ApiError(code, message, response.status, details, traceId);
  }

  static fromXhr(xhr: XMLHttpRequest): ApiError {
    try {
      const body = JSON.parse(xhr.responseText) as {
        error?: { code?: string; message?: string; trace_id?: string };
      };
      return new ApiError(
        body.error?.code ?? 'http_error',
        body.error?.message ?? `The upload failed (${xhr.status}).`,
        xhr.status,
        {},
        body.error?.trace_id,
      );
    } catch {
      return new ApiError('http_error', `The upload failed (${xhr.status}).`, xhr.status);
    }
  }

  /** True when the caller lacks permission, as opposed to being unauthenticated. */
  get isForbidden(): boolean {
    return this.status === 403 || this.code === 'permission_denied';
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  /**
   * Whether retrying could plausibly help.
   *
   * Used to decide if a "try again" button is worth offering: showing one for a
   * validation error just invites the user to fail twice.
   */
  get isRetryable(): boolean {
    return this.status >= 500 || this.status === 0 || this.status === 429;
  }
}

/** A message worth showing a user, from any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Something went wrong.';
}
