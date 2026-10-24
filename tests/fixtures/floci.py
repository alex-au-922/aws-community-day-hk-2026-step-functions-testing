"""Declare floci fixture values and own their resource cleanup."""

from collections.abc import Iterator

import boto3
import pytest
from botocore.config import Config
from mypy_boto3_dynamodb import DynamoDBClient
from mypy_boto3_lambda import LambdaClient
from mypy_boto3_sqs import SQSClient
from mypy_boto3_stepfunctions import SFNClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import HttpWaitStrategy


@pytest.fixture(scope="session")
def floci_container(
    docker_network: Network, session_id: str
) -> Iterator[DockerContainer]:
    """Run pinned Floci with access to Docker for its real Lambda runtimes."""
    name = f"floci-demo-{session_id}"
    container = (
        DockerContainer(
            "floci/floci:2.3.0@sha256:e0b292df4dc9559b7b1bf4f51bdda4bb8e269276236438cd03cae661260ea8a9"
        )
        .with_name(name)
        .with_network(docker_network)
        .with_exposed_ports(4566)
        .with_volume_mapping("/var/run/docker.sock", "/var/run/docker.sock", "rw")
        .with_env("FLOCI_HOSTNAME", name)
        .with_env("FLOCI_BASE_URL", f"http://{name}:4566")
        .with_env("FLOCI_SERVICES_DOCKER_NETWORK", docker_network.name)
        .with_env("FLOCI_SERVICES_LAMBDA_DOCKER_HOST_OVERRIDE", name)
        .with_env("FLOCI_SERVICES_LAMBDA_EPHEMERAL", "false")
        .with_env("FLOCI_DOCKER_RESOURCE_NAMESPACE", session_id)
        .with_env("FLOCI_DNS_CONTAINER_FALLBACK_ENABLED", "false")
        .with_env(
            "QUARKUS_LOG_CATEGORY__IO_GITHUB_HECTORVENT_FLOCI_SERVICES_SQS__LEVEL",
            "DEBUG",
        )
    )
    container.waiting_for(
        HttpWaitStrategy(4566, "/_localstack/health").for_status_code(200)
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def floci_endpoint(floci_container: DockerContainer) -> str:
    """Expose the actual published emulator address to host SDK clients."""
    return f"http://{floci_container.get_container_host_ip()}:{floci_container.get_exposed_port(4566)}"


@pytest.fixture(scope="session")
def floci_internal_url(floci_container: DockerContainer) -> str:
    """Derive the emulator address reachable from the Lambda network."""
    return f"http://{floci_container.get_wrapped_container().name}:4566"


@pytest.fixture(scope="session")
def aws_session() -> Iterator[boto3.Session]:
    """Supply explicit local credentials and region without consulting AWS profiles."""
    with pytest.MonkeyPatch.context() as environment:
        environment.delenv("AWS_PROFILE", raising=False)
        environment.delenv("AWS_DEFAULT_PROFILE", raising=False)
        yield boto3.Session(
            aws_access_key_id="test",
            aws_secret_access_key="test",
            region_name="ap-east-1",
        )


@pytest.fixture(scope="session")
def dynamodb_client(aws_session: boto3.Session, floci_endpoint: str) -> DynamoDBClient:
    """Connect host-side data assertions directly to the emulator."""
    return aws_session.client("dynamodb", endpoint_url=floci_endpoint)  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture(scope="session")
def sqs_client(aws_session: boto3.Session, floci_endpoint: str) -> SQSClient:
    """Connect queue setup and delivery assertions to the emulator."""
    return aws_session.client(  # pyright: ignore[reportUnknownMemberType]
        "sqs", endpoint_url=floci_endpoint, config=Config(read_timeout=25)
    )


@pytest.fixture(scope="session")
def lambda_client(aws_session: boto3.Session, floci_endpoint: str) -> LambdaClient:
    """Use the real Lambda HTTP API for deployment and direct action scenarios."""
    return aws_session.client("lambda", endpoint_url=floci_endpoint)  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture(scope="session")
def sfn_client(aws_session: boto3.Session, floci_endpoint: str) -> SFNClient:
    """Use Standard workflow start, describe, history and callback APIs."""
    return aws_session.client("stepfunctions", endpoint_url=floci_endpoint)  # pyright: ignore[reportUnknownMemberType]
