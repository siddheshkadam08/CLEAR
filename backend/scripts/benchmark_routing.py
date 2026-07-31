"""Benchmark the model-routing change: cost and latency, before against after.

The "before" is the behaviour this work replaced - every task went to the single
configured ``LLM_MODEL``, because ``route_model`` only recognised two special
purposes and defaulted everything else. The "after" is the tiered router.

What this measures is a *pricing and configuration* difference, not a live one:
it counts the calls a real pipeline run makes, per task, and prices them under
both routing policies using the same token volumes. That isolates the routing
decision from provider variance, which is the only way to attribute a change to
the routing rather than to the weather on the provider's side.

Latency is measured live, because per-tier timeouts and retries only show up
against a real provider. That part is opt-in (``--live``) since it costs money.

Usage:
    python -m scripts.benchmark_routing                    # cost model only
    python -m scripts.benchmark_routing --live --samples 3 # + measured latency
    python -m scripts.benchmark_routing --json report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from app.ai.routing import TASK_TIERS, LLMTask, ModelTier, get_router
from app.ai.rag.providers import TokenUsage
from app.core.config import get_settings

#: Calls a single 28-page agreement makes, per task, taken from a real run.
#: Extraction dominates: one call per clause category, and there are ~30.
_WORKLOAD: dict[LLMTask, tuple[int, int, int]] = {
    # task: (calls, input_tokens_each, output_tokens_each)
    LLMTask.CLAUSE_EXTRACTION: (30, 4_200, 900),
    LLMTask.ENTITY_EXTRACTION: (4, 3_800, 600),
    LLMTask.KEY_VALUE_EXTRACTION: (4, 3_800, 700),
    LLMTask.RISK_ANALYSIS: (3, 4_000, 800),
    LLMTask.DOCUMENT_CLASSIFICATION: (1, 1_600, 120),
    LLMTask.SUMMARIZATION: (13, 2_400, 400),
    LLMTask.LEGAL_REASONING: (1, 5_000, 1_200),
}


@dataclass(slots=True)
class TaskCost:
    task: str
    tier: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "tier": self.tier,
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 4),
        }


@dataclass(slots=True)
class Comparison:
    before: list[TaskCost] = field(default_factory=list)
    after: list[TaskCost] = field(default_factory=list)
    latency: dict[str, Any] = field(default_factory=dict)

    @property
    def before_total(self) -> float:
        return sum(entry.cost_usd for entry in self.before)

    @property
    def after_total(self) -> float:
        return sum(entry.cost_usd for entry in self.after)

    @property
    def saving_pct(self) -> float:
        if not self.before_total:
            return 0.0
        return round((self.before_total - self.after_total) / self.before_total * 100, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "before": [entry.as_dict() for entry in self.before],
            "after": [entry.as_dict() for entry in self.after],
            "totals": {
                "before_usd": round(self.before_total, 4),
                "after_usd": round(self.after_total, 4),
                "saving_usd": round(self.before_total - self.after_total, 4),
                "saving_pct": self.saving_pct,
            },
            "latency": self.latency,
        }


def _cost(model: str, calls: int, input_each: int, output_each: int) -> float:
    usage = TokenUsage(input_tokens=input_each * calls, output_tokens=output_each * calls)
    return usage.cost_usd(model)


def build_comparison() -> Comparison:
    """Price the same workload under single-model and tiered routing."""
    settings = get_settings().llm
    router = get_router()
    comparison = Comparison()

    for task, (calls, input_each, output_each) in _WORKLOAD.items():
        # Before: every task took the one configured model, whatever it was.
        legacy_model = settings.model
        comparison.before.append(
            TaskCost(
                task=task.value,
                tier="(single model)",
                model=legacy_model,
                calls=calls,
                input_tokens=input_each * calls,
                output_tokens=output_each * calls,
                cost_usd=_cost(legacy_model, calls, input_each, output_each),
            )
        )

        choice = router.resolve(task)
        comparison.after.append(
            TaskCost(
                task=task.value,
                tier=choice.tier.value,
                model=choice.model,
                calls=calls,
                input_tokens=input_each * calls,
                output_tokens=output_each * calls,
                cost_usd=_cost(choice.model, calls, input_each, output_each),
            )
        )

    return comparison


async def measure_latency(samples: int) -> dict[str, Any]:
    """Measured latency per tier, against the configured provider.

    Reports p50/p95/p99 rather than a mean: the mean hides exactly the tail that
    per-tier timeouts exist to bound.
    """
    from app.ai.rag.providers import get_inference_provider

    provider = get_inference_provider()
    results: dict[str, Any] = {}

    probes = {
        ModelTier.SIMPLE: LLMTask.DOCUMENT_CLASSIFICATION,
        ModelTier.COMPLEX: LLMTask.CLAUSE_COMPARISON,
    }

    for tier, task in probes.items():
        durations: list[float] = []
        failures = 0
        for _ in range(samples):
            started = time.perf_counter()
            try:
                await provider.generate(
                    system="You are a benchmarking probe. Answer in one word.",
                    prompt="Reply with the single word: acknowledged.",
                    purpose=task.value,
                    max_tokens=16,
                )
                durations.append(time.perf_counter() - started)
            except Exception as exc:  # noqa: BLE001 - a failed probe is a datapoint
                failures += 1
                print(f"  {tier.value} probe failed: {exc}", file=sys.stderr)

        if durations:
            ordered = sorted(durations)
            results[tier.value] = {
                "samples": len(ordered),
                "failures": failures,
                "mean_s": round(statistics.fmean(ordered), 3),
                "p50_s": round(ordered[len(ordered) // 2], 3),
                "p95_s": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
                "p99_s": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))], 3),
                "timeout_s": get_router().resolve(task).timeout_seconds,
            }
        else:
            results[tier.value] = {"samples": 0, "failures": failures}

    return results


def render(comparison: Comparison) -> None:
    print("\nModel routing benchmark")
    print("=" * 78)
    print(f"{'task':<28}{'tier':<12}{'model':<24}{'before':>7}{'after':>8}")
    print("-" * 78)
    for before, after in zip(comparison.before, comparison.after, strict=True):
        print(
            f"{after.task:<28}{after.tier:<12}{after.model[:23]:<24}"
            f"{before.cost_usd:>7.3f}{after.cost_usd:>8.3f}"
        )
    print("-" * 78)
    print(
        f"{'TOTAL (one 28-page contract)':<64}"
        f"{comparison.before_total:>7.3f}{comparison.after_total:>8.3f}"
    )
    print(
        f"\nSaving: ${comparison.before_total - comparison.after_total:.3f} per contract "
        f"({comparison.saving_pct}%)"
    )
    print(f"At 1,000 contracts: ${(comparison.before_total - comparison.after_total) * 1000:,.0f}")

    tiers = {task.value: tier.value for task, tier in TASK_TIERS.items()}
    by_tier: dict[str, int] = {}
    for tier in tiers.values():
        by_tier[tier] = by_tier.get(tier, 0) + 1
    print(f"\nTask coverage: {len(TASK_TIERS)} tasks routed - {by_tier}")

    if comparison.latency:
        print("\nMeasured latency")
        print("-" * 78)
        for tier, stats in comparison.latency.items():
            if not stats.get("samples"):
                print(f"  {tier}: no successful probes ({stats.get('failures', 0)} failed)")
                continue
            print(
                f"  {tier:<10} n={stats['samples']:<3} mean={stats['mean_s']:>6.2f}s  "
                f"p50={stats['p50_s']:>6.2f}s  p95={stats['p95_s']:>6.2f}s  "
                f"p99={stats['p99_s']:>6.2f}s  (timeout {stats['timeout_s']}s)"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Measure latency against the provider.")
    parser.add_argument("--samples", type=int, default=3, help="Probes per tier when --live.")
    parser.add_argument("--json", dest="json_path", default="", help="Write the report as JSON.")
    args = parser.parse_args()

    comparison = build_comparison()
    if args.live:
        comparison.latency = asyncio.run(measure_latency(args.samples))

    render(comparison)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(comparison.as_dict(), handle, indent=2)
        print(f"\nWrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
