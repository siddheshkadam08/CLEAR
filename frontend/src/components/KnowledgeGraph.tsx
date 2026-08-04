/**
 * The contract's knowledge graph, as an interactive SVG.
 *
 * Hand-rolled force layout rather than a graph library. One contract's graph is
 * tens of nodes, not thousands; the simulation below is ~40 lines, and d3-force
 * plus a renderer is ~90 kB into a bundle that already carries recharts and
 * pdf.js. If a graph ever needs thousands of nodes this is the wrong renderer and
 * should be replaced rather than tuned.
 *
 * **Unresolved edges are drawn, not hidden.** An edge whose target could not be
 * matched to a node means the text cited something — a schedule, an annexe, a
 * clause number — that is not in this contract. That is a finding about the
 * repository, usually a document nobody uploaded, and a viewer that quietly
 * dropped those edges would present a tidier graph than the truth. They render
 * dashed, against a hollow placeholder node.
 *
 * Layout is deterministic: node start positions come from a hash of the node key
 * rather than `Math.random`, so the same contract lays out the same way every
 * time. A graph that rearranges itself on every visit is one nobody can describe
 * to a colleague.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import type { ContractGraph, GraphEdge, GraphNode } from '@/api/types';
import { humanise } from '@/lib/format';

const WIDTH = 900;
const HEIGHT = 560;
/** Enough to settle a few dozen nodes; run once, up front, not per frame. */
const ITERATIONS = 320;

/** Node colours by type. Keyed loosely: an unknown type still renders. */
const NODE_STYLE: Record<string, { fill: string; stroke: string; r: number }> = {
  contract: { fill: '#1D4ED8', stroke: '#1E3A8A', r: 26 },
  party: { fill: '#0F766E', stroke: '#115E59', r: 18 },
  clause: { fill: '#7C3AED', stroke: '#5B21B6', r: 15 },
  obligation: { fill: '#B45309', stroke: '#92400E', r: 14 },
  risk: { fill: '#BE123C', stroke: '#9F1239', r: 14 },
  governing_law: { fill: '#0369A1', stroke: '#075985', r: 14 },
  date: { fill: '#4D7C0F', stroke: '#3F6212', r: 13 },
};
const FALLBACK_STYLE = { fill: '#64748B', stroke: '#475569', r: 13 };

const styleFor = (type: string) => NODE_STYLE[type] ?? FALLBACK_STYLE;

const keyOf = (type: string, ref: string) => `${type}::${ref}`;

/** The inverse of `keyOf`. A ref may itself contain "::", so only split once. */
function splitKey(key: string): { type: string; ref: string } {
  const at = key.indexOf('::');
  return at === -1
    ? { type: 'unknown', ref: key }
    : { type: key.slice(0, at), ref: key.slice(at + 2) };
}

interface Placed {
  key: string;
  node: GraphNode | null;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

/**
 * Deterministic pseudo-random in [0, 1) from a string.
 *
 * FNV-1a. Only needs to spread start positions apart; using `Math.random` here
 * would make the layout different on every render.
 */
function hashUnit(value: string, salt: number): number {
  let hash = 2166136261 ^ salt;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 100000) / 100000;
}

/**
 * A few hundred iterations of repulsion + spring attraction + centring.
 *
 * Run synchronously in a memo rather than animated: the result is what matters,
 * and animating it would re-render the tree 300 times to arrive at the same
 * picture.
 */
