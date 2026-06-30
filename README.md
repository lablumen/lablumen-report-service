# lablumen-report-service

Handles everything related to lab reports — uploading PDFs, serving them securely to patients, and powering the AI chat feature so patients can ask questions about their results in plain English.

---

## Responsibilities

- **Report upload (staff only)** — accepts PDF uploads, stores them in a private KMS-encrypted S3 bucket, and creates a database record linking the report to the ordered test. Automatically marks an appointment as `Completed` when all ordered tests have reports.
- **Report listing** — patients see only their own reports; staff see all.
- **Presigned URL generation** — returns a short-lived (2-minute), SigV4-signed S3 URL so the browser fetches the PDF directly from S3. The service never proxies the file bytes.
- **AI chat (RAG)** — patients ask questions in natural language; the service retrieves the most relevant text chunks from their specific report and answers using Amazon Nova Lite in a lab-nurse persona.

---

## Tech Stack

| Component | Detail |
|---|---|
| Runtime | Python 3.12 |
| Framework | FastAPI (fully async) |
| ORM | SQLAlchemy 2.x (async) |
| Database driver | asyncpg |
| Validation | Pydantic v2 + python-multipart |
| Storage | AWS S3 (boto3 — PutObject + presigned URLs) |
| AI models | Amazon Titan Embed (embeddings), Amazon Nova Lite (chat) via Bedrock |
| Auth | Cognito JWT verification (PyJWT) |

---

## Source Layout

```
app/
  main.py      Entry point; registers routers
  auth.py      JWT verification and role enforcement
  bedrock.py   Titan Embed (question vectorization) and Nova Lite (chat) via Bedrock Converse API
  s3.py        PutObject upload and SigV4 presigned URL generation
  db.py        Async engine and session factory
  schemas.py   Pydantic request/response schemas
  config.py    Settings from environment variables
  routers/
    reports.py  Upload, list, view (presigned URL), and AI chat endpoints
    health.py   /healthz liveness probe
```

---

## API Endpoints

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/api/v1/reports/upload` | Staff | Upload a PDF (multipart form, links to a test mapping) |
| GET | `/api/v1/reports` | Patient, Staff | List reports (patients see own only) |
| GET | `/api/v1/reports/{id}/view` | Patient, Staff | Get a 2-minute presigned S3 URL for the PDF |
| POST | `/api/v1/reports/{id}/chat` | Patient | Ask a question about a report (RAG-powered) |
| GET | `/healthz` | Internal | Liveness probe |

---

## AI Chat — How It Works

1. The patient's question is embedded into a 1,536-dimension vector using Amazon Titan Embed.
2. The top 3 most semantically similar text chunks from that patient's report are retrieved from the `report_embeddings` table using pgvector cosine similarity (HNSW index).
3. The stored plain-English AI summary (`ai_layman_summary`) is prepended as full context.
4. Amazon Nova Lite receives the system prompt, context, conversation history, and question, then responds.
5. Answers are grounded in the actual content of the patient's report — not generic medical information.

Access control is enforced at every step: patients can only chat about reports that belong to their own appointments.

---

## Configuration

All values are injected by External Secrets Operator from AWS at pod startup.

| Variable | Source | Description |
|---|---|---|
| `DATABASE_URL` | Secrets Manager | PostgreSQL async connection string |
| `COGNITO_USER_POOL_ID` | SSM | Cognito pool ID |
| `COGNITO_APP_CLIENT_ID` | SSM | Cognito app client ID |
| `REPORTS_BUCKET` | SSM | S3 bucket name for report PDFs |
| `PRESIGNED_URL_TTL` | SSM | Presigned URL expiry in seconds |
| `BEDROCK_EMBED_MODEL` | SSM | `amazon.titan-embed-text-v1` |
| `BEDROCK_TEXT_MODEL` | SSM | `amazon.nova-lite-v1:0` |
| `AWS_REGION` | SSM | AWS region |
| `CORS_ORIGINS` | SSM | Allowed CORS origins |

---

## CI/CD

| Trigger | What Happens |
|---|---|
| Pull request | Lint (`ruff`), unit tests (`pytest`), SAST (SonarCloud), SCA (Snyk), container scan (Trivy) |
| Merge to `main` | Build image → Trivy gate → push to ECR → update `values-dev.yaml` → ArgoCD deploys to dev |
| GitHub Release | Retag ECR image SHA → semver → update `values-prod.yaml` → ArgoCD deploys to production |

CI/CD logic is centralized in `lablumen-shared`.
