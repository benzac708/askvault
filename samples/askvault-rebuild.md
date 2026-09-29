# AskVault Rebuild and Teardown

## The drill

The whole node-and-app lifecycle is scripted in the askvault-gitops
repository under hack/. The scripts are numbered so sort order is dependency
order: preflight, k3s, argocd, traefik, monitoring, cloudflare, gitops,
teardown, acceptance. A single rebuild orchestrator drives the sequence and
prompts for the credentials it needs.

## Teardown order

Teardown order is load-bearing. Argo CD Application objects are deleted
before the argocd namespace, because an Application finalizer otherwise
deadlocks the namespace deletion. That lesson is baked into the teardown
script.

## Acceptance

The acceptance script runs live checks against the cluster, including that
the argocd CLI can report sync state. A stub test harness runs 52 assertions
with no cluster and no network, exercising refusal behaviour before any
destructive action would run.