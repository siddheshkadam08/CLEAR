"""Self-contained HTML reports.

One file per concern, plus an index. Each is standalone: inline CSS, inline SVG,
no scripts, no external requests. That is what lets a report be attached to a
change record, opened from a build artefact on a machine with no network, or
served from the admin UI under a strict CSP without an exception.

Every user-supplied string - a question, a case id, a clause heading - passes
through ``escape``. These reports render text extracted from counterparty
documents, and a report that rendered it as markup would be an injection vector
in the one artefact people trust most.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from app.evaluation.benchmark.baseline import RegressionReport, Verdict
from app.evaluation.metrics.scorecard import Scorecard
from app.evaluation.reports.charts import (
    BAD,
    GOOD,
    NEUTRAL,
    PALETTE,
    Series,
    bar_chart,
    delta_bars,
    line_chart,
    reliability_diagram,
)
from app.evaluation.runner.result import RunResult

_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;padding:32px;background:#F7F8FA;color:#0F172A;
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:17px;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid #E4E7EC}
h3{font-size:14px;margin:20px 0 8px;color:#5B6478}
.sub{color:#5B6478;font-size:13px;margin:0 0 24px}
.card{background:#fff;border:1px solid #E4E7EC;border-radius:12px;padding:20px;margin-bottom:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.tile{background:#fff;border:1px solid #E4E7EC;border-radius:12px;padding:14px 16px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#5B6478}
.tile .n{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.tile .d{font-size:12px;margin-top:2px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #EEF1F4;
  font-variant-numeric:tabular-nums}
th{font-weight:600;color:#5B6478;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
td.num,th.num{text-align:right}
.scroll{overflow-x:auto}
.chart{display:block;margin:8px 0}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600}
.ok{background:#ECFDF5;color:#059669}
.bad{background:#FEF2F2;color:#DC2626}
.warn{background:#FFFBEB;color:#D97706}
.mute{background:#F1F5F9;color:#5B6478}
.banner{padding:14px 18px;border-radius:12px;margin-bottom:20px;font-weight:600}
.banner.ok{background:#ECFDF5;border:1px solid #A7F3D0}
.banner.bad{background:#FEF2F2;border:1px solid #FECACA}
.note{font-size:12px;color:#5B6478;margin-top:8px}
nav{margin-bottom:24px;display:flex;flex-wrap:wrap;gap:8px}
nav a{font-size:12px;padding:5px 12px;border-radius:999px;background:#fff;
  border:1px solid #E4E7EC;color:#2563EB;text-decoration:none}
code{background:#F1F5F9;padding:1px 5px;border-radius:4px;font-size:12px}
"""

_PAGES = (
    ("overall_report.html", "Overall"),
    ("retrieval_report.html", "Retrieval"),
    ("citation_report.html", "Citations"),
    ("planner_report.html", "Planner"),
    ("confidence_report.html", "Confidence"),
    ("latency_report.html", "Latency"),
    ("cost_report.html", "Cost"),
)

#: Metrics where a fall is an improvement. Used to colour deltas by goodness
#: rather than by sign - see ``charts.delta_bars``.
LOWER_IS_BETTER = {
    "hallucination_rate",
    "false_accept_rate",
    "false_reject_rate",
    "false_filtering_rate",
    "broken_citations",
    "duplicate_rate",
    "latency_p50_ms",
    "latency_p95_ms",
    "mean_cost_usd",
    "ece",
}


