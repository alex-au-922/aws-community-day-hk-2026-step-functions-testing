"""Exercise deployed workflows through AWS APIs and independent Stripe effects."""

import json
import re
from pathlib import Path
from typing import cast

import httpx
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_sqs import SQSClient
from mypy_boto3_stepfunctions.client import SFNClient
from mypy_boto3_stepfunctions.type_defs import HistoryEventTypeDef
from pydantic import JsonValue
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy


def test_create_cancel_and_replay(
    test_function_name: str,
    floci_container: DockerContainer,
    sfn_client: SFNClient,
    dynamodb_client: DynamoDBClient,
    sqs_client: SQSClient,
    merchant_queue: str,
    create_order_machine: str,
    cancel_order_machine: str,
    orders_table: str,
    stock_table: str,
    stripe_api: httpx.Client,
) -> None:
    """Charge after approval, refund on cancellation and replay stored progress."""

    # Declare the request this caller submits to the public workflow interface.
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }

    # Start an order that requires merchant approval before charging.
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
    assert {key: value for key, value in request.items() if key != "task_token"} == {
        key: value for key, value in order_input.items() if key != "payment_method_id"
    }
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

    # Check that pagination preserved unique event IDs and valid earlier links.
    seen = {0}
    for event in history:
        assert event["id"] not in seen
        assert event.get("previousEventId", 0) in seen
        seen.add(event["id"])

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
    evidence = {
        "create": {
            "execution": execution,
            "result": result,
            "history": history,
            "order": order,
            "stock": stock,
            "ledger": ledger,
        }
    }

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "create-cancel-and-replay.json").write_text(
        json.dumps(
            {"test_function_name": test_function_name, **evidence},
            indent=2,
            default=str,
        )
        + "\n"
    )

    # Approval must produce one confirmed order, one stock deduction and one charge.
    assert result["status"] == "SUCCEEDED"
    assert all(
        event["type"]
        not in {"LambdaFunctionFailed", "TaskFailed", "TaskTimedOut", "ExecutionFailed"}
        for event in history
    )
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
    assert len(ledger["charges"]) == 1
    assert len(ledger["payment_intents"]) == 1
    assert order["payment_id"] == {"S": ledger["payment_intents"][0]["id"]}
    assert ledger["payment_intents"][0]["status"] == "succeeded"
    assert ledger["payment_intents"][0]["amount"] == 12500
    assert ledger["payment_intents"][0]["currency"] == "hkd"
    assert ledger["payment_intents"][0]["metadata"] == {"order_id": "order-001"}
    assert order["payment_id"] == {"S": ledger["charges"][0]["payment_intent"]}
    assert ledger["charges"][0]["paid"] is True
    assert ledger["refunds"] == []

    # Replay creation to verify that it returns the existing order unchanged.
    replay = sfn_client.start_execution(
        stateMachineArn=create_order_machine, input=json.dumps(order_input)
    )["executionArn"]

    # Wait for this execution's terminal log, then read its public status.
    LogMessageWaitStrategy(
        re.compile(rf"Execution {re.escape(replay)} completed with status \S+")
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    replay_result = sfn_client.describe_execution(executionArn=replay)

    # Read every history page to inspect the actual state transitions.
    replay_history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=replay, PaginationConfig={"PageSize": 5}
    ):
        replay_history.extend(page["events"])

    # Read the committed order directly, bypassing the Orders Lambda.
    replay_order_response = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in replay_order_response
    replay_order = replay_order_response["Item"]
    replay_stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in replay_stock

    # Read Stripe's real payment records independently of the Lambda and proxy.
    replay_ledger_payment_intents = stripe_api.get(
        "/v1/payment_intents", params={"limit": 100}
    )
    replay_ledger_payment_intents.raise_for_status()
    replay_ledger_charges = stripe_api.get("/v1/charges", params={"limit": 100})
    replay_ledger_charges.raise_for_status()
    replay_ledger_refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    replay_ledger_refunds.raise_for_status()
    replay_ledger = {
        "payment_intents": replay_ledger_payment_intents.json()["data"],
        "charges": replay_ledger_charges.json()["data"],
        "refunds": replay_ledger_refunds.json()["data"],
    }
    evidence["replay"] = {
        "execution": replay,
        "result": replay_result,
        "history": replay_history,
        "order": replay_order,
        "stock": replay_stock,
        "ledger": replay_ledger,
    }

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "create-cancel-and-replay.json").write_text(
        json.dumps(
            {"test_function_name": test_function_name, **evidence},
            indent=2,
            default=str,
        )
        + "\n"
    )

    assert replay_result["status"] == "SUCCEEDED"
    assert all(
        event["type"]
        not in {"LambdaFunctionFailed", "TaskFailed", "TaskTimedOut", "ExecutionFailed"}
        for event in replay_history
    )
    assert [
        event["stateEnteredEventDetails"]["name"]
        for event in replay_history
        if event["type"]
        in {"TaskStateEntered", "ChoiceStateEntered", "PassStateEntered"}
        and "stateEnteredEventDetails" in event
    ] == [
        "CreateOrder",
        "IsNewOrder",
        "ReturnExistingOrder",
    ]
    assert replay_order == order
    assert replay_stock["Item"]["available"] == {"N": "8"}
    assert replay_ledger == ledger

    # Cancel the confirmed order, refund its payment and restore stock.
    cancellation = sfn_client.start_execution(
        stateMachineArn=cancel_order_machine,
        input=json.dumps({"order_id": "order-001"}),
    )["executionArn"]

    # Wait for this execution's terminal log, then read its public status.
    LogMessageWaitStrategy(
        re.compile(rf"Execution {re.escape(cancellation)} completed with status \S+")
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    cancellation_result = sfn_client.describe_execution(executionArn=cancellation)

    # Read every history page to inspect the actual state transitions.
    cancellation_history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=cancellation, PaginationConfig={"PageSize": 5}
    ):
        cancellation_history.extend(page["events"])

    # Read the committed order directly, bypassing the Orders Lambda.
    cancelled_response = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in cancelled_response
    cancelled = cancelled_response["Item"]
    restored_stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in restored_stock

    # Read Stripe's real payment records independently of the Lambda and proxy.
    refunded_payment_intents = stripe_api.get(
        "/v1/payment_intents", params={"limit": 100}
    )
    refunded_payment_intents.raise_for_status()
    refunded_charges = stripe_api.get("/v1/charges", params={"limit": 100})
    refunded_charges.raise_for_status()
    refunded_refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    refunded_refunds.raise_for_status()
    refunded = {
        "payment_intents": refunded_payment_intents.json()["data"],
        "charges": refunded_charges.json()["data"],
        "refunds": refunded_refunds.json()["data"],
    }
    evidence["cancel"] = {
        "execution": cancellation,
        "result": cancellation_result,
        "history": cancellation_history,
        "order": cancelled,
        "stock": restored_stock,
        "ledger": refunded,
    }

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "create-cancel-and-replay.json").write_text(
        json.dumps(
            {"test_function_name": test_function_name, **evidence},
            indent=2,
            default=str,
        )
        + "\n"
    )

    assert cancellation_result["status"] == "SUCCEEDED"
    assert all(
        event["type"]
        not in {"LambdaFunctionFailed", "TaskFailed", "TaskTimedOut", "ExecutionFailed"}
        for event in cancellation_history
    )
    assert [
        event["stateEnteredEventDetails"]["name"]
        for event in cancellation_history
        if event["type"]
        in {"TaskStateEntered", "ChoiceStateEntered", "PassStateEntered"}
        and "stateEnteredEventDetails" in event
    ] == [
        "GetOrder",
        "IsOrderCancelled",
        "RefundPayment",
        "RecordRefund",
    ]
    assert cancelled["status"] == {"S": "CANCELLED"}
    assert restored_stock["Item"]["available"] == {"N": "10"}
    assert len(refunded["charges"]) == len(refunded["refunds"]) == 1
    assert cancelled["refund_id"] == {"S": refunded["refunds"][0]["id"]}
    assert order["payment_id"] == {"S": refunded["refunds"][0]["payment_intent"]}
    assert refunded["refunds"][0]["status"] == "succeeded"
    assert refunded["refunds"][0]["amount"] == 12500

    # Replay cancellation to verify that it cannot issue another refund.
    repeated_cancellation = sfn_client.start_execution(
        stateMachineArn=cancel_order_machine,
        input=json.dumps({"order_id": "order-001"}),
    )["executionArn"]

    # Wait for this execution's terminal log, then read its public status.
    LogMessageWaitStrategy(
        re.compile(
            rf"Execution {re.escape(repeated_cancellation)} completed with status \S+"
        )
    ).with_startup_timeout(45).wait_until_ready(floci_container)
    repeated_result = sfn_client.describe_execution(executionArn=repeated_cancellation)

    # Read every history page to inspect the actual state transitions.
    repeated_history: list[HistoryEventTypeDef] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(
        executionArn=repeated_cancellation, PaginationConfig={"PageSize": 5}
    ):
        repeated_history.extend(page["events"])

    # Read the committed order directly, bypassing the Orders Lambda.
    repeated_order_response = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in repeated_order_response
    repeated_order = repeated_order_response["Item"]
    repeated_stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in repeated_stock

    # Read Stripe's real payment records independently of the Lambda and proxy.
    repeated_ledger_payment_intents = stripe_api.get(
        "/v1/payment_intents", params={"limit": 100}
    )
    repeated_ledger_payment_intents.raise_for_status()
    repeated_ledger_charges = stripe_api.get("/v1/charges", params={"limit": 100})
    repeated_ledger_charges.raise_for_status()
    repeated_ledger_refunds = stripe_api.get("/v1/refunds", params={"limit": 100})
    repeated_ledger_refunds.raise_for_status()
    repeated_ledger = {
        "payment_intents": repeated_ledger_payment_intents.json()["data"],
        "charges": repeated_ledger_charges.json()["data"],
        "refunds": repeated_ledger_refunds.json()["data"],
    }
    evidence["repeated_cancel"] = {
        "execution": repeated_cancellation,
        "result": repeated_result,
        "history": repeated_history,
        "order": repeated_order,
        "stock": repeated_stock,
        "ledger": repeated_ledger,
    }

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "create-cancel-and-replay.json").write_text(
        json.dumps(
            {"test_function_name": test_function_name, **evidence},
            indent=2,
            default=str,
        )
        + "\n"
    )

    assert repeated_result["status"] == "SUCCEEDED"
    assert all(
        event["type"]
        not in {"LambdaFunctionFailed", "TaskFailed", "TaskTimedOut", "ExecutionFailed"}
        for event in repeated_history
    )
    assert [
        event["stateEnteredEventDetails"]["name"]
        for event in repeated_history
        if event["type"]
        in {"TaskStateEntered", "ChoiceStateEntered", "PassStateEntered"}
        and "stateEnteredEventDetails" in event
    ] == [
        "GetOrder",
        "IsOrderCancelled",
        "ReturnExistingOrder",
    ]
    assert repeated_order == cancelled
    assert repeated_stock["Item"]["available"] == {"N": "10"}
    assert repeated_ledger == refunded
