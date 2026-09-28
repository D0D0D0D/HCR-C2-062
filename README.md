# HCR-C2-062 — GMP Manufacturing Deviation & CAPA Report Drafting Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: HCR

## Overview

GMP Manufacturing Deviation & CAPA Report Drafting Agent turns a newly reported manufacturing
deviation into a first draft of the investigation report and CAPA plan, grounded in what the site
has already recorded about similar deviations and how the corrective actions taken then actually
worked out.

It is for quality-assurance staff at a GMP manufacturing site: an equipment excursion, a process
deviation or an out-of-spec test result has been raised, and someone now has to write it up. The
agent retrieves comparable past deviations and their CAPA outcomes — was the action effective, did
the issue recur — and drafts the narrative, the root-cause hypothesis, the open questions and the
CAPA plan around them.

**It does not make a quality decision.** Disposition, release, and the judgement of whether a
deviation is acceptable are not produced — they belong to the site's quality-assurance
professional. That is a design constraint, not a missing feature.

Two properties make the drafting trustworthy enough to be a starting point:

- **Every cited past case is checked mechanically.** A drafted claim that references deviation
  `dev_xxx` or CAPA `capa_xxx` is verified against the records actually retrieved. The model
  cannot invent a precedent, because a citation that does not resolve does not survive the check.
- **The root-cause section is explicitly a hypothesis.** It is framed as 5 Whys / 5M1E reasoning
  with open questions attached, not as a determination.

Human review is not optional here. The first `/invoke` normally suspends with
`status: "awaiting_human"` and waits for a QA reviewer to approve, correct or reject the draft
through `/resume`. An authenticated caller **cannot turn that off**: the request model has no
`hitl_allowed` field, and a body flag by that name is silently dropped rather than honoured. Only
a deployment-level flag, used for the staging smoke test where no reviewer exists, can skip it.

The knowledge base is a derived search aid, not a system of record. Original files are not
retained, and personnel names and employee numbers are replaced with role labels — *"filling
process operator A"* — before anything is stored.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime, with a checkpointer — HITL is enabled and `memory_enabled: true`, so
  suspend/resume needs one;
- caller authentication in front of the entry adapter;
- optionally a `BaseLLM` client and a database, injected through the graph configuration.

Both optional dependencies degrade explicitly:

| Absent | Behaviour |
|---|---|
| LLM client | Only the deterministic, template-conformant draft is produced. The workflow still completes and the human review still happens. |
| Database | The knowledge base starts empty — retrieval returns nothing rather than failing. Useful from day one via `/ingest`; useless until something is ingested. |

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) / `timeout_s` (`60`) | Backbone retry and timeout budget |
| `memory_enabled` (`true`) | Required by HITL — the checkpointer that makes suspend/resume possible |
| `hitl.enabled` (`true`) / `hitl.max_hitl` (`12`) | Human review, and a safety ceiling on review rounds. The ceiling is a runaway guard, not a budget for the dialogue |
| `ingest.max_file_bytes` (`10 MiB`) | Per-file ceiling on ingested CSV / XLSX / DOCX |
| `ingest.max_batch_records` (`500`) | Per-batch record ceiling |
| `ingest.max_free_text_chars` (`20000`) | Per-field ceiling on free text — bounds both checkpoint growth and prompt-injection surface |
| `retrieval.top_k_similar_deviations` (`8`) | Similar-deviation candidates retrieved |
| `retrieval.min_similarity` (`0.55`) | Similarity floor. Unrelated cases are not handed to the model |

The taxonomy — deviation categories, process types, root-cause classes — lives in
`config/taxonomy.yaml` and is extended or renamed per site **without code changes**.

Accepted input formats are **CSV, XLSX and DOCX** structured exports. PDF and scanned paper are
not accepted: a format that is never parsed carries neither its attack surface nor its OCR
misreading risk, and an OCR error in a GMP record is not a cosmetic problem.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR. Without the `agenticstar-agentcore`
distribution installed, `src/api/server.py` cannot be imported at all — the graph, the state
schema and the security gates all come from it, and there is no vendored substitute in this
repository.

With the framework installed but no platform around it, the standalone server starts and
`/health` answers `{"status": "ok", "agent": "HCR-C2-062"}`. There are no start-up secrets to
resolve — the manifest declares none. Requests still need an authenticated caller: with no
upstream middleware and no matching `INVOKE_AUTH_TOKEN`, the first node's verified-external
requirement stops the invocation. With no model and no database injected, an authenticated
`/invoke` still runs — producing the deterministic draft against an empty knowledge base — and
still suspends for human review. The test suite runs without a platform connection.

## Input

Four routes, all on the same graph — the route fixes the mode; it is never inferred from the text.

