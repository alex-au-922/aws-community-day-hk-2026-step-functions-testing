"""Check Stripe response contracts through the deployed Payments Lambda."""

import json
import re
from pathlib import Path
from textwrap import dedent
from typing import cast

import httpx
import pytest
from mypy_boto3_lambda import LambdaClient
from pydantic import JsonValue
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy, LogMessageWaitStrategy


@pytest.mark.parametrize(
    ("fault", "expected_error"),
    [
        pytest.param(
            'ngx.status = 200; ngx.say("{"); return ngx.exit(200)',
            "PaymentUnavailable",
            id="malformed-refund",
        ),
        pytest.param(
            'ngx.status = 200; ngx.say([[{"id":"re_pending","status":"pending"}]]); return ngx.exit(200)',
            "PaymentUnavailable",
            id="pending-refund",
        ),
        pytest.param(
            "return ngx.exit(409)", "HTTPStatusError", id="unhandled-client-error"
        ),
        pytest.param(
            "return ngx.exit(444)",
            "PaymentUnavailable",
            id="connection-lost",
        ),
    ],
)
def test_unconfirmed_refund_requires_verification_before_another_attempt(
    lambda_client: LambdaClient,
    payments_function: str,
    stripe_api: httpx.Client,
    fault_lua: Path,
    fault_proxy_container: DockerContainer,
    fault: str,
    expected_error: str,
) -> None:
    """Verify no refund exists before retrying an intercepted refund manually."""
    # Create a real charge through the deployed Lambda before exercising refunds.
    charge_invocation = lambda_client.invoke(
        FunctionName=payments_function,
        Payload=json.dumps(
            {
                "action": "charge",
                "order_id": "order-001",
                "amount_minor": 12500,
                "currency": "HKD",
                "payment_method_id": "pm_card_visa",
            }
        ),
    )
    payment = cast(
        dict[str, JsonValue], json.loads(charge_invocation["Payload"].read())
    )
    assert "FunctionError" not in charge_invocation

    # Intercept the refund at the HTTP boundary; no request reaches localstripe.
    fault_lua.write_text(
        dedent(f"""\
            return function()
                if ngx.get_phase() ~= "access" or ngx.req.get_method() ~= "POST"
                    or ngx.var.uri ~= "/v1/refunds" then return end
                ngx.header.content_type = "application/json"
                {fault}
            end
        """)
    )
    fault_proxy_container.get_wrapped_container().restart()
    HttpWaitStrategy(8080, "/health").for_status_code(200).wait_until_ready(
        fault_proxy_container
    )
    refund_invocation = lambda_client.invoke(
        FunctionName=payments_function,
        Payload=json.dumps(
            {
                "action": "refund",
                "order_id": "order-001",
                "payment_id": payment["payment_id"],
            }
        ),
    )
    failed = cast(dict[str, JsonValue], json.loads(refund_invocation["Payload"].read()))

    # Reconcile independent Stripe records before deciding another attempt is safe.
    payment_intents = stripe_api.get("/v1/payment_intents", params={"limit": 100})
    payment_intents.raise_for_status()
    charges = stripe_api.get("/v1/charges", params={"limit": 100})
    charges.raise_for_status()
    refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    refunds.raise_for_status()
    before = {
        "payment_intents": cast(
            list[dict[str, JsonValue]], payment_intents.json()["data"]
        ),
        "charges": cast(list[dict[str, JsonValue]], charges.json()["data"]),
        "refunds": cast(list[dict[str, JsonValue]], refunds.json()["data"]),
    }

    # The Lambda reports the injected failure, and no financial refund occurred.
    assert "FunctionError" in refund_invocation
    assert failed["errorType"] == expected_error
    assert len(before["payment_intents"]) == len(before["charges"]) == 1
    assert before["payment_intents"][0]["id"] == payment["payment_id"]
    assert before["refunds"] == []

    # Remove the fault only after verification, then make one explicit new attempt.
    fault_lua.write_text("return function() end\n")
    fault_proxy_container.get_wrapped_container().restart()
    HttpWaitStrategy(8080, "/health").for_status_code(200).wait_until_ready(
        fault_proxy_container
    )
    recovery_invocation = lambda_client.invoke(
        FunctionName=payments_function,
        Payload=json.dumps(
            {
                "action": "refund",
                "order_id": "order-001",
                "payment_id": payment["payment_id"],
            }
        ),
    )
    recovered = cast(
        dict[str, JsonValue], json.loads(recovery_invocation["Payload"].read())
    )

    # Read Stripe directly again to verify one successful refund of that charge.
    payment_intents = stripe_api.get("/v1/payment_intents", params={"limit": 100})
    payment_intents.raise_for_status()
    charges = stripe_api.get("/v1/charges", params={"limit": 100})
    charges.raise_for_status()
    refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    refunds.raise_for_status()
    after = {
        "payment_intents": cast(
            list[dict[str, JsonValue]], payment_intents.json()["data"]
        ),
        "charges": cast(list[dict[str, JsonValue]], charges.json()["data"]),
        "refunds": cast(list[dict[str, JsonValue]], refunds.json()["data"]),
    }

    assert "FunctionError" not in recovery_invocation
    assert recovered["refund_id"] == after["refunds"][0]["id"]
    assert len(after["charges"]) == len(after["refunds"]) == 1
    assert after["refunds"][0]["payment_intent"] == payment["payment_id"]
    assert after["refunds"][0]["status"] == "succeeded"


