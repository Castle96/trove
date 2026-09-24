"""Unit tests for the Trivy scan service (pure helpers + subprocess fakes).

No Docker or Trivy binary is required: the ``trivy`` CLI is stubbed out.
"""

from __future__ import annotations

import json

import pytest

from app.dockwatch.config import get_settings as dw_settings
from app.dockwatch.services import trivy_service as trivy


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("nginx", "docker.io/library/nginx:latest"),
        ("nginx:1.27", "docker.io/library/nginx:1.27"),
        ("registry.example.com/acme/app", "registry.example.com/acme/app:latest"),
        ("ghcr.io/org/tool:2", "ghcr.io/org/tool:2"),
        ("localhost:5000/app", "localhost:5000/app:latest"),
        ("  DOCKER.IO/ALPINE ", "docker.io/alpine:latest"),
        ("https://docker.io/library/httpd", "docker.io/library/httpd:latest"),
    ],
)
def test_normalize_image_ref(given: str, expected: str) -> None:
    assert trivy.normalize_image_ref(given) == expected


_SAMPLE_REPORT = {
    "Metadata": {"ImageDigest": "sha256:abc"},
    "Results": [
        {
            "Target": "nginx:latest (debian:12.9)",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-2",
                    "Severity": "HIGH",
                    "PkgName": "openssl",
                    "Title": "A",
                },
                {
                    "VulnerabilityID": "CVE-2024-1",
                    "Severity": "CRITICAL",
                    "PkgName": "curl",
                    "Title": "B",
                },
                {
                    "VulnerabilityID": "CVE-2024-3",
                    "Severity": "LOW",
                    "PkgName": "zlib",
                    "Title": "C",
                },
            ],
        }
    ],
}


def test_summarize_report_counts_and_sorts() -> None:
    counts, flat = trivy.summarize_report(_SAMPLE_REPORT)
    assert counts == {"CRITICAL": 1, "HIGH": 1, "MEDIUM": 0, "LOW": 1, "UNKNOWN": 0}
    assert [v["severity"] for v in flat] == ["CRITICAL", "HIGH", "LOW"]
    assert flat[0]["vulnerability_id"] == "CVE-2024-1"
    assert flat[0]["target"] == "nginx:latest (debian:12.9)"
    assert flat[2]["fixed_version"] is None


def test_summarize_report_empty_and_misshaped() -> None:
    counts, flat = trivy.summarize_report({})
    assert counts["CRITICAL"] == 0 and flat == []
    counts2, flat2 = trivy.summarize_report(
        {"Results": [{"Target": "x", "Vulnerabilities": [{"Severity": "shrug"}]}]}
    )
    assert counts2["SHRUG"] == 1
    assert flat2[0]["severity"] == "SHRUG"


def test_resolve_trivy_bin_path_candidates(monkeypatch) -> None:
    dw_settings.cache_clear()
    monkeypatch.setattr(trivy.shutil, "which", lambda name: None)
    monkeypatch.setattr(trivy.Path, "exists", lambda self: False)
    with pytest.raises(trivy.TrivyNotInstalledError, match="trivy binary not found"):
        trivy.resolve_trivy_bin()


def test_resolve_trivy_bin_custom_setting(monkeypatch) -> None:
    monkeypatch.setenv("DOCKWATCH_TRIVY_BIN", "/opt/trivy/bin")
    dw_settings.cache_clear()
    monkeypatch.setattr(trivy.shutil, "which", lambda name: None)
    monkeypatch.setattr(trivy.Path, "exists", lambda self: True)
    try:
        assert trivy.resolve_trivy_bin() == "/opt/trivy/bin"
    finally:
        dw_settings.cache_clear()


def test_run_scan_sync_success(monkeypatch) -> None:
    class _Ok:
        returncode = 0
        stdout = json.dumps(_SAMPLE_REPORT)

    released: list[str] = []

    def fake_run(cmd, **kw):
        assert cmd[0].endswith("trivy") and cmd[1] == "image"
        released.append("trivy")
        return _Ok()

    monkeypatch.setattr(trivy, "resolve_trivy_bin", lambda: "/usr/bin/trivy")
    monkeypatch.setattr(trivy.subprocess, "run", fake_run)
    parsed = trivy._run_scan_sync("nginx", "docker", 60.0)
    assert released == ["trivy"]
    assert parsed["Metadata"]["ImageDigest"] == "sha256:abc"


def test_run_scan_sync_nonzero_exit(monkeypatch) -> None:
    class _Err:
        returncode = 1
        stderr = "module 'podman' has exited with error"
        stdout = ""

    monkeypatch.setattr(trivy.resolve_trivy_bin, "__call__", lambda *a, **k: "/usr/bin/trivy")
    monkeypatch.setattr(trivy.subprocess, "run", lambda *a, **k: _Err())
    with pytest.raises(trivy.TrivyScanError, match="podman"):
        trivy._run_scan_sync("nginx", "docker", 60.0)


def test_run_scan_sync_invalid_json(monkeypatch) -> None:
    class _Bad:
        returncode = 0
        stderr = ""
        stdout = "not json"

    monkeypatch.setattr(trivy.resolve_trivy_bin, "__call__", lambda *a, **k: "/usr/bin/trivy")
    monkeypatch.setattr(trivy.subprocess, "run", lambda *a, **k: _Bad())
    with pytest.raises(trivy.TrivyScanError, match="invalid JSON"):
        trivy._run_scan_sync("nginx", "docker", 60.0)