def write_html_reports(
    scorecard: Scorecard,
    run: RunResult,
    regression: RegressionReport,
    output_dir: Path,
) -> list[Path]:
    """Write every HTML report. Returns the paths written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = {
        "overall_report.html": _overall(scorecard, regression, output_dir),
        "retrieval_report.html": _retrieval(scorecard, run),
        "citation_report.html": _citation(scorecard, run),
        "planner_report.html": _planner(scorecard),
        "confidence_report.html": _confidence(scorecard),
        "latency_report.html": _latency(scorecard),
        "cost_report.html": _cost(scorecard, run),
    }

    written: list[Path] = []
    for name, body in pages.items():
        path = output_dir / name
        path.write_text(_page(name, scorecard, body), encoding="utf-8")
        written.append(path)
    return written


# =============================================================================
# Pages
# =============================================================================
def _trend(output_dir: Path, dataset: str) -> str:
    """Previous runs of the same dataset, if any have been recorded.

    Reads the sibling directories of this run - the layout ``run_benchmark``
    writes - so a history exists without anything extra being stored. Fewer than
    two points is not a trend and renders nothing.
    """
    from app.evaluation.benchmark.history import TRENDED, load_history, trend_series

    points = load_history(output_dir.parent, dataset=dataset)
    if len(points) < 2:
        return ""

    series = trend_series(points)
    chart = line_chart(
        [point.label[-12:] for point in points],
        [
            Series(label=metric, values=series[metric], colour=PALETTE[index % len(PALETTE)])
            for index, metric in enumerate(TRENDED)
            if any(series[metric])
        ],
        y_max=1.0,
    )

    rows = "".join(
        f"<tr><td>{escape(point.label)}</td>"
        f'<td class="num">{point.cases}</td>'
        f'<td class="num">{point.metrics.get("composite", 0.0):.3f}</td>'
        f'<td class="num">{point.metrics.get("recall@10", 0.0):.3f}</td>'
        f'<td class="num">{point.metrics.get("citation_precision", 0.0):.3f}</td>'
        f'<td><span class="pill {"ok" if point.passed else "bad"}">'
        f"{'pass' if point.passed else 'fail'}</span></td></tr>"
        for point in reversed(points)
    )

    return f"""
<h2>Trend</h2>
<div class="card">{chart}</div>
<div class="card scroll"><table>
<thead><tr><th>Run</th><th class="num">Cases</th><th class="num">Composite</th>
<th class="num">Recall@10</th><th class="num">Citation precision</th>
<th>Gate</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">Assembled from previous runs' summary files. A run missing a
metric carries the previous value forward rather than drawing a zero - a gap in
the data must not look like a collapse in quality.</p></div>"""


def _overall(scorecard: Scorecard, regression: RegressionReport, output_dir: Path) -> str:
    passed = regression.passed
    banner = f'<div class="banner {"ok" if passed else "bad"}">{escape(regression.summary())}</div>'

    tiles = _tiles(
        [
            ("Composite", f"{scorecard.composite:.3f}", None),
            ("Recall@10", _fmt(scorecard.key_metrics["recall@10"]), None),
            ("MRR", _fmt(scorecard.key_metrics["mrr"]), None),
            ("Citation precision", _fmt(scorecard.citation.precision), None),
            (
                "Hallucination rate",
                _fmt(scorecard.citation.hallucination_rate),
                "lower is better",
            ),
            ("Guardrail accuracy", _fmt(scorecard.guardrail.accuracy), None),
            ("p95 latency", f"{scorecard.performance.latency_p95:.0f} ms", None),
            ("Cost / query", f"${scorecard.performance.mean_cost_usd:.5f}", None),
        ]
    )

    comparisons = [c for c in regression.comparisons if c.delta is not None]
    chart = ""
    if comparisons:
        chart = (
            '<div class="card"><h3>Change against baseline</h3>'
            + delta_bars(
                [c.metric for c in comparisons[:12]],
                [c.delta or 0.0 for c in comparisons[:12]],
                lower_is_better=LOWER_IS_BETTER,
            )
            + '<p class="note">Green is an improvement whichever way the bar points - '
            "a fall in hallucination rate is good.</p></div>"
        )

    rows = "".join(
        f"<tr><td>{escape(c.metric)}</td>"
        f'<td class="num">{_opt(c.baseline)}</td>'
        f'<td class="num">{_opt(c.current)}</td>'
        f'<td class="num">{_signed(c.delta)}</td>'
        f"<td>{_verdict_pill(c.verdict, c.blocking)}</td>"
        f"<td>{escape(c.reason)}</td></tr>"
        for c in regression.comparisons
    )

    failing = _failing_cases_table(scorecard)

    return f"""
{banner}
{tiles}
{chart}
{_trend(output_dir, scorecard.dataset)}
<h2>Regression detail</h2>
<div class="card scroll"><table>
<thead><tr><th>Metric</th><th class="num">Baseline</th><th class="num">Current</th>
<th class="num">Delta</th><th>Verdict</th><th>Reason</th></tr></thead>
<tbody>{rows}</tbody></table></div>
{failing}
<h2>Run configuration</h2>
<div class="card scroll">{_kv_table(scorecard.configuration)}</div>
"""


def _retrieval(scorecard: Scorecard, run: RunResult) -> str:
    retrieval = scorecard.retrieval
    ks = sorted(retrieval.recall)

    recall_chart = bar_chart(
        [f"@{k}" for k in ks] or ["-"],
        [retrieval.recall.get(k, 0.0) for k in ks] or [0.0],
        max_value=1.0,
    )
    precision_chart = bar_chart(
        [f"@{k}" for k in sorted(retrieval.precision)] or ["-"],
        [retrieval.precision[k] for k in sorted(retrieval.precision)] or [0.0],
        colours=[PALETTE[1]] * max(len(retrieval.precision), 1),
        max_value=1.0,
    )

    tiles = _tiles(
        [
            ("Scored cases", str(retrieval.scored_cases), f"of {retrieval.total_cases}"),
            ("MRR", _fmt(retrieval.mrr), None),
            ("nDCG@10", _fmt(retrieval.ndcg.get(10, 0.0)), None),
            ("Mean similarity", _fmt(retrieval.mean_similarity), None),
            (
                "Mean rerank score",
                _fmt(retrieval.mean_rerank_score) if retrieval.mean_rerank_score else "-",
                "reranker off" if retrieval.mean_rerank_score is None else None,
            ),
            ("Retrieved / query", f"{retrieval.mean_retrieved:.1f}", None),
            ("In context / query", f"{retrieval.mean_in_context:.1f}", None),
            ("Duplicate rate", _fmt(retrieval.mean_duplicate_rate), "lower is better"),
        ]
    )

    zero = retrieval.zero_recall_case_ids
    zero_block = ""
    if zero:
        lookup = {result.case.id: result for result in run.results}
        rows = "".join(
            f"<tr><td><code>{escape(case_id)}</code></td>"
            f"<td>{escape(lookup[case_id].case.question[:110]) if case_id in lookup else ''}</td>"
            f'<td class="num">{_fmt(lookup[case_id].top_similarity) if case_id in lookup else "-"}</td>'
            f"<td>{escape(lookup[case_id].retrieval_mode or '') if case_id in lookup else ''}</td></tr>"
            for case_id in zero[:40]
        )
        zero_block = f"""
