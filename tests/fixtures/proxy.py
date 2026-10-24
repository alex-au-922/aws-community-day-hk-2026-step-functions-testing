"""Declare HTTP proxy fixture values and own their resource cleanup."""

import textwrap
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import HttpWaitStrategy


@pytest.fixture(scope="session")
def fault_proxy_image(
    session_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """Build OpenResty from inline Dockerfile and nginx configuration text."""
    build_dir = tmp_path_factory.mktemp("fault_proxy_build")
    (build_dir / "Dockerfile").write_text(
        textwrap.dedent("""\
            FROM openresty/openresty:1.29.2.5-1-alpine@sha256:57ff12584886d4bdcd415b52580ef194d06f361ebaeb07f9155b1af8ac957cf6

            COPY nginx.conf /usr/local/openresty/nginx/conf/nginx.conf
            EXPOSE 8080
        """)
    )
    (build_dir / "nginx.conf").write_text(
        textwrap.dedent("""\
            # Each test owns one worker and its mounted Lua script.
            worker_processes 1;
            error_log /dev/stderr warn;

            events {}

            http {
                lua_package_path "/faults/?.lua;;";
                resolver 127.0.0.11 ipv6=off;
                log_format requests escape=json '{"method":"$request_method","path":"$uri"}';
                access_log /dev/stdout requests;

                server {
                    listen 8080;

                    location = /health {
                        return 200 "ok";
                    }

                    location / {
                        proxy_pass http://$http_host$request_uri;
                        proxy_http_version 1.1;
                        proxy_set_header Host $http_host;
                        proxy_set_header Connection "";
                        proxy_next_upstream off;

                        access_by_lua_block { require("fault")() }
                        header_filter_by_lua_block { require("fault")() }
                        body_filter_by_lua_block { require("fault")() }
                    }
                }
            }
        """)
    )
    with DockerImage(build_dir, tag=f"order-fault-proxy:{session_id}") as image:
        yield str(image)


@pytest.fixture
def fault_lua(tmp_path: Path) -> Path:
    """Provide this test's mounted Lua file, initially forwarding every request."""
    tmp_path.chmod(0o755)
    path = tmp_path / "fault.lua"
    path.write_text("return function() end\n")
    return path


@pytest.fixture
def fault_proxy_container(
    fault_proxy_image: str,
    fault_lua: Path,
    docker_network: Network,
    session_id: str,
) -> Iterator[DockerContainer]:
    """Own this test's HTTP forward proxy and mounted fault script."""
    container = (
        DockerContainer(fault_proxy_image)
        .with_name(f"fault-proxy-{session_id}-{uuid4().hex[:8]}")
        .with_network(docker_network)
        .with_exposed_ports(8080)
        .with_volume_mapping(str(fault_lua.parent), "/faults", "ro")
    )
    container.waiting_for(HttpWaitStrategy(8080, "/health").for_status_code(200))
    with container:
        yield container


@pytest.fixture
def fault_proxy_internal_url(fault_proxy_container: DockerContainer) -> str:
    """Derive the HTTP proxy address reachable by Lambda runtimes."""
    return f"http://{fault_proxy_container.get_wrapped_container().name}:8080"
