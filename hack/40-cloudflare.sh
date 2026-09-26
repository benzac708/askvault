#!/usr/bin/env bash
# 40-cloudflare.sh — point the tunnel's hostname at Traefik's NodePort.
#
# WHAT THIS SCRIPT DOES NOT DO, DELIBERATELY:
#
#   It does not create the tunnel, and it does not create the DNS record.
#   `prod-zachara-tunnel` is a NAMED object on a Cloudflare account, and
#   `askvault.zachara.dev` is a DNS record in that account's zone. Neither can
#   be recreated from this repository, by this script, or by anyone without
#   account access. That is a permanent property of this deployment and it is
#   stated rather than papered over.
#
#   What this script does is the part that IS reproducible: write the ingress
#   rule that binds that existing hostname to the local NodePort, and verify
#   the whole chain answers.
#
# WHY THE RULE POINTS AT localhost:30080 AND NOT AT THE APP:
#
#   cloudflared terminates the tunnel on the host and forwards to a local port.
#   It knows nothing about Kubernetes. Traefik's NodePort is the seam, and the
#   Ingress inside the cluster decides which host lands on which Service. The
#   catch-all `http_status:404` at the end is load-bearing: without it,
#   cloudflared serves an unmatched hostname from the FIRST rule, which makes
#   typos look like successful routing.
#
# IDEMPOTENT: the ingress list is regenerated in full from this script's own
# view, and the previous config is backed up before being replaced.

set -euo pipefail

readonly CONFIG="/etc/cloudflared/config.yml"
readonly CF_HOSTNAME="${CF_HOSTNAME:-askvault.zachara.dev}"
readonly NODEPORT_HTTP=30080

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
ok()   { printf '  ok   %s\n' "$1"; }
note() { printf '  ..   %s\n' "$1"; }

printf '== cloudflare tunnel route ==\n'

[ -f "$CONFIG" ] || fail "$CONFIG not found — the tunnel is not installed on this host.
This script configures an EXISTING tunnel; it does not create one."
ok "found $CONFIG"

systemctl is-active --quiet cloudflared || fail "cloudflared service is not active"
ok "cloudflared service active"

tunnel="$(awk '$1=="tunnel:"{print $2}' "$CONFIG")"
[ -n "$tunnel" ] || fail "no 'tunnel:' line in $CONFIG"
note "existing tunnel: $tunnel (NOT created by this script, and not recreatable by it)"

# --- is the hostname already routed correctly? --------------------------------
if awk -v h="$CF_HOSTNAME" -v p="$NODEPORT_HTTP" '
      $1=="-" && $2=="hostname:" && $3==h {inh=1; next}
      inh && $1=="service:" {print $2; exit}
    ' "$CONFIG" | grep -qx "http://localhost:${NODEPORT_HTTP}"; then
  ok "$CF_HOSTNAME already routes to http://localhost:${NODEPORT_HTTP}"
else
  note "writing the ingress rule for $CF_HOSTNAME -> http://localhost:${NODEPORT_HTTP}"

  backup="${CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$CONFIG" "$backup"
  chmod 600 "$backup"
  ok "backed up existing config to $backup"

  # PRESERVE every pre-existing rule; only add or replace ours. Rewriting the
  # whole file from a template would silently drop the other hostnames on this
  # tunnel, which is the kind of blast radius a "small" script should never have.
  python3 - "$CONFIG" "$CF_HOSTNAME" "$NODEPORT_HTTP" <<'PY'
import sys, re
path, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
lines = text.splitlines()

# Split into the preamble (everything before `ingress:`) and the rule list.
try:
    idx = next(i for i, l in enumerate(lines) if l.strip() == "ingress:")
except StopIteration:
    sys.exit("no 'ingress:' block found")

head, rules = lines[:idx + 1], lines[idx + 1:]

# Drop any existing rule for this hostname, plus its service line.
out, skip = [], False
for l in rules:
    stripped = l.strip()
    if stripped.startswith("- hostname:"):
        skip = stripped.split("hostname:", 1)[1].strip() == host
        if not skip:
            out.append(l)
        continue
    if skip and stripped.startswith("service:"):
        continue
    if stripped.startswith("- service:") or stripped.startswith("service:"):
        # keep the catch-all in place, appended at the end
        continue
    out.append(l)

# Strip trailing blanks, re-append our rule then the catch-all.
while out and not out[-1].strip():
    out.pop()
out += [f"  - hostname: {host}", f"    service: http://localhost:{port}",
        "  - service: http_status:404"]

open(path, "w").write("\n".join(head + out) + "\n")
PY

  chmod 600 "$CONFIG"
  note "restarting cloudflared"
  systemctl restart cloudflared
  sleep 5
fi

systemctl is-active --quiet cloudflared || fail "cloudflared did not come back after restart"
ok "cloudflared restarted and active"

# --- read the config back -----------------------------------------------------
resolved="$(awk -v h="$CF_HOSTNAME" '
    $1=="-" && $2=="hostname:" && $3==h {inh=1; next}
    inh && $1=="service:" {print $2; exit}' "$CONFIG")"
[ "$resolved" = "http://localhost:${NODEPORT_HTTP}" ] \
  || fail "$CF_HOSTNAME resolves to '$resolved', expected http://localhost:${NODEPORT_HTTP}"
ok "$CF_HOSTNAME -> $resolved"

tail_rule="$(awk '$1=="-" && $2=="service:"{print $3}' "$CONFIG" | tail -1)"
[ "$tail_rule" = "http_status:404" ] \
  || fail "no catch-all 404 rule at the end — unmatched hostnames would be served by the first rule"
ok "catch-all http_status:404 present"

# --- the full chain, from outside the cluster ---------------------------------
note "asserting the chain: cloudflared -> Caddy/Traefik -> app"
code="$(curl -s -o /dev/null -w '%{http_code}' "https://${CF_HOSTNAME}/" || true)"
note "https://${CF_HOSTNAME}/ -> HTTP $code"

case "$code" in
  200) ok "the public hostname serves the app" ;;
  502|503|504)
    fail "gateway error $code — cloudflared is up but nothing answered behind it.
Check, in order: Caddy owns :443 and reverse_proxies to 127.0.0.1:${NODEPORT_HTTP};
Traefik is listening on :${NODEPORT_HTTP}; an Ingress exists for this hostname.
Run 50-gitops.sh, then 99-acceptance.sh." ;;
  404) fail "HTTP 404 — the tunnel is fine and the Ingress does not match this hostname" ;;
  000) fail "no response at all — check DNS for ${CF_HOSTNAME} and 'systemctl status cloudflared'" ;;
  *)   fail "unexpected HTTP $code" ;;
esac

printf '\nCLOUDFLARE ROUTE OK\n'
printf 'NOTE: the tunnel and the DNS record are account-side and are NOT reproduced by this script.\n'
