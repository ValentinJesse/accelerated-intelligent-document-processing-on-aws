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
inventing page coordinates. Conversion, actual MIME detection, source-byte hash
verification, worker time/memory limits, and tenant authorization belong to the
calling reader and remain required before activation. The adapter bounds the
serialized result and validates page geometry; it is not a document reader.

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
name from PyPI. Run `pytest samples/docling-evidence-adapter/test_adapter.py`.
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
