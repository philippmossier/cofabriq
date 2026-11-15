# Embeddings

A batch of texts turned into vectors (OpenAI `text-embedding-3-small`), stored in pgvector.

## Why

Similar issues and documents end up close together as vectors, so "find the
earlier issues like this one" is a nearest-neighbour query in Postgres. It is
the cheap, broad first stage; an LLM rerank and a checked verdict come after
it (ADR-003).

## Code

`cofabriq.context.ingest.embed.openai_embedder`
