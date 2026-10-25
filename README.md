# Testing Step Functions locally with Floci

An AWS Community Day Hong Kong talk by **Alex Au and Sky Ng**. The demo uses
pytest to run merchant approval, stock reservation, payment and cancellation
through real Step Functions workflows in Floci.

## Slides and saved reports

- [PowerPoint v39](presentation/output/floci-sky-alex-v39.pptx)
- [English speaker script](presentation/output/floci-sky-alex-v39-speaker-script.txt): Sky presents sections 1–3; Alex presents sections 4–7.
- [Full-suite HTML coverage report](presentation/assets/demo-report-v39/index.html)
- [Single stock-retry test HTML coverage report](presentation/assets/demo-report-stock-retry-v39/index.html)
- [Editable architecture slide](presentation/output/aws-workflow-architecture-single-slide.pptx)

Download or clone the repository to open the HTML reports with their supporting
files. The saved full-suite run passed **36 tests**, with **250/250 application
statements and 30/30 branch outcomes** covered. The single-test report contains
only `test_order_transaction_retry_recovers_before_payment`; it is intentionally
partial coverage.

## Run the demo

Requires [mise](https://mise.jdx.dev/) and a running Docker daemon. Run these
commands from the repository root:

```sh
mise run init
mise run check

# Full suite: requires 100% application statement and branch coverage.
mise run test
open output/htmlcov/index.html
```

For the stock-retry demo, run exactly one test and generate its own report:

```sh
COVERAGE_FAIL_UNDER=0 mise run test -- \
  tests/test_workflows/test_stock_reservation.py::test_order_transaction_retry_recovers_before_payment
open output/htmlcov/index.html
```

Each test run replaces the generated report under `output/`. The two saved
reports under `presentation/assets/` remain available as offline fallbacks.

To show the fault injection and workflow configuration in Zed:

```sh
open -a Zed \
  tests/test_workflows/test_stock_reservation.py \
  src/create_order.asl.json
```

## Workflow and test boundary

Create: **create order → merchant accepts → reserve stock → charge → confirm**.

Merchant rejection, approval timeout or insufficient stock cancels the unpaid
order. A definitive payment rejection releases the reserved stock. An uncertain
payment outcome fails the workflow while retaining the reservation for
reconciliation.

Cancel: **read confirmed order → refund → record cancellation and restore stock**.

Orders and Payments are separate Lambda components. Orders owns DynamoDB order
and stock records; Payments owns payment HTTP calls and error classification.
The merchant receives approval requests through SQS and resumes the waiting
workflow with `SendTaskSuccess` and its task token.

Tests deploy the checked-in ASL and original Lambda handlers to pinned Floci.
They use service APIs to assert workflow history, order and stock records, and
localstripe payment records. OpenResty injects HTTP errors through Lua; application
SDK calls are not mocked. Coverage runs inside each Lambda container, tags line
hits with `test_function_name`, and saves through `SIGUSR1` before teardown.

Local tests exercise workflow behaviour, including rejection, retries and lost
payment replies. The pinned localstripe does **not** implement `Idempotency-Key`,
so these tests do not prove Stripe's idempotency behaviour. The workflows do not
automatically retry uncertain charges or refunds. AWS permissions and real-service
contracts require separate validation.

- [Application components](src/README.md)
- [Workflow inputs and deployment settings](src/workflows.md)
- [Create-order ASL](src/create_order.asl.json)
- [Cancel-order ASL](src/cancel_order.asl.json)
- [Fixtures, fault injection and coverage collection](tests/README.md)

Generated test outputs and the local virtual environment are ignored by Git.

## Durable Functions bonus

The optional [MiniStack + Python Durable Functions implementation](bonus/durable/README.md)
reuses the order and payment clients with checkpointed steps and a merchant SQS
callback. Run `mise run check-durable` and `mise run test-durable` separately
from the Step Functions suite. The first slice verifies suspension, callback
resume and checkpoint reuse before payment, then checks the stored order,
stock and localstripe ledger. Its README documents MiniStack compatibility gaps.
