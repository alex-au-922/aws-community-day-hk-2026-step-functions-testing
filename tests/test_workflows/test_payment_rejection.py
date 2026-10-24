"""Exercise deployed workflows through AWS APIs and independent Stripe effects."""

import json
import re
from pathlib import Path
from textwrap import dedent
from typing import cast

import httpx
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_sqs import SQSClient
from mypy_boto3_stepfunctions.client import SFNClient
from mypy_boto3_stepfunctions.type_defs import HistoryEventTypeDef
from pydantic import JsonValue
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy, LogMessageWaitStrategy


def test_definitive_payment_rejection_releases_stock(
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
    """An injected card decline reaches Catch and releases reserved stock."""

    # Declare the request this caller submits to the public workflow interface.
    order_input = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
        "payment_method_id": "pm_card_visa",
    }

    # Inject a card decline so Catch can release the reserved stock.
    fault_lua.write_text(
        dedent("""\
            return function()
                if ngx.get_phase() ~= "access" or ngx.var.uri ~= "/v1/payment_intents" then
                    return
                end
                ngx.status = 402
                ngx.header.content_type = "application/json"
                ngx.say([[{"error":{"type":"card_error","code":"card_declined","message":"The card was declined"}}]])
                return ngx.exit(402)
            end
        """)
    )
    # Load the decline response and wait for the proxy before starting the workflow.
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
    # Wait for the completed proxy request, including a deliberately delayed reply.
    LogMessageWaitStrategy(
        re.compile(r'\{"method":"POST","path":"/v1/payment_intents"\}')
    ).with_startup_timeout(10).wait_until_ready(fault_proxy_container)
    requests = [
        json.loads(line) for line in fault_proxy_container.get_logs()[0].splitlines()
    ]
    attempts = requests.count({"method": "POST", "path": "/v1/payment_intents"})

    # Save the observed responses with this test function name for review.
    evidence_directory = Path("output/evidence")
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "payment-rejection.json").write_text(
        json.dumps(
            {
                "test_function_name": test_function_name,
                "execution": execution,
                "result": result,
                "history": history,
                "order": order,
                "stock": stock,
                "ledger": ledger,
                "payment_attempts": attempts,
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
    ] == ["PaymentRejected"]
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
        "ReleaseStock",
    ]
    assert order["status"] == {"S": "CANCELLED"}
    assert stock["Item"]["available"] == {"N": "10"}
    assert attempts == 1
    assert ledger == {"payment_intents": [], "charges": [], "refunds": []}
