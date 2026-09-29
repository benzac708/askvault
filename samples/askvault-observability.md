# AskVault Observability

## Metrics

The service exposes OpenMetrics at /metrics. A ServiceMonitor in the
askvault-gitops repository selects the service and Prometheus scrapes it. The
metrics include request counters, latency and a count of passages considered
per answer.

## Logs

Logs are structured JSON, one object per line on stdout, so they can be
searched rather than merely read. The log level is configured via the
deployment environment.

## Monitoring stack

Prometheus runs from the kube-prometheus-stack chart, installed by the
rebuild scripts. It is instrumentation, not incident response: there is no
Grafana and no alerting rule configured. That is stated honestly rather than
implied away.