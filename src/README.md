# Components

The Standard Step Functions definitions live in `src/`. Each component owns its
client, schemas, constraints, and configuration; neither Lambda imports or invokes the other.

| Component | Public client | Responsibility |
| --- | --- | --- |
| [Orders](lambdas/orders/client.py) | `OrderClient(session, table_name, stock_table_name)` | Seven order actions, guarded transitions, and atomic order/stock updates. |
| [Payments](lambdas/payments/client.py) | `PaymentClient(api_url, api_key, timeout_seconds)` | Confirm Stripe PaymentIntents, refund them, validate results, and classify HTTP failures. |

Clients are Pydantic models constructed with keyword arguments. The two handlers
use Powertools `@event_parser` to validate an action-discriminated request union,
then `match` dispatches one operation and serializes its result. Settings load per
invocation. Clients accept domain fields, `OrderRecord`, or `ConfirmedOrder`, independently of
Lambda request models; each public operation makes one SDK or HTTP call.

Orders keeps its order and action schemas together in [models.py](lambdas/orders/models.py),
with named failures in [errors.py](lambdas/orders/errors.py). Immutable fields are `order_id`,
`sku`, `quantity`, `amount_minor`, and `currency`. Quantity is 1–999; amount is in
the currency's smallest unit (`12500` means HKD 125.00). Orders and Payments keep
format aliases locally.

Orders uses conditional writes, consistent reads, or one two-item transaction.
Reservation, release, and refund recording update order and stock atomically.
These three actions share one private transaction operation; their public methods
state the source/target status and stock change explicitly.
Exact replay uses returned order condition-failure snapshots, without another
read or repeated stock movement. Payments sends stable Stripe idempotency keys;
the local emulator does not enforce them, so payment uncertainty is not retried
automatically. Bounded retries remain on guarded Orders operations.

The merchant is an external actor, outside `src/`. Its interface is SQS
`ReceiveMessage`/`DeleteMessage` and Step Functions
`SendTaskSuccess`/`SendTaskFailure`. Tests play that role with independent SDK
calls; they do not import either Lambda's implementation, models, or clients.

[Workflow contracts](workflows.md) cover actions, states, callbacks, recovery, and configuration.

## Validation status

`mise run check` checks strict Pyright for source and tests, Ruff lint/formatting,
and JSON syntax. `mise run` deploys the unchanged handlers and real ASL to Floci,
asserts order/stock, localstripe payment records and history, and enforces 100% production
statement/branch coverage with per-test attribution collected inside the Lambda
containers. The host tests do not execute production Python. See the [test guide](../tests/README.md).
Local results establish the pinned emulator's behavior; AWS permissions and API
differences still require separate contract validation.
