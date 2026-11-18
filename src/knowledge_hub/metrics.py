"""A tiny Prometheus text exposition (plan 3.2). Labels come from fixed sets only.

Never put a sub, audience, email or document id in a label. Tool names are the registered ones;
every other label value is checked against a closed set and falls back to ``other``.
"""

from __future__ import annotations

import threading

TOOL_OUTCOMES = frozenset({"ok", "error", "internal", "rate_limited"})
REJECTION_CODES = frozenset(
    {
        "bad_body", "batch_not_allowed", "malformed", "unknown_key", "bad_algorithm", "bad_type",
        "bad_signature", "bad_audience", "bad_issuer", "expired", "not_yet_valid", "bad_lifetime",
        "bad_version", "bad_principal_type", "request_required", "request_mismatch",
        "request_unexpected", "replayed", "bad_audience_name", "bad_content_length", "disconnected",
        "human_refused", "missing_header", "service_tool_call", "too_large",
    }
)  # fmt: skip
WEBHOOK_RESULTS = frozenset({"queued", "ignored", "bad_signature", "bad_request", "too_large"})
WRITE_FAILURE_REASONS = frozenset({"refused", "forgejo_error"})
JOB_STATES = ("queued", "running", "done", "failed")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._calls: dict[tuple[str, str], int] = {}
            self._rejections: dict[str, int] = {}
            self._webhooks: dict[str, int] = {}
            self._write_failures: dict[tuple[str, str], int] = {}
            self._usage_dropped = 0
            self._seconds: dict[str, list[float]] = {}  # tool -> [sum, count]

    def tool_call(self, tool: str, outcome: str, seconds: float) -> None:
        outcome = outcome if outcome in TOOL_OUTCOMES else "error"
        with self._lock:
            self._calls[(tool, outcome)] = self._calls.get((tool, outcome), 0) + 1
            acc = self._seconds.setdefault(tool, [0.0, 0])
            acc[0] += seconds
            acc[1] += 1

    def claim_rejection(self, code: str) -> None:
        code = code if code in REJECTION_CODES else "other"
        with self._lock:
            self._rejections[code] = self._rejections.get(code, 0) + 1

    def webhook_event(self, result: str) -> None:
        result = result if result in WEBHOOK_RESULTS else "other"
        with self._lock:
            self._webhooks[result] = self._webhooks.get(result, 0) + 1

    def agent_write_failure(self, tool: str, reason: str) -> None:
        reason = reason if reason in WRITE_FAILURE_REASONS else "forgejo_error"
        with self._lock:
            self._write_failures[(tool, reason)] = self._write_failures.get((tool, reason), 0) + 1

    def usage_dropped(self, n: int) -> None:
        with self._lock:
            self._usage_dropped += n

    def render(self, job_states: dict[str, int] | None = None) -> str:
        out: list[str] = []

        def block(name: str, kind: str, help_: str, rows: list[tuple[str, float | int]]) -> None:
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {kind}")
            out.extend(f"{label} {value}" for label, value in rows)

        with self._lock:
            calls = sorted(self._calls.items())
            rejections = sorted(self._rejections.items())
            webhooks = sorted(self._webhooks.items())
            seconds = sorted(self._seconds.items())
            failures = sorted(self._write_failures.items())
            dropped = self._usage_dropped
        block(
            "kh_tool_calls_total",
            "counter",
            "Tool calls by tool and outcome.",
            [(f'kh_tool_calls_total{{tool="{_escape(t)}",outcome="{o}"}}', n) for (t, o), n in calls],
        )
        block(
            "kh_claim_rejections_total",
            "counter",
            "Rejected identity claims by reason code.",
            [(f'kh_claim_rejections_total{{code="{c}"}}', n) for c, n in rejections],
        )
        block(
            "kh_webhook_events_total",
            "counter",
            "Forgejo webhook deliveries by result.",
            [(f'kh_webhook_events_total{{result="{r}"}}', n) for r, n in webhooks],
        )
        block(
            "kh_agent_write_failures_total",
            "counter",
            "Failed agent writes by tool and reason.",
            [
                (f'kh_agent_write_failures_total{{tool="{_escape(t)}",reason="{r}"}}', n)
                for (t, r), n in failures
            ],
        )
        block(
            "kh_usage_events_dropped_total",
            "counter",
            "Usage events dropped before they were stored.",
            [("kh_usage_events_dropped_total", dropped)],
        )
        block(
            "kh_tool_seconds_sum",
            "counter",
            "Seconds spent in tool calls.",
            [(f'kh_tool_seconds_sum{{tool="{_escape(t)}"}}', round(a[0], 6)) for t, a in seconds],
        )
        block(
            "kh_tool_seconds_count",
            "counter",
            "Timed tool calls.",
            [(f'kh_tool_seconds_count{{tool="{_escape(t)}"}}', int(a[1])) for t, a in seconds],
        )
        if job_states is not None:
            block(
                "kh_index_jobs",
                "gauge",
                "Index jobs by state.",
                [(f'kh_index_jobs{{state="{s}"}}', job_states.get(s, 0)) for s in JOB_STATES],
            )
        return "\n".join(out) + "\n"


METRICS = Metrics()
