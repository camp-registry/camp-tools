"""App installation tokens that survive a multi-hour sweep (camp-tools#42)."""

import base64
import json
import shutil
import subprocess

import pytest

from camp import apptoken
from camp.apptoken import AppTokenSource, resolve, invalidate, sign_jwt, token_from_env

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None,
                                   reason="openssl CLI not available")


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available")
    d = tmp_path_factory.mktemp("key")
    priv, pub = d / "key.pem", d / "pub.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(priv), "2048"],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    return priv.read_text(), pub


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@needs_openssl
def test_jwt_is_rs256_and_verifies_with_the_public_key(rsa_key, tmp_path):
    pem, pub = rsa_key
    jwt = sign_jwt("4342622", pem, now=1_700_000_000)
    header_b64, payload_b64, sig_b64 = jwt.split(".")
    assert json.loads(_b64url_decode(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(_b64url_decode(payload_b64))
    assert payload == {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540,
                       "iss": "4342622"}
    sig = tmp_path / "sig.bin"
    sig.write_bytes(_b64url_decode(sig_b64))
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
        input=f"{header_b64}.{payload_b64}".encode(), capture_output=True)
    assert verified.returncode == 0, verified.stderr


@needs_openssl
def test_signing_never_leaves_the_key_on_disk(rsa_key, tmp_path, monkeypatch):
    pem, _ = rsa_key
    monkeypatch.setattr(apptoken.tempfile, "tempdir", str(tmp_path))
    sign_jwt("1", pem)
    assert list(tmp_path.iterdir()) == []


def _fake_api(responses):
    calls = []

    def api(method, path, bearer):
        calls.append((method, path))
        return responses[(method, path)]
    api.calls = calls
    return api


@needs_openssl
def test_source_mints_once_and_refreshes_after_the_window(rsa_key):
    pem, _ = rsa_key
    clock = {"t": 1000.0}
    api = _fake_api({
        ("POST", "/app/installations/77/access_tokens"):
            (201, {"token": "ghs_first", "expires_at": "x"}),
    })
    source = AppTokenSource("1", pem, installation_id=77, refresh_after=100,
                            log=lambda *_: None, api=api, clock=lambda: clock["t"])
    assert source.token() == "ghs_first"
    clock["t"] += 50
    assert source.token() == "ghs_first"      # still fresh: no second mint
    assert source.mints == 1
    api_responses = {("POST", "/app/installations/77/access_tokens"):
                     (201, {"token": "ghs_second", "expires_at": "y"})}
    source._api = _fake_api(api_responses)
    clock["t"] += 60                          # past the refresh window
    assert source.token() == "ghs_second"
    assert source.mints == 2


@needs_openssl
def test_invalidate_forces_a_fresh_mint(rsa_key):
    pem, _ = rsa_key
    api = _fake_api({("POST", "/app/installations/5/access_tokens"):
                     (201, {"token": "ghs_a"})})
    source = AppTokenSource("1", pem, installation_id=5, log=lambda *_: None, api=api)
    source.token()
    assert invalidate(source) is True
    source.token()
    assert source.mints == 2
    assert invalidate("ghp_fixed") is False


@needs_openssl
def test_installation_discovered_when_the_app_has_exactly_one(rsa_key):
    pem, _ = rsa_key
    api = _fake_api({
        ("GET", "/app/installations"): (200, [{"id": 149}]),
        ("POST", "/app/installations/149/access_tokens"): (201, {"token": "ghs_x"}),
    })
    source = AppTokenSource("1", pem, log=lambda *_: None, api=api)
    assert source.token() == "ghs_x"
    assert source.installation_id == "149"


@needs_openssl
def test_ambiguous_installation_is_an_error(rsa_key):
    pem, _ = rsa_key
    api = _fake_api({("GET", "/app/installations"): (200, [{"id": 1}, {"id": 2}])})
    source = AppTokenSource("1", pem, log=lambda *_: None, api=api)
    with pytest.raises(RuntimeError, match="CAMP_APP_INSTALLATION_ID"):
        source.token()


class _StubSource(AppTokenSource):
    """A source whose mint is a counter, no openssl or network involved."""

    def __init__(self):
        super().__init__("1", "pem", installation_id=1, log=lambda *_: None)

    def _mint(self):
        self.mints += 1
        self._token = f"ghs_{self.mints}"
        self._minted_at = self._clock()


def _http_error(code, headers=None):
    import io
    import urllib.error
    return urllib.error.HTTPError("https://api.github.com/x", code, "err",
                                  headers or {}, io.BytesIO(b"{}"))


def test_scan_request_rotates_the_token_after_a_401(monkeypatch):
    import camp.scan as scan
    seen = []

    class Resp:
        status = 200
        headers = {}

        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=30):
        seen.append(req.get_header("Authorization"))
        if len(seen) == 1:
            raise _http_error(401)
        return Resp()

    monkeypatch.setattr(scan.urllib.request, "urlopen", urlopen)
    source = _StubSource()
    status, body, _ = scan._request("https://api.github.com/x", source)
    assert (status, body) == (200, b"ok")
    assert seen == ["Bearer ghs_1", "Bearer ghs_2"]


def test_scan_request_does_not_loop_on_a_bad_fixed_token(monkeypatch):
    import camp.scan as scan
    calls = {"n": 0}

    def urlopen(req, timeout=30):
        calls["n"] += 1
        raise _http_error(401)

    monkeypatch.setattr(scan.urllib.request, "urlopen", urlopen)
    status, _, _ = scan._request("https://api.github.com/x", "ghp_expired")
    assert status == 401 and calls["n"] == 1


def test_fetch_component_rotates_the_token_after_a_401(monkeypatch):
    import camp.scan as scan
    from tests.helpers import _candidate
    seen = []

    class Resp:
        def read(self):
            return b"$plugin->component = 'mod_x';"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=30):
        seen.append(req.get_header("Authorization"))
        if len(seen) == 1:
            raise _http_error(401)
        return Resp()

    monkeypatch.setattr(scan.urllib.request, "urlopen", urlopen)
    status, component, _ = scan._fetch_component(_candidate(), _StubSource())
    assert (status, component) == ("ok", "mod_x")
    assert seen == ["Bearer ghs_1", "Bearer ghs_2"]


def test_resolve_handles_fixed_tokens_and_none():
    assert resolve(None) is None
    assert resolve("") is None
    assert resolve("ghp_x") == "ghp_x"


def test_token_from_env_prefers_app_credentials():
    assert token_from_env({"GITHUB_TOKEN": "ghp_x"}) == "ghp_x"
    assert token_from_env({}) is None
    source = token_from_env({"CAMP_APP_ID": "9", "CAMP_APP_PRIVATE_KEY": "pem",
                             "CAMP_APP_INSTALLATION_ID": "3", "GITHUB_TOKEN": "ghp_x"})
    assert isinstance(source, AppTokenSource)
    assert source.installation_id == "3"
