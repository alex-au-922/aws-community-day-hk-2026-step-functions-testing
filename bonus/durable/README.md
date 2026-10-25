# MiniStack Durable Functions bonus

This first slice runs the same business order as the Step Functions demo:
**create order → merchant accepts → reserve stock → charge → confirm**.
The orchestration uses the official AWS Durable Execution SDK for Python.
Workflow source and tests live under `bonus/durable/`; the existing application
handlers under `src/` and baseline tests under `tests/` remain unchanged.

## Run

Requires mise and a running Docker daemon. From the repository root:

```sh
mise run check-durable
mise run test-durable
```

These tasks select the optional `durable` dependency group. The SDK requires
Python **3.11 or later**; the repository's mise configuration selects Python 3.14.
The group pins `aws-durable-execution-sdk-python` **2.1.0** without adding it to
the baseline runtime dependencies. Pytest writes its result to
`output/durable/junit.xml`.

The environment uses Testcontainers and MiniStack **1.5.32**, pinned to the image
index digest
`sha256:c3afdc0f5a5147477b0634cbe2d2ebb032079989a52c8a1e1e59aea7413feb78`.
`LAMBDA_STRICT=1` and `LAMBDA_EXECUTOR=docker` require real Lambda containers.
MiniStack and those containers share an isolated Docker network; host clients
use the published endpoint, while Lambda SDK clients use the internal endpoint.

## What the first test proves

The merchant approval callback is the suspension point. Before answering it,
the test observes the created order and receives the approval request through
SQS. It then answers the durable callback and waits for the execution to finish.

On replay, the completed **CreateOrder** and **callback submitter** steps must
reuse their checkpoints. History and business observations check that order
creation and approval submission are not repeated, and that the completed order
has stock **8**, exactly one charge and status **CONFIRMED**.

There is no artificial wait after payment. This slice therefore proves replay
across merchant approval; it does not demonstrate replay of a completed payment
step. Rejection, retry, timeout, cancellation and compensation remain later work.

## MiniStack compatibility boundaries

- **Enable flag:** MiniStack 1.5.32 requires `DurableConfig.Enabled=true` on the
  wire. Current boto3's AWS model does not expose that field. The MiniStack-only
  deployment fixture sends a signed raw HTTP CreateFunction request with this
  flag; subsequent operations use boto3.
- **Callback deadlines:** SDK 2.1.0 uses zero to mean no timeout. MiniStack treats
  zero as an immediate deadline, so the workflow supplies positive overall and
  heartbeat timeouts explicitly.
- **Synchronous invocation:** MiniStack returns the SDK's `PENDING` envelope
  when the function suspends. The test obtains its durable execution ARN and
  polls the execution API. AWS synchronous invocation waits for the durable
  execution to complete, subject to the invocation timeout.
- **Published versions:** The initial invocation uses a published version.
  MiniStack's resume implementation selects `$LATEST`; the test keeps code and
  configuration identical after publication. It does not verify version
  isolation during a deployment.

These are local compatibility observations, not a claim of full production
parity.

## Evidence and limits

`mise run test-durable` clears only `output/durable/` before each run. It saves
`merchant-callback.json` with the initial response, pre-approval state, terminal
execution, paginated history, DynamoDB items and localstripe ledger, plus the
emulator and Lambda runtime logs. It does not clear the baseline coverage report.

The test has passed against the pinned Docker image and SDK. History contains
one invocation ending while approval waits, a callback success, a second
invocation and execution success. Each named mutation/submission step has one
successful checkpoint. The approval queue has no second request after resume.
This is evidence for this local callback/replay path, not every interruption,
concurrent order or real-service failure mode.

The four business mutation steps explicitly disable retries and use
at-most-once-per-retry semantics.
An interrupted step with no completed checkpoint can therefore fail rather than
repeat its side effect. The SDK callback submitter disables scheduled retries but uses the SDK's default
at-least-once-per-retry semantics. An interruption after sending SQS and before
completing its checkpoint may resend the approval request. This test proves
reuse of a completed submitter checkpoint, not interruption-safe single delivery.
This does not provide a global exactly-once guarantee.
In particular, rejection/uncertain-payment reconciliation and replay of a
completed payment checkpoint are outside this first slice.

The durable source lives outside `src/`, so the baseline Python coverage total
continues to describe the original Step Functions application. This bonus does
not yet collect Lambda coverage or claim 100% durable-workflow coverage.

References: [pinned MiniStack implementation](https://github.com/ministackorg/ministack/tree/v1.5.32),
[AWS durable invocation semantics](https://docs.aws.amazon.com/lambda/latest/dg/durable-invoking.html),
and [official Python SDK](https://github.com/aws/aws-durable-execution-sdk-python).
