"""One-time local OAuth authorization command."""

from __future__ import annotations

from .auth import CredentialProvider


def main() -> None:
    result = CredentialProvider().get_credentials(interactive=True)
    print(f"Authenticated with {result.method}. Token saved locally.")


if __name__ == "__main__":
    main()