<h2>Questions that retrieved nothing relevant ({len(zero)})</h2>
<div class="card scroll"><table>
<thead><tr><th>Case</th><th>Question</th><th class="num">Top similarity</th>
<th>Mode</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">The triage list. A case here either has a wrong expectation, a
missing document, or a genuine retrieval failure - and the three are
distinguishable only by looking.</p></div>"""

    by_tag = _by_tag_table(scorecard)

    return f"""
{tiles}
<h2>Recall and precision</h2>
<div class="card"><h3>Recall@k</h3>{recall_chart}
<h3>Precision@k</h3>{precision_chart}</div>
{by_tag}
{zero_block}
"""


def _citation(scorecard: Scorecard, run: RunResult) -> str:
    citation = scorecard.citation
    tiles = _tiles(
        [
            ("Citation precision", _fmt(citation.precision), None),
            ("Citation recall", _fmt(citation.recall), None),
            ("Fully grounded", _fmt(citation.fully_grounded_rate), "all citations correct"),
            ("Hallucination rate", _fmt(citation.hallucination_rate), "lower is better"),
            ("Hallucinated labels", str(citation.hallucinated), "stripped before display"),
            ("Broken citations", str(citation.broken), "should be zero"),
            ("Imprecise", str(citation.imprecise), "real, but not what was expected"),
            ("Uncited answers", str(citation.uncited_answers), "substantive, no citation"),
        ]
    )

    breakdown = bar_chart(
        ["correct", "imprecise", "broken", "hallucinated"],
        [
            float(
                max(
                    citation.total_citations
                    - citation.imprecise
                    - citation.broken
                    - citation.hallucinated,
                    0,
                )
            ),
            float(citation.imprecise),
            float(citation.broken),
            float(citation.hallucinated),
        ],
        colours=[GOOD, "#D97706", BAD, BAD],
        value_format="{:.0f}",
    )

    lookup = {result.case.id: result for result in run.results}
    rows = "".join(
        f"<tr><td><code>{escape(case_id)}</code></td>"
        f"<td>{escape(lookup[case_id].case.question[:110]) if case_id in lookup else ''}</td>"
        f'<td class="num">{len(lookup[case_id].citations) if case_id in lookup else 0}</td>'
        f'<td class="num">{_fmt(lookup[case_id].confidence) if case_id in lookup else "-"}</td></tr>'
        for case_id in citation.worst_case_ids[:30]
    )
    worst = ""
    if rows:
        worst = f"""
