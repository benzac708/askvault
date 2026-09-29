# AskVault

**Live:** https://askvault.zachara.dev

A retrieval-augmented Q&A service for internal documentation, running on
Kubernetes on a single ARM VPS. Ask it a question and it answers from the
document corpus with citations, or declines when the corpus does not cover the
question.

It is small on purpose. The interesting part is not the application - it is the
operational layer around it: GitOps reconciliation, an immutable image
pipeline, health probes that mean something, and a rebuild that is scripted
rather than remembered.

---

## What is actually being demonstrated

Read this before the architecture, because the framing matters and overstating
it would be the easiest thing to do here.

**The operations are platform-grade. The architecture is not a platform.**
There is one service, one replica, and a lexical retriever with no embeddings
and no vector store. It is a RAG service running under a full internal-platform
operational layer - not a distributed system.

What that layer does contain, and what it is worth looking at:

- **GitOps, for real.** Argo CD reconciles the cluster from a separate
  repository. Drift is corrected without being asked: scaling the deployment to
  three replicas by hand results in Argo reverting it to one, unaided.
- **An immutable promotion path.** CI publishes `sha-<commit>` and `main`.
  Production runs a pinned SHA. Promotion is a one-line commit, and a rollback
  is the same line pointing at the previous tag. No rebuild, no registry
  round-trip, and the running artifact is always traceable to a commit.
- **Probes that assert different things.** `/healthz` is liveness, `/readyz`
  checks the retrieval index. They are separate because one endpoint meaning
  two things is how a probe ends up asserting the wrong condition.
- **An honest retrieval design.** See "Retrieval is lexical, and that is a
  decision" below.

---

## Architecture

```
        ┌──────────────┐
        │  Cloudflare  │  askvault.zachara.dev
        └──────┬───────┘
               │ cloudflared tunnel (host)
               ▼
        ┌──────────────┐
        │    Caddy     │  sole owner of host :80 / :443
        └──────┬───────┘
               │ reverse_proxy → 127.0.0.1:30080
               ▼
┌──────────────────────────────────────────────────────────┐
│  k3s  (single ARM node)                                  │
│                                                          │
│  ┌─ namespace: traefik ──────────┐                       │
│  │  Traefik 3.7  (NodePort       │                       │
│  │  30080 / 30443)               │                       │
│  └───────────┬───────────────────┘                       │
│              │ Ingress: / and /chat only                 │
│              ▼                                           │
│  ┌─ namespace: askvault-prod ────┐                       │
│  │  Deployment (1 replica)       │                       │
│  │  ├ /healthz  liveness         │                       │
│  │  ├ /readyz   readiness        │                       │
│  │  ├ /metrics  OpenMetrics      │                       │
│  │  ├ POST /chat                 │                       │
│  │  └ GET|POST /  (server-       │                       │
│  │       rendered HTML, no build)│                       │
│  └───────────────┬───────────────┘                       │
│                  │                                       │
│  ┌─ namespace: argocd ───────────┐                       │
│  │  Argo CD watches the gitops   │                       │
│  │  repo and reconciles the      │                       │
│  │  askvault-prod/-dev overlays  │                       │
│  └───────────────────────────────┘                       │
└──────────────────────────────────────────────────────────┘

  ┌──────────── GitHub ────────────┐
  │  askvault         app + CI     │
  │  askvault-gitops  manifests    │
  │  ghcr.io          image        │
  └────────────────────────────────┘
```

Two repositories, deliberately:

| Repository | Contains | Why separate |
|---|---|---|
| `askvault` | Application code, tests, Dockerfile, CI workflow | Written by developers, changes often |
| `askvault-gitops` | Kustomize base, overlays, Argo CD objects | Read by a cluster at elevated privilege, changes rarely |

The split is a least-privilege boundary. The credential Argo CD holds is a
**read-only deploy key** scoped to the gitops repository alone. There is no
long-lived token that can write to the application repository or to the
cluster. A monorepo would be simpler; the split is here because the thing that
has cluster access should not also be the thing a developer pushes to.

---

## How a request is answered

```
question
   │
   ▼
SQLite FTS5 (BM25)  ──►  top-k passages  ──►  LLM with numbered passages
   │                                              │
   │                                              ▼
   └── citations are built from the PASSAGES, not from the model's output
```

That last line is the design decision worth explaining. The prompt numbers the
passages `[1]`, `[2]` so the model *can* cite them - but the `citations` field
in the response is constructed from the `Passage` objects that were retrieved.
If the model ignores the markers, hallucinates, or returns prose with no
numbers at all, **the citations are still correct.** A RAG layer whose citations
depend on the model cooperating lies under load. There is a test that asserts
this using a deliberately rude stub model that returns garbage.

### Retrieval is lexical, and that is a decision