function layout(nodes: Placed[], edges: GraphEdge[]): Placed[] {
  const index = new Map(nodes.map((entry, i) => [entry.key, i]));
  // Resolved to the node objects themselves, not indices: `noUncheckedIndexedAccess`
  // would otherwise make every access in the inner loop an undefined check, in the
  // one place that runs a few hundred thousand times.
  const links: Array<[Placed, Placed]> = [];
  for (const edge of edges) {
    const a = index.get(keyOf(edge.source_type, edge.source_ref));
    const b = index.get(keyOf(edge.target_type, edge.target_ref));
    if (a === undefined || b === undefined) continue;
    const from = nodes[a];
    const to = nodes[b];
    if (from && to) links.push([from, to]);
  }

  const centreX = WIDTH / 2;
  const centreY = HEIGHT / 2;

  for (let step = 0; step < ITERATIONS; step += 1) {
    // Cooling: large moves early, small corrections late.
    const cooling = 1 - step / ITERATIONS;

    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      if (!a) continue;
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        if (!b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distanceSq = dx * dx + dy * dy || 0.01;
        const distance = Math.sqrt(distanceSq);
        const force = 9000 / distanceSq;
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }

    for (const [a, b] of links) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const force = (distance - 120) * 0.02;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    for (const node of nodes) {
      node.vx += (centreX - node.x) * 0.008;
      node.vy += (centreY - node.y) * 0.008;
      node.x += node.vx * cooling;
      node.y += node.vy * cooling;
      node.vx *= 0.82;
      node.vy *= 0.82;
      // Keep everything on the canvas; the viewBox does not scroll.
      node.x = Math.min(WIDTH - 40, Math.max(40, node.x));
      node.y = Math.min(HEIGHT - 40, Math.max(40, node.y));
    }
  }
  return nodes;
}

