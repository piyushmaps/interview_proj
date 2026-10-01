# Intuit Agentic RAG — project context and implementation handoff

Prepared September 25, 2026 from the planning conversation. This document transfers product intent, decisions, findings, and unresolved questions to Codex in VS Code.

## Start here

The user wants to evolve an existing internal engineering knowledge assistant into a production service. They first wanted to understand how it should be built from scratch, then worked through requirements conversationally. They are learning architecture and prefer plain-language explanations with concrete examples. Keep ordinary responses concise; avoid repeatedly asking them to decide technical details they have already delegated to the assistant.

**Project:** `C:\Projects\intuit_agentic_rag` (saved Codex project label: `intuit_agentic_rag`). It is a Git repository. The original voice phrase “agentic React” referred to this project; inspected backend code is Python, not evidence of an existing React frontend.

**Current next milestone:** create a small, measurable pilot using four public Backstage documentation pages. Prepare ten questions with expected answers and source links, ingest those pages, and evaluate accuracy, citations, streaming, and the five-second completion target.

**Nothing was implemented, ingested, deployed, or tested during this discussion.** Repository code was inspected read-only. This handoff is a planning deliverable, not an instruction to deploy or spend unrestricted money. Read current repository instructions and inspect the current checkout before implementing; observations below may be stale.

## 1. Product and scope

- Internal assistant for approximately **20,000 Intuit employees**.
- The user initially said external customers, then explicitly corrected this: **one internal company service**, not a multi-customer SaaS product. Do not revive the external-customer architecture.
- Latest participation assumption: **50% participate**, meaning **10,000 active employees**, each asking **4–5 questions per week**.
- Approximately 40,000–50,000 questions/week, or 173,000–217,000/month using 52 weeks/12 months.
- Approximately 0.28–0.35 questions/second averaged across five eight-hour workdays. This does not establish peak traffic or model/search call volume.
- Expected corpus: potentially **one billion chunks** from Slack, Jira, wiki/portal documentation, plus existing ownership/capability data concepts.
- The corpus size is an unverified design target. The user does not possess the real dataset yet. Do not block useful pilot work on obtaining it, or claim that capacity is proven.
- Billions of chunks and billions of tokens are different units. Some later cost examples used one billion tokens only to explain embedding pricing.
- User-facing purpose: find relevant documents/conversations, summarize accurately, and supply direct supporting links.
- Initial assistant is read-only knowledge retrieval and answering. Writing tickets or sending messages was not requested.

## 2. Confirmed product decisions

### Answers and sources

1. Understand the employee’s question and retrieve relevant Slack messages, Jira issues, and portal documents.
2. Provide an accurate summarized answer and direct links to supporting sources.
3. If no supported answer exists, state that clearly. If related material exists, show it separately as **Related information**, not as proof of an answer.
4. If sources conflict, **present the newest relevant information first**, then describe older conflicting information with dates and links so the employee can judge.
5. Newer does not automatically mean correct. Missing or ambiguous dates must be acknowledged.
6. This freshness-first preference differs from the current synthesis code’s hard-coded authority preference (DevPortal over Slack; City Map for ownership). Reconcile implementation with this requirement rather than silently retaining the old rule.

### Freshness

- Begin processing **new and changed content as soon as detected**.
- Skip unchanged content. Do not periodically re-embed the entire corpus.
- “Immediate” means continuous incremental processing, not zero-latency search visibility.
- Maximum acceptable source-to-search lag remains undefined.
- Permission changes, deletion, and redaction changes need explicit handling even when text is unchanged.

### User experience

- Stream answer text progressively.
- Target **five seconds for the complete answer and citations**, not merely time to first token.
- If it takes longer, keep processing and show meaningful progress. Do not show a retry prompt merely because five seconds elapsed.
- Actual failures should not leave the interface pretending to process indefinitely. A temporary-unavailability message was selected as an engineering default; a hard request deadline is still undefined.
- The five-second target is unverified; no percentile or load-based acceptance criterion has been agreed.

### Access and sensitive information

- Only content eligible for **all Intuit employees** belongs in the searchable corpus. “Public” in the discussion meant company-wide, not internet-public.
- Employee sign-in remains part of the design.
- Ingestion is responsible for automatically checking eligibility and detecting sensitive material.
- User chose **redact sensitive portions and preserve the safe remainder**, then recheck before embedding/indexing.
- Summaries and citation snippets use sanitized content.
- Uncertain or misleading-after-redaction items stay out of search.
- Source eligibility and sensitivity are independent checks. Redacting a restricted source does not make it eligible for all employees.
- Automated screening is imperfect and must be evaluated. Do not promise perfect detection.
- Original source records remain unchanged. Source links must not leak sensitive values. Opening originals remains governed by source permissions; sanitized-copy linking behavior needs further design.

