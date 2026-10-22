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

## Linux ARM64 native-reader measurement

A Python 3.12.14/Linux ARM64 disposable container read the 11 pilot originals
and two generic-MIME variants successfully. The stricter run took 64.4 seconds
wall time / 81.3 process CPU seconds, with 1,937,805,312 bytes peak container
memory (1.81 GiB). This is a small native-reading pilot, not throughput or
independent accuracy acceptance. Linux AMD64 and scanned-document OCR remain
unqualified. An existing 1 GiB worker cannot host this measured reader unchanged.

`reader-requirements-linux-arm64.txt` freezes the measured native-reader packages.
It does not contain the first-party upstream package needed by adapter integration
tests; install that package from this fork as described above. Use a separate
Python 3.12 environment. Select the CPU wheels explicitly before resolving the
remaining reader dependencies:

```sh
python -m pip install --no-deps --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchvision==0.29.1+cpu
python -m pip install -r samples/docling-evidence-adapter/reader-requirements-linux-arm64.txt
python -m pip check
```

The [official CPU wheel index](https://download.pytorch.org/whl/cpu/torch/) supplies
the CPU build; the default wheel selection pulled unnecessary GPU dependencies
in the first install attempt. Dependency installation had no customer files or
credentials mounted. Reading used only pre-provisioned models, offline environment
settings, and Docker networking disabled. Root, code, dependencies, sources and
models were read-only; only output and a 512 MiB temporary filesystem were writable.
The supervisor imposed a 300-second deadline, 2 CPUs, 3 GiB RAM, no swap, 128 PIDs,
an unprivileged UID, dropped capabilities and no privilege escalation. Runtime
checks verified cgroup limits and mounts. Inactive tunnel interfaces may exist
with Docker networking disabled; they must not be mistaken for active network
access. The run had no active non-loopback interface or IPv4 route.

All 13 results preserved the retained macOS body/table text and passed CRM
complete-evidence validation on PostgreSQL 15 and 17 (995 blocks including the
two variants). Agreement between readers/platforms is not a labeled accuracy
benchmark. Private samples and result payloads are kept outside this public fork.
No AWS resources, paid API, existing-service restart or production reader
activation was involved.