Retrieval is SQLite FTS5 with BM25 ranking. No embeddings, no vector database.

The honest consequence: the query builder ORs every token, so **any** word
landing anywhere in the corpus returns passages. "How do I price a banana"
returns three passages, because `how`, `do`, `a` and `i` all appear in the
corpus. There is no relevance floor.

This is the deliberate price of zero infrastructure. High recall, no
precision. Two things follow, and both are load-bearing:

1. The `NO_CONTEXT` branch is a cheap pre-filter for out-of-vocabulary
   questions - **not** the guard against unsupported answers.
2. Rejecting an unsupported question is the **model's** job, via the system
   prompt. Verified: asked about a refund window that does not exist in the
   corpus, it answers "The provided context does not contain information about
   a refund window."

This is asserted in the test suite as a documented property, so it reads as a
decision rather than an oversight. Closing it properly means vector retrieval,
which means an embedding model and a vector store on a 23 GB VPS - deferred
deliberately, not forgotten.

---

## Configuration

Provider, model and base URL are **configuration**, so they live in a
`ConfigMap`. The API key is a **credential**, so it lives in a `Secret`. The
split is the point: nothing that rotates on a schedule should require editing a
manifest that is reviewed in a pull request.

| Setting | Where | Value in `prod` |
|---|---|---|
| `LLM_PROVIDER` | ConfigMap | `openrouter` |
| `LLM_MODEL` | ConfigMap | `google/gemini-2.5-flash-lite` |
| `LLM_BASE_URL` | ConfigMap | `https://openrouter.ai/api/v1` |
| `LLM_API_KEY` | Secret | injected out of band, never in Git |

The model id is pinned rather than set to a floating free tier. `openrouter/free`
is a rotating pool whose behaviour changes without notice, and an unpinned free
model in a portfolio project is indistinguishable from an outage. The id was
read off OpenRouter's live `/api/v1/models` rather than guessed, because a
guessed id fails at runtime with a 404 that also looks like an outage.

Rate limits are set below the provider's own ceilings, so the application
refuses before the provider does: **10 req/min per IP, 15 req/min globally, 40
req/day globally.** At the pinned model's pricing that ceiling costs under two
US cents a day if fully consumed.

See [`.env.example`](.env.example) for the full setting list.

---

## Running it locally

No cluster required. The default provider is `mock`, so the app runs and the
test suite passes with no API key.

