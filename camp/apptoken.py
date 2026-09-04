"""GitHub App installation tokens that outlive a single hour.

An installation access token expires 60 minutes after it is minted and a
full discovery sweep runs for several, so a token handed to `camp scan` at
job start dies mid-run: every search after that point fails 401 and every
candidate fetch reports transient. Instead of a fixed token the long
sweeps take an `AppTokenSource`, which signs the App JWT itself (RS256
through the `openssl` CLI, no extra Python dependency) and mints a fresh
installation token whenever the current one is near expiry or a request
comes back 401.

Configuration comes from the environment (see `token_from_env`):

    CAMP_APP_ID               numeric App id
    CAMP_APP_PRIVATE_KEY      the App's PEM private key (contents, not a path)
    CAMP_APP_INSTALLATION_ID  optional; discovered via /app/installations
                              when absent (the App must have exactly one)
    GITHUB_TOKEN              fallback: a fixed token, used as before

Callers that only ever pass the token through can hold either a `str` or
an `AppTokenSource`; `resolve()` turns both into the header value.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request

API = "https://api.github.com"
USER_AGENT = "camp-tools"
# GitHub caps App JWT lifetime at 10 minutes; issue slightly in the past to
# absorb clock skew between the runner and GitHub.
JWT_BACKDATE = 60
JWT_LIFETIME = 540
# Installation tokens live 60 minutes; refresh well before that so a long
# request sequence never straddles the expiry.
REFRESH_AFTER = 50 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_jwt(app_id: str, private_key_pem: str, now: float | None = None) -> str:
    """RS256 App JWT signed with the openssl CLI. The key touches disk only
    as a 0600 temporary file for the duration of the signing call."""
    issued = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iat": issued - JWT_BACKDATE,
                                  "exp": issued + JWT_LIFETIME,
                                  "iss": str(app_id)},
                                 separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    fd, key_path = tempfile.mkstemp(prefix="camp-app-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem if private_key_pem.endswith("\n")
                    else private_key_pem + "\n")
        os.chmod(key_path, 0o600)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input, capture_output=True, check=False)
    finally:
        os.unlink(key_path)
    if result.returncode != 0:
        raise RuntimeError("openssl could not sign the App JWT: "
                           + result.stderr.decode(errors="replace").strip())
    return f"{header}.{payload}.{_b64url(result.stdout)}"


def _api(method: str, path: str, bearer: str) -> tuple[int, dict]:
    request = urllib.request.Request(API + path, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {bearer}",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            body = {}
        return exc.code, body


class AppTokenSource:
    """Mints installation tokens on demand and refreshes them before they
    expire. `token()` is what request builders call; `invalidate()` is what
    a 401 handler calls before retrying."""

    def __init__(self, app_id: str, private_key_pem: str,
                 installation_id: str | int | None = None,
                 refresh_after: int = REFRESH_AFTER, log=print,
                 api=_api, clock=time.time):
        self.app_id = str(app_id)
        self._pem = private_key_pem
        self.installation_id = str(installation_id) if installation_id else None
        self._refresh_after = refresh_after
        self._log = log
        self._api = api
        self._clock = clock
        self._token: str | None = None
        self._minted_at = 0.0
        self.mints = 0

    def token(self) -> str:
        if self._token is None or \
                self._clock() - self._minted_at >= self._refresh_after:
            self._mint()
        return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        self._token = None

    def _mint(self) -> None:
        jwt = sign_jwt(self.app_id, self._pem, now=self._clock())
        if self.installation_id is None:
            status, body = self._api("GET", "/app/installations", jwt)
            installations = body if isinstance(body, list) else []
            if status != 200 or len(installations) != 1:
                raise RuntimeError(
                    f"could not determine the App installation (HTTP {status}, "
                    f"{len(installations)} installations); set "
                    f"CAMP_APP_INSTALLATION_ID")
            self.installation_id = str(installations[0]["id"])
        status, body = self._api(
            "POST", f"/app/installations/{self.installation_id}/access_tokens", jwt)
        if status != 201 or not body.get("token"):
            raise RuntimeError(f"installation token request failed (HTTP {status}): "
                               f"{body.get('message', '')}")
        self._token = body["token"]
        self._minted_at = self._clock()
        self.mints += 1
        self._log(f"  minted installation token #{self.mints} "
                  f"(expires {body.get('expires_at', '?')})")


def resolve(token) -> str | None:
    """Header value for either a fixed token or an AppTokenSource."""
    if token is None or isinstance(token, str):
        return token or None
    return token.token()


def invalidate(token) -> bool:
    """Drop a source's cached token after a 401. True when a retry makes
    sense (a source can mint again); False for fixed tokens."""
    if isinstance(token, AppTokenSource):
        token.invalidate()
        return True
    return False


def token_from_env(environ=os.environ, log=print):
    """An AppTokenSource when App credentials are configured, else the
    fixed GITHUB_TOKEN (or None)."""
    app_id = environ.get("CAMP_APP_ID")
    pem = environ.get("CAMP_APP_PRIVATE_KEY")
    if app_id and pem:
        return AppTokenSource(app_id, pem,
                              installation_id=environ.get("CAMP_APP_INSTALLATION_ID"),
                              log=log)
    return environ.get("GITHUB_TOKEN") or None
