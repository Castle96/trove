"""Unit tests for port-discovery rules (host/scheme/label derivation + trim)."""

from __future__ import annotations

from app.dockwatch.services.discovery import compute_hotlinks, derive_endpoint_host


# --------------------------------------------------------------------------- host
def test_derive_endpoint_host_tcp_url() -> None:
    assert derive_endpoint_host("tcp://1.2.3.4:2375") == "1.2.3.4"


def test_derive_endpoint_host_http_url() -> None:
    assert derive_endpoint_host("https://dock.example.com:9443") == "dock.example.com"


def test_derive_endpoint_host_ssh_url() -> None:
    assert derive_endpoint_host("ssh://admin@10.0.0.5:22") == "10.0.0.5"


def test_derive_endpoint_host_socket_is_localhost() -> None:
    assert derive_endpoint_host("unix:///var/run/docker.sock") == "localhost"


def test_derive_endpoint_host_missing_scheme() -> None:
    assert derive_endpoint_host("10.1.1.1:2375") == "10.1.1.1"


def test_derive_endpoint_host_empty_is_localhost() -> None:
    assert derive_endpoint_host("") == "localhost"


# --------------------------------------------------------------------------- hotlinks
WEB = {
    "id": "cont1",
    "short_id": "cont1",
    "name": "web",
    "image": "nginx:latest",
    "state": "running",
    "labels": {},
    "ports": [
        {"ContainerPort": "80/tcp", "HostIp": None, "HostPort": "8080"},
        {"ContainerPort": "443/tcp", "HostIp": "10.0.0.9", "HostPort": "443"},
        {"ContainerPort": "53/udp", "HostIp": None, "HostPort": None},
    ],
}


def test_compute_hotlinks_any_host_binding_uses_endpoint_host() -> None:
    links = compute_hotlinks("tcp://1.2.3.4:2375", WEB)
    by_port = {lnk["container_port"]: lnk for lnk in links}
    assert by_port["80/tcp"]["host"] == "1.2.3.4"
    assert by_port["80/tcp"]["url"] == "http://1.2.3.4:8080"
    assert by_port["80/tcp"]["scheme"] == "http"


def test_compute_hotlinks_specific_hip_overrides_endpoint_host() -> None:
    links = compute_hotlinks("tcp://1.2.3.4:2375", WEB)
    by_port = {lnk["container_port"]: lnk for lnk in links}
    assert by_port["443/tcp"]["host"] == "10.0.0.9"
    assert by_port["443/tcp"]["scheme"] == "https"  # published port 443
    assert by_port["443/tcp"]["url"] == "https://10.0.0.9:443"


def test_compute_hotlinks_skips_unpublished_ports() -> None:
    links = compute_hotlinks("tcp://1.2.3.4:2375", WEB)
    assert [lnk for lnk in links if lnk["host_port"] == ""] == []
    assert len(links) == 2  # 53/udp has no HostPort and is dropped


def test_compute_hotlinks_label_forces_https_and_alias() -> None:
    container = {
        "id": "c2",
        "short_id": "c2",
        "name": "api",
        "labels": {"trove.link.scheme": "https", "trove.link.name": "API backend"},
        "ports": [{"ContainerPort": "3000/tcp", "HostIp": None, "HostPort": "3000"}],
    }
    links = compute_hotlinks("tcp://10.0.0.5:2375", container)
    assert links[0]["scheme"] == "https"
    assert links[0]["url"] == "https://10.0.0.5:3000"
    assert links[0]["label"] == "API backend"


def test_compute_hotlinks_label_falls_back_to_container_name() -> None:
    container = {
        "id": "c3",
        "short_id": "c3",
        "name": "postgres",
        "labels": {},
        "ports": [{"ContainerPort": "5432/tcp", "HostIp": None, "HostPort": "5432"}],
    }
    assert compute_hotlinks("tcp://1.2.3.4:2375", container)[0]["label"] == "postgres"


def test_compute_hotlinks_empty_ports() -> None:
    assert compute_hotlinks("tcp://1.2.3.4:2375", {"id": "x", "name": "x", "ports": []}) == []


def test_compute_hotlinks_known_any_ip_treated_as_endpoint_host() -> None:
    container = {
        "id": "c4",
        "short_id": "c4",
        "name": "svc",
        "labels": {},
        "ports": [{"ContainerPort": "9000/tcp", "HostIp": "::", "HostPort": "9000"}],
    }
    links = compute_hotlinks("tcp://2.2.2.2:2375", container)
    assert links[0]["host"] == "2.2.2.2"


def test_compute_hotlinks_https_label_on_443_url() -> None:
    container = {
        "id": "c5",
        "short_id": "c5",
        "name": "tls",
        "labels": {},
        "ports": [{"ContainerPort": "443/tcp", "HostIp": "0.0.0.0", "HostPort": "8443"}],
    }
    links = compute_hotlinks("tcp://3.3.3.3:2375", container)
    assert links[0]["scheme"] == "http"  # only HostPort == "443" forces https
    assert links[0]["host"] == "3.3.3.3"
    assert links[0]["url"] == "http://3.3.3.3:8443"