<h2>Worst-cited answers</h2>
<div class="card scroll"><table>
<thead><tr><th>Case</th><th>Question</th><th class="num">Citations</th>
<th class="num">Confidence</th></tr></thead><tbody>{rows}</tbody></table></div>"""

    return f"""
{tiles}
<h2>Citation breakdown</h2>
<div class="card">{breakdown}
<p class="note"><strong>Broken</strong> citations should be structurally
impossible - the answer validator allow-lists the labels it offered. A non-zero
count is a defect in the validator, not in the model.</p></div>
{worst}
"""


def _planner(scorecard: Scorecard) -> str:
    planner = scorecard.planner
    guardrail = scorecard.guardrail

    tiles = _tiles(
        [
            (
                "Intent accuracy",
                _pct(planner.intent_accuracy),
                f"{planner.intent_labelled} labelled",
            ),
            (
                "Doc-type accuracy",
                _pct(planner.document_type_accuracy),
                f"{planner.document_type_labelled} labelled",
            ),
            ("False filtering", _fmt(planner.false_filtering_rate), "lower is better"),
            ("Filter declined", str(planner.filter_declined), "below threshold"),
            ("Filter retries", str(planner.filter_retries), "widened after empty"),
            ("Analysis unavailable", str(planner.analysis_unavailable), "rules only"),
            ("Scope truncations", str(planner.scope_truncations), None),
            ("Guardrail accuracy", _fmt(guardrail.accuracy), None),
        ]
    )

    matrix = f"""
<div class="card"><h3>Guardrail confusion matrix</h3>
<table>
<thead><tr><th></th><th class="num">Answered</th><th class="num">Declined</th></tr></thead>
<tbody>
<tr><td><strong>Should answer</strong></td>
<td class="num"><span class="pill ok">{guardrail.true_accept}</span></td>
<td class="num"><span class="pill warn">{guardrail.false_reject}</span></td></tr>
<tr><td><strong>Should decline</strong></td>
<td class="num"><span class="pill bad">{guardrail.false_accept}</span></td>
<td class="num"><span class="pill ok">{guardrail.true_reject}</span></td></tr>
</tbody></table>
<p class="note">False accept is the dangerous quadrant: the platform produced
contract terms for a question the corpus cannot support. False reject is costly
but safe.</p></div>"""

    level = f"""