## 3. Sensitive-content defaults selected at the user’s request

These are project defaults, **not verified Intuit policy**. Align with the actual organization’s classification rules before production.

- Personal identifiers/contact information: individual names, personal email, phone, home address, government IDs, identifying employee/customer IDs.
- Secrets: passwords, API keys, tokens, private keys, secret-bearing connection strings or URLs.
- Customer financial/tax data: account/card details, tax records, income, balances, individual transactions.
- Private employee/HR information: compensation, reviews, disciplinary and personal records.
- Health/biometric information.
- Restricted business/security information not approved for all employees.

Preserve useful general instructions, service/team names, approved architecture descriptions, safe examples, and non-sensitive source links. Blanket name redaction may affect named-owner answers; team ownership can remain useful, but exceptions need actual policy, not assumptions.

Use a combination of deterministic secret detection, personal-data detection, and context-aware classification. Processing must occur in an approved environment; do not send raw sensitive content to unapproved external screening or embedding services or ordinary logs.

## 4. Memory and retention

The user asked for explanations and diagrams distinguishing these concepts:

- **Persistent knowledge:** stored sanitized documents, chunks, vectors, metadata, and links.
- **Session memory:** bounded earlier turns from the same employee’s current chat, used to understand follow-ups such as “Who owns it?”
- **Model context:** question + relevant chat turns + a small evidence set for one response. Never the whole corpus.
- **Agent workflow:** coordinates source selection, retrieval, and answering; it is not itself long-term memory.
- **Answer cache:** performance optimization, not chat memory or authoritative history.
- **Historical versions/backups:** ordinary storage for comparison and recovery, not personal memory.

Proposed session memory is not implemented in the inspected question flow. No long-term personal memory or saved cross-session chat history was selected for the initial release. Session expiration is unresolved. Follow-up questions must retrieve fresh evidence rather than treating previous generated answers as facts.

### Retention defaults selected by the assistant at the user’s request

| Data | Starting retention/behavior |
|---|---|
| Current approved searchable content | While source remains available, eligible, and within ingestion scope; do not delete useful knowledge solely for age |
| Separate older conflicting sources | Same eligibility rules, with dates and freshness labels |
| Superseded revisions of the same document | Sanitized text for 90 days outside the main vector index; controlled history comparison |
| Search snapshots | Hourly points for 14 days plus daily points for 30 days |
| Application/ingestion metadata backups | 30 days; target recoverable state no more than 15 minutes old |
| Unredacted staging | Delete after sanitization; 24-hour maximum for unresolved items |
| Diagnostic logs | 30 days, excluding full documents/questions/secrets by default |
| Access/admin audit records | 90 days, restricted; never searchable knowledge |
| Aggregate operational metrics | 13 months without question/document text |
| Saved conversations | Off initially; temporary session context only |

These affect app-owned copies, not source retention settings. Source deletion/restriction/sensitivity discovery overrides ordinary search retention. Invalidate affected caches. Reapply current exclusion/deletion/redaction state before serving a restored index. Backup expiry and any required earlier purge must be designed explicitly; backups are not a way to retain searchable deleted material.

## 5. Availability and recovery defaults

User confirmed 24/7 availability and delegated technical targets to the assistant:

- Initial **99.9% monthly availability** target, approximately 43 minutes downtime per 30-day month.
- Ordinary server failure: automatic failover, restoration within five minutes, preferably no visible interruption.
- Major infrastructure outage: target one-hour switch to a prepared healthy environment.
- Metadata recovery point: no more than 15 minutes old; replay source changes where recoverable.
- Continue using the last safe index during ingestion failure, indicate staleness when relevant, and resume durable work later.

Important qualifications:

- One hour may exceed the monthly downtime allowance and counts as a missed availability target.
- Restoring a corrupted billion-chunk index from historical snapshots is **not proven to fit one hour**. A separate restore target must be measured.
- Multi-zone replicas protect ordinary infrastructure failure; historical backups protect against mistakes/corruption that can propagate to replicas.
- Regional recovery requires more than copying vectors: identity, API, metadata, redaction/deletion state, source checkpoints, configuration, and model dependencies must work too.
- Reducing standby capacity was discussed as a possible cost saving, but **the user did not explicitly approve weakening recovery targets**. Budget was subsequently parked.

## 6. Existing repository — read-only observations

Paths below are relative to `C:\Projects\intuit_agentic_rag`. Inspected during September 19–20 discussion; recheck before relying on them.

