"""Declare stripe fixture values and own their resource cleanup."""

from collections.abc import Iterator

import httpx
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import HttpWaitStrategy


@pytest.fixture(scope="session")
def stripe_container(
    docker_network: Network, session_id: str
) -> Iterator[DockerContainer]:
    """Run localstripe's stateful Stripe API without a custom payment server."""
    container = (
        DockerContainer(
            "adrienverge/localstripe:1.15.10@sha256:d911ba3d9ac4ba68ae4f6630ab7816000cd0df7a75ced3e88fac551bb47ea875"
        )
        .with_kwargs(platform="linux/amd64")
        .with_name(f"localstripe-{session_id}")
        .with_network(docker_network)
        .with_exposed_ports(8420)
    )
    container.waiting_for(HttpWaitStrategy(8420, "/").for_status_code(401))
    with container:
        yield container


@pytest.fixture(scope="session")
def stripe_internal_url(stripe_container: DockerContainer) -> str:
    """Derive the Stripe API address reachable from sibling containers."""
    return f"http://{stripe_container.get_wrapped_container().name}:8420"


@pytest.fixture
def stripe_api(stripe_container: DockerContainer) -> Iterator[httpx.Client]:
    """Reset localstripe and read its real ledger independently of fault injection."""
    url = f"http://{stripe_container.get_container_host_ip()}:{stripe_container.get_exposed_port(8420)}"
    with httpx.Client(
        base_url=url, headers={"Authorization": "Bearer sk_test_demo"}, timeout=10
    ) as client:
        client.delete("/_config/data").raise_for_status()
        yield client
