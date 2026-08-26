# AWS deployment scaffolding (Phase 6)

Target architecture matching the resume bullet:

```
S3 (raw docs)
  └─ event → Lambda (re-embed / re-index)
       └─ OpenSearch (vector index)

ECS Fargate (FastAPI agent)
  └─ Application Load Balancer
  └─ CloudWatch (latency, 5xx, ingestion lag)
```

## Suggested next steps

1. Create S3 bucket for raw connector dumps (`jira/`, `slack/`, `devportal/`, `city_map/`).
2. Swap `VECTOR_STORE=opensearch` and point at Amazon OpenSearch Service.
3. Package API image → ECR → ECS Fargate service behind an ALB.
4. Lambda triggered on `s3:ObjectCreated:*` runs `python -m src.ingestion.pipeline` (or a lighter upsert path).
5. CloudWatch dashboard: p95 `/ask` latency, error rate, docs lag (age of newest indexed doc).

Terraform/CDK modules are intentionally not checked in yet — add once account/region are chosen.
