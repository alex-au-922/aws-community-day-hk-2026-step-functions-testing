# Merchant-approved orders

[Create](create_order.asl.json) has nine states. Its normal route is
`CreateOrder → IsNewOrder → AwaitMerchantApproval → ReserveStock → ChargePayment → ConfirmOrder`.
Other states return progress or handle unpaid cancellation/stock release.
[Cancel](cancel_order.asl.json) has five states:
`GetOrder → IsOrderCancelled → RefundPayment → RecordRefund`, plus an existing-order return.

Create input is flat; cancel input contains only `order_id`:

```json
{"order_id":"order-001","sku":"meal-001","quantity":1,"amount_minor":12500,"currency":"HKD","payment_method_id":"pm_card_visa"}
```

`payment_method_id` stays in workflow input and goes only to Payments; it is not
part of the order record or merchant message. The caller supplies a previously
collected payment method with permission for off-session use. This bounded demo
uses synchronous card confirmation; customer authentication/redirect flows and
payment-method collection are outside it. `pm_card_visa` is a local test value.

Results contain order fields, `status`, and confirmed payment/refund IDs when
available. Cancellation is a full refund of a confirmed PaymentIntent.

## Lambda action payloads

ASL supplies `action`; callers submit the workflow input above.

| Component | `action` | Other fields | Operation/result |
| --- | --- | --- | --- |
| Orders | `create_order` | `order_id`, `sku`, `quantity`, `amount_minor`, `currency` | Conditional create or matching stored order. |
| Orders | `get_order` | `order_id` | Consistent read; handler admits `CONFIRMED` or `CANCELLED`. |
| Orders | `reserve_stock` | `order` | Atomically `PENDING → RESERVED` and subtract quantity. |
| Orders | `confirm_order` | `order`, `payment_id` | Record payment: `RESERVED → CONFIRMED`. |
| Orders | `cancel_unpaid_order` | `order` | `PENDING → CANCELLED`; stock was never reserved. |
| Orders | `release_stock` | `order` | Atomically `RESERVED → CANCELLED` and restore quantity. |
| Orders | `record_refund` | `order`, `refund_id` | Atomically `CONFIRMED → CANCELLED` and restore quantity; original payment is in `order`. |
| Payments | `charge` | `order_id`, `amount_minor`, `currency`, `payment_method_id` | Confirmed PaymentIntent ID as `payment_id`. |
| Payments | `refund` | `order_id`, `payment_id` | Confirmed `refund_id`. |

## Merchant callback and compensation

`AwaitMerchantApproval` sends the five immutable fields plus `task_token` as a
flat SQS `MessageBody` through `sqs:sendMessage.waitForTaskToken`. It waits at most
300 seconds. `ResultPath: null` discards callback output and preserves workflow data.
Merchant acceptance is a decision; Orders reserves stock afterward. Rejection
(`MerchantRejected`), callback timeout, or `OutOfStock` runs `CancelUnpaidOrder`.
Only definitive charge rejection (`PaymentRejected`) runs `ReleaseStock`.

The external merchant calls SQS `ReceiveMessage`, decodes the JSON body, and makes
the business decision. Acceptance calls `SendTaskSuccess` with the message's
`task_token` and output `{}`; rejection calls `SendTaskFailure` with that token
and error `MerchantRejected`. Only after resolving the callback does the consumer
call `DeleteMessage` with the delivery's receipt handle. A long poll can wait up
to 20 seconds; its HTTP read timeout must be longer. An empty poll is normal.

The consumer handles expired/already-closed tokens and acknowledges obsolete
deliveries; transient or unresolved callback failures remain unacknowledged.
This repository implements the workflow side of that contract. It does not ship
a merchant application client: tests perform the external actor's SDK calls
explicitly without importing production Python.

## Stored progress and replay

`PENDING` means created; `RESERVED` means stock held; `CONFIRMED` records payment.
`CANCELLED` covers unpaid cancellation, rejected-payment compensation, or refund.
The stock table's numeric `available` field changes atomically with each stock
transition. Stock rows must already exist.

All transitions check immutable fields and source status. Transactions put the
order update first and return its `ALL_OLD` condition-failure snapshot. An exact
expected target record recognizes replay after a lost response without moving
stock twice; different/stale results fail. Durable replay relies on these guards,
not the `ClientRequestToken` time window. `confirm_order` also permits the same
recorded payment; unpaid cancellation permits the same unpaid cancelled order.

