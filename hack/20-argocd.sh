#!/usr/bin/env bash
# 20-argocd.sh — install Argo CD, then install the argocd CLI.
#
# WHY --server-side --force-conflicts IS MANDATORY HERE:
#
#   Argo CD's install.yaml contains CRDs whose schemas exceed the 262144-byte
#   limit on the `kubectl apply` client-side annotation. A plain `kubectl apply`
#   fails with "metadata.annotations: Too long". This is not a formatting
#   preference; without the flag the install simply does not complete.
#
#   The first drill attempt lost time to this because the error message points
#   at the annotation, not at the size limit. Finding 4.
#
# WHY THE CLI IS DOWNLOADED AS A BINARY RATHER THAN `curl | sh`:
#   same supply-chain reason as 10-k3s.sh, and the version must match the
#   server or `argocd app` subcommands report confusing gRPC errors.
#
# IDEMPOTENT: re-applying install.yaml of the same version is a no-op.

set -euo pipefail

readonly ARGOCD_VERSION="v3.5.3"
readonly ARGOCD_NS="argocd"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
ok()   { printf '  ok   %s\n' "$1"; }
note() { printf '  ..   %s\n' "$1"; }

export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

printf '== argocd ==\n'

# --- 1. the argocd CLI binary -------------------------------------------------
if command -v argocd >/dev/null 2>&1; then
  note "argocd CLI present: $(argocd version --client --short 2>/dev/null || echo unknown)"
else
  note "installing argocd CLI $ARGOCD_VERSION"
  curl -fsSL -o /tmp/argocd \
    "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-arm64"
  install -m 0755 /tmp/argocd "$HOME/.local/bin/argocd"
  rm -f /tmp/argocd
fi
command -v argocd >/dev/null 2>&1 || fail "argocd CLI not on PATH after install"
ok "argocd CLI: $(argocd version --client --short 2>/dev/null || echo present)"

# --- 2. the server ------------------------------------------------------------
if sudo -E k3s kubectl get ns "$ARGOCD_NS" >/dev/null 2>&1; then
  note "namespace $ARGOCD_NS already exists"
else
  sudo -E k3s kubectl create namespace "$ARGOCD_NS"
fi

note "applying Argo CD $ARGOCD_VERSION (server-side; see the header for why)"
curl -fsSL -o /tmp/argocd-install.yaml \
  "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
sudo -E k3s kubectl apply -n "$ARGOCD_NS" --server-side --force-conflicts \
  -f /tmp/argocd-install.yaml
rm -f /tmp/argocd-install.yaml

note "waiting for argocd-server (up to 300s)"
sudo -E k3s kubectl -n "$ARGOCD_NS" rollout status deploy/argocd-server --timeout=300s

# --- 3. assert it is really up, not merely applied ---------------------------
# An Application CRD that exists but whose controller is crash-looping produces
# a cluster that looks installed and reconciles nothing. Check the controller.
for d in argocd-server argocd-repo-server argocd-applicationset-controller; do
  ready="$(sudo -E k3s kubectl -n "$ARGOCD_NS" get deploy "$d" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
  [ "${ready:-0}" -ge 1 ] || fail "$d has no ready replica"
  ok "$d ready"
done

sudo -E k3s kubectl -n "$ARGOCD_NS" get pods

# --- 4. the CRDs the later scripts depend on ---------------------------------
for crd in applications.argoproj.io appprojects.argoproj.io; do
  sudo -E k3s kubectl get crd "$crd" >/dev/null 2>&1 \
    || fail "CRD $crd missing — 50-gitops.sh cannot work"
  ok "CRD $crd present"
done

printf '\nARGOCD OK — the initial admin password is in secret argocd-initial-admin-secret\n'
printf 'Retrieve it with:\n'
printf "  sudo -E k3s kubectl -n %s get secret argocd-initial-admin-secret \\\\\n" "$ARGOCD_NS"
printf "    -o jsonpath='{.data.password}' | base64 -d\n"