export function KnowledgeGraph({ graph }: { graph: ContractGraph }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const svgRef = useRef<SVGSVGElement>(null);

  const nodeTypes = useMemo(
    () => Array.from(new Set(graph.nodes.map((node) => node.node_type))).sort(),
    [graph.nodes],
  );

  const { placed, links, byKey } = useMemo(() => {
    const visible = graph.nodes.filter((node) => !hidden.has(node.node_type));
    const visibleKeys = new Set(visible.map((node) => keyOf(node.node_type, node.ref)));

    const edges = graph.edges.filter(
      (edge) =>
        !hidden.has(edge.source_type) &&
        !hidden.has(edge.target_type) &&
        visibleKeys.has(keyOf(edge.source_type, edge.source_ref)),
    );

    // Placeholders for edge targets that resolved to nothing. Without these the
    // edge would have nowhere to end and would simply vanish - which is the one
    // outcome this view must avoid.
    const phantoms: Placed[] = [];
    for (const edge of edges) {
      const key = keyOf(edge.target_type, edge.target_ref);
      if (visibleKeys.has(key)) continue;
      visibleKeys.add(key);
      phantoms.push({
        key,
        node: null,
        x: WIDTH * hashUnit(key, 7),
        y: HEIGHT * hashUnit(key, 13),
        vx: 0,
        vy: 0,
      });
    }

    const seeded: Placed[] = [
      ...visible.map((node) => {
        const key = keyOf(node.node_type, node.ref);
        return {
          key,
          node,
          // The contract sits at the centre; it is what everything hangs off.
          x: node.node_type === 'contract' ? WIDTH / 2 : WIDTH * hashUnit(key, 7),
          y: node.node_type === 'contract' ? HEIGHT / 2 : HEIGHT * hashUnit(key, 13),
          vx: 0,
          vy: 0,
        };
      }),
      ...phantoms,
    ];

    const settled = layout(seeded, edges);
    return {
      placed: settled,
      links: edges,
      byKey: new Map(settled.map((entry) => [entry.key, entry])),
    };
  }, [graph.nodes, graph.edges, hidden]);

  // Deselect when the selection is filtered out, so the detail panel never
  // describes something no longer on screen.
  useEffect(() => {
    if (selected && !byKey.has(selected)) setSelected(null);
  }, [byKey, selected]);

  const selectedEntry = selected ? byKey.get(selected) : null;
  const connected = useMemo(() => {
    if (!selected) return new Set<string>();
    const set = new Set<string>();
    for (const edge of links) {
      const source = keyOf(edge.source_type, edge.source_ref);
      const target = keyOf(edge.target_type, edge.target_ref);
      if (source === selected) set.add(target);
      if (target === selected) set.add(source);
    }
    return set;
  }, [links, selected]);

  const stats = graph.statistics;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-500">
        <span>
          <strong className="text-slate-700 dark:text-slate-200">{stats.nodes ?? 0}</strong> nodes
        </span>
        <span>
          <strong className="text-slate-700 dark:text-slate-200">{stats.edges ?? 0}</strong> edges
        </span>
        {stats.unresolved_edges ? (
          <span className="text-amber-600">{stats.unresolved_edges} unresolved</span>
        ) : null}
        {stats.dangling_references ? (
          <span className="text-amber-600">{stats.dangling_references} dangling references</span>
        ) : null}
        <span className="ml-auto text-slate-400">Click a node to trace its connections</span>
      </div>

      {/* Legend doubles as the filter: clicking a type hides it. */}
      <div className="flex flex-wrap gap-2">
        {nodeTypes.map((type) => {
          const style = styleFor(type);
          const on = !hidden.has(type);
          return (
            <button
              key={type}
              type="button"
              aria-pressed={on}
              onClick={() =>
                setHidden((current) => {
                  const next = new Set(current);
                  if (next.has(type)) next.delete(type);
                  else next.add(type);
                  return next;
                })
              }
              className={[
                'flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset transition',
                on
                  ? 'bg-white text-slate-700 ring-slate-200'
                  : 'bg-slate-50 text-slate-400 ring-slate-100 line-through',
              ].join(' ')}
            >
              <span
                className="h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: on ? style.fill : '#CBD5E1' }}
              />
              {humanise(type)}
              <span className="text-slate-400">
                {stats.by_node_type?.[type] ?? 0}
              </span>
            </button>
          );
        })}
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-slate-50/60 dark:border-slate-700 dark:bg-slate-900/40">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="h-[560px] w-full min-w-[700px]"
          role="img"
          aria-label={`Knowledge graph: ${stats.nodes ?? 0} nodes, ${stats.edges ?? 0} edges`}
        >
          <defs>
            <marker
              id="graph-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#94A3B8" />
            </marker>
          </defs>

          {links.map((edge, i) => {
            const a = byKey.get(keyOf(edge.source_type, edge.source_ref));
            const b = byKey.get(keyOf(edge.target_type, edge.target_ref));
            if (!a || !b) return null;
            const touched =
              !selected || a.key === selected || b.key === selected;
            return (
              <line
                key={`${edge.relation}-${edge.source_ref}-${edge.target_ref}-${i}`}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={edge.is_resolved ? '#94A3B8' : '#F59E0B'}
                strokeWidth={touched ? 1.4 : 0.6}
                strokeOpacity={touched ? 0.85 : 0.18}
                // Dashed means "the text said so but nothing here matches".
                strokeDasharray={edge.is_resolved ? undefined : '4 3'}
                markerEnd="url(#graph-arrow)"
              >
                <title>
                  {humanise(edge.relation)}
                  {edge.label ? ` — ${edge.label}` : ''}
                  {edge.is_resolved ? '' : ' (unresolved)'}
                </title>
              </line>
            );
          })}

          {placed.map((entry) => {
            const parts = splitKey(entry.key);
            const type = entry.node?.node_type ?? parts.type;
            const style = styleFor(type);
            const isPhantom = entry.node === null;
            const dimmed =
              selected !== null && entry.key !== selected && !connected.has(entry.key);
            const label = entry.node?.label ?? parts.ref;

            return (
              <g
                key={entry.key}
                transform={`translate(${entry.x} ${entry.y})`}
                opacity={dimmed ? 0.25 : 1}
                onClick={() => setSelected(entry.key === selected ? null : entry.key)}
                className="cursor-pointer"
              >
                <circle
                  r={style.r}
                  fill={isPhantom ? 'transparent' : style.fill}
                  stroke={isPhantom ? '#F59E0B' : style.stroke}
                  strokeWidth={entry.key === selected ? 3 : 1.5}
                  strokeDasharray={isPhantom ? '4 3' : undefined}
                />
                <text
                  y={style.r + 12}
                  textAnchor="middle"
                  className="pointer-events-none fill-slate-600 text-[10px] dark:fill-slate-300"
                >
                  {label.length > 22 ? `${label.slice(0, 20)}…` : label}
                </text>
                <title>{`${humanise(type)}: ${label}${isPhantom ? ' (not found in this contract)' : ''}`}</title>
              </g>
            );
          })}
        </svg>
      </div>

      {selectedEntry ? <NodeDetail entry={selectedEntry} edges={links} /> : null}

      {graph.dangling.length ? <DanglingPanel graph={graph} /> : null}
    </div>
  );
}