<div class="card"><h3>Per-level guardrail</h3>
<p>A document summary cleared the answer threshold while no clause or chunk did in
<strong>{guardrail.document_summary_would_have_passed}</strong> cases. Of those,
<strong>{guardrail.hallucinations_prevented}</strong> were questions the corpus
genuinely could not answer.</p>
<p class="note">Each of those would have passed a guardrail computed on the
whole-result maximum. This is the fix measured rather than assumed.</p></div>"""

    strategies = bar_chart(
        list(planner.strategy_counts) or ["-"],
        [float(v) for v in planner.strategy_counts.values()] or [0.0],
        colours=list(PALETTE),
        value_format="{:.0f}",
    )
    modes = bar_chart(
        list(planner.mode_counts) or ["-"],
        [float(v) for v in planner.mode_counts.values()] or [0.0],
        colours=[PALETTE[2]] * max(len(planner.mode_counts), 1),
        value_format="{:.0f}",
    )

    failures = ""
    if planner.false_filtering_case_ids:
        items = "".join(
            f"<li><code>{escape(case_id)}</code></li>"
            for case_id in planner.false_filtering_case_ids[:20]
        )
        failures = (
            '<div class="card"><h3>False filtering</h3><ul>'
            + items
            + '</ul><p class="note">A document-type filter was applied and nothing the '
            "case expected came back. Either the type was wrong or the document is "
            "missing.</p></div>"
        )

    return f"""
{tiles}
<h2>Guardrail</h2>
{matrix}
{level}
<h2>Planner decisions</h2>
<div class="card"><h3>Strategy</h3>{strategies}<h3>Retrieval mode</h3>{modes}</div>
{failures}
"""


def _confidence(scorecard: Scorecard) -> str:
    calibration = scorecard.calibration
    if calibration is None:
        return '<div class="card">No calibration data.</div>'

    raw = calibration.raw
    tiles = _tiles(
        [
            ("Samples", str(raw.samples), "judgeable cases"),
            ("ECE", _fmt(raw.ece), "lower is better"),
            ("MCE", _fmt(raw.mce), "worst bin"),
            ("Brier", _fmt(raw.brier), None),
            ("Base rate", _fmt(raw.base_rate), "share correct"),
            (
                "Bias",
                f"{raw.mean_bias:+.3f}",
                "positive = over-confident",
            ),
            ("ECE after Platt", _fmt(calibration.platt_metrics.ece), None),
            ("ECE after isotonic", _fmt(calibration.isotonic_metrics.ece), None),
        ]
    )

    rows = "".join(
        f"<tr><td>{b['lower']:.1f}-{b['upper']:.1f}</td>"
        f'<td class="num">{b["count"]}</td>'
        f'<td class="num">{b["mean_confidence"]:.3f}</td>'
        f'<td class="num">{b["observed_accuracy"]:.3f}</td>'
        f'<td class="num">{b["gap"]:+.3f}</td></tr>'
        for b in (bin_.as_dict() for bin_ in raw.bins)
        if b["count"]
    )

    return f"""
{tiles}
<h2>Reliability</h2>
<div class="card">{reliability_diagram([bin_.as_dict() for bin_ in raw.bins])}
<p class="note">Bars below the dashed diagonal are over-confidence, shaded red.
That is the harmful direction: a reviewer reading 80% and getting 55% acts on
something they should have checked.</p></div>
<div class="card scroll"><table>
<thead><tr><th>Bin</th><th class="num">Count</th><th class="num">Mean confidence</th>
<th class="num">Observed</th><th class="num">Gap</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>Recommendation</h2>
<div class="card"><p>{escape(calibration.recommendation)}</p>
<p class="note">Calibrators are fitted and scored here only. Runtime confidence is
unchanged - a figure that silently changed meaning between releases would be worse
than one that was never calibrated.</p></div>
"""


def _latency(scorecard: Scorecard) -> str:
    performance = scorecard.performance
    tiles = _tiles(
        [
            ("p50", f"{performance.latency_p50:.0f} ms", None),
            ("p95", f"{performance.latency_p95:.0f} ms", None),
            ("p99", f"{performance.latency_p99:.0f} ms", None),
            ("max", f"{performance.latency_max:.0f} ms", None),
            ("mean", f"{performance.latency_mean:.0f} ms", None),
            ("Cases", str(performance.cases), None),
        ]
    )

    stage_chart = bar_chart(
        [stage.stage for stage in performance.stages] or ["-"],
        [stage.mean for stage in performance.stages] or [0.0],
        colours=list(PALETTE),
        value_format="{:.0f}",
    )

    rows = "".join(
        f"<tr><td>{escape(stage.stage)}</td>"
        f'<td class="num">{stage.mean:.0f}</td>'
        f'<td class="num">{stage.p50:.0f}</td>'
        f'<td class="num">{stage.p95:.0f}</td>'
        f'<td class="num">{stage.p99:.0f}</td>'
        f'<td class="num">{stage.share:.1%}</td></tr>'
        for stage in performance.stages
    )

    return f"""
{tiles}
<h2>Where the time goes</h2>
<div class="card">{stage_chart}
<p class="note">Mean milliseconds per stage. The share column is what says what to
optimise next.</p></div>
<div class="card scroll"><table>
<thead><tr><th>Stage</th><th class="num">Mean</th><th class="num">p50</th>
<th class="num">p95</th><th class="num">p99</th><th class="num">Share</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card"><p class="note">Measured inside the process, from the start of
retrieval planning to the end of generation. It excludes HTTP, auth and
serialisation, so it is a floor on what a user experiences rather than the figure
itself - the load-test driver measures that over the wire.</p></div>
"""


def _cost(scorecard: Scorecard, run: RunResult) -> str:
    performance = scorecard.performance
    tiles = _tiles(
        [
            ("Total", f"${performance.total_cost_usd:.4f}", f"{performance.cases} queries"),
            ("Mean / query", f"${performance.mean_cost_usd:.5f}", None),
            ("p95 / query", f"${performance.p95_cost_usd:.5f}", None),
            ("Total tokens", f"{performance.total_tokens:,}", None),
            ("Mean tokens", f"{performance.mean_tokens:.0f}", None),
            (
                "Projected / 10k",
                f"${performance.mean_cost_usd * 10_000:.2f}",
                "at this mean",
            ),
        ]
    )

    lookup = {result.case.id: result for result in run.results}
    rows = "".join(
        f"<tr><td><code>{escape(case_id)}</code></td>"
        f"<td>{escape(lookup[case_id].case.question[:90]) if case_id in lookup else ''}</td>"
        f'<td class="num">{lookup[case_id].tokens if case_id in lookup else 0:,}</td>'
        f'<td class="num">${lookup[case_id].cost_usd:.5f}</td></tr>'
        for case_id in performance.most_expensive_case_ids[:20]
        if case_id in lookup
    )

    return f"""
{tiles}
<h2>Most expensive questions</h2>
<div class="card scroll"><table>
<thead><tr><th>Case</th><th>Question</th><th class="num">Tokens</th>
<th class="num">Cost</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">Cost is attributed from the provider's own usage figures. A model
with no entry in the pricing table attributes zero rather than a guess, so a total
of $0.00 means unpriced, not free.</p></div>
"""


# =============================================================================
# Fragments
# =============================================================================
def _failing_cases_table(scorecard: Scorecard) -> str:
    sections = [
        ("Zero-recall questions", scorecard.retrieval.zero_recall_case_ids),
        ("False accepts (answered when it should not)", scorecard.guardrail.false_accept_case_ids),
        ("False rejects (declined when it should not)", scorecard.guardrail.false_reject_case_ids),
        ("Worst-cited answers", scorecard.citation.worst_case_ids),
    ]
    blocks = [
        f"<h3>{escape(title)} ({len(ids)})</h3><p>"
        + " ".join(f"<code>{escape(case_id)}</code>" for case_id in ids[:25])
        + "</p>"
        for title, ids in sections
        if ids
    ]
    if not blocks:
        return ""
    return f'<h2>Failing cases</h2><div class="card">{"".join(blocks)}</div>'


def _by_tag_table(scorecard: Scorecard) -> str:
    if not scorecard.by_tag:
        return ""
    rows = "".join(
        f"<tr><td>{escape(tag)}</td>"
        f'<td class="num">{int(values["cases"])}</td>'
        f'<td class="num">{values["recall@10"]:.3f}</td>'
        f'<td class="num">{values["mrr"]:.3f}</td>'
        f'<td class="num">{values["citation_precision"]:.3f}</td>'
        f'<td class="num">{values["guardrail_accuracy"]:.3f}</td></tr>'
        for tag, values in sorted(scorecard.by_tag.items())
    )
    return f"""
