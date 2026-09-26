"""Prometheus instrumentation, collected in one module on purpose.

Metric names and label sets are the contract between the app and the
Grafana dashboard in D35, and a contract split across call sites drifts. Every
metric is declared exactly once, here, so the dashboard can be written against
a known set.

Label cardinality is the failure mode to watch. `route` is a matched path
template, never a raw path; see `_route_template` in `app.main`. A public
endpoint that labels by raw path lets any scanner inflate the series count
without limit.
"""

from prometheus_client import Counter, Histogram

# Panels 1 and 2: traffic shape and latency.
REQUESTS = Counter(
    "askvault_requests_total",
    "HTTP requests by matched route, method and status code.",
    ["route", "method", "status"],
)

LATENCY = Histogram(
    "askvault_request_duration_seconds",
    "Request latency by matched route and method.",
    ["route", "method"],
    # Wide tail on purpose: the slowest thing this app does is a provider round
    # trip, and a bucket that stops at 1s would hide exactly the interesting part.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Panel 3: how much context each question actually retrieved.
PASSAGES = Histogram(
    "askvault_passages_considered",
    "Passages handed to the model per question. The honest measure of retrieval.",
    buckets=(0, 1, 2, 3, 5, 8, 10),
)

# Panel 4: provider health, split by outcome so 429 and 502 are separable.
LLM_CALLS = Counter(
    "askvault_llm_calls_total",
    "LLM calls by configured provider and outcome.",
    ["provider", "outcome"],
)

# Panel 5: abuse pressure, labelled by which ceiling did the rejecting. The
# label is the diagnostic: a spike in `per-ip` is one visitor, a spike in
# `daily` means the day's budget is gone and no code change will help.
RATE_LIMITED = Counter(
    "askvault_rate_limited_total",
    "Requests rejected by the in-app limiter, by the ceiling that rejected them.",
    ["scope"],
)
