# Local Docling evidence adapter

Status: offline integration candidate. No deployed handler or workflow activation.
The fork is based on upstream `v0.6.12` (`205b8c3fb6b9ec7fd29c1ac88fa566053bffe65e`).

## Integration boundary

| Upstream capability | Current dependency | Integration decision |
| --- | --- | --- |
| Source arrival and scheduling | S3, EventBridge, SQS, Lambda, DynamoDB | Keep backend connector authorization and revision ledger authoritative; do not start a second source scheduler |
| Workflow and retries | Step Functions and Lambda | Preserve upstream workflow; replacing OCR does not remove these AWS dependencies |
| OCR | Textract, BDA or Bedrock/LambdaHook | Translate local Docling evidence through the existing structured OCR response contract |
| Classification, extraction, assessment | Configurable model calls, including LambdaHook | Separate qualification required; OCR translation does not infer or accept financial facts |
| Review and evaluation | Upstream workflow plus storage/authentication services | Preserve upstream mechanisms; reconcile approved results through the application's tenant-scoped acceptance contract |
| Artifact persistence | S3 | Adapter returns data only; a future authorized caller owns storage, isolation and retention |
| UI identity | Cognito and API Gateway | Existing CRM authentication remains authoritative; no parallel login introduced |

See [upstream architecture](../../docs/architecture.md) and
[LambdaHook contract](../../docs/lambda-hook-inference.md). No AWS resources,
managed extraction requests or paid models are provisioned by this sample.
An offline adapter is not proof of a running cloud-independent workflow.

## Contract

`build_evidence` consumes a successful `DoclingDocument` and an explicit immutable
source binding. It retains the complete Docling representation, body and table
Markdown, and item/cell source anchors. A page-specific hook response exposes
normalized coordinates to the **unchanged** upstream OCR artifact consumer.
Missing confidence stays missing. DOCX structural references are retained without
inventing page coordinates. `reader.read_evidence` now invokes local Docling
after source-byte hash and signature checks. It bounds file/archive sizes, page
counts/dimensions and output, rejects partial conversion, and preserves body text
and tables. The adapter bounds serialized results and validates page geometry.
Tenant authorization and hard worker time/memory/network limits remain the
caller’s responsibility and are required before activation.

The source binding is an identity assertion by the authorized caller, not proof
of authorization. Never use data from the document itself to choose a tenant,
company, source revision, output path, or destination. Do not publish private
source documents, converted evidence or credentials in this repository.

The normalized envelope must be stored intact: the per-page hook view alone
cannot preserve DOCX structure or all cross-page table semantics. Hook metering
reports zero model tokens, not zero CPU, storage or operating cost. A deployed
Lambda request/transport adapter and CRM candidate delivery are not implemented.

## Offline verification

Use Python 3.12 and the pinned `requirements.txt`. Install the first-party package
from this checkout's `lib/idp_common_pkg`, never an unrelated package of the same
name from PyPI. Run `pytest samples/docling-evidence-adapter`.
The test suite is registered in the upstream test runner and package-test recipe;
its Docling core dependency is declared in the test extra.
Tests call the actual upstream OCR artifact and page-data consumers without
constructing AWS clients. Fixtures are synthetic; private-corpus replay stays
outside the repository. This does not qualify OCR or financial publication.

Page block IDs resolve through `blockAnchors` to original structural references.
`itemsWithoutUnambiguousPage` explicitly records unlocated text and multi-page
tables. Their complete evidence remains available; callers must not claim that
the page hooks alone cover those items. Upstream labels a block as a line; the
envelope retains the true text-item or table-cell granularity for reviewers.

## Native reader qualification

`reader.read_evidence(data, source, models=absolute_models_path)` accepts PDF and
DOCX bytes. Its policy is `ReadLimits`; limits cannot be disabled. Unsupported
formats, MIME/hash mismatches, encrypted or oversized archives, excessive PDF
pages/dimensions, conversion errors and partial output fail explicitly. Pages
without anchored text and empty documents require reading review. No OCR fallback
or financial acceptance is enabled.

Generic `application/octet-stream` metadata can be resolved to PDF/DOCX by byte
detection. ZIP metadata can resolve to DOCX only after its container checks pass.
The original declared MIME remains in the source binding; `reading.detectedMime`
records the detected type. A conflicting specific MIME still fails. This uses no
company names, paths or filename conventions.

Docling's conversion timeout is cooperative, so an isolated supervisor must also
enforce a hard deadline and memory limit. Use network denial and offline model
settings, with a pre-provisioned model directory and no provider credentials.
`reader-requirements-macos-arm64.txt` records the measured Python 3.12 qualification
environment; it is not a qualified Linux production image. No document-model API
is required for native reading. Full-reader tests against private sources run
separately from the small offline contract/preflight suite.

The CRM's existing normalization v1 allow-list cannot store this full envelope.
A versioned contract migration and acceptance support for structural anchors must
precede delivery. Do not flatten away tables/anchors to bypass that boundary.
