# AskVault Architecture

## What AskVault is

AskVault is a retrieval-augmented question-answering service. You ask it a
question about its documentation corpus and it answers with citations, or it
refuses when the corpus does not cover the question. It is a FastAPI service
with one server-rendered HTML page, no frontend build step, and no external
requests to third-party scripts or fonts.

## The endpoints

The service exposes `/` (the web page and its form), `/chat` (the JSON
question endpoint), `/healthz` (liveness), `/readyz` (readiness, reports the
retrieval index state), and `/metrics` (Prometheus). Only `/` and `/chat` are
reachable from the public internet; the rest are reached in-cluster by kubelet
and Prometheus.

## Storage

The corpus is baked into the image and the index is derived at startup into a
writable SQLite database. There is no persistent storage requirement and no
external database.

## Rate limits

Rate limits are enforced inside the application process: ten requests per
minute per IP, fifteen per minute globally, and forty per day globally. They
sit below the model provider's own quota so the app refuses before the
provider does.