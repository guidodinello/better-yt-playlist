# pyright: basic
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


def _paths_for(project: str) -> tuple[Path, Path]:
    """Client secret + token paths for a named quota project.

    ``"default"`` keeps the original ``BYP_CLIENT_SECRET``/``BYP_TOKEN``
    filenames; any other name looks for ``BYP_CLIENT_SECRET_<NAME>`` /
    ``BYP_TOKEN_<NAME>`` env vars, falling back to
    ``client_secret_<name>.json`` / ``token_<name>.json``. Each project is a
    separate GCP project with its own daily quota, used to spread a large
    one-off import across more than 10,000 units/day.
    """
    if project == "default":
        secret = os.environ.get("BYP_CLIENT_SECRET", "client_secret.json")
        token = os.environ.get("BYP_TOKEN", "token.json")
    else:
        suffix = project.upper()
        secret = os.environ.get(f"BYP_CLIENT_SECRET_{suffix}", f"client_secret_{project}.json")
        token = os.environ.get(f"BYP_TOKEN_{suffix}", f"token_{project}.json")
    return Path(secret), Path(token)


def _run_flow(secret: Path) -> Credentials:
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


def get_credentials(project: str = "default") -> Credentials:
    """Return valid credentials for ``project``, refreshing or re-authenticating as needed."""
    secret, token = _paths_for(project)
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
        creds = _run_flow(secret)
    if creds is None:  # _run_flow always returns creds or raises; keeps types honest
        raise RuntimeError("authentication produced no credentials")

    token.write_text(creds.to_json())
    token.chmod(0o600)
    return creds


def get_client(project: str = "default") -> Any:
    """Build an authenticated YouTube Data API v3 client for ``project``."""
    return build(
        "youtube",
        "v3",
        credentials=get_credentials(project),
        cache_discovery=False,
    )