## Stripe contract and recovery

Payments submits form-encoded `POST /v1/payment_intents` with amount, lowercase
currency, payment method, immediate confirmation, off-session use, and order
metadata. The result must report `status: succeeded`; its `pi_...` ID becomes
`payment_id`. `POST /v1/refunds` references that PaymentIntent and returns a
successful `re_...` ID as `refund_id`. Each operation makes one HTTP request,
authenticated with the configured Stripe secret key.

Requests carry `Idempotency-Key: charge:{order_id}` or `refund:{order_id}`.
[Stripe API v1](https://docs.stripe.com/api/idempotent_requests) stores a first
execution's status/body, including server errors, and permits pruning keys after
at least 24 hours. This is not permanent order-lifetime deduplication.
The pinned [localstripe implementation](https://github.com/adrienverge/localstripe/blob/v1.15.10/localstripe/server.py)
does not implement that header, so local tests cannot establish Stripe's guarantee.
OpenResty adds HTTP faults through Lua kept in each test; its native JSON access
logs record requests after completion. This does not add a payment idempotency
layer. In the local test environment both Lambdas use `HTTP_PROXY`, while
`AWS_ENDPOINT_URL` still selects Floci and `PAYMENT_API_URL` still selects
localstripe. Assertion clients read those services directly.

HTTP 402 raises `PaymentRejected`; only that definite charge rejection releases
stock. Network failures, transient HTTP failures, unfinished responses, and invalid
success bodies raise `PaymentUnavailable`. Other HTTP failures propagate.
`ChargePayment` and `RefundPayment` have **no automatic Retry**, including Lambda
invocation failures that could hide an already-applied financial effect.
Orders' guarded operations keep bounded retries: up to three after the first
attempt, with an initial one-second delay and backoff 2. Their persisted conditions
and transaction replay checks protect stock from repeated changes.

An unresolved charge may already have succeeded: retain `RESERVED` and reconcile
history, stored progress, and Stripe records before deciding how to recover.
An unknown refund keeps the order `CONFIRMED` and its stock held. A failed
confirmation/refund-recording step can likewise lag a financial effect.
Do not blindly restart or redrive an uncertain payment against localstripe.
On AWS, eligible [redrive](https://docs.aws.amazon.com/step-functions/latest/dg/redrive-executions.html)
resumes the original failed step with preserved input/results, but the operator
must establish whether repeating that payment is safe within Stripe's actual
contract. Recovery tooling and Floci redrive validation are outside this source.

The caller must enforce one active create execution per `order_id`, using a
deterministic execution name and identical canonical input for duplicate starts.
[Standard StartExecution](https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html)
reuses a running same-name/same-input execution; a closed name returns
`ExecutionAlreadyExists` until reusable after 90 days. No starter is implemented.
New create executions re-enter approval for matching `PENDING` orders; all other
statuses, including `RESERVED`, return stored progress and do not resume recovery.

## Configuration and permissions

Handlers are `src.lambdas.orders.handler.handle` and `src.lambdas.payments.handler.handle`.
Use Python 3.10+ and Lambda timeouts below the 30-second task limit.
Orders requires `ORDERS_TABLE_NAME` (string key `order_id`) and
`ORDERS_STOCK_TABLE_NAME` (string key `sku`, numeric `available`). Payments uses
`PAYMENT_API_URL`, `PAYMENT_API_KEY` (Pydantic `SecretStr`), and
`PAYMENT_TIMEOUT_SECONDS` (default 5, greater than 0, at most 10).
Substitute `${OrdersFunctionArn}`, `${PaymentsFunctionArn}`, `${MerchantQueueUrl}`.
Lambda tasks use [direct function resources](https://docs.aws.amazon.com/step-functions/latest/dg/connect-lambda.html#connect-lambda-direct), returning function output directly.

Workflow roles need `lambda:InvokeFunction`; create also needs `sqs:SendMessage`.
Orders needs `dynamodb:GetItem` on Orders and `dynamodb:UpdateItem` on both tables;
[transaction permissions use the underlying item actions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html).
Payments needs Stripe network access. Merchant needs `sqs:ReceiveMessage`,
`sqs:DeleteMessage`, `states:SendTaskSuccess`, and `states:SendTaskFailure`;
[callback principals must belong to the workflow's AWS account](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html#connect-wait-token).
Configure queue visibility/dead-letter handling and normal Lambda logging permissions.
