"""Consolidated routing analysis report writer.

Reads the routing fixture, drives each question through the live router,
pulls per-service `/metrics`, and writes `routing-analysis-report.md` with:
- Routing accuracy against the fixture's `expected` labels
- Per-service request volume + p95 latency from `/metrics`
- One identified routing pattern (e.g., backend imbalance)
- A short cross-service correlation paragraph

Honors Track — TODO implementations required.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from typing import Any

import httpx


def load_fixture(path: str) -> list[dict[str, str]]:
    """Read the routing fixture's questions list."""
    with open(path) as f:
        payload = json.load(f)
    return payload["questions"]


def drive_router(router_base: str, questions: list[dict[str, str]]) -> list[dict[str, Any]]:
    """POST each question to the router; return the list of routing decisions."""
    decisions: list[dict[str, Any]] = []
    for q in questions:
        # Direct httpx.post() call (no client context manager) so this
        # function stays trivially monkeypatchable in tests.
        response = httpx.post(
            f"{router_base}/route",
            json={"question": q["question"]},
            timeout=30.0,
        )
        response.raise_for_status()
        body = response.json()
        decision = dict(body["decision"])
        decision["expected"] = q.get("expected")
        decisions.append(decision)
    return decisions


def routing_accuracy(decisions: list[dict[str, Any]]) -> float:
    """Return the fraction of decisions whose `target` matches `expected`."""
    if not decisions:
        return 0.0
    correct = sum(1 for d in decisions if d.get("target") == d.get("expected"))
    return correct / len(decisions)


def fetch_metrics(base_url: str) -> str:
    """GET {base_url}/metrics and return the OpenMetrics text body."""
    response = httpx.get(f"{base_url}/metrics", timeout=30.0)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# OpenMetrics text parsing helpers.
# ---------------------------------------------------------------------------

_SAMPLE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)\s*$'
)
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _parse_openmetrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    samples = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        labels = dict(_LABEL_RE.findall(m.group("labels") or ""))
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        samples.append((m.group("name"), labels, value))
    return samples


def _request_counts_and_errors(samples: list[tuple[str, dict, float]]) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"total": 0.0, "errors": 0.0})
    for name, labels, value in samples:
        if name != "service_requests_total":
            continue
        service = labels.get("service", "unknown")
        stats[service]["total"] += value
        status = labels.get("status", "")
        if status.startswith(("4", "5")):
            stats[service]["errors"] += value
    return stats


def _p95_latency_seconds(samples: list[tuple[str, dict, float]]) -> dict[str, float | None]:
    # Group cumulative histogram buckets by service.
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for name, labels, value in samples:
        if name != "service_request_latency_seconds_bucket":
            continue
        service = labels.get("service", "unknown")
        le_raw = labels.get("le", "")
        le = float("inf") if le_raw == "+Inf" else float(le_raw)
        buckets[service].append((le, value))

    p95: dict[str, float | None] = {}
    for service, bucket_list in buckets.items():
        bucket_list.sort(key=lambda pair: pair[0])
        total = bucket_list[-1][1] if bucket_list else 0.0
        if total <= 0:
            p95[service] = None
            continue
        target = 0.95 * total
        finite_boundaries = [le for le, _ in bucket_list if le != float("inf")]
        chosen = None
        for le, cumulative in bucket_list:
            if cumulative >= target:
                chosen = le
                break
        if chosen is None or chosen == float("inf"):
            # The p95 falls past the last finite bucket boundary; report the
            # largest finite boundary as a lower-bound approximation rather
            # than a non-numeric "inf".
            chosen = finite_boundaries[-1] if finite_boundaries else None
        p95[service] = chosen
    return p95


def routing_accuracy_from_decisions(decisions: list[dict[str, Any]]) -> float:
    return routing_accuracy(decisions)


