# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The venv lives at `.venv/`; on Windows invoke it as `.venv/Scripts/python.exe -m ...`.

```bash
pip install -e ".[dev]"                        # install (adds pytest, ruff, fpdf2)

python -m src.ingestion.pipeline               # incremental ingest → vector index
python -m src.ingestion.pipeline --full-refresh  # re-embed everything
python -m src.ingestion.pipeline --no-prune    # keep docs that vanished from source

uvicorn src.api.main:app --reload --port 8000  # API (docs at /docs, health at /health)
python -m src.eval.harness --baseline          # score agent vs. plain RAG on golden set
python -m src.eval.harness --fail-under 0.8    # exit non-zero if fact recall drops

pytest -q                                      # full suite (~12s, no network needed)
pytest tests/test_retriever.py::test_distant_results_are_dropped   # single test
ruff check .                                   # lint (line-length 100, target py311)
```

Console entry points `ingest`, `serve`, `evaluate` map to the same three mains.

Ingestion and `/ask` need `OPENAI_API_KEY` in `.env` (copy from `.env.example`). The test
suite does not — backends and embeddings are stubbed. OpenSearch tests skip themselves
when nothing is listening; start a cluster with
`docker compose -f infra/docker-compose.yml up -d opensearch`.

## Architecture

Four sources (Jira, Slack, DevPortal, City Map — all backed by sample files under
`data/sample/`) flow through one pipeline into one shared vector index, and a
three-node LangGraph agent answers over it.

**Ingest:** `connectors → Document → content hash → chunk → embed → VectorStore`.
Every stage in [pipeline.py](src/ingestion/pipeline.py) is a generator, so peak memory is
set by `embed_batch_size`, not corpus size.

**Query:** `route → retrieve → synthesize` ([graph.py](src/agent/graph.py),
[nodes.py](src/agent/nodes.py)). The router is an LLM call returning JSON that picks 1–3
sources plus optional sub-queries; sub-queries retrieve concurrently in a thread pool and
are deduped in sub-query order; synthesis answers from evidence only and its citation list
is filtered down to the `[n]` markers the answer actually used.

### Invariants worth preserving

- **`SearchHit.score` is cosine similarity in every backend, higher is better.** FAISS
  returns inner product over normalized vectors, OpenSearch returns its own scaled score
  (`_cosine_from_score` converts), and BM25-only hits get cosine recomputed against the
  stored embedding rather than carrying an unbounded BM25 score past the cutoff. Every
  relevance decision downstream assumes this. `l2_to_cosine` in
  [base.py](src/vectorstore/base.py) exists for backends that report distance.
- **Backends absorb their own limitations; the retriever holds only policy.**
  [retriever.py](src/retrieval/retriever.py) embeds the query, asks for the routed sources,
  and drops hits below `min_similarity` — nothing else. FAISS's lack of metadata filtering
  is handled inside [faiss_store.py](src/vectorstore/faiss_store.py) with an *escalating*
  over-fetch window (×3 per round); OpenSearch filters natively and needs none of it. Do
  not push a store-specific workaround back up into the retriever.
- **The relevance cutoff is what makes "I don't know" possible.** A vector index always
  returns k neighbours however distant. `min_similarity` (default 0.28; on-topic questions
  score 0.69–0.85, off-topic ≤ 0.09) filters them out, and `synthesize_answer`
  short-circuits to a refusal with zero citations before spending an LLM call.
  `retrieve(min_similarity=...)` uses `-2.0` as the "use the default" sentinel because it
  sits below the cosine range; `None` disables the cutoff.
- **Chunk ids are deterministic (`doc_id#position`)**, so re-ingesting a document
  overwrites its chunks. A *shrunk* document still leaves orphans, which is why an update
  issues `delete_docs` before upserting.
- **The manifest is the whole incremental/resumable story.**
  [manifest.py](src/ingestion/manifest.py) (SQLite, keyed by content hash) makes unchanged
  documents free, makes a killed run resume where it stopped, and makes vanished documents
  prunable. Pruning is only safe after a complete pass — a partial run must pass
  `prune=False`.
- **Not everything deserves an embedding.** [city_map.py](src/connectors/city_map.py)
  treats a sheet by what it is: small reference tables (≤ `csv_small_sheet_rows`) become
  one document per row; large tables become a single *profile* document (schema, row
  count, per-column stats, samples); large tables with a prose column get the profile plus
  rows up to `csv_max_text_rows`. Prose detection is a heuristic on average cell length.
  This is what kept the sample corpus at ~4.6k documents instead of ~1.15M.
- **Failures are isolated in extraction, fatal in embedding.** A corrupt file is logged to
  `connector.failures` and skipped; a persistently failing embedding API raises, because a
  half-indexed corpus reporting success is worse than a stopped job the manifest makes
  cheap to retry.
- **Chunking is source-aware.** Slack/Jira/City Map documents under `chunk_size` are kept
  whole (`KEEP_WHOLE_SOURCES`) — splitting a five-message thread destroys the context that
  makes it retrievable. Long DevPortal docs split on Markdown headings first.
- **Tracing must never break the app.** [tracing.py](src/observability/tracing.py) is inert
  without Langfuse keys and swallows its own errors. LangChain instruments the LLM calls;
  `record_retrieval` exists because retrieval is our code and is the part you need when an
  answer is wrong. Tracing context does not cross thread boundaries, so retrieval is
  recorded on the main thread after the pool joins.

### Adding things

- **A source:** implement `iter_documents()` on `BaseConnector`, add the enum value to
  `SourceType`, register it in `build_connectors()`, and add it to the router prompt's
  source list in `route_sources` — `VALID_SOURCES` is derived from the enum, so an
  unregistered source is silently dropped from routing output.
- **A file format:** write an `Extracted`-returning function in
  [extractors.py](src/ingestion/extractors.py) and `register_extractor(suffix, fn)`.
- **A vector backend:** implement the `VectorStore` protocol and branch in
  [factory.py](src/vectorstore/factory.py). Nothing outside that module knows the backend.

### Evaluation

[harness.py](src/eval/harness.py) scores routing and grounding separately. Routing is
recall/precision over the expected source set, deliberately *not* exact match — a router
that over-selects still surfaces every fact. Grounding normalizes both answer and golden
fact (casefold, collapse non-alphanumerics) so `alex.chen` matches "Alex Chen". Golden set:
[data/eval/golden.json](data/eval/golden.json).

### Docs

[docs/walkthrough/](docs/walkthrough/README.md) is a chapter-by-chapter reading of
the code in data-flow order, written for someone learning the project. Chapter 11
lists the defensible decisions and the known gaps.

[HOW_IT_WORKS.md](HOW_IT_WORKS.md) is the long-form design narrative and is the best
context for *why* decisions were made, but parts of it lag the code — it still describes
L2 distances and a `relevance_threshold` of 1.2, and points at a `tests/test_retrieval.py`
that is now [tests/test_retriever.py](tests/test_retriever.py). Trust the code on scoring.
