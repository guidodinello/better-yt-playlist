"""OAuth 2.0 (installed-app flow) for the YouTube Data API.

Reordering a playlist is a write, so the full ``youtube`` scope is required
(not ``youtube.readonly``).

Refresh tokens issued to an OAuth consent screen in "Testing" publishing
status expire after 7 days. A long reorder run will hit this; the handling
here is simply to detect the dead token, drop it, and re-run the consent
flow. Progress is never lost because it lives in the ``target_order`` table,
not in the credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube"]


def _client_secret_path() -> Path:
    return Path(os.environ.get("BYP_CLIENT_SECRET", "client_secret.json"))


def _token_path() -> Path:
    return Path(os.environ.get("BYP_TOKEN", "token.json"))


def _run_flow() -> Credentials:
    secret = _client_secret_path()
    if not secret.exists():
        raise SystemExit(
            f"OAuth client secret not found at {secret}.\n"
            "Create an OAuth 2.0 Client ID of type 'Desktop app' in the Google "
            "Cloud console, download the JSON, and save it there "
            "(or point BYP_CLIENT_SECRET at it)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    # The installed-app flow always yields an oauth2 user Credentials; the
    # broader union in the stubs also admits external-account creds.
    return cast(Credentials, flow.run_local_server(port=0))


def get_credentials() -> Credentials:
    """Return valid credentials, refreshing or re-authenticating as needed."""
    token = _token_path()
    creds: Credentials | None = None
    if token.exists():
        creds = cast(Credentials, Credentials.from_authorized_user_file(str(token), SCOPES))

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            print(
                "Stored token could not be refreshed (likely past the 7-day "
                "limit for a 'Testing' OAuth app). Re-authenticating."
            )
            token.unlink(missing_ok=True)
            creds = None

    if not creds:
        creds = _run_flow()
    if creds is None:  # _run_flow always returns creds or raises; keeps types honest
        raise RuntimeError("authentication produced no credentials")

    token.write_text(creds.to_json())
    token.chmod(0o600)
    return creds


def get_client() -> Any:
    """Build an authenticated YouTube Data API v3 client."""
    return build(
        "youtube",
        "v3",
        credentials=get_credentials(),
        cache_discovery=False,
    )
