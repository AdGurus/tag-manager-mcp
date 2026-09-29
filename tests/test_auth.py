from __future__ import annotations

import pytest

from tag_manager_mcp.auth import AuthenticationError, CredentialProvider


def test_invalid_auth_mode() -> None:
    with pytest.raises(AuthenticationError, match="GTM_AUTH_MODE"):
        CredentialProvider({"GTM_AUTH_MODE": "magic"})


def test_auto_mode_reports_both_missing_methods(tmp_path) -> None:
    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "auto",
            "GTM_OAUTH_TOKEN_FILE": str(tmp_path / "missing-token.json"),
        }
    )
    with pytest.raises(AuthenticationError) as exc:
        provider.get_credentials(interactive=False)
    message = str(exc.value)
    assert "user OAuth" in message
    assert "service account" in message

