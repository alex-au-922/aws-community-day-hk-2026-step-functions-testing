"""Own the optional MiniStack deployment without changing the Floci fixtures."""

import json
import shutil
import textwrap
from collections.abc import Iterator
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from docker import DockerClient
from mypy_boto3_dynamodb import DynamoDBClient
from mypy_boto3_lambda import LambdaClient
from mypy_boto3_sqs import SQSClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import HttpWaitStrategy

from tests.fixtures import ROLE, ROOT
from tests.fixtures.docker import docker_client as docker_client
from tests.fixtures.docker import docker_network as docker_network
from tests.fixtures.docker import session_id as session_id
from tests.fixtures.dynamodb import orders_table as orders_table
from tests.fixtures.dynamodb import stock_quantity as stock_quantity
from tests.fixtures.dynamodb import stock_table as stock_table
from tests.fixtures.floci import aws_session as aws_session
from tests.fixtures.stripe import stripe_api as stripe_api
from tests.fixtures.stripe import stripe_container as stripe_container
from tests.fixtures.stripe import stripe_internal_url as stripe_internal_url
from tests.fixtures.workflows import merchant_queue as merchant_queue


@pytest.fixture(scope="session")
def ministack_container(
    docker_network: Network, session_id: str
) -> Iterator[DockerContainer]:
    """Require real Docker Lambda runtimes; never fall back to host execution."""
    name = f"ministack-durable-{session_id}"
    container = (
        DockerContainer(
            "ministackorg/ministack:1.5.32@sha256:c3afdc0f5a5147477b0634cbe2d2ebb032079989a52c8a1e1e59aea7413feb78"
        )
        .with_name(name)
        .with_network(docker_network)
        .with_exposed_ports(4566)
        .with_volume_mapping("/var/run/docker.sock", "/var/run/docker.sock", "rw")
        .with_env("MINISTACK_HOSTNAME", name)
        .with_env("DOCKER_NETWORK", docker_network.name)
        .with_env("LAMBDA_EXECUTOR", "docker")
        .with_env("LAMBDA_STRICT", "1")
        .with_env("AWS_ENDPOINT_URL", f"http://{name}:4566")
    )
    container.waiting_for(
        HttpWaitStrategy(4566, "/_localstack/health").for_status_code(200)
    )
    with container:
        try:
            yield container
        finally:
            output = ROOT / "output/durable"
            output.mkdir(parents=True, exist_ok=True)
            stdout, stderr = container.get_logs()
            (output / "ministack.log").write_bytes(stdout + stderr)


@pytest.fixture(scope="session")
def ministack_endpoint(ministack_container: DockerContainer) -> str:
    return f"http://{ministack_container.get_container_host_ip()}:{ministack_container.get_exposed_port(4566)}"


@pytest.fixture(scope="session")
def dynamodb_client(
    aws_session: boto3.Session, ministack_endpoint: str
) -> DynamoDBClient:
    return aws_session.client("dynamodb", endpoint_url=ministack_endpoint)  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture(scope="session")
def sqs_client(aws_session: boto3.Session, ministack_endpoint: str) -> SQSClient:
    return aws_session.client(  # pyright: ignore[reportUnknownMemberType]
        "sqs", endpoint_url=ministack_endpoint, config=Config(read_timeout=25)
    )


@pytest.fixture(scope="session")
def lambda_client(aws_session: boto3.Session, ministack_endpoint: str) -> LambdaClient:
    return aws_session.client(  # pyright: ignore[reportUnknownMemberType]
        "lambda", endpoint_url=ministack_endpoint, config=Config(read_timeout=180)
    )


