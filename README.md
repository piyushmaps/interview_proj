# Intuit Assist — Agentic RAG Knowledge Assistant

ChatGPT-style agentic RAG over multi-source capability documentation:

| Source | Stand-in data | Purpose |
|--------|---------------|---------|
| **Jira** | Sample tickets | Capability requests, bugs, decisions |
| **Slack** | Sample threads | Tribal Q&A and how-tos |
| **DevPortal** | Markdown docs | Official capability creation guides |
| **City Map** | JSON graph | Owners, dependencies, capability topology |

The agent **routes** to the right sources, **decomposes** multi-part questions, **retrieves in parallel**, and **synthesizes** citation-backed answers.

## Roadmap phases

| Phase | Status | What |
|-------|--------|------|
| 0 | ✅ Scaffolded | Repo, schema, sample data |
| 1 | ✅ Scaffolded | Connectors + normalization |
| 2 | ✅ Scaffolded | Chunk → embed → FAISS index |
| 3 | ✅ Scaffolded | Baseline RAG |
| 4 | ✅ Scaffolded | LangGraph agent (router → retrieve → synthesize) |
| 5 | ✅ Implemented | Golden eval set + routing/grounding metrics |
| 6 | 🔜 Scaffolded | AWS (ECS, S3, Lambda, OpenSearch, CloudWatch) |
| 7 | 🔜 | Streaming, memory, scheduled sync |

## Quick start

```bash
# 1. Create venv & install
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"

# 2. Configure
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
# Add your OPENAI_API_KEY

# 3. Ingest sample data → build local FAISS index
python -m src.ingestion.pipeline

# 4. Run API
uvicorn src.api.main:app --reload --port 8000

# 5. Ask a question
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"How do I create a new capability?\"}"
```

Interactive docs: http://localhost:8000/docs

```bash
# 6. Score the agent against the golden set
python -m src.eval.harness --baseline
```

## Project layout

```
src/
  models/          # Shared Document schema
  connectors/      # Jira, Slack, DevPortal, City Map loaders
  ingestion/       # Chunk, embed, index pipeline
  retrieval/       # Vector retrieval with source filtering
  agent/           # LangGraph: router → retrieve → synthesize
  eval/            # Golden-set harness: routing + grounding metrics
  api/             # FastAPI chat endpoint
data/
  sample/          # Stand-in content for all 4 sources
  eval/            # Golden Q&A set (Phase 5)
infra/             # Docker + AWS scaffolding (Phase 6)
```

## Example questions (sample data)

- "How do I create a new capability?"
- "Who owns the Payments capability?"
- "What did the team discuss about capability onboarding in Slack?"
- "Are there open Jira tickets related to capability registration?"

## AWS deployment (Phase 6)

See `infra/` for Docker + Terraform stubs targeting:

- **ECS Fargate** + ALB — API service
- **S3** — raw document landing zone
- **Lambda** — event-driven re-ingestion on S3 put
- **OpenSearch** — production vector store
- **CloudWatch** — latency / error / ingestion-lag metrics
