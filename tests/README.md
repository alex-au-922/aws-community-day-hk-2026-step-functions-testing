# Black-box tests

Run `mise run doctor`, `mise run init`, `mise run check`, then `mise run` with
Docker available. The default task executes pytest, generates HTML/JSON coverage
and fails unless every production statement and branch is covered.

Native reporting is verified with **Python 3.14** (Lambda **3.14.8**), coverage.py
**7.16.2** and its `sysmon` core: **250/250 production statements, 30/30 branches**,
with pytest contexts on every executed statement. The complete `mise run`
passed all **36 tests** using the native CLI and save signal described below.
Static analysis confirms no test imports application Python, including
annotations. Application source and coverage
exclusions are unchanged. Session containers, network, volumes and built images
were confirmed removed after the run.

The suite uses Floci **2.3.0**, pinned by image digest. One shared Lambda image
contains the original `src.lambdas.orders.handler.handle` and
`src.lambdas.payments.handler.handle` commands. Functions are registered separately
for each test, stay warm during that test, and are deleted afterward.
Production source contains no endpoint rewrites, fault switches or pytest data.

## Read the tests

Workflow cases exercise the complete deployed flow end to end. Direct Lambda
action cases are black-box component integration tests; both use public APIs
without importing application Python. The coverage total combines these cases.

- [conftest.py](conftest.py) registers [fixture modules](fixtures/) explicitly
  through `pytest_plugins` and owns the session hook. Modules separate Docker,
  Floci/SDK clients, localstripe, the HTTP proxy, DynamoDB, Lambdas and workflows; each keeps
  dependencies and resource teardown together. Endpoint fixtures derive addresses
  from their actual container dependency. Pytest does not scan this directory
  automatically.
- [test_workflows/](test_workflows/) groups complete end-to-end tests by behavior:
  [order lifecycle](test_workflows/test_order_lifecycle.py),
  [merchant approval](test_workflows/test_merchant_approval.py),
  [stock reservation](test_workflows/test_stock_reservation.py),
  [payment rejection](test_workflows/test_payment_rejection.py), and
  [payment uncertainty](test_workflows/test_payment_uncertainty.py).
  Each test explicitly starts, receives, decides, acknowledges, waits, reads and
  asserts. History assertions check actual error names and attempt counts
  independently of localstripe payment records.
- [test_order_actions.py](test_order_actions.py) invokes deployed actions through
  the Lambda API, including stale snapshots and committed transactions whose
  responses were lost. Assertions read DynamoDB directly.
- [test_payment_actions.py](test_payment_actions.py) checks refund uncertainty and
  HTTP errors through the deployed Payments Lambda.
- [test_merchant.py](test_merchant.py) distinguishes idle polls, task deadlines
  and late callbacks through native SQS and Step Functions APIs.

Every test keeps its SDK calls, response decoding and assertions in the test body,
with comments explaining each block. There is no project utility layer for
invocation, polling, reads or evidence output. Native SDK paginators and DynamoDB
attribute decoding remain visible where used.

Order payloads are declared inside each test. Fixtures retain only the stock and
approval-deadline defaults needed to prepare the environment; scenarios override
them with direct `pytest.mark.parametrize`, without indirect request handling.

Workflow cases use Testcontainers to wait for pinned Floci's SQS DEBUG enqueue
log, matched to the test's unique queue URL, then call `ReceiveMessage` once.
Completion waits match the actual execution ARN before reading its result. The
redelivery test uses a second native long poll to wait for visibility expiry.
Logs synchronize the test environment; message data and execution results still
come from the actual APIs. These waits tolerate local Lambda startup without
warm-up invocations or production test flags.

Tests are independent external actors: they receive SQS messages, decode the
message body, call `SendTaskSuccess`/`SendTaskFailure`, and delete the delivery
explicitly. They do not import `src`, including clients or models used only in
type annotations. The merchant is a business actor outside this application's
production source, so there is no merchant client in the coverage denominator.

Both Lambdas use the same [OpenResty fixture](fixtures/proxy.py) through
`HTTP_PROXY`. Orders keeps `AWS_ENDPOINT_URL` pointing to Floci; Payments keeps
`PAYMENT_API_URL` pointing to the pinned `adrienverge/localstripe` container.
Host assertion clients read Floci and localstripe directly. Each test owns a proxy
with a temporary Lua directory mounted into it; the default script forwards every
request. Payment assertions read native PaymentIntent, charge and refund records,
independently of proxy logs. There is no custom payment server or deduplication
facade.

Fault scenarios keep their Lua directly inside the relevant test. At the point
where the fault should begin, the test writes that script, restarts OpenResty,
waits for `/health`, then makes the target request. Nginx's native `proxy_pass`
handles forwarding, headers and response framing. The default Lua script does
nothing; the shared proxy configuration needs no custom forwarding callback or
response-copying loop.