### Existing architecture

- Python package with FastAPI, LangGraph, LangChain/OpenAI integration, FAISS, optional OpenSearch, and tracing/evaluation code.
- `src/models/document.py`: canonical Document (id, source, title, body, URL, author, updated time, arbitrary metadata).
- `src/connectors/`: Jira, Slack, DevPortal, City Map adapters; sample data exists. Production integration maturity was not fully audited.
- `src/ingestion/pipeline.py`: streaming/incremental orchestration; content hashing via SQLite manifest; batching, retries, updates, and pruning.
- `src/vectorstore/`: FAISS/OpenSearch abstraction and implementations.
- `src/agent/graph.py`: route -> retrieve -> synthesize; sync and async versions; streamed events.
- `src/agent/nodes.py`: LLM source routing, optional subqueries, concurrent retrieval, evidence synthesis and citation filtering.
- `src/api/main.py`: `/ask`, `/ask/stream`, health, cache-clear endpoints; async handling and reusable retriever.
- `src/api/cache.py`: in-process exact/semantic answer cache.
- `src/config.py`: defaults included `gpt-4o-mini`, `text-embedding-3-small`, FAISS, cache enabled, 15-minute cache TTL, chunk size 800 characters, overlap 120. These are code observations, not approved production sizing.
- `src/eval/`, `src/observability/`, and tests already exist. They were not run.
- `README.md`, `HOW_IT_WORKS.md`, `docs/walkthrough/` contain explanations. Some docs are inconsistent/outdated; prefer code for current behavior.
- `infra/Dockerfile`, `infra/docker-compose.yml`: container/local setup. Compose explicitly disables OpenSearch security for local development.
- `infra/aws/README.md`: AWS architecture notes; explicitly says Terraform/CDK modules were not checked in yet.

### Concrete concerns identified, not fixed

1. API code had no application authentication protecting questions or cache administration.
2. Retrieval state had no employee permission context. Final product scope is company-wide eligible content, which still requires enforced ingestion eligibility and sign-in.
3. Cache keys used questions/semantic similarity, without data or permission versions. Stale/excluded answers can survive indexing changes.
4. Updates deleted old chunks before writing replacements. Failed writes can leave a gap.
5. Manifest progress could commit before local FAISS persistence, creating a crash-recovery inconsistency.
6. Pruning requires a complete successful enumeration of the correct scope; incomplete connector results must not be treated as deletion.
7. Input had a minimum length but no maximum; model-generated subquery count was not bounded in the inspected flow.
8. Each request starts with a question; no cross-question session history/checkpointer was visible.
9. Synthesis authority rules differ from the user’s freshness-first preference.
10. Docker image did not declare a non-root user, and dependencies used lower bounds rather than a demonstrated reproducible lock.
11. Streaming errors exposed exception text in the inspected code.

This was not a comprehensive security, correctness, or production audit.

## 7. Proposed architecture and build order

Keep useful current components. Logical layers do not all need separate microservices.

**Ingestion:** approved sources -> eligibility/sensitivity checks -> sanitized versioned storage -> durable jobs -> chunk/embed workers -> versioned search publication.

**Questions:** signed-in employee -> session context -> bounded source/query planning -> eligible hybrid retrieval -> bounded reranking/evidence selection -> streamed answer and citations -> bounded session update.

**Supporting state:** document/version metadata, source checkpoints, job leases, exclusion/deletion records, evaluation data, diagnostics, snapshots.

Recommended sequence:

1. Define contracts for document identity, source revision, content hash, permission/eligibility version, redaction version, chunking/embedding versions, and job status.
2. Build one small end-to-end source integration with representative tests.
3. Make jobs repeatable, resumable, tolerant of duplicate/out-of-order events, and separately scalable from queries.
4. Publish new document versions safely; do not assume transactions across search and metadata stores.
5. Implement identity, sanitized evidence handling, source freshness/deletion rules, bounded requests, safe errors, and cache invalidation.
6. Add bounded session memory for follow-ups.
7. Evaluate retrieval and answer quality before expanding agent behavior.
8. Add automated deployment, monitoring, recovery exercises, and release gates.
9. Scale corpus in measured stages; benchmark compressed/disk/object-backed vector approaches before committing to large conventional clusters.

AWS was a reference direction inherited from the repo, not a fully finalized service selection. Candidate services discussed: ECS Fargate, S3, SQS, managed OpenSearch, a managed metadata database, optional shared session/cache store, Secrets Manager, and monitoring. Verify current capabilities, prices, compatibility, company policy, and requirements before final selections.