| Route | Purpose |
|---|---|
| `POST /invoke` | Investigate a new deviation and draft the report and CAPA plan |
| `POST /resume` | Return the QA reviewer's approve / correct / reject decision |
| `POST /ingest` | Load past deviation and CAPA records (CSV / XLSX / DOCX) |
| `POST /feedback` | Record how a CAPA actually turned out — effective, recurred, with an evidence note |

`/invoke` takes `input`, `session_id`, `thread_id`, and **`company_id` and `site_id`**, optionally
with `file_bytes_b64`. `/resume` takes `thread_id`, `feedback`, and **`company_id` and `site_id`
as required fields** — they are not conveniences, they are the tenant-ownership proof.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "充填工程ラインで温度逸脱を検知した。詳細はQA 部門で確認予定。",
    "session_id": "demo-001",
    "thread_id": "demo-001",
    "company_id": "ACME",
    "site_id": "SITE1"
  }'
```

When `INVOKE_AUTH_TOKEN` is configured, a caller with no verified middleware trust must present a
matching bearer token or receive **401**. `/resume` runs the same authentication *and* then checks
`company_id` / `site_id` against the suspended checkpoint's tenant — **403** on mismatch, **404**
if the thread is unknown. Knowing a `thread_id` is not authority to answer someone else's QA
review, and a thread id is the kind of thing that leaks into a log line or a chat message.

File type is decided from the **content** — magic bytes and MIME sniffing. A client-declared
`Content-Type` or filename extension is not trusted.

## Output

A completed run returns `report`, holding `investigation_report` (`event_narrative`,
`rca_hypothesis`, `similar_past_case_facts`, `open_questions`), `capa_plan` (with
`past_effectiveness_evidence.similar_capa_cases`), and `recurrence_flags` — each naming a
`deviation_id` and a `risk_level`.

```json
{
  "status": "success",
  "output": {
    "report": {
      "investigation_report": {
        "event_narrative": "...",
        "rca_hypothesis": "5 Whys / 5M1E に基づく仮説（断定しない）",
        "similar_past_case_facts": ["類似逸脱 dev_xxx が過去に記録されている"],
        "open_questions": ["..."]
      },
      "capa_plan": {
        "corrective_action": {"...": "..."},
        "past_effectiveness_evidence": {"similar_capa_cases": ["capa_xxx"]}
      },
      "recurrence_flags": [{"deviation_id": "dev_xxx", "risk_level": "moderate"}]
    }
  }
}
```

The first `/invoke` on a new deviation normally returns `status: "awaiting_human"` instead, with
the thread to resume. That is the expected path, not a failure. `similar_past_case_facts` states
what is *recorded*, not what is *concluded* — the phrasing is deliberate.

## Security and Limitations

The graph requires verified-external trust, so an anonymous caller never reaches a node body.
`/resume` additionally proves tenant ownership before a decision can be injected into a suspended
review.

Whether human review happens is a **deployment decision, not a caller decision**. The
`InvokeRequest` model has no `hitl_allowed` field; a legacy caller still posting
`"hitl_allowed": false` has that value ignored, not honoured. Only a process-environment flag set
by the staging smoke-test deployment can skip the review, and it is never read from a request
body.

Personnel identity is removed by construction rather than masked afterwards: names and employee
numbers become role labels before a record enters the knowledge base. That base is a derived
search aid over the customer's QMS — it is not the system of record, and original files are not
stored, so it must not be treated as a GxP record store.

Known limits, stated rather than implied:

- Retrieval quality depends on what has been ingested. An empty base yields a draft with no
  precedent, which is a legitimate but thin result.
- The similarity floor exists so unrelated cases are not handed to the model; a genuinely
  comparable deviation described very differently can fall below it and be missed. Tuning it per
  site is expected.
- The citation check proves that a referenced case exists among those retrieved — not that it is
  the *right* precedent for this deviation. That judgement stays with the reviewer.

Output is machine-generated and assistive. It does not substitute for the site's quality
processes, and every draft is intended to be reviewed, corrected and owned by a qualified person
before it becomes part of any record.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), outer backbone wrapping an inner workflow graph
- Generation mode: `llm`, degrading to a deterministic template-conformant draft when no client is injected
- HITL: **enabled** (`hitl.enabled: true`, `max_hitl: 12`) — requires `memory_enabled: true`; not caller-disableable
- Persistence: checkpointer required; the knowledge base is a derived search copy, never a system of record
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using fakes for the model and the database. Running the
agent itself starts the standalone server:

```bash
uvicorn src.api.server:app --reload
```

Set `INVOKE_AUTH_TOKEN` before calling any route, or use real authentication middleware.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the input-channel sanitisation
design, the HITL design and the API authorisation boundary. `docs/03_test_spec.md` records the
test specification and the verification results, including the API-level authorisation tests and
a live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
