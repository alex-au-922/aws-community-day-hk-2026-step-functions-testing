# Testing Python workflows with Floci

An order workflow can finish successfully and still leave the wrong stock or
payment records. This demo tests the business result across Step Functions,
Lambda, SQS, DynamoDB and a payment API, including retries and lost payment replies.
It runs locally in Docker, injects failures through HTTP, and connects Python
coverage back to the pytest scenarios that exercised the code.

Presented by **Alex Au and Sky Ng** at **AWS Community Day Hong Kong,
24 October 2026**.

**[Download the slides (PPTX)](https://raw.githubusercontent.com/alex-au-922/aws-community-day-hk-2026-step-functions-testing/main/presentation/output/testing-python-workflows-with-floci.pptx)**
· **[Read the slides (PDF)](https://raw.githubusercontent.com/alex-au-922/aws-community-day-hk-2026-step-functions-testing/main/presentation/output/testing-python-workflows-with-floci.pdf)**

The shared deck has **48 slides**: the original 40-slide talk and an expanded
eight-slide MiniStack / Durable Functions bonus added after the talk.
The [English speaker script](presentation/output/testing-python-workflows-with-floci-speaker-script.txt)
and [editable architecture slide](presentation/output/aws-workflow-architecture-single-slide.pptx)
are also included.

## Run the demo

Requires [mise](https://mise.jdx.dev/) and a running Docker daemon. The fixtures
start the local services and Lambda containers; no AWS account is needed.
The mise configuration selects Python 3.14 and the locked uv version.

```sh
git clone https://github.com/alex-au-922/aws-community-day-hk-2026-step-functions-testing.git
cd aws-community-day-hk-2026-step-functions-testing
mise trust
mise install
mise run init
mise run check
```

Run the stock-retry example shown in the talk:

```sh
COVERAGE_FAIL_UNDER=0 mise run test -- \
  tests/test_workflows/test_stock_reservation.py::test_order_transaction_retry_recovers_before_payment
```

The test injects two failed stock transactions, then checks recovery before
payment. It asserts workflow history, confirmed order, stock of eight and exactly
one charge. `COVERAGE_FAIL_UNDER=0` allows this single scenario's partial coverage.

Run the full suite to enforce **100% application statement and branch coverage**:

```sh
mise run test
```

Open `output/htmlcov/index.html` in a browser (`open output/htmlcov/index.html`
on macOS). Expand a covered line's context to see its test function name, then
read that test's assertions. Each run replaces the generated coverage report;
the single-test and full-suite reports are not cumulative.

## What the workflow checks

Create: **create order → merchant accepts → reserve stock → charge → confirm**.
Starting with ten units and ordering two should leave stock of eight and one
charge.

Merchant rejection, approval timeout or insufficient stock cancels the unpaid
order. A definitive payment rejection releases the reservation. An uncertain
payment outcome fails the workflow while retaining the reservation for
reconciliation; it does not automatically retry the charge.

Cancel: **read confirmed order → refund → record cancellation and restore stock**.
An uncertain refund also requires verification before another attempt.

Orders and Payments are separate Lambda components. Orders owns DynamoDB order
and stock records; Payments owns payment HTTP calls and error classification.
The merchant receives approval requests through SQS and resumes the waiting
workflow with `SendTaskSuccess` and its task token.

## How the tests gather evidence

Tests deploy the checked-in ASL and original Lambda handlers to **Floci 2.3.0**,
pinned by Docker image digest. Testcontainers owns the services and teardown.
Application SDK calls use local endpoints; OpenResty injects HTTP errors through
test-owned Lua scripts. The tests do not import the application Python or mock
its SDK calls.

Workflow history checks transitions, errors and retry counts. Separate API reads
check committed DynamoDB order and stock records and the localstripe payment
ledger. Coverage runs inside the Lambda containers, records the test function
name as its context, and saves through `SIGUSR1` before teardown.

The saved baseline run passed **36 tests**, covering **250/250 Python application
statements and 30/30 branch outcomes** under `src/`. This combines complete
workflow tests and direct Lambda component integration tests. The coverage total
does not measure ASL, test fixtures or the optional durable bonus, and execution
coverage alone does not establish correctness.

- [Saved full-suite HTML coverage report](presentation/assets/demo-report-v39/index.html)
- [Saved stock-retry-only HTML coverage report](presentation/assets/demo-report-stock-retry-v39/index.html)

Clone or download the repository to open these HTML reports with their supporting
files. The stock-retry report is intentionally partial coverage. Fresh reports,
test evidence and the virtual environment are ignored by Git.

The pinned localstripe does **not** implement `Idempotency-Key`, so these tests
do not prove Stripe's idempotency behaviour. Local emulator results also do not
validate AWS permissions or every real-service contract. The signal-based
coverage collection applies to this Docker environment.

## Optional MiniStack / Durable Functions bonus

The [bonus implementation](bonus/durable/README.md) expresses the creation flow
with the official AWS Durable Execution SDK for Python. It reuses the order and
payment clients and replaces Step Functions orchestration with checkpointed
steps and a merchant SQS callback.

```sh
mise run check-durable
mise run test-durable
```

These tasks select the optional `durable` dependency group and run separately
from the baseline suite. **One test has passed** with real Docker Lambda
containers on **MiniStack 1.5.32** and **SDK 2.1.0**. It observes suspension at
merchant approval, sends the callback, and verifies that replay reuses completed
order-creation and approval-submission checkpoints. The final records show a
confirmed order, stock of eight and one charge.

This bonus does not yet port rejection, retry, timeout, cancellation or
compensation scenarios, test replay of a completed payment checkpoint, or collect
durable-workflow coverage. It does not establish a global exactly-once guarantee.
Its [README](bonus/durable/README.md) explains the interruption limits and
MiniStack compatibility gaps. Results are written under `output/durable/` and do
not replace the baseline coverage report.

## Explore the implementation

- [Application components](src/README.md)
- [Workflow inputs and deployment settings](src/workflows.md)
- [Create-order ASL](src/create_order.asl.json)
- [Cancel-order ASL](src/cancel_order.asl.json)
- [Fixtures, fault injection and coverage collection](tests/README.md)