@pytest.mark.parametrize("status", [400, 409, 422])
def test_charge_client_error_propagates_without_a_payment(
    lambda_client: LambdaClient,
    payments_function: str,
    stripe_api: httpx.Client,
    fault_lua: Path,
    fault_proxy_container: DockerContainer,
    status: int,
) -> None:
    """Unclassified Stripe client errors propagate without claiming completion."""
    # Return a client error without forwarding the request to localstripe.
    fault_lua.write_text(
        dedent(f"""\
            return function()
                if ngx.get_phase() ~= "access" or ngx.req.get_method() ~= "POST"
                    or ngx.var.uri ~= "/v1/payment_intents" then return end
                return ngx.exit({status})
            end
        """)
    )
    fault_proxy_container.get_wrapped_container().restart()
    HttpWaitStrategy(8080, "/health").for_status_code(200).wait_until_ready(
        fault_proxy_container
    )
    # Invoke the deployed handler once and decode the public Lambda error payload.
    invocation = lambda_client.invoke(
        FunctionName=payments_function,
        Payload=json.dumps(
            {
                "action": "charge",
                "order_id": "order-001",
                "amount_minor": 12500,
                "currency": "HKD",
                "payment_method_id": "pm_card_visa",
            }
        ),
    )
    result = cast(dict[str, JsonValue], json.loads(invocation["Payload"].read()))

    # Verify effects through Stripe's own APIs, independently of the fault proxy.
    payment_intents = stripe_api.get("/v1/payment_intents", params={"limit": 100})
    payment_intents.raise_for_status()
    charges = stripe_api.get("/v1/charges", params={"limit": 100})
    charges.raise_for_status()
    refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    refunds.raise_for_status()
    ledger = {
        "payment_intents": cast(
            list[dict[str, JsonValue]], payment_intents.json()["data"]
        ),
        "charges": cast(list[dict[str, JsonValue]], charges.json()["data"]),
        "refunds": cast(list[dict[str, JsonValue]], refunds.json()["data"]),
    }

    # Count the intercepted HTTP requests to establish that no retry was hidden.
    LogMessageWaitStrategy(
        re.compile(r'"method":"POST","path":"/v1/payment_intents"')
    ).with_startup_timeout(10).wait_until_ready(fault_proxy_container)
    attempts = sum(
        cast(dict[str, str], json.loads(line))
        == {"method": "POST", "path": "/v1/payment_intents"}
        for line in fault_proxy_container.get_logs()[0].decode().splitlines()
    )

    assert "FunctionError" in invocation
    assert result["errorType"] == "HTTPStatusError"
    assert ledger == {"payment_intents": [], "charges": [], "refunds": []}
    assert attempts == 1
