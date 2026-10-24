"""Exercise delivery and callback boundaries through public SQS and Step Functions APIs."""

import json
import re
from pathlib import Path
from typing import cast

import httpx
import pytest
from mypy_boto3_dynamodb import DynamoDBClient
from mypy_boto3_sqs import SQSClient
from mypy_boto3_stepfunctions import SFNClient
from mypy_boto3_stepfunctions.type_defs import HistoryEventTypeDef
from pydantic import JsonValue
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy


def test_empty_long_poll_is_not_an_order_timeout(
    sqs_client: SQSClient,
    merchant_queue: str,
) -> None:
    """An idle queue returns no work without sending any workflow callback."""
    # Poll an idle queue once; an empty delivery does not trigger any callback.
    response = sqs_client.receive_message(QueueUrl=merchant_queue, WaitTimeSeconds=1)
    assert response.get("Messages", []) == []


def test_redelivered_request_reuses_the_same_waiting_task(
    floci_container: DockerContainer,
    sfn_client: SFNClient,
    sqs_client: SQSClient,
    merchant_queue: str,
    create_order_machine: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stripe_api: httpx.Client,
    test_function_name: str,
) -> None:
    """SQS visibility expiry redelivers one approval rather than creating another order."""
    # Declare the request this caller submits to the public workflow interface.
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }

    # Start an order that remains blocked until its merchant approval arrives.
    execution = sfn_client.start_execution(
        stateMachineArn=create_order_machine, input=json.dumps(order_input)
    )["executionArn"]
    # Wait for this queue's enqueue log, then receive its actual approval request.
    LogMessageWaitStrategy(
        re.compile(rf"Sent message \S+ to queue {re.escape(merchant_queue)}")
    ).with_startup_timeout(180).wait_until_ready(floci_container)
    first = sqs_client.receive_message(
        QueueUrl=merchant_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20
    ).get("Messages", [])
    assert len(first) == 1
    first_message = first[0]
    assert "Body" in first_message and "ReceiptHandle" in first_message

    # Leave the first delivery unacknowledged and long-poll through visibility expiry.
    delivered_again = sqs_client.receive_message(
        QueueUrl=merchant_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20
    ).get("Messages", [])
    assert len(delivered_again) == 1
    repeated_message = delivered_again[0]
    assert "Body" in repeated_message and "ReceiptHandle" in repeated_message
    first_request = cast(dict[str, JsonValue], json.loads(first_message["Body"]))
    repeated_request = cast(dict[str, JsonValue], json.loads(repeated_message["Body"]))
    assert repeated_request == first_request
    assert repeated_message["ReceiptHandle"] != first_message["ReceiptHandle"]

    # Approve the original waiting task using the redelivered token, then acknowledge.
    task_token = repeated_request["task_token"]
    assert isinstance(task_token, str)
    sfn_client.send_task_success(taskToken=task_token, output="{}")
    sqs_client.delete_message(
        QueueUrl=merchant_queue, ReceiptHandle=repeated_message["ReceiptHandle"]
    )
    # Wait for this execution's terminal log, then read its public result.
    LogMessageWaitStrategy(
        re.compile(rf"Execution {re.escape(execution)} completed with status \S+")
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    result = sfn_client.describe_execution(executionArn=execution)

    # Fetch every history page to preserve the workflow's recorded outcome.
    history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=execution
    ):
        history.extend(page["events"])

    # Read committed order and stock records directly, bypassing the Lambdas.
    order = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )

    # Read Stripe's actual records independently of the fault proxy.
    payment_intents = stripe_api.get("/v1/payment_intents", params={"limit": 100})
    payment_intents.raise_for_status()
    charges = stripe_api.get("/v1/charges", params={"limit": 100})
    charges.raise_for_status()
    refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    refunds.raise_for_status()
    ledger = {
        "payment_intents": payment_intents.json()["data"],
        "charges": charges.json()["data"],
        "refunds": refunds.json()["data"],
    }

    # Save the observed API responses alongside this test function name for review.
    evidence = Path("output/evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "redelivered-request.json").write_text(
        json.dumps(
            {
                "test_function_name": test_function_name,
                "execution": execution,
                "result": result,
                "history": history,
                "order": order,
                "stock": stock,
                "ledger": ledger,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    # Redelivery must finish the same order with one stock deduction and one charge.
    assert result["status"] == "SUCCEEDED"
    assert "Item" in order and order["Item"]["status"] == {"S": "CONFIRMED"}
    assert "Item" in stock and stock["Item"]["available"] == {"N": "8"}
    assert len(ledger["charges"]) == 1
    assert ledger["refunds"] == []


@pytest.mark.parametrize("approval_timeout", [1])
def test_late_callback_cannot_resume_a_cancelled_order(
    floci_container: DockerContainer,
    sfn_client: SFNClient,
    sqs_client: SQSClient,
    merchant_queue: str,
    create_order_machine: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stripe_api: httpx.Client,
    test_function_name: str,
) -> None:
    """A late callback leaves the cancelled workflow and business state closed.

    Floci 2.3.0 accepts the callback API request after timeout; this checks its
    business effect separately from AWS's stricter token-validation contract.
    """
    # Declare the request this caller submits to the public workflow interface.
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }

    # Start an order whose merchant approval expires after one second.
    execution = sfn_client.start_execution(
        stateMachineArn=create_order_machine,
        input=json.dumps(order_input),
    )["executionArn"]

    # Wait for this queue's enqueue log, then receive the actual approval request.
    LogMessageWaitStrategy(
        re.compile(rf"Sent message \S+ to queue {re.escape(merchant_queue)}")
    ).with_startup_timeout(180).wait_until_ready(floci_container)
    messages = sqs_client.receive_message(
        QueueUrl=merchant_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20
    ).get("Messages", [])
    assert len(messages) == 1
    message = messages[0]
    assert "Body" in message and "ReceiptHandle" in message

    # Wait for this execution to finish so the merchant's callback is late.
    LogMessageWaitStrategy(
        re.compile(rf"Execution {re.escape(execution)} completed with status \S+")
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    result = sfn_client.describe_execution(executionArn=execution)

    # Confirm the timeout path has already cancelled the order successfully.
    assert result["status"] == "SUCCEEDED"
    output = result.get("output")
    assert output is not None
    assert json.loads(output)["status"] == "CANCELLED"

    # Send approval with the expired token, then acknowledge its SQS delivery.
    request = cast(dict[str, JsonValue], json.loads(message["Body"]))
    task_token = request["task_token"]
    assert isinstance(task_token, str)
    sfn_client.send_task_success(taskToken=task_token, output="{}")
    sqs_client.delete_message(
        QueueUrl=merchant_queue, ReceiptHandle=message["ReceiptHandle"]
    )
    # Read the execution again to check whether the late callback changed it.
    after = sfn_client.describe_execution(executionArn=execution)

    # Fetch every history page to inspect the workflow's final recorded outcome.
    history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=execution
    ):
        history.extend(page["events"])

    # Read committed order and stock records directly, bypassing the Lambdas.
    order = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )

    # Check Stripe's real records for any charge or refund caused by late approval.
    payment_intents = stripe_api.get("/v1/payment_intents", params={"limit": 100})
    payment_intents.raise_for_status()
    charges = stripe_api.get("/v1/charges", params={"limit": 100})
    charges.raise_for_status()
    refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    refunds.raise_for_status()
    ledger = {
        "payment_intents": payment_intents.json()["data"],
        "charges": charges.json()["data"],
        "refunds": refunds.json()["data"],
    }

    # Save the observed API responses alongside this test function name for review.
    evidence = Path("output/evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "late-callback.json").write_text(
        json.dumps(
            {
                "test_function_name": test_function_name,
                "execution": execution,
                "before": result,
                "after": after,
                "history": history,
                "order": order,
                "stock": stock,
                "ledger": ledger,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    # The callback must leave execution, cancellation, stock and payments unchanged.
    assert after["status"] == result["status"]
    assert after.get("output") == result.get("output")
    assert after.get("stopDate") == result.get("stopDate")
    assert "Item" in order and order["Item"]["status"] == {"S": "CANCELLED"}
    assert "Item" in stock and stock["Item"]["available"] == {"N": "10"}
    assert ledger == {"payment_intents": [], "charges": [], "refunds": []}
    assert history[-1]["type"] == "ExecutionSucceeded"
