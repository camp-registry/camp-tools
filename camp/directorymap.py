"""The old moodle.org directory's component-to-repository map
(camp-tools#30, the systemic fix for #14).

The directory's pluglist is a frozen historical snapshot: for every
component it published, the repository its listing named. That makes it
an authority the scanner can hold new arrivals against, the same shape
as the standard-components table (#25/#29) and the plugin-type table
(#16). A candidate declaring a mapped component from a different
repository parks in needs-review with the directory evidence; the review
rules for what happens next are already precedent (evidence-picture
repoints, the directory-decides identity rule, the
maintenance-continuation KEEP).

The committed directorymap.json is generated from a pluglist snapshot by
`camp build-directory-map`; since the upstream API can disappear any
day, the snapshot in the repository is the durable artifact. URLs are
stored trimmed to host/owner/repo (crosscheck's normalization), so
lookups compare repository identity, not deep paths.
"""

from __future__ import annotations

import json
import re
import urllib.request
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "directorymap.json"
VCS_HOSTS = ("github.com/", "gitlab.com/", "bitbucket.org/")


# URL normalization lives here (the import leaf) so crosscheck and the
# scanner gate share one definition without a cycle.
def _norm(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.removesuffix(".git")


def _repo_url(url: str) -> str:
    """https URL trimmed to host/owner/repo (drops deep paths like
    bitbucket /src/... suffixes)."""
    parts = _norm(url).split("/")
    return "https://" + "/".join(parts[:3]) if len(parts) >= 3 else "https://" + _norm(url)


def load_pluglist(source: str) -> dict[str, str]:
    """component -> VCS URL map from a pluglist.php JSON document (local
    path or URL). Components without a usable VCS-host URL are dropped."""
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "camp-tools"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
    else:
        data = json.load(open(source))
    out = {}
    for plugin in data.get("plugins", []):
        component, url = plugin.get("component"), plugin.get("source")
        if component and url and _norm(url).startswith(VCS_HOSTS):
            out[component] = url
    return out


@lru_cache(maxsize=1)
def load() -> dict:
    with open(DATA_PATH) as f:
        return json.load(f)


def directory_source(component: str) -> str | None:
    """The repository the old directory published for `component`, as a
    normalized https://host/owner/repo URL, or None when the directory
    never mapped it (or had no usable VCS URL for it)."""
    return load()["components"].get(component)


def same_repo(url_a: str, url_b: str) -> bool:
    """Repository-identity comparison: host/owner/repo, case-insensitive,
    scheme/www/.git/deep-path insensitive."""
    return _norm(_repo_url(url_a)) == _norm(_repo_url(url_b))


def build_map(pluglist_source: str) -> dict:
    components = {comp: _repo_url(url)
                  for comp, url in load_pluglist(pluglist_source).items()}
    return {"source": "moodle.org directory pluglist (frozen snapshot)",
            "components": dict(sorted(components.items()))}


def write_map(table: dict) -> None:
    with open(DATA_PATH, "w") as f:
        json.dump(table, f, indent=1)
        f.write("\n")
    load.cache_clear()
