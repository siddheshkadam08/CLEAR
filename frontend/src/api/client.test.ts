/**
 * The SSE reader, tested at the byte level.
 *
 * Everything else stubs the Copilot stream at the `onEvent` boundary, which is
 * convenient and skips the only part that had a bug in it: the server frames its
 * events with CRLF, the reader split on "\n\n", and so not one event was ever
 * dispatched. The stream ended cleanly having delivered nothing - no error, no
 * tokens, a spinner that ran past four minutes. A test that hands the parser its
 * own mocked events cannot see that; only one that hands it bytes can.
 */

import { describe, expect, it, vi } from 'vitest';

import { apiStream } from './client';

function streamOf(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function collect() {
  const seen: [string, unknown][] = [];
  return { seen, onEvent: (event: string, data: unknown) => seen.push([event, data]) };
}

describe('apiStream', () => {
  it('reads events the server framed with CRLF', async () => {
    // Exactly what the backend puts on the wire - verified against the running
    // API, which emits \r\n\r\n between frames and never \n\n.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamOf(
        'event: plan\r\ndata: {"scope":"contract"}\r\n\r\n',
        'event: token\r\ndata: {"text":"Hello"}\r\n\r\n',
        'event: done\r\ndata: {"citations":[]}\r\n\r\n',
      ),
    );
    const { seen, onEvent } = collect();

    await apiStream('/copilot/stream', { query: 'q' }, { onEvent });

    expect(seen.map(([name]) => name)).toEqual(['plan', 'token', 'done']);
    expect(seen.find(([name]) => name === 'token')?.[1]).toEqual({ text: 'Hello' });
  });

  it('reads events framed with bare LF', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamOf('event: token\ndata: {"text":"a"}\n\nevent: done\ndata: {}\n\n'),
    );
    const { seen, onEvent } = collect();

    await apiStream('/copilot/stream', { query: 'q' }, { onEvent });

    expect(seen.map(([name]) => name)).toEqual(['token', 'done']);
  });

  it('joins a frame split across two reads', async () => {
    // A frame boundary lands mid-chunk often enough that this is the normal case,
    // not an edge one - the reader has to hold the remainder rather than drop it.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamOf('event: token\r\ndata: {"te', 'xt":"split"}\r\n\r\n'),
    );
    const { seen, onEvent } = collect();

    await apiStream('/copilot/stream', { query: 'q' }, { onEvent });

    expect(seen).toEqual([['token', { text: 'split' }]]);
  });
});
