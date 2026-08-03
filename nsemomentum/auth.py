"""Upstox OAuth login flow and token storage.

Upstox access tokens are valid for one trading day (expire ~3:30 AM IST the
next day), so a fresh login is needed each morning — either via
`python -m nsemomentum login` (terminal) or the "Connect Upstox" button on the
web dashboard.

App credentials (API key / secret / redirect URI) come from, in priority
order: environment / .env, then a saved credentials file written by the web
"connect" screen.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests

BASE_URL = "https://api.upstox.com"
HOME = Path(os.environ.get("NSEMOMENTUM_HOME", str(Path.home() / ".nsemomentum")))
TOKEN_FILE = HOME / "credentials.json"
APP_FILE = HOME / "app_credentials.json"


class AuthError(RuntimeError):
    """Raised for recoverable auth failures (web-friendly, unlike SystemExit)."""


@dataclass
class AppCredentials:
    api_key: str = ""
    api_secret: str = ""
    redirect_uri: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.api_secret and self.redirect_uri)


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def load_app_credentials() -> AppCredentials:
    """App credentials from env/.env, falling back to the saved app file."""
    _load_dotenv()
    creds = AppCredentials(
        api_key=os.environ.get("UPSTOX_API_KEY", ""),
        api_secret=os.environ.get("UPSTOX_API_SECRET", ""),
        redirect_uri=os.environ.get("UPSTOX_REDIRECT_URI", ""),
    )
    if not creds.complete and APP_FILE.exists():
        try:
            saved = json.loads(APP_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            saved = {}
        creds.api_key = creds.api_key or saved.get("api_key", "")
        creds.api_secret = creds.api_secret or saved.get("api_secret", "")
        creds.redirect_uri = creds.redirect_uri or saved.get("redirect_uri", "")
    return creds


def save_app_credentials(api_key: str, api_secret: str, redirect_uri: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    APP_FILE.write_text(
        json.dumps(
            {
                "api_key": api_key.strip(),
                "api_secret": api_secret.strip(),
                "redirect_uri": redirect_uri.strip(),
            }
        ),
        encoding="utf-8",
    )
    APP_FILE.chmod(0o600)


def get_app_credentials() -> tuple[str, str, str]:
    """CLI helper: returns creds or exits with a message."""
    creds = load_app_credentials()
    if not creds.complete:
        raise SystemExit(
            "Missing Upstox app credentials. Set UPSTOX_API_KEY, UPSTOX_API_SECRET "
            "and UPSTOX_REDIRECT_URI in the environment or a .env file (see .env.example), "
            "or enter them on the web dashboard's Connect screen."
        )
    return creds.api_key, creds.api_secret, creds.redirect_uri


def build_login_url(api_key: str, redirect_uri: str, state: str = "") -> str:
    params = {"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri}
    if state:
        params["state"] = state
    return f"{BASE_URL}/v2/login/authorization/dialog?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, api_key: str, secret: str, redirect_uri: str) -> str:
    """Exchange an authorization code for an access token. Raises AuthError."""
    try:
        resp = requests.post(
            f"{BASE_URL}/v2/login/authorization/token",
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code.strip(),
                "client_id": api_key,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise AuthError(f"Network error contacting Upstox: {exc}") from exc
    if resp.status_code >= 400:
        raise AuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("access_token")
    if not token:
        raise AuthError(f"Token exchange returned no access_token: {resp.text[:300]}")
    return token


def save_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"access_token": token}), encoding="utf-8")
    TOKEN_FILE.chmod(0o600)


def stored_token() -> str | None:
    """The saved/env token if any, without raising."""
    _load_dotenv()
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if token:
        return token
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("access_token")
        except (ValueError, OSError):
            return None
    return None


def load_token() -> str:
    """Access token from env or the saved file; exits if absent (CLI use)."""
    token = stored_token()
    if token:
        return token
    raise SystemExit(
        "No Upstox access token found. Run `python -m nsemomentum login` first, "
        "connect from the web dashboard, or set UPSTOX_ACCESS_TOKEN."
    )


def verify_token(token: str) -> dict | None:
    """Return the Upstox profile dict if the token is valid, else None."""
    try:
        resp = requests.get(
            f"{BASE_URL}/v2/user/profile",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("data")


def interactive_login() -> None:
    api_key, secret, redirect = get_app_credentials()
    url = build_login_url(api_key, redirect)
    print("Open this URL in a browser, log in to Upstox and approve access:\n")
    print(f"  {url}\n")
    print(f"You will be redirected to {redirect}?code=XXXX — paste the `code` value below.")
    code = input("Authorization code: ").strip()
    token = exchange_code(code, api_key, secret, redirect)
    save_token(token)
    print(f"Access token saved to {TOKEN_FILE} (valid for today's session).")
