"""Observe durable suspension and replay through deployed service interfaces."""

import json
import time
from typing import cast
from uuid import uuid4

import httpx
from docker import DockerClient
from mypy_boto3_dynamodb import DynamoDBClient
from mypy_boto3_lambda import LambdaClient
from mypy_boto3_lambda.type_defs import EventTypeDef
from mypy_boto3_sqs import SQSClient
from pydantic import JsonValue

from tests.fixtures import ROOT


def test_merchant_callback_resumes_checkpointed_order(
    lambda_client: LambdaClient,
    durable_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    sqs_client: SQSClient,
    merchant_queue: str,
    stripe_api: httpx.Client,
    docker_client: DockerClient,
) -> None:
    """Resume after approval without repeating completed creation or submission."""
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }
    started = lambda_client.invoke(
        FunctionName=durable_function,
        InvocationType="RequestResponse",
        DurableExecutionName=f"order-{uuid4().hex}",
        Payload=json.dumps(order_input).encode(),
    )
    # MiniStack 1.5.32 returns the suspended invocation's envelope immediately.
    initial_payload = json.loads(started["Payload"].read())
    assert initial_payload["Status"] == "PENDING", initial_payload
    execution = started["DurableExecutionArn"]
    messages = sqs_client.receive_message(
        QueueUrl=merchant_queue, MaxNumberOfMessages=1, WaitTimeSeconds=20
    ).get("Messages", [])
    assert len(messages) == 1
    message = messages[0]
    assert "Body" in message and "ReceiptHandle" in message
    request = cast(dict[str, JsonValue], json.loads(message["Body"]))
    callback_id = request["callback_id"]
    assert isinstance(callback_id, str)
    assert request["order_id"] == "order-001"
    sqs_client.delete_message(
        QueueUrl=merchant_queue, ReceiptHandle=message["ReceiptHandle"]
    )

    waiting = lambda_client.get_durable_execution(DurableExecutionArn=execution)
    assert waiting["Status"] == "RUNNING"
    pending_order = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    ).get("Item", {})
    pending_stock = dynamodb_client.get_item(
        TableName=stock_table, Key={"sku": {"S": "product-001"}}, ConsistentRead=True
    ).get("Item", {})
    assert pending_order["status"] == {"S": "PENDING"}
    assert pending_stock["available"] == {"N": "10"}
    charges_before = stripe_api.get("/v1/charges")
    charges_before.raise_for_status()
    assert charges_before.json()["data"] == []

    history_before: list[EventTypeDef] = []
    for page in lambda_client.get_paginator("get_durable_execution_history").paginate(
        DurableExecutionArn=execution,
        IncludeExecutionData=True,
        PaginationConfig={"PageSize": 3},
    ):
        assert "Events" in page
        history_before.extend(page["Events"])
    assert sum(e.get("EventType") == "InvocationCompleted" for e in history_before) == 1
    assert [
        e.get("Name") for e in history_before if e.get("EventType") == "StepSucceeded"
    ] == ["CreateOrder", "AwaitMerchantApproval submitter"]

    lambda_client.send_durable_execution_callback_success(
        CallbackId=callback_id, Result=b"{}"
    )
    deadline = time.monotonic() + 45
    while True:
        result = lambda_client.get_durable_execution(
            DurableExecutionArn=execution, IncludeExecutionData=True
        )
        if result["Status"] != "RUNNING":
            break
        assert time.monotonic() < deadline, result
        time.sleep(0.1)
    assert result["Status"] == "SUCCEEDED", result

    history: list[EventTypeDef] = []
    for page in lambda_client.get_paginator("get_durable_execution_history").paginate(
        DurableExecutionArn=execution,
        IncludeExecutionData=True,
        PaginationConfig={"PageSize": 3},
    ):
        assert "Events" in page
        history.extend(page["Events"])
    order = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    ).get("Item", {})
    stock = dynamodb_client.get_item(
        TableName=stock_table, Key={"sku": {"S": "product-001"}}, ConsistentRead=True
    ).get("Item", {})
    ledger: dict[str, JsonValue] = {}
    for kind in ("payment_intents", "charges", "refunds"):
        response = stripe_api.get(f"/v1/{kind}", params={"limit": 100})
        response.raise_for_status()
        ledger[kind] = response.json()["data"]
    output = ROOT / "output/durable"
    output.mkdir(parents=True, exist_ok=True)
    (output / "merchant-callback.json").write_text(
        json.dumps(
            {
                "test_function_name": "test_merchant_callback_resumes_checkpointed_order",
                "execution": execution,
                "initial_payload": initial_payload,
                "before_callback": {
                    "execution": waiting,
                    "history": history_before,
                    "order": pending_order,
                    "stock": pending_stock,
                    "charges": charges_before.json()["data"],
                },
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
    assert sum(e.get("EventType") == "InvocationCompleted" for e in history) >= 2
    expected_steps = [
        "CreateOrder",
        "AwaitMerchantApproval submitter",
        "ReserveStock",
        "ChargePayment",
        "ConfirmOrder",
    ]
    for event_type in ("StepStarted", "StepSucceeded"):
        assert [
            e.get("Name") for e in history if e.get("EventType") == event_type
        ] == expected_steps
    assert sum(e.get("EventType") == "CallbackSucceeded" for e in history) == 1
    assert order["status"] == {"S": "CONFIRMED"}
    assert stock["available"] == {"N": "8"}
    assert isinstance(ledger["charges"], list) and len(ledger["charges"]) == 1
    intents = ledger["payment_intents"]
    assert isinstance(intents, list) and len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, dict)
    assert intent["status"] == "succeeded"
    assert order["payment_id"] == {"S": intent["id"]}
    assert ledger["refunds"] == []
    returned = json.loads(result["Result"])
    assert returned["status"] == "CONFIRMED"
    assert returned["payment_id"] == intent["id"]
    assert (
        sqs_client.receive_message(QueueUrl=merchant_queue, WaitTimeSeconds=1).get(
            "Messages", []
        )
        == []
    )
    # Strict mode must have actually created a Docker Lambda environment.
    runtimes = docker_client.containers.list(filters={"label": "ministack=lambda"})
    assert any(
        f"AWS_LAMBDA_FUNCTION_NAME={durable_function.split(':')[0]}"
        in runtime.attrs["Config"].get("Env", [])
        for runtime in runtimes
    )