The same Lua function runs in the native access, header-filter and body-filter
phases. A before-write fault returns its error in the access phase; a local
counter implements bounded failures. An after-commit fault replaces a successful
upstream status in the header filter, then discards the original body chunks and
emits its error body at EOF. There is no administration API, scenario schema,
recursive request matching or reset endpoint. Restarting loads the changed
script and clears its counters. The OpenResty image is pinned by digest and
supports ARM64 and AMD64.

Payment cases also inject HTTP status/body errors or close the connection without
an HTTP response. The lost-payment acknowledgement case explicitly sends an
upstream subrequest from the access-phase Lua, waits for localstripe's real
response, then delays the main reply for 3.5 seconds, beyond the payment client's
timeout. The committed charge is verified through localstripe's ledger, and the
workflow must stop without retrying.

Request counts come from nginx's native JSON access logs. Tests wait for the
matching completed-request log before reading and counting those records; a
workflow reaching a terminal state does not by itself mean proxy logging has
finished. The logs show observed requests, while the independently read ledger
shows financial effects.

localstripe is shared for the session and reset through its native API per test.
The pinned localstripe image is `linux/amd64`, so Docker emulates that architecture
on an Apple Silicon host. OpenResty's pinned image supports ARM64 and AMD64.

Fixtures create temporary build contexts and write their Dockerfile text inline,
following the Contineo `jenkins-oidc-sync` pattern. The OpenResty fixture also writes
its nginx configuration inline into that temporary directory. One Lambda image
supplies both handlers; there is no separate checked-in Lambda Dockerfile.

