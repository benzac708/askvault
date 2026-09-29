# AskVault GitOps

## Two repositories

AskVault is delivered from two repositories. The askvault repository holds
the application code, tests, Dockerfile and CI workflow. The askvault-gitops
repository holds the Kustomize base, overlays and Argo CD objects. The split
is a least-privilege boundary: Argo CD holds a read-only deploy key scoped to
the GitOps repository only.

## Promotion

CI publishes immutable sha-tagged images plus a moving :main tag to GHCR.
The dev overlay tracks :main so dev exercises whatever was just merged. The
prod overlay pins a specific sha in a one-line newTag edit; promotion is that
commit, and rollback is the same line pointing at the previous sha. No
rebuild is needed for a rollback.

## Drift correction

Argo CD reconciles with automated sync, prune and selfHeal. A hand edit to a
Deployment that disagrees with the repository is reverted by Argo, unaided.