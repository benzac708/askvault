# AskVault Security

## Container hardening

The container runs as UID 10001, non-root, with a read-only root filesystem,
no writable path outside the SQLite data directory, all capabilities dropped,
and a seccomp profile. The pod sets allow-privilege-escalation false and uses
fsGroup so the non-root user can write its own database volume.

## Secret handling

The image pull credential for GHCR is a plain Kubernetes Secret, documented
as the accepted trade-off on a single-node drill rather than hidden. The
model provider key is a Kubernetes Secret injected out of band. Neither is
committed to any repository, and a secret scan runs in CI over every commit.

## Edge surface

The public surface is exactly two Traefik paths. Health, readiness and
metrics endpoints are reachable only in-cluster. There is no public ingress
to the Kubernetes API or to Argo CD; administration is over Tailscale.

## Scanning

The CI image job runs gitleaks over the full history, hadolint on the
Dockerfile, and a Trivy scan with a gate on fixable critical findings.
Unfixed findings inherited from the base image are documented rather than
silenced.