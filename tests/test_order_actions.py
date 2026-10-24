"""Exercise order action contracts through deployed Lambdas and real DynamoDB."""

import json
from pathlib import Path
from textwrap import dedent
from typing import cast

import pytest
from boto3.dynamodb.types import TypeDeserializer
from mypy_boto3_dynamodb import DynamoDBClient
from mypy_boto3_lambda import LambdaClient
from pydantic import JsonValue
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy


def test_changed_order_details_are_rejected(
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
) -> None:
    """An existing order ID cannot be reused with different payment details."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Create the order, then reuse its ID with a different payment amount.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    created = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {"action": "create_order", **order_details, "amount_minor": 1}
        ),
    )
    changed = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Read DynamoDB directly to verify the rejected request changed no fields.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    deserializer = TypeDeserializer()
    assert changed["errorType"] == "OrderConflict"
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == created


def test_missing_order_cannot_be_cancelled(
    lambda_client: LambdaClient, orders_function: str
) -> None:
    """Cancellation lookup reports a missing order without creating one."""
    # Ask the deployed Lambda for an order that has never been created.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "get_order", "order_id": "missing-order"}),
    )
    result = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    assert result["errorType"] == "OrderNotFound"


def test_pending_order_cannot_start_refund(
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
) -> None:
    """A cancellation lookup rejects an order that has not confirmed payment."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Create a pending order without reserving stock or confirming payment.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    created = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "get_order", "order_id": "order-001"}),
    )
    result = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # A failed cancellation lookup must preserve the entire pending record.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    deserializer = TypeDeserializer()
    assert result["errorType"] == "OrderNotCancellable"
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == created


def test_reservation_and_release_replay_move_stock_once(
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stock_quantity: int,
) -> None:
    """Replaying committed reservation and release snapshots cannot move stock twice."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Reserve stock, then replay the same pending snapshot as a retried task would.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    pending = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "reserve_stock", "order": pending}),
    )
    reserved = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "reserve_stock", "order": pending}),
    )
    reserved_again = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Both calls must describe one committed reservation and one stock deduction.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock
    deserializer = TypeDeserializer()
    assert reserved["status"] == "RESERVED"
    assert reserved_again == reserved
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == reserved
    assert stock["Item"]["available"] == {"N": str(stock_quantity - 2)}

    # Replay release with the original reserved snapshot to check compensation.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "release_stock", "order": reserved}),
    )
    cancelled = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "release_stock", "order": reserved}),
    )
    cancelled_again = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Repeated compensation must return the same order and restore stock once.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock
    assert cancelled["status"] == "CANCELLED"
    assert cancelled_again == cancelled
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == cancelled
    assert stock["Item"]["available"] == {"N": str(stock_quantity)}


def test_confirmation_and_refund_replay_restore_stock_once(
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stock_quantity: int,
) -> None:
    """The original confirmed snapshot safely records the same refund more than once."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Prepare a reservation, then confirm payment twice with the same snapshot.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    pending = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "reserve_stock", "order": pending}),
    )
    reserved = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {"action": "confirm_order", "order": reserved, "payment_id": "pay-001"}
        ),
    )
    confirmed = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {"action": "confirm_order", "order": reserved, "payment_id": "pay-001"}
        ),
    )
    confirmed_again = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "get_order", "order_id": "order-001"}),
    )
    current = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock

    assert confirmed["status"] == "CONFIRMED"
    assert confirmed_again == current == confirmed
    assert stock["Item"]["available"] == {"N": str(stock_quantity - 2)}

    # Record the same refund twice using the original confirmed order snapshot.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {"action": "record_refund", "order": confirmed, "refund_id": "refund-001"}
        ),
    )
    cancelled = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {"action": "record_refund", "order": confirmed, "refund_id": "refund-001"}
        ),
    )
    cancelled_again = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # The stored refund identifiers and stock must agree with one cancellation.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock
    deserializer = TypeDeserializer()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["payment_id"] == "pay-001"
    assert cancelled["refund_id"] == "refund-001"
    assert cancelled_again == cancelled
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == cancelled
    assert stock["Item"]["available"] == {"N": str(stock_quantity)}


@pytest.mark.parametrize("action", ["reserve_stock", "release_stock"])
def test_stale_reservation_actions_leave_confirmed_order_unchanged(
    action: str,
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stock_quantity: int,
) -> None:
    """A delayed reservation action cannot alter a payment-confirmed order."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Advance the order beyond reservation, retaining the earlier task snapshots.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    pending = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "reserve_stock", "order": pending}),
    )
    reserved = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {"action": "confirm_order", "order": reserved, "payment_id": "pay-001"}
        ),
    )
    confirmed = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Submit the stale reservation or release event after payment confirmation.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps(
            {
                "action": action,
                "order": pending if action == "reserve_stock" else reserved,
            }
        ),
    )
    result = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock

    # Conditional writes must protect both the confirmed order and its reservation.
    deserializer = TypeDeserializer()
    assert result["errorType"] == "TransactionCanceledException"
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == confirmed
    assert stock["Item"]["available"] == {"N": str(stock_quantity - 2)}


def test_deleted_order_cannot_reserve_stock(
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stock_quantity: int,
) -> None:
    """A reservation whose order disappeared fails without changing stock."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Retain a pending snapshot, then remove its order through the public database API.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    pending = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    dynamodb_client.delete_item(
        TableName=orders_table, Key={"order_id": {"S": "order-001"}}
    )

    # Attempt the stale reservation and inspect both records directly afterward.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "reserve_stock", "order": pending}),
    )
    result = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock

    assert result["errorType"] == "TransactionCanceledException"
    assert not stored.get("Item")
    assert stock["Item"]["available"] == {"N": str(stock_quantity)}


