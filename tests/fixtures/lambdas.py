"""Declare lambdas fixture values and own their resource cleanup."""

import io
import shutil
import tarfile
import textwrap
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from docker import DockerClient
from mypy_boto3_lambda import LambdaClient
from testcontainers.core.image import DockerImage

from tests.fixtures import ROLE, ROOT


@pytest.fixture(scope="session")
def lambda_image(
    session_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """Build one instrumented Lambda image from an inline Dockerfile."""
    build_dir = tmp_path_factory.mktemp("lambda_build")
    (build_dir / "Dockerfile").write_text(
        textwrap.dedent(r"""
            FROM public.ecr.aws/lambda/python:3.14@sha256:fc4e15a5e09e2c9b6184810ea0fdc87afe1c1c734c98cbe27d739a35c3f15e7e AS base

            FROM base AS builder
            COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv
            COPY pyproject.toml uv.lock /build/
            WORKDIR /build
            RUN uv export --frozen --no-dev --group runtime --format requirements-txt --output-file /tmp/requirements.txt \
                && uv pip install --python /var/lang/bin/python3 --target /packages --require-hashes --requirements /tmp/requirements.txt

            FROM base
            COPY --from=builder /packages ${LAMBDA_TASK_ROOT}/
            COPY pyproject.toml /build/pyproject.toml
            ENV AWS_EXECUTION_ENV=AWS_Lambda_python3.14 \
                COVERAGE_FILE=/tmp/coverage/.coverage
            COPY src /var/task/src
            WORKDIR /var/task
            ENTRYPOINT ["/var/lang/bin/python3", "-m", "coverage", "run", "--rcfile=/build/pyproject.toml", "--save-signal=USR1", "-m", "awslambdaric"]
        """).strip()
        + "\n"
    )
    shutil.copytree(ROOT / "src", build_dir / "src")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, build_dir / name)
    with DockerImage(build_dir, tag=f"order-lambdas:{session_id}") as image:
        yield str(image)


def _collect_coverage(client: DockerClient, function_name: str) -> None:
    """Signal native coverage and copy only after its completed file appears."""
    for container in client.containers.list(
        filters={
            "label": [
                "io.floci.service=lambda",
                f"io.floci.resource-id={function_name}",
            ]
        }
    ):
        container.kill(signal="SIGUSR1")
        # Coverage 7.16.2 closes the database before adding its final hash suffix.
        result = container.exec_run(
            [
                "timeout",
                "10",
                "sh",
                "-c",
                "until ls /tmp/coverage/.coverage.*.H*h >/dev/null 2>&1; do sleep 0.05; done",
            ]
        )
        assert result.exit_code == 0, container.logs().decode()
        archive, _ = container.get_archive("/tmp/coverage")
        with tarfile.open(fileobj=io.BytesIO(b"".join(archive))) as saved:
            for member in saved.getmembers():
                if member.isfile() and Path(member.name).name.startswith(".coverage."):
                    content = saved.extractfile(member)
                    assert content is not None
                    target = (
                        ROOT
                        / "output/coverage"
                        / f"{Path(member.name).name}.{container.short_id}"
                    )
                    target.write_bytes(content.read())


@pytest.fixture
def orders_function(
    lambda_client: LambdaClient,
    lambda_image: str,
    orders_table: str,
    stock_table: str,
    floci_internal_url: str,
    fault_proxy_internal_url: str,
    docker_client: DockerClient,
    test_function_name: str,
) -> Iterator[str]:
    """Register Orders with real SDK routing and one pytest coverage context."""
    name = f"orders-{uuid4().hex}"
    result = lambda_client.create_function(
        FunctionName=name,
        Role=ROLE,
        PackageType="Image",
        Code={"ImageUri": lambda_image},
        ImageConfig={"Command": ["src.lambdas.orders.handler.handle"]},
        Timeout=20,
        Environment={
            "Variables": {
                "ORDERS_TABLE_NAME": orders_table,
                "ORDERS_STOCK_TABLE_NAME": stock_table,
                "AWS_ENDPOINT_URL": floci_internal_url,
                "HTTP_PROXY": fault_proxy_internal_url,
                "AWS_MAX_ATTEMPTS": "1",
                "COVERAGE_CONTEXT": test_function_name,
            }
        },
    )
    yield result["FunctionArn"]
    try:
        _collect_coverage(docker_client, name)
    finally:
        lambda_client.delete_function(FunctionName=name)


@pytest.fixture
def payments_function(
    lambda_client: LambdaClient,
    lambda_image: str,
    stripe_internal_url: str,
    fault_proxy_internal_url: str,
    docker_client: DockerClient,
    test_function_name: str,
) -> Iterator[str]:
    """Register Payments from the same image with its own settings and context."""
    name = f"payments-{uuid4().hex}"
    result = lambda_client.create_function(
        FunctionName=name,
        Role=ROLE,
        PackageType="Image",
        Code={"ImageUri": lambda_image},
        ImageConfig={"Command": ["src.lambdas.payments.handler.handle"]},
        Timeout=20,
        Environment={
            "Variables": {
                "PAYMENT_API_URL": stripe_internal_url,
                "HTTP_PROXY": fault_proxy_internal_url,
                "PAYMENT_API_KEY": "sk_test_demo",
                "PAYMENT_TIMEOUT_SECONDS": "2",
                "COVERAGE_CONTEXT": test_function_name,
            }
        },
    )
    yield result["FunctionArn"]
    try:
        _collect_coverage(docker_client, name)
    finally:
        lambda_client.delete_function(FunctionName=name)