```bash
uv sync
uv run pytest -q          # 96 passed
uv run uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 and ask something the corpus covers:

- "Who can approve access to production systems?"
- "What should I do first during an incident?"

The corpus is the `samples/` directory: an onboarding guide, an access-control
policy, an incident-response runbook, and an IT FAQ.

---

## Known limitations

Everything here is a deliberate trade-off or an open problem, stated because a
reviewer will find them anyway.

### Security

The container is the hardened part, and the secret handling is the weak part.

- **Runs as UID 10001, non-root, with `readOnlyRootFilesystem: true` and all
  capabilities dropped.** The only writable path is an `emptyDir` at `/data`,
  which holds the SQLite index.
- **`ghcr-pull` is a plain Kubernetes `Secret`.** In production this would be
  **Sealed Secrets** or the **External Secrets Operator**, so the secret is
  encrypted in Git or sourced from a KMS rather than existing as cluster state.
  It is not used here because adding a KMS dependency to a single-VPS drill
  with one credential would be ceremony, not security - and the honest version
  of that trade-off is to name it rather than to imply it was not considered.
- **The image reports vulnerabilities inherited from the Debian base, and the
  numbers are not small: 5 CRITICAL, 55 HIGH, 104 MEDIUM, 102 LOW, plus 5
  UNKNOWN.** CI gates on **fixable CRITICAL only**, and that count is
  **0** - which is why the build passes and why that gate is the honest one:
  gating on unfixed upstream base-image findings would block every build
  forever and teach everyone to bypass the gate.

  The two OpenSSL MEDIUMs worth naming are `CVE-2026-63072` and
  `CVE-2026-63076`, both fixed in `3.0.22-1~deb12u1`. The base image is pinned
  to `python:3.12-slim-bookworm`; see "Deferred" below for why the bump is
  separated from the rest of this work rather than folded into it.

  5 CRITICAL is a number that deserves to be looked at rather than rounded off.
  They are unfixed base-image findings with no available patch, which is why
  the gate ignores them - but "no patch available" is a different claim from
  "not a problem", and a scan that is suspiciously clean would be its own
  signal.
- **The cluster API is not exposed.** There is no public ingress to Argo CD or
  to the Kubernetes API; administration is over Tailscale.

### Reproducibility

Three things are specific to this deployment and cannot be recreated by a third
party:

1. The `prod-zachara-tunnel` cloudflared tunnel is a named object on a
   Cloudflare account.
2. The `askvault.zachara.dev` DNS record is Cloudflare-side.
3. **The image is `linux/arm64` only.** It will not pull on an x86 machine -
   you get a manifest mismatch, not a slow build. CI builds with
   `runs-on: ubuntu-24.04-arm` to match the deployment target, which is the
   correct default here and the wrong one for a general audience.

### Deferred, with reasons

- **Vector retrieval.** Needs an embedding model and a vector store; ~1 GB of
  memory for marginal gain over a four-document corpus.
- **OpenSSL base bump.** Deferred rather than blocked: the fixes exist in a
  later Debian revision, and the bump is a one-line change reviewed on its own
  merits rather than buried in another commit.
- **Multi-replica.** The rate limiter is in-process, so a second replica would
  multiply the global ceiling by the replica count. Scaling horizontally
  requires moving the counter to Redis, which is the correct fix and is not
  worth a Redis instance at one replica.

---

## Open problem: the kubelet anonymous-pull 403

Stated plainly because it is unresolved and because the resolution path is
interesting.

**Symptom.** A pod with no `imagePullSecret` will not start on a cold node,
even though the same image pulls fine from a shell on the same host.

**Fix applied, and verified.** An `imagePullSecret` is set on the pod. Proven
by A/B test rather than assertion: two identical pods at
`imagePullPolicy: Always` - the one with the secret reached `Succeeded`, the one
without reached `ErrImagePull`.

**But the fix is a workaround, not a root cause,** and three candidate causes
were tested and disproven:

| Hypothesis | Result |
|---|---|
| The package is private | `ghcr.io` package visibility is `public`; anonymous token URL returns 200 |
| Anonymous pull is rate-limited | 105/105 anonymous token requests returned 200, with no rate-limit headers present |
| IPv6 vs IPv4 divergence | `ghcr.io` resolves to a single A record; `-6` has no route, so containerd cannot be choosing differently |

So: a public package, no rate limiting, one address family, one host - and the
kubelet's own token request still returns `403 Forbidden` while the identical
request from a shell returns `200`. Whatever decides this is server-side and
not yet characterised. The elimination is recorded here because the next person
to meet this will reach for the same plausible causes.

---

## Repository layout

```
askvault/
├── app/
│   ├── main.py                 FastAPI app: routes, lifespan, middleware
│   ├── core/
│   │   ├── config.py           pydantic-settings, 16 fields
│   │   ├── limits.py           rate limiter, three ceilings
│   │   └── logging.py          stdlib JSON log formatter
│   ├── observability/metrics.py
│   ├── services/
│   │   ├── answer.py           retrieval + prompt assembly
│   │   ├── llm/                provider seam: mock | openai_compat
│   │   └── retrieval/          SQLite FTS5 store and models
│   └── web/template.html       one server-rendered page, no build step
├── samples/                    the document corpus
├── tests/                      96 tests
├── Dockerfile                  two-stage, uv builder → slim runtime
└── .github/workflows/ci.yml    verify → image → publish

askvault-gitops/
├── base/                       deployment, service, ingress, configmap,
│                               servicemonitor, kustomization
├── overlays/
│   ├── dev/                    rolling :main tag
│   └── prod/                   pinned sha, public hostname
└── argo/                       AppProject + both Applications
```

The environment difference between `dev` and `prod` is namespace, environment
label, the three LLM config keys, the rate ceilings, and the hostname - and
nothing else. That constraint is enforced by review, not by a tool, which is
worth knowing before trusting it.

---

## The rebuild

The full teardown-and-rebuild procedure is scripted in `hack/`, numbered so the
sort order is the dependency order:

```bash
hack/00-preflight.sh      # verify host facts, enumerate occupied ports
hack/10-k3s.sh            # install k3s, Traefik disabled
hack/20-argocd.sh         # install Argo CD
hack/30-traefik.sh        # Traefik as NodePort
hack/40-cloudflare.sh     # tunnel routes
hack/50-gitops.sh         # apply the Argo objects
hack/51-grafana-link.sh   # re-point the host Grafana at the new Prometheus ClusterIP
hack/90-teardown.sh       # delete in the correct order
hack/99-acceptance.sh     # the drill asserts
```

Bootstrap is **bash rather than Terraform**, deliberately. This is a
single-VPS drill and GitOps owns everything after bootstrap; the scripts exist
so that the rebuild is reproducible, not because bash is the right answer at
scale. The scripts are idempotent and each declares its prerequisite.

The teardown order is load-bearing and is the part worth reading: Argo CD
`Application` objects are deleted **before** the `argocd` namespace, because an
`ImageUpdater` finalizer otherwise deadlocks the namespace deletion. That is
the kind of thing that only exists after breaking it once.

---

## Licence

MIT - see [LICENSE](LICENSE).