The Lambda builder and runtime use the same pinned AWS base image. In the builder,
`uv pip install --target /packages` installs the locked dependencies; the final
stage copies those packages into `/var/task`, following the
[official uv Lambda pattern](https://docs.astral.sh/uv/guides/integration/aws-lambda/).
The uv binary, lockfile and temporary requirements stay in the builder.

AWS credentials and region are explicit local values. The host AWS profile is
temporarily unset and restored. Host clients use the configured Floci endpoint;
Lambdas receive standard SDK endpoint configuration.
`AWS_MAX_ATTEMPTS=1` makes SDK fault attempts observable separately from ASL retries.
The approval deadline is shortened only in the deployed test definition.

## Coverage and reports

The inline Dockerfile in the [Lambda fixture](fixtures/lambdas.py) uses an
exec-form entrypoint:

```text
python -m coverage run --rcfile=/build/pyproject.toml --save-signal=USR1 -m awslambdaric
```

Docker's `CMD` supplies the original handler path. This makes the native coverage
and Runtime Interface Client process PID 1; Floci provides its Lambda Runtime API.
The image needs no Runtime Interface Emulator, shell wrapper or custom startup
module.

Coverage uses its normal configuration in `pyproject.toml`: `sysmon`, branch and
parallel measurement, plus a static context expanded from `COVERAGE_CONTEXT`.
The `test_function_name` fixture returns `request.node.name`: the test function
name, including a `[parameter-id]` suffix for a parametrized case. Both Lambda
fixtures pass that value into `COVERAGE_CONTEXT`. The current suite has unique
function names across modules; keep them unique so contexts remain distinct.
`COVERAGE_FILE` selects `/tmp/coverage/.coverage` inside the container.

Before deleting a function, teardown sends `SIGUSR1` to its Docker container,
using coverage.py's native
[`--save-signal`](https://coverage.readthedocs.io/en/7.16.2/subprocess.html#long-running-processes)
support. It waits at most ten seconds for the finalized coverage file, then copies
the saved data through Docker's archive API. There is no `.pth` startup, socket
server or control script. The complete CLI/signal flow passed all 36 tests.

Production Python executes only inside the deployed Lambda containers. The host
tests use public APIs and never import it to obtain coverage. Coverage.py combines
the copied container data using the `/var/task/src` path mapping. Coverage counts
**all Python under `src/`**: both Lambda components, their models, clients and
settings. Tests, environment servers and instrumentation are outside this denominator.
There are no project-specific coverage exclusions; Coverage.py's standard
`TYPE_CHECKING` exclusion applies to production SDK-only type imports. The test
environment installs `boto3-stubs` and imports its SDK types directly in tests and
fixtures, without `TYPE_CHECKING` guards or quoted annotations.

The mise task runs pytest without host tracing, then coverage.py's native
`coverage combine`, `coverage html --show-contexts`, `coverage json --show-contexts`
and `coverage report` commands. The 100% gate applies to measured statements and
branches. Per-test contexts come from environment expansion in coverage.py's
[measurement-context configuration](https://coverage.readthedocs.io/en/7.16.2/contexts.html),
rather than a host pytest-cov measurement. HTML and JSON are emitted unchanged;
there is no report post-processing or coverage-data repair.

### Native context reporting

Host reporting uses **Python 3.14.0** and the Lambda image uses **3.14.8**.
Coverage.py 7.16.2 uses its supported
[`sysmon` core](https://coverage.readthedocs.io/en/7.16.2/config.html#run-core).
Native reports contain **250/250 statements and 30/30 branches**, with actual
pytest contexts on all 250 executed statement lines. The multiline `if` at
`src/lambdas/orders/client.py:346` has four recorded contexts. Context labels are
test function names, including parameter suffixes when present.

The previous Python 3.13 measurement counted 283 statements. Python 3.14's deferred
annotations mean 33 annotation-only lines no longer count as executable statements
in native analysis; no application source or coverage exclusions changed. Keeping
the measuring and reporting interpreter versions aligned avoids comparing two
different statement denominators. The complete pipeline passed all 36 tests with
the direct PID 1 entrypoint and native save-signal collection, combining 47 saved
coverage files into these native reports.

HTML, JSON and measurement data remain unmodified. See the official
[context-reporting guide](https://coverage.readthedocs.io/en/7.16.2/contexts.html#context-reporting).

Generated artifacts:

| Path | Evidence |
| --- | --- |
| `output/htmlcov/index.html` | Native source coverage with expandable pytest contexts for every executed statement. |
| `output/coverage.json` | Native statement/branch totals, missing paths and unmodified contexts. |
| `output/junit.xml` | Pytest cases, verdicts and durations. |
| `output/evidence/*.json` | Workflow history, outcomes, order/stock and ledger observations labelled by `test_function_name`. |

Each run discards previous coverage and observations. State-machine teardown
stops its own unfinished executions before removing dependent functions, tables
and queues. Testcontainers owns the session containers, images and network.

## Emulator boundaries

### Payment retries

`adrienverge/localstripe:1.15.10` implements useful stateful payment/refund APIs,
but **does not implement `Idempotency-Key`**. Its
[request routing](https://github.com/adrienverge/localstripe/blob/v1.15.10/localstripe/server.py)
passes new create requests to resource constructors; no header-based result cache
deduplicates them. Stable headers in the application do not change this.

The checked-in payment states therefore make one attempt and fail on uncertainty.
An unknown charge retains `RESERVED`; an unknown refund leaves the confirmed order
and stock unchanged. Tests inspect the ledger separately, including the case where
a financial effect occurred but its response was not received in time. They do
not claim that repeating that request against localstripe would be safe.

OpenResty's [Lua hooks](https://github.com/openresty/lua-nginx-module#directives)
exercise our HTTP client and workflow handling. Its
[nginx access logs](https://nginx.org/en/docs/http/ngx_http_log_module.html#log_format)
record the requests observed by the proxy. Neither emulates Stripe's financial
idempotency, concurrent request guarantees, key-retention policy or real API
permissions. No second payment implementation fills that gap.

Bounded `Retry` is demonstrated on guarded DynamoDB operations instead. Their
persisted replay checks also handle a committed transaction whose first response
was lost. `Catch`, timeout, and compensation remain separate workflow scenarios.
Stripe's actual idempotency contract requires its own integration checks; its
[API v1 key retention](https://docs.stripe.com/api/idempotent_requests) is at least
24 hours, not the permanent lifetime of an order.

**100% source coverage measures which statements and branches ran against this
environment. It cannot prove a dependency feature the emulator does not provide.**
See the [saved slides and reports](../README.md#slides-and-saved-reports).

### AWS differences

Floci 2.3.0 returns HTTP 200 for a callback using an expired task token. The late
callback test verifies that the completed workflow, order, stock and payment
ledger remain unchanged. This is a known HTTP validation difference: AWS documents
[`TaskTimedOut`, HTTP 400](https://docs.aws.amazon.com/step-functions/latest/apireference/API_SendTaskSuccess.html#API_SendTaskSuccess_Errors).
Floci's HTTP handler ignores its service's missing-token boolean:
[pinned implementation](https://github.com/floci-io/floci/blob/2.3.0/src/main/java/io/github/hectorvent/floci/services/stepfunctions/StepFunctionsJsonHandler.java#L511).

Local tests do not verify AWS IAM enforcement, AWS callback error parity or AWS
redrive. The direct Lambda action tests cover durable replay without pretending
to provide an emulator redrive API. Current Floci also documents Durable Functions;
the separate MiniStack bonus should compare the current emulator support.