@pytest.fixture(scope="session")
def durable_image(
    tmp_path_factory: pytest.TempPathFactory, session_id: str
) -> Iterator[str]:
    """Package unchanged business clients and the official durable SDK together."""
    build = tmp_path_factory.mktemp("durable_image")
    (build / "Dockerfile").write_text(
        textwrap.dedent("""\
            FROM public.ecr.aws/lambda/python:3.14@sha256:fc4e15a5e09e2c9b6184810ea0fdc87afe1c1c734c98cbe27d739a35c3f15e7e AS builder
            COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv
            COPY pyproject.toml uv.lock /build/
            WORKDIR /build
            RUN uv export --frozen --no-dev --group durable --format requirements-txt --output-file /tmp/requirements.txt \
                && uv pip install --python /var/lang/bin/python3 --target /packages --require-hashes --requirements /tmp/requirements.txt

            FROM public.ecr.aws/lambda/python:3.14@sha256:fc4e15a5e09e2c9b6184810ea0fdc87afe1c1c734c98cbe27d739a35c3f15e7e
            COPY --from=builder /packages ${LAMBDA_TASK_ROOT}/
            COPY src /var/task/src
            COPY bonus /var/task/bonus
            CMD ["bonus.durable.workflow.handle"]
        """)
    )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, build / name)
    shutil.copytree(ROOT / "src", build / "src")
    shutil.copytree(
        ROOT / "bonus",
        build / "bonus",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    with DockerImage(build, tag=f"durable-orders:{session_id}") as image:
        yield str(image)


@pytest.fixture
def durable_function(
    aws_session: boto3.Session,
    lambda_client: LambdaClient,
    ministack_endpoint: str,
    ministack_container: DockerContainer,
    durable_image: str,
    orders_table: str,
    stock_table: str,
    merchant_queue: str,
    stripe_internal_url: str,
    docker_client: DockerClient,
) -> Iterator[str]:
    """Publish one function version; isolate MiniStack's Enabled wire extension here."""
    name = f"durable-order-{uuid4().hex}"
    internal = f"http://{ministack_container.get_wrapped_container().name}:4566"
    payload = json.dumps(
        {
            "FunctionName": name,
            "Role": ROLE,
            "PackageType": "Image",
            "Code": {"ImageUri": durable_image},
            "Timeout": 120,
            # MiniStack 1.5.32 requires Enabled, which AWS's boto3 model omits.
            "DurableConfig": {
                "Enabled": True,
                "ExecutionTimeout": 180,
                "RetentionPeriodInDays": 1,
            },
            "Environment": {
                "Variables": {
                    "AWS_ENDPOINT_URL": internal,
                    "AWS_MAX_ATTEMPTS": "1",
                    "ORDERS_TABLE_NAME": orders_table,
                    "ORDERS_STOCK_TABLE_NAME": stock_table,
                    "MERCHANT_QUEUE_URL": merchant_queue,
                    "PAYMENT_API_URL": stripe_internal_url,
                    "PAYMENT_API_KEY": "sk_test_demo",
                    "PAYMENT_TIMEOUT_SECONDS": "2",
                }
            },
        }
    )
    url = f"{ministack_endpoint}/2015-03-31/functions"
    request = AWSRequest(
        method="POST",
        url=url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    credentials = aws_session.get_credentials()
    assert credentials is not None
    SigV4Auth(credentials.get_frozen_credentials(), "lambda", "ap-east-1").add_auth(
        request
    )
    httpx.post(
        url, content=payload, headers=dict(request.headers), timeout=30
    ).raise_for_status()
    try:
        lambda_client.get_waiter("function_active_v2").wait(
            FunctionName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 30}
        )
        version = lambda_client.publish_version(FunctionName=name)["Version"]
        assert version != "$LATEST"
        yield f"{name}:{version}"
    finally:
        output = ROOT / "output/durable"
        output.mkdir(parents=True, exist_ok=True)
        try:
            for runtime in docker_client.containers.list(
                all=True, filters={"label": "ministack=lambda"}
            ):
                environment = runtime.attrs["Config"].get("Env", [])
                if f"AWS_LAMBDA_FUNCTION_NAME={name}" in environment:
                    (output / f"lambda-{runtime.short_id}.log").write_bytes(
                        runtime.logs()
                    )
        finally:
            lambda_client.delete_function(FunctionName=name)
