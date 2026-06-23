"""Unit tests for the IMDSPreflightJob command renderer.

The Job's K8s resource graph (image, securityContext, gating annotation)
is intentionally not unit-tested here: rendering it requires a full
Pulumi runtime and the asserted bits are constant. The interesting
testable surface is the per-clientId shell script the container runs;
we keep it as a pure function so it round-trips cleanly.
"""

from __future__ import annotations

import pytest

from stacks.control_plane.azure._imds_preflight_job import (
    IMDS_PROBE_IMAGE,
    _IMDS_CURL_TIMEOUT_SECONDS,
    _probe_command,
)


_CLIENT_ID = "5a7d5349-776d-413e-9b65-157066a60e71"


def test_probe_command_shape() -> None:
    cmd = _probe_command(_CLIENT_ID)

    # First two args fix the shell + flag (we want a single -c script
    # so the whole thing is one execve, not pipes between containers).
    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    assert len(cmd) == 3


def test_probe_command_curls_token_endpoint_with_client_id() -> None:
    script = _probe_command(_CLIENT_ID)[2]

    assert "169.254.169.254/metadata/identity/oauth2/token" in script
    assert "api-version=2018-02-01" in script
    assert f"client_id={_CLIENT_ID}" in script
    # Resource must be the URL-encoded management.azure.com (audience
    # for ARM tokens; the *only* audience CAPZ uses).
    assert "resource=https%3A%2F%2Fmanagement.azure.com%2F" in script


def test_probe_command_requires_metadata_header_and_bypasses_proxy() -> None:
    # IMDS requires ``Metadata: true``. It also rejects requests via
    # an HTTP proxy (Azure docs are explicit). Both must show up in the
    # curl invocation or the request would silently fail in some
    # environments.
    script = _probe_command(_CLIENT_ID)[2]

    assert "Metadata: true" in script
    assert "--noproxy '*'" in script


def test_probe_command_enforces_curl_timeout() -> None:
    script = _probe_command(_CLIENT_ID)[2]

    # ``-m`` is curl's "max time per request" flag. Job-level
    # activeDeadlineSeconds catches Job-level hangs; this catches
    # in-curl hangs (e.g. TCP connect that never gets RST).
    assert f"-m {_IMDS_CURL_TIMEOUT_SECONDS}" in script


def test_probe_command_fails_when_no_access_token() -> None:
    # The decisive bit: ``grep -q access_token``. Without this the
    # script would exit 0 on any HTTP response (including IMDS error
    # bodies), defeating the point of the preflight.
    script = _probe_command(_CLIENT_ID)[2]

    assert "grep -q access_token" in script


def test_probe_command_exits_nonzero_on_curl_failure() -> None:
    # ``set -e`` ensures a non-zero curl exit (timeout, connection
    # refused, etc.) propagates to the script's exit code, which the
    # Job translates into Failed \u2014 then pulumi-kubernetes surfaces
    # as a stack error via waitFor=condition=Complete.
    script = _probe_command(_CLIENT_ID)[2]

    assert script.lstrip().startswith("set -e")


def test_probe_command_emits_debug_lines_for_pulumi_diagnostics() -> None:
    # Pulumi captures stdout/stderr of failed Jobs and includes it in
    # the Stack diagnostics. The two ``echo [preflight]`` lines make
    # the failure mode legible to the operator without ``kubectl logs``.
    script = _probe_command(_CLIENT_ID)[2]

    assert "[preflight] curling IMDS" in script
    assert "[preflight] IMDS response:" in script


def test_probe_image_pinned_by_tag() -> None:
    # Tag pin is what we use in the docs + runbook. Bare ``latest``
    # would defeat reproducibility on a multi-host fleet; missing
    # registry would resolve via the host docker daemon's default
    # registry which is implementation-defined.
    assert IMDS_PROBE_IMAGE.startswith("curlimages/curl:")
    assert ":" in IMDS_PROBE_IMAGE
    tag = IMDS_PROBE_IMAGE.split(":", 1)[1]
    assert tag != "latest"


@pytest.mark.parametrize(
    "bad_client_id",
    ["", "00000000-0000-0000-0000-000000000000"],
)
def test_probe_command_renders_for_any_string_input(bad_client_id: str) -> None:
    # _probe_command is intentionally permissive about its input \u2014
    # GUID validation happens upstream in parse_control_plane_azure_spec.
    # Confirm the renderer doesn't choke on edge cases (empty string,
    # all-zeros). Both should produce a syntactically valid script that
    # would simply fail the IMDS lookup at run time.
    cmd = _probe_command(bad_client_id)
    assert cmd[0] == "sh"
    # client_id ends up in the URL even when empty; we leave that to
    # IMDS to reject (it returns a 400 with ``invalid_request``).
    assert f"client_id={bad_client_id}" in cmd[2]
