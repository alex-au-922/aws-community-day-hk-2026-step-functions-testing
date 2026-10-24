"""Declare DynamoDB table fixture values and own their resource cleanup."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from mypy_boto3_dynamodb import DynamoDBClient


@pytest.fixture
def orders_table(dynamodb_client: DynamoDBClient) -> Iterator[str]:
    """Own an isolated order table for this test's workflow executions."""
    name = f"orders-{uuid4().hex}"
    dynamodb_client.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    yield name
    dynamodb_client.delete_table(TableName=name)


@pytest.fixture
def stock_quantity() -> int:
    """Start with ten units unless a scenario overrides the table's availability."""
    return 10


@pytest.fixture
def stock_table(dynamodb_client: DynamoDBClient, stock_quantity: int) -> Iterator[str]:
    """Own a stock table with the scenario's declared initial availability."""
    name = f"stock-{uuid4().hex}"
    dynamodb_client.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "sku", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "sku", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.put_item(
        TableName=name,
        Item={"sku": {"S": "product-001"}, "available": {"N": str(stock_quantity)}},
    )
    yield name
    dynamodb_client.delete_table(TableName=name)
