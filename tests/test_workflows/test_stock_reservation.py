"""Exercise deployed workflows through AWS APIs and independent Stripe effects."""

import json
import re
from pathlib import Path
from textwrap import dedent
from typing import cast

import httpx
import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_sqs import SQSClient
from mypy_boto3_stepfunctions.client import SFNClient
from mypy_boto3_stepfunctions.type_defs import HistoryEventTypeDef
from pydantic import JsonValue
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy, LogMessageWaitStrategy


@pytest.mark.parametrize("stock_quantity", [0])
def test_insufficient_stock_cancels_before_payment(
    test_function_name: str,
    floci_container: DockerContainer,
    sfn_client: SFNClient,
    dynamodb_client: DynamoDBClient,
    sqs_client: SQSClient,
    merchant_queue: str,
    create_order_machine: str,
    orders_table: str,
    stock_table: str,
    stripe_api: httpx.Client,
) -> None:
    """A rejected stock transaction leaves both balance and provider untouched."""

    # Declare the request this caller submits to the public workflow interface.
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }

    # Approve an order whose declared starting stock is empty.
    execution = sfn_client.start_execution(
        stateMachineArn=create_order_machine, input=json.dumps(order_input)
    )["executionArn"]

    # Wait for this queue's enqueue log, then receive its actual approval request.
    LogMessageWaitStrategy(
        re.compile(rf"Sent message \S+ to queue {re.escape(merchant_queue)}")
    ).with_startup_timeout(180).wait_until_ready(floci_container)
    messages = sqs_client.receive_message(
        QueueUrl=merchant_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20
    ).get("Messages", [])
    assert len(messages) == 1
    message = messages[0]
    assert "Body" in message and "ReceiptHandle" in message
    request = cast(dict[str, JsonValue], json.loads(message["Body"]))
    task_token = request["task_token"]
    assert isinstance(task_token, str)
    sfn_client.send_task_success(taskToken=task_token, output="{}")
    sqs_client.delete_message(
        QueueUrl=merchant_queue, ReceiptHandle=message["ReceiptHandle"]
    )

    # Wait for this execution's terminal log, then read its public status.
    LogMessageWaitStrategy(
        re.compile(rf"Execution {re.escape(execution)} completed with status \S+")
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    result = sfn_client.describe_execution(executionArn=execution)

    # Read every history page to inspect the actual state transitions.
    history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=execution, PaginationConfig={"PageSize": 5}
    ):
        history.extend(page["events"])

    # Read the committed order directly, bypassing the Orders Lambda.
    order_response = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in order_response
    order = order_response["Item"]
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock

    # Read Stripe's real payment records independently of the Lambda and proxy.
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

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "insufficient-stock.json").write_text(
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

    assert result["status"] == "SUCCEEDED"
    assert [
        event.get("lambdaFunctionFailedEventDetails", {}).get("error")
        for event in history
        if event["type"] == "LambdaFunctionFailed"
    ] == ["OutOfStock"]
    assert all(event["type"] != "ExecutionFailed" for event in history)
    assert [
        event["stateEnteredEventDetails"]["name"]
        for event in history
        if event["type"]
        in {"TaskStateEntered", "ChoiceStateEntered", "PassStateEntered"}
        and "stateEnteredEventDetails" in event
    ] == [
        "CreateOrder",
        "IsNewOrder",
        "AwaitMerchantApproval",
        "ReserveStock",
        "CancelUnpaidOrder",
    ]
    assert order["status"] == {"S": "CANCELLED"}
    assert stock["Item"]["available"] == {"N": "0"}
    assert ledger == {"payment_intents": [], "charges": [], "refunds": []}


def test_order_transaction_retry_recovers_before_payment(
    test_function_name: str,
    floci_container: DockerContainer,
    sfn_client: SFNClient,
    dynamodb_client: DynamoDBClient,
    sqs_client: SQSClient,
    merchant_queue: str,
    create_order_machine: str,
    orders_table: str,
    stock_table: str,
    stripe_api: httpx.Client,
    fault_lua: Path,
    fault_proxy_container: DockerContainer,
) -> None:
    """Retry transient DynamoDB failures and reserve stock once before charging."""

    # Declare the request this caller submits to the public workflow interface.
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }

    # Fail two stock transactions, then let subsequent requests reach DynamoDB.
    fault_lua.write_text(
        dedent("""\
            local remaining = 2
            return function()
                if ngx.get_phase() ~= "access" then return end
                local target = ngx.req.get_headers()["x-amz-target"]
                if target == "DynamoDB_20120810.TransactWriteItems" and remaining > 0 then
                    remaining = remaining - 1
                    ngx.status = 500
                    ngx.header.content_type = "application/x-amz-json-1.0"
                    ngx.say([[{"__type":"InternalServerError","message":"Temporary DynamoDB failure"}]])
                    return ngx.exit(500)
                end
            end
        """)
    )
    # Load this scenario and wait for the proxy before starting the workflow.
    fault_proxy_container.get_wrapped_container().restart()
    HttpWaitStrategy(8080, "/health").for_status_code(200).wait_until_ready(
        fault_proxy_container
    )
    execution = sfn_client.start_execution(
        stateMachineArn=create_order_machine, input=json.dumps(order_input)
    )["executionArn"]

    # Wait for this queue's enqueue log, then receive its actual approval request.
    LogMessageWaitStrategy(
        re.compile(rf"Sent message \S+ to queue {re.escape(merchant_queue)}")
    ).with_startup_timeout(180).wait_until_ready(floci_container)
    messages = sqs_client.receive_message(
        QueueUrl=merchant_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20
    ).get("Messages", [])
    assert len(messages) == 1
    message = messages[0]
    assert "Body" in message and "ReceiptHandle" in message
    request = cast(dict[str, JsonValue], json.loads(message["Body"]))
    task_token = request["task_token"]
    assert isinstance(task_token, str)
    sfn_client.send_task_success(taskToken=task_token, output="{}")
    sqs_client.delete_message(
        QueueUrl=merchant_queue, ReceiptHandle=message["ReceiptHandle"]
    )

    # Wait for this execution's terminal log, then read its public status.
    LogMessageWaitStrategy(
        re.compile(rf"Execution {re.escape(execution)} completed with status \S+")
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    result = sfn_client.describe_execution(executionArn=execution)

    # Read every history page to inspect the actual state transitions.
    history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=execution, PaginationConfig={"PageSize": 5}
    ):
        history.extend(page["events"])

    # Read the committed order directly, bypassing the Orders Lambda.
    order_response = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in order_response
    order = order_response["Item"]
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock

    # Read Stripe's real payment records independently of the Lambda and proxy.
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

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "order-transaction-retry.json").write_text(
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

    assert result["status"] == "SUCCEEDED"
    assert [
        event.get("lambdaFunctionFailedEventDetails", {}).get("error")
        for event in history
        if event["type"] == "LambdaFunctionFailed"
    ] == ["InternalServerError", "InternalServerError"]
    assert sum(event["type"] == "LambdaFunctionScheduled" for event in history) == 6
    assert sum(event["type"] == "LambdaFunctionStarted" for event in history) == 6
    assert sum(event["type"] == "LambdaFunctionSucceeded" for event in history) == 4
    assert all(event["type"] != "ExecutionFailed" for event in history)
    assert [
        event["stateEnteredEventDetails"]["name"]
        for event in history
        if event["type"]
        in {"TaskStateEntered", "ChoiceStateEntered", "PassStateEntered"}
        and "stateEnteredEventDetails" in event
    ] == [
        "CreateOrder",
        "IsNewOrder",
        "AwaitMerchantApproval",
        "ReserveStock",
        "ChargePayment",
        "ConfirmOrder",
    ]
    assert order["status"] == {"S": "CONFIRMED"}
    assert stock["Item"]["available"] == {"N": "8"}
    assert len(ledger["payment_intents"]) == len(ledger["charges"]) == 1
    assert order["payment_id"] == {"S": ledger["payment_intents"][0]["id"]}
    assert ledger["payment_intents"][0]["status"] == "succeeded"
    assert ledger["refunds"] == []