def test_transaction_error_without_item_reasons_is_preserved(
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stock_quantity: int,
    fault_lua: Path,
    fault_proxy_container: DockerContainer,
) -> None:
    """Opaque SDK cancellation diagnostics propagate without becoming a parser error."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    pending = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Return a real DynamoDB error shape without optional per-item diagnostics.
    fault_lua.write_text(
        dedent("""\
            return function()
                local target = ngx.req.get_headers()["x-amz-target"]
                if ngx.get_phase() == "access"
                    and target == "DynamoDB_20120810.TransactWriteItems" then
                    ngx.status = 400
                    ngx.header.content_type = "application/x-amz-json-1.0"
                    ngx.say([[{
                        "__type": "TransactionCanceledException",
                        "message": "Transaction cancelled without item details"
                    }]])
                    return ngx.exit(400)
                end
            end
        """)
    )
    fault_proxy_container.get_wrapped_container().restart()
    HttpWaitStrategy(8080, "/health").for_status_code(200).wait_until_ready(
        fault_proxy_container
    )

    # Invoke the reservation only after the proxy has loaded this test's Lua.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "reserve_stock", "order": pending}),
    )
    result = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Preserve the SDK error and verify that the failed transaction changed nothing.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock
    deserializer = TypeDeserializer()
    assert result["errorType"] == "TransactionCanceledException"
    assert "without item details" in str(result["errorMessage"])
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == pending
    assert stock["Item"]["available"] == {"N": str(stock_quantity)}


@pytest.mark.parametrize("action", ["reserve_stock", "release_stock", "record_refund"])
def test_lost_transaction_response_can_replay_without_moving_stock_twice(
    action: str,
    lambda_client: LambdaClient,
    orders_function: str,
    dynamodb_client: DynamoDBClient,
    orders_table: str,
    stock_table: str,
    stock_quantity: int,
    fault_lua: Path,
    fault_proxy_container: DockerContainer,
) -> None:
    """A committed transaction with a lost response is recovered by the same action."""
    # Define the order used in this scenario.
    order_details = {
        "order_id": "order-001",
        "sku": "product-001",
        "quantity": 2,
        "amount_minor": 12500,
        "currency": "HKD",
    }

    # Reach the source state required by the transaction under test.
    response = lambda_client.invoke(
        FunctionName=orders_function,
        Payload=json.dumps({"action": "create_order", **order_details}),
    )
    snapshot = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    if action != "reserve_stock":
        response = lambda_client.invoke(
            FunctionName=orders_function,
            Payload=json.dumps({"action": "reserve_stock", "order": snapshot}),
        )
        snapshot = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    if action == "record_refund":
        response = lambda_client.invoke(
            FunctionName=orders_function,
            Payload=json.dumps(
                {"action": "confirm_order", "order": snapshot, "payment_id": "pay-001"}
            ),
        )
        snapshot = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    event: dict[str, object] = {"action": action, "order": snapshot}
    if action == "record_refund":
        event["refund_id"] = "refund-001"

    # Let DynamoDB commit, then replace its acknowledgement with an HTTP 500 error.
    fault_lua.write_text(
        dedent("""\
            local lost = false
            return function()
                local phase = ngx.get_phase()
                local target = ngx.req.get_headers()["x-amz-target"]
                if phase == "header_filter" and not lost
                    and target == "DynamoDB_20120810.TransactWriteItems"
                    and ngx.status >= 200 and ngx.status < 300 then
                    lost = true
                    ngx.ctx.replace_response = true
                    ngx.status = 500
                    ngx.header.content_type = "application/x-amz-json-1.0"
                    ngx.header.content_length = nil
                elseif phase == "body_filter" and ngx.ctx.replace_response then
                    ngx.arg[1] = ngx.arg[2] and [[{
                        "__type": "InternalServerError",
                        "message": "Transaction committed; acknowledgement lost"
                    }]] or nil
                end
            end
        """)
    )
    fault_proxy_container.get_wrapped_container().restart()
    HttpWaitStrategy(8080, "/health").for_status_code(200).wait_until_ready(
        fault_proxy_container
    )

    # Invoke the target transaction after its setup has completed without faults.
    response = lambda_client.invoke(
        FunctionName=orders_function, Payload=json.dumps(event)
    )
    failed = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))

    # Bypass the fault proxy to observe the transaction's actual committed outcome.
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    deserializer = TypeDeserializer()
    committed = {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    }
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock
    expected_quantity = (
        stock_quantity - 2 if action == "reserve_stock" else stock_quantity
    )
    assert failed["errorType"] == "InternalServerError"
    assert committed["status"] == (
        "RESERVED" if action == "reserve_stock" else "CANCELLED"
    )
    assert stock["Item"]["available"] == {"N": str(expected_quantity)}

    # Replay the identical event after the acknowledgement loss.
    response = lambda_client.invoke(
        FunctionName=orders_function, Payload=json.dumps(event)
    )
    replayed = cast(dict[str, JsonValue], json.loads(response["Payload"].read()))
    stored = dynamodb_client.get_item(
        TableName=orders_table,
        Key={"order_id": {"S": "order-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stored
    stock = dynamodb_client.get_item(
        TableName=stock_table,
        Key={"sku": {"S": "product-001"}},
        ConsistentRead=True,
    )
    assert "Item" in stock

    # The replay must return the committed record without moving stock again.
    assert replayed == committed
    assert {
        name: deserializer.deserialize(value) for name, value in stored["Item"].items()
    } == committed
    assert stock["Item"]["available"] == {"N": str(expected_quantity)}