<h2>By segment</h2>
<div class="card scroll"><table>
<thead><tr><th>Tag</th><th class="num">Cases</th><th class="num">Recall@10</th>
<th class="num">MRR</th><th class="num">Citation precision</th>
<th class="num">Guardrail</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">Segments with fewer than three cases are omitted - an average over
two is noise presented as a trend.</p></div>"""


def _tiles(entries: list[tuple[str, str, str | None]]) -> str:
    cells = "".join(
        f'<div class="tile"><div class="k">{escape(key)}</div>'
        f'<div class="n">{escape(value)}</div>'
        + (f'<div class="d">{escape(detail)}</div>' if detail else "")
        + "</div>"
        for key, value, detail in entries
    )
    return f'<div class="tiles">{cells}</div>'


def _kv_table(payload: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{escape(str(key))}</td><td><code>{escape(str(value))}</code></td></tr>"
        for key, value in sorted(payload.items())
    )
    return f"<table><tbody>{rows}</tbody></table>"


def _verdict_pill(verdict: Verdict, blocking: bool) -> str:
    classes = {
        Verdict.IMPROVED: "ok",
        Verdict.REGRESSED: "bad" if blocking else "warn",
        Verdict.UNCHANGED: "mute",
        Verdict.MISSING: "bad" if blocking else "warn",
        Verdict.NEW: "mute",
    }
    return f'<span class="pill {classes[verdict]}">{verdict.value}</span>'


def _page(current: str, scorecard: Scorecard, body: str) -> str:
    nav = "".join(
        f'<a href="{name}" {"style=font-weight:600" if name == current else ""}>{escape(title)}</a>'
        for name, title in _PAGES
    )
    title = next((t for n, t in _PAGES if n == current), "Report")
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} - {escape(scorecard.dataset)}</title>
<style>{_CSS}</style></head>
<body><main>
<h1>{escape(title)}</h1>
<p class="sub">{escape(scorecard.dataset)} &middot; {scorecard.cases} cases &middot;
{escape(scorecard.label or "unlabelled")} &middot; generated {generated}</p>
<nav>{nav}</nav>
{body}
</main></body></html>
"""


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _opt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _signed(value: float | None) -> str:
    if value is None:
        return "-"
    colour = GOOD if value > 0 else (BAD if value < 0 else NEUTRAL)
    return f'<span style="color:{colour}">{value:+.4f}</span>'


__all__ = ["LOWER_IS_BETTER", "write_html_reports"]
