"""Declare workflows fixture values and own their resource cleanup."""

import json
from collections.abc import Iterator
from typing import cast
from uuid import uuid4

import pytest
from mypy_boto3_sqs import SQSClient
from mypy_boto3_stepfunctions import SFNClient

from tests.fixtures import ROLE, ROOT


@pytest.fixture
def test_function_name(request: pytest.FixtureRequest) -> str:
    """Use the test function name for coverage and saved workflow evidence."""
    # Pytest leaves node untyped; a function-scoped request belongs to an Item.
    return cast(pytest.Item, request.node).name  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture
def approval_timeout() -> int:
    """Give merchant approval thirty seconds unless a scenario overrides it."""
    return 30


@pytest.fixture
def merchant_queue(sqs_client: SQSClient) -> Iterator[str]:
    """Own the approval queue and its delivery visibility boundary."""
    url = sqs_client.create_queue(
        QueueName=f"approval-{uuid4().hex}", Attributes={"VisibilityTimeout": "1"}
    )["QueueUrl"]
    yield url
    sqs_client.delete_queue(QueueUrl=url)


@pytest.fixture
def create_order_machine(
    sfn_client: SFNClient,
    orders_function: str,
    payments_function: str,
    merchant_queue: str,
    approval_timeout: int,
) -> Iterator[str]:
    """Deploy the real create definition, substituting resources and test deadline."""
    definition = (ROOT / "src/create_order.asl.json").read_text()
    definition = definition.replace("${OrdersFunctionArn}", orders_function)
    definition = definition.replace("${PaymentsFunctionArn}", payments_function)
    definition = definition.replace("${MerchantQueueUrl}", merchant_queue)
    parsed = json.loads(definition)
    parsed["States"]["AwaitMerchantApproval"]["TimeoutSeconds"] = approval_timeout
    arn = sfn_client.create_state_machine(
        name=f"create-{uuid4().hex}",
        definition=json.dumps(parsed),
        roleArn=ROLE,
        type="STANDARD",
    )["stateMachineArn"]
    yield arn

    # Stop this fixture's unfinished executions before deleting its state machine.
    for execution in sfn_client.list_executions(
        stateMachineArn=arn, statusFilter="RUNNING"
    )["executions"]:
        execution_arn = execution["executionArn"]
        sfn_client.stop_execution(
            executionArn=execution_arn, cause="Test resource teardown"
        )
        stopped = sfn_client.describe_execution(executionArn=execution_arn)
        assert stopped["status"] != "RUNNING"
    sfn_client.delete_state_machine(stateMachineArn=arn)


@pytest.fixture
def cancel_order_machine(
    sfn_client: SFNClient,
    orders_function: str,
    payments_function: str,
) -> Iterator[str]:
    """Deploy cancellation from its checked-in, flat ASL definition."""
    definition = (ROOT / "src/cancel_order.asl.json").read_text()
    definition = definition.replace("${OrdersFunctionArn}", orders_function)
    definition = definition.replace("${PaymentsFunctionArn}", payments_function)
    arn = sfn_client.create_state_machine(
        name=f"cancel-{uuid4().hex}",
        definition=definition,
        roleArn=ROLE,
        type="STANDARD",
    )["stateMachineArn"]
    yield arn

    # Cancellation owns the same explicit shutdown sequence for its executions.
    for execution in sfn_client.list_executions(
        stateMachineArn=arn, statusFilter="RUNNING"
    )["executions"]:
        execution_arn = execution["executionArn"]
        sfn_client.stop_execution(
            executionArn=execution_arn, cause="Test resource teardown"
        )
        stopped = sfn_client.describe_execution(executionArn=execution_arn)
        assert stopped["status"] != "RUNNING"
    sfn_client.delete_state_machine(stateMachineArn=arn)
