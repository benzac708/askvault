# AskVault Retrieval

## How an answer is built

A question is run through SQLite FTS5 with BM25 ranking over the corpus
sections. The top passages are numbered and placed into a prompt; the model
is told to cite them. The citations field in the answer is constructed from
the retrieved passages, never parsed out of the model's reply, so citations
stay correct even when the model hallucinates or ignores the numbers.

## Lexical, deliberately

Retrieval is lexical only: no embeddings and no vector store. This means any
word that lands anywhere in the corpus returns passages, so recall is high
and precision is not. The system prompt is the guard that refuses questions
the corpus does not cover, and the NO_CONTEXT branch is a cheap pre-filter.
Vector retrieval is a deferred upgrade, not an accident.

## The corpus

The corpus is a set of markdown documents under the samples directory,
including documentation about AskVault itself. The index is rebuilt at
startup, so a released corpus change is shipped with the image.