def render_report(
    decisions: list[dict[str, Any]],
    accuracy: float,
    service_metrics: dict[str, str],
    report_path: str,
) -> None:
    """Write the consolidated `routing-analysis-report.md`."""
    lines: list[str] = []

    lines.append("# Multi-Service Routing Analysis Report")
    lines.append("")

    # ---- Routing Accuracy ----
    lines.append("## Routing Accuracy")
    lines.append("")
    lines.append(f"Routing accuracy on the held-out fixture: **{accuracy:.3f}** "
                  f"({sum(1 for d in decisions if d.get('target') == d.get('expected'))}/"
                  f"{len(decisions)} correct).")
    lines.append("")

    # ---- Per-Service Metrics ----
    lines.append("## Per-Service Metrics")
    lines.append("")
    lines.append("| Service | Total Requests | Error Rate | p95 Latency (s) |")
    lines.append("|---|---:|---:|---:|")
    for service_name, raw_text in service_metrics.items():
        samples = _parse_openmetrics(raw_text)
        counts = _request_counts_and_errors(samples)
        p95_by_service = _p95_latency_seconds(samples)

        # A service's own /metrics only carries its own `service` label,
        # but fall back to aggregating everything found if that label is
        # missing for some reason.
        if counts:
            total = sum(v["total"] for v in counts.values())
            errors = sum(v["errors"] for v in counts.values())
            p95_values = [v for v in p95_by_service.values() if v is not None]
            p95 = max(p95_values) if p95_values else None
        else:
            total, errors, p95 = 0.0, 0.0, None

        error_rate = (errors / total) if total else 0.0
        p95_display = f"{p95:.4f}" if p95 is not None else "n/a"
        lines.append(f"| {service_name} | {int(total)} | {error_rate:.2%} | {p95_display} |")
    lines.append("")

    # ---- Routing Pattern ----
    lines.append("## Routing Pattern")
    lines.append("")
    target_counts = Counter(d.get("target", "unknown") for d in decisions)
    total_decisions = sum(target_counts.values()) or 1
    for target, count in target_counts.most_common():
        pct = count / total_decisions
        lines.append(f"- `{target}`: {count}/{total_decisions} requests ({pct:.1%})")
    if target_counts:
        dominant, dominant_count = target_counts.most_common(1)[0]
        dominant_pct = dominant_count / total_decisions
        if dominant_pct >= 0.65:
            lines.append("")
            lines.append(
                f"Observed pattern: routing is skewed toward `{dominant}` "
                f"({dominant_pct:.0%} of traffic), suggesting the fixture "
                "(or real traffic) leans toward that question type."
            )
        else:
            lines.append("")
            lines.append("Observed pattern: routing decisions are reasonably balanced "
                          "across backends.")
    lines.append("")

    # ---- Cross-Service Correlation ----
    lines.append("## Cross-Service Correlation")
    lines.append("")
    sample_ids = [d.get("request_id") for d in decisions[:3] if d.get("request_id")]
    sample_display = ", ".join(sample_ids) if sample_ids else "(no decisions recorded)"
    lines.append(
        "Each inbound request is assigned a `request_id` (uuid4) by the router's "
        "`request_id_middleware` and forwarded to the chosen backend as the "
        "`X-Request-ID` header. Both `ner-kg` and `rag` echo the same header "
        "back on their response and include it in every structured log line "
        "they emit for that request, so filtering `docker compose logs` on a "
        "single id yields a continuous trace across all three services. "
        f"Example ids captured in this run: {sample_display}."
    )
    lines.append("")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M11 Stretch-Thu routing analysis")
    p.add_argument("--fixture", required=True)
    p.add_argument("--router-base", required=True)
    p.add_argument("--ner-kg-base", required=True)
    p.add_argument("--rag-base", required=True)
    p.add_argument("--report-out", default="routing-analysis-report.md")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    questions = load_fixture(args.fixture)
    decisions = drive_router(args.router_base, questions)
    accuracy = routing_accuracy(decisions)
    service_metrics = {
        "router": fetch_metrics(args.router_base),
        "ner-kg": fetch_metrics(args.ner_kg_base),
        "rag": fetch_metrics(args.rag_base),
    }
    render_report(decisions, accuracy, service_metrics, args.report_out)
    print(f"Wrote {args.report_out} (accuracy={accuracy:.3f})")


if __name__ == "__main__":
    main()