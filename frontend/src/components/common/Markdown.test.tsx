/**
 * Answers arrive as markdown, and two of these tests are about safety rather
 * than formatting.
 *
 * The renderer's input is model output built from contract text. A contract can
 * contain anything someone put in a Word document, including markup aimed at
 * whatever reads it downstream - so raw HTML must stay inert rather than being
 * parsed into elements.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Markdown } from './Markdown';

describe('Markdown', () => {
  it('renders emphasis as elements rather than literal asterisks', () => {
    render(<Markdown>{'The cap is **USD 1,000,000**.'}</Markdown>);

    expect(screen.getByText('USD 1,000,000').tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*/)).not.toBeInTheDocument();
  });

  it('renders a list', () => {
    render(<Markdown>{'- notice period\n- cure period'}</Markdown>);

    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('renders a GFM table', () => {
    render(
      <Markdown>{'| Clause | Page |\n| --- | --- |\n| Termination | 18 |'}</Markdown>,
    );

    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('Termination')).toBeInTheDocument();
  });

  it('renders a fenced code block', () => {
    const { container } = render(<Markdown>{'```\nNET 30\n```'}</Markdown>);

    expect(container.querySelector('pre')).toBeInTheDocument();
    expect(screen.getByText(/NET 30/)).toBeInTheDocument();
  });

  it('does not parse raw HTML', () => {
    const { container } = render(<Markdown>{'<img src=x onerror="alert(1)">'}</Markdown>);

    expect(container.querySelector('img')).toBeNull();
  });

  it('does not execute a script tag', () => {
    const { container } = render(<Markdown>{'<script>alert(1)</script>'}</Markdown>);

    expect(container.querySelector('script')).toBeNull();
  });

  it('opens links without handing the new page a reference back', () => {
    const { container } = render(<Markdown>{'[terms](https://example.com)'}</Markdown>);

    const link = container.querySelector('a');
    expect(link?.getAttribute('rel')).toContain('noopener');
    expect(link?.getAttribute('rel')).toContain('noreferrer');
  });

  it('renders an empty answer without throwing', () => {
    expect(() => render(<Markdown>{''}</Markdown>)).not.toThrow();
  });
});
