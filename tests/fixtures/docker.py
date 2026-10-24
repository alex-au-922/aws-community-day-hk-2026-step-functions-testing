"""Declare docker fixture values and own their resource cleanup."""

from collections.abc import Iterator
from uuid import uuid4

import docker
import pytest
from docker import DockerClient
from testcontainers.core.network import Network


@pytest.fixture(scope="session")
def docker_network() -> Iterator[Network]:
    """Own the isolated network used by the emulator and application containers."""
    with Network() as network:
        yield network


@pytest.fixture(scope="session")
def session_id() -> str:
    """Give this run a distinct Docker and AWS resource namespace."""
    return uuid4().hex[:12]


@pytest.fixture(scope="session")
def docker_client() -> Iterator[DockerClient]:
    """Use the selected Docker context for image and runtime operations."""
    client = docker.from_env()
    yield client
    client.close()