function NodeDetail({ entry, edges }: { entry: Placed; edges: GraphEdge[] }) {
  const key = entry.key;
  const parts = splitKey(key);
  const type = entry.node?.node_type ?? parts.type;
  const outgoing = edges.filter((edge) => keyOf(edge.source_type, edge.source_ref) === key);
  const incoming = edges.filter((edge) => keyOf(edge.target_type, edge.target_ref) === key);

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="h-2.5 w-2.5 rounded-full"
          style={{ backgroundColor: styleFor(type).fill }}
        />
        <span className="font-semibold text-slate-900 dark:text-slate-100">
          {entry.node?.label ?? parts.ref}
        </span>
        <span className="text-xs text-slate-500">{humanise(type)}</span>
        {!entry.node ? (
          <span className="text-xs text-amber-600">not found in this contract</span>
        ) : null}
      </div>

      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        <EdgeList title="Points at" edges={outgoing} pick={(edge) => edge.target_ref} />
        <EdgeList title="Pointed at by" edges={incoming} pick={(edge) => edge.source_ref} />
      </div>

      {entry.node && Object.keys(entry.node.attributes).length ? (
        <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
          {Object.entries(entry.node.attributes)
            .filter(([, value]) => value !== null && value !== undefined && value !== '')
            .map(([name, value]) => (
              <div key={name} className="flex gap-2">
                <dt className="shrink-0 text-slate-400">{humanise(name)}</dt>
                <dd className="min-w-0 truncate text-slate-600 dark:text-slate-300">
                  {String(value)}
                </dd>
              </div>
            ))}
        </dl>
      ) : null}
    </div>
  );
}

function EdgeList({
  title,
  edges,
  pick,
}: {
  title: string;
  edges: GraphEdge[];
  pick: (edge: GraphEdge) => string;
}) {
  if (!edges.length) return null;
  return (
    <div>
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
        {title}
      </p>
      <ul className="space-y-1 text-xs text-slate-600 dark:text-slate-300">
        {edges.slice(0, 12).map((edge, i) => (
          <li key={`${edge.relation}-${pick(edge)}-${i}`} className="truncate">
            <span className="font-medium">{humanise(edge.relation)}</span> {pick(edge)}
            {!edge.is_resolved ? <span className="ml-1 text-amber-600">(unresolved)</span> : null}
            {edge.origin === 'extracted' ? (
              <span className="ml-1 text-slate-400">· from the text</span>
            ) : null}
          </li>
        ))}
        {edges.length > 12 ? (
          <li className="text-slate-400">and {edges.length - 12} more</li>
        ) : null}
      </ul>
    </div>
  );
}

function DanglingPanel({ graph }: { graph: ContractGraph }) {
  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 dark:border-amber-900/50 dark:bg-amber-950/20">
      <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">
        {graph.dangling.length} reference{graph.dangling.length === 1 ? '' : 's'} could not be
        resolved
      </p>
      <p className="mt-1 text-xs text-amber-800 dark:text-amber-300">
        The text cites something that is not in this contract — most often a schedule, annexe or
        related agreement that has not been uploaded.
      </p>
      <ul className="mt-2 space-y-1 text-xs text-amber-900 dark:text-amber-200">
        {graph.dangling.slice(0, 10).map((entry, i) => (
          <li key={`${entry.reference}-${i}`} className="truncate">
            <span className="font-mono font-medium">“{entry.reference}”</span>
            {entry.relation ? (
              <span className="ml-1 opacity-75">as {humanise(entry.relation)}</span>
            ) : null}
            {entry.reason ? <span className="ml-1 opacity-75">— {entry.reason}</span> : null}
          </li>
        ))}
        {graph.dangling.length > 10 ? (
          <li className="opacity-75">and {graph.dangling.length - 10} more</li>
        ) : null}
      </ul>
    </div>
  );
}

export default KnowledgeGraph;
