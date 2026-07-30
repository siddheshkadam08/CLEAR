---
name: iris-ui-design-system
description: Apply the RFP Intelligence UI/UX design system (React + TypeScript + Tailwind + lucide-react + recharts) to a frontend. Use when building, restyling, or reviewing any UI in this app or porting its look to another app — creating a page/screen/dashboard, adding a card, table, modal, form, badge, chart, sidebar, tab bar, toggle, empty state, or loading state; or when asked to "make it match", "same UI", "apply our design system", "use the same look as RFP Intelligence".
---

# RFP Intelligence Design System

A fixed, already-decided visual language. Reproducing it is a matching exercise, not a design
exercise. Never invent a color, radius, shadow, or component shape — compose from the primitives.

## Workflow

1. **Read `reference/design-system.md`** — the full spec: config files, every component's exact
   class string, layout grids, chart config, state contract, microcopy rules. Do this before
   writing any markup. Don't work from memory of this page; the class strings must be exact.
2. **Check what already exists** in `src/components/common/` and `src/components/layout/`. Reuse
   `Badge`, `EmptyState`, `LoadingSpinner` and the layout shell — never re-implement them inline.
3. **Build**, composing only the catalogued patterns.
4. **Verify** against the acceptance checklist (§11 of the reference) before reporting done.

When porting to a *new* app: write the three config files (§1) verbatim first, then the shell
(§3), then the shared primitives (§4.1–4.3), then pages.

## Tokens — quick reference

| | |
|---|---|
| Primary | `blue-600` fill, `blue-700` hover, `blue-500` focus border, `blue-100` focus ring, `blue-50` tint |
| Neutrals | **`slate` only** — never `gray`/`zinc`/`neutral`/`stone` |
| Semantic | success `emerald` · warning `amber` · danger `rose` · info `blue` · neutral `slate` — each as `bg-{h}-50 text-{h}-700 ring-{h}-200` |
| Radius | `rounded-3xl` modals · `rounded-2xl` cards/panels/banners/bubbles · `rounded-xl` buttons/inputs/nav · `rounded-lg` compact · `rounded-full` badges/pills/avatars/progress |
| Card | `rounded-2xl border border-slate-200 bg-white p-6 shadow-sm` |
| Primary button | `inline-flex items-center gap-2 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-70` |
| Field | `w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm text-slate-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100` |
| Rhythm | page root `space-y-6` · main `px-4 py-6 sm:px-6 lg:px-8` · grids `gap-4`/`gap-6` |
| Headings | `font-semibold`, never `font-bold` |
| Icons | `lucide-react` only — `h-4 w-4` in buttons, `h-5 w-5` nav/section, `h-6 w-6` KPI, `h-8 w-8` empty state |

## State contract — every data section

```tsx
{/* error banner renders ABOVE, separately, so stale content stays visible */}
{error ? <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

{loading ? (
  <LoadingSpinner label="Loading X..." />        // skeletons for KPI grids / charts
) : items.length ? (
  <>{/* content */}</>
) : (
  <EmptyState icon={DomainIcon} title="No X found" description="What makes content appear." />
)}
```

Errors show real API messages via a shared `getApiErrorMessage(err, fallback)` helper. Optional
data (badge counts, dropdown options) fails silently with a comment saying why.

## Hard rules

1. Light mode only — no dark variants, no theme toggle.
2. No component library (no MUI/shadcn/Chakra/Ant). Tailwind utilities on native elements.
3. `border border-slate-200` and `shadow-sm` always travel together on cards.
4. Every section heading gets a one-line `text-sm text-slate-500` subtitle. No bare headings.
5. Loading → error → empty → content in every data section, always.
6. One primary button per section.
7. Tables always wrapped in `overflow-x-auto`; `—` for missing values, never blank.
8. Clickable cards/rows are `<button type="button">`, never `<div onClick>`.
9. No inline `style` except computed progress widths and chart fills.
10. No animation beyond `transition`, `animate-spin`, `animate-pulse`, drawer `transition-transform`.
11. Async buttons swap icon → `<Loader2 className="h-4 w-4 animate-spin" />` and label → progressive form (`Saving…`).
12. Focus is always visible — never `outline-none` without a replacement ring.

## Files

- `reference/design-system.md` — the complete spec. Load it before implementing.
