# AskVault Deployment

## Where it runs

AskVault runs on a single-node k3s cluster on an Oracle Cloud Ampere ARM
virtual machine (four OCPUs, 24 GB RAM, free tier). The image is built
for linux/arm64 only, matching the node.

## The cluster objects

The application lives in the askvault-prod namespace (and askvault-dev for
the moving :main tag): a Deployment with one replica, a ClusterIP Service on
port 8000, and an Ingress. The Ingress is served by Traefik and exposes
exactly two exact paths: `/` and `/chat`.

## The public edge

A cloudflared tunnel terminates TLS at the Cloudflare edge and forwards
plain HTTP to a Caddy reverse proxy on the host, which forwards to the
Traefik NodePort on port 30080. The Kubernetes API and Argo CD are
admin-only over Tailscale; there is no public ingress to them.

## Probes

The Deployment defines a liveness probe on `/healthz` and a readiness probe
on `/readyz`. They assert different things: liveness is process health,
readiness is index state.