## 8. Cost discussion — preserve uncertainty

The user found detailed budget discussion too verbose and expensive, then said to leave it. **No budget was approved.**

Illustrative operating envelopes previously presented:

| Phase | Corpus | Monthly cloud/API allowance |
|---|---:|---:|
| Pilot | 1 million chunks | $1,000–$3,000 |
| Initial production | 10 million | $3,000–$8,000 |
| Scale qualification | 100 million | $12,000–$40,000 |
| Full target with standby | 1 billion | $40,000–$135,000 |

A simplified **$80,000/month** number was later given when asked for brevity. It was **not a calculated quote or proven requirement**. Do not treat it as a committed budget or necessary cost. Conventional replicated search and standby dominated those speculative envelopes. Paid screening/OCR, staffing, support, taxes, and major migration costs were excluded or unresolved. Object/disk-backed search may materially change costs.

Rates checked during the discussion, subject to re-verification:

- Embeddings: $0.02 per million tokens for `text-embedding-3-small`.
- At illustrative 500 tokens/chunk, one billion chunks = 500 billion tokens = $10,000 for one embedding pass only.
- One billion **tokens**, by contrast, costs $20 per embedding pass.
- Update scenario (not forecast): 0.1% of one billion chunks processed daily -> about $300/month embedding charge over 30 days; 1% -> $3,000/month.
- Answer-model scenario: 6,000 input and 700 output tokens across routing/answering per question, at $0.15/$0.60 per million respectively -> $0.00132/question.
- Latest participation gives about **$229–$286/month** for those answer-model calls only.
- Lower employee participation does not halve corpus storage or ingestion costs. It may reduce search compute needs, to be measured.
- Immediate vs hourly processing is not intrinsically a different per-token rate. Savings require fewer repeated edits, more efficient infrastructure, or an explicitly different provider pricing mode. Do not re-embed the full corpus on a schedule.

## 9. Public pilot corpus selected

The user has no portal sample data and asked to find some online. Four official Backstage pages were found and linked; **they have not been downloaded or ingested**:

1. Software Catalog: https://backstage.io/docs/features/software-catalog/
2. Descriptor Format: https://backstage.io/docs/features/software-catalog/descriptor-format/
3. Life of an Entity: https://backstage.io/docs/features/software-catalog/life-of-an-entity/
4. Creating and Publishing Documentation: https://backstage.io/docs/features/techdocs/creating-and-publishing/

These are public Backstage docs, not Intuit internal material. Preserve that provenance in ingestion and answers. Verify current contents and applicable reuse terms before importing. Keep canonical URLs, retrieval timestamps, and content hashes.

### Immediate pilot work

- Inspect current code and repository instructions.
- Read the four pages and prepare **ten questions with expected facts and supporting URLs**; no questions were authored yet.
- Include direct lookup, process explanation, cross-page synthesis, unanswerable/related-only behavior, and a follow-up requiring session context if that feature is implemented.
- Isolate pilot data/index from existing data; preserve the user’s work.
- Ingest and evaluate citations, answer accuracy, routing/retrieval, streaming, time to first text, and completion time.
- Report actual observed results, including failure cases. Do not claim five-second performance or production readiness from design alone.
- These pages cannot prove real-world sensitive-data screening, conflicting-history handling, or billion-scale behavior. Use separately labeled synthetic fixtures for those cases and representative approved data when available.

## 10. Open decisions and limitations

- Actual corpus size, bytes/tokens per chunk, daily changes, languages, attachment/OCR needs, and source API limits.
- Exact source-to-search freshness target and peak traffic.
- Five-second completion percentile and maximum request duration.
- Session expiration, session privacy, conversation-context screening, and whether saved history is later desired.
- Real Intuit data classification, approved processing providers, and identity integration.
- Source eligibility proof, redaction evaluation thresholds, and handling misleading partial documents.
- Linking to originals versus a sanitized viewer when originals contain removed information.
- Concrete recovery topology, measured restore times, costs, and budget approval.
- No final technology choice for large vector storage or metadata databases.
- No implementation, runtime tests, embeddings, deployments, or paid workloads were executed in this conversation.

## 11. Communication and continuation guidance

The user prefers short explanations, one question at a time, and examples instead of terminology. Diagrams helped explain memory. Provide detailed files when needed but summarize outcomes briefly in chat. Do not re-ask settled requirements. When making a technical choice, state its practical reason and distinguish defaults from confirmed company policy.

For a fresh VS Code session, use this file as context, then inspect the repository and continue the small Backstage pilot. Keep broader production requirements visible, but avoid expanding the first pilot into a complete production rebuild without a concrete execution request.

