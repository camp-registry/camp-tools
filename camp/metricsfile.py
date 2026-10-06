"""Repository metrics live beside the entry, not inside it (camp-index#522,
camp-tools#70).

The scheduled jobs (enrich, the metrics refresh, the upstream tag row)
rewrite a plugin's observed metrics nightly. While that block sat inside
`plugins/<type>/<component>.yml`, every claim or hand-authored release
pull request on the same file conflicted with whichever refresh landed
first, and the author had to rebase by hand. The block now lives in
`metrics/<type>/<component>.yml`, written only by the registry's jobs; the
entry keeps what humans and the publisher write.

Transition (train one): readers take the sidecar when it exists and fall
back to the entry's block; every writer saves through `save_entry`, which
moves the block out of any entry it touches, so the index migrates on its
own from the first nightly run, and `migrate` finishes the rest in one
commit. Train two drops the fallback and the entry schema's `metrics`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DIRNAME = "metrics"


def path_for(index_dir: str | Path, component: str) -> Path:
    return Path(index_dir) / DIRNAME / component.partition("_")[0] / f"{component}.yml"


def load(index_dir: str | Path, entry: dict) -> dict:
    """The metrics for `entry`: the sidecar when present, else the entry's
    own block (transition), else {}. Never writes."""
    path = path_for(index_dir, entry["component"])
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f)
        return dict(data) if isinstance(data, dict) else {}
    return dict(entry.get("metrics") or {})


def attach(index_dir: str | Path, entry: dict) -> dict:
    """`entry` with its metrics in place under "metrics", for code that
    reads an entry as one document (the site, the enrich queue). The
    result is for reading; write through `save_entry`."""
    metrics = load(index_dir, entry)
    if metrics:
        entry["metrics"] = metrics
    else:
        entry.pop("metrics", None)
    return entry


def write(index_dir: str | Path, component: str, metrics: dict) -> Path:
    path = path_for(index_dir, component)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(dict(metrics), f, sort_keys=False, allow_unicode=True)
    return path


def save_entry(index_dir: str | Path, path: str | Path, entry: dict) -> None:
    """Write an entry file with its metrics split out to the sidecar. The
    one writer the registry's jobs use: an entry they touch migrates on
    its own. Metrics are written only when the document carries some, so
    an entry whose block already moved leaves the sidecar untouched."""
    document = dict(entry)
    metrics = document.pop("metrics", None)
    if metrics:
        write(index_dir, document["component"], metrics)
    with open(path, "w") as f:
        yaml.safe_dump(document, f, sort_keys=False, allow_unicode=True)


def remove(index_dir: str | Path, component: str) -> bool:
    """Delete the sidecar with its listing (opt-out, removals). True when
    there was one."""
    path = path_for(index_dir, component)
    if path.exists():
        path.unlink()
        return True
    return False


def orphans(index_dir: str | Path) -> list[str]:
    """Sidecars with no entry: a removed or never-listed component must not
    keep serving signals."""
    index = Path(index_dir)
    found = []
    for path in sorted((index / DIRNAME).glob("*/*.yml")):
        component = path.stem
        if not (index / "plugins" / component.partition("_")[0] / f"{component}.yml").exists():
            found.append(component)
    return found


def stray_blocks(index_dir: str | Path) -> list[str]:
    """Entries still carrying a metrics block (what `migrate` has left to
    move; after train two, a validation error)."""
    found = []
    for path in sorted((Path(index_dir) / "plugins").glob("*/*.yml")):
        with open(path) as f:
            entry = yaml.safe_load(f) or {}
        if entry.get("metrics"):
            found.append(entry.get("component") or path.stem)
    return found


def migrate(index_dir: str | Path, dry_run: bool = False, log=print) -> dict:
    """Move every entry's metrics block into its sidecar. Idempotent: an
    entry without a block is left alone; an existing sidecar is overwritten
    by the block only when the entry still has one (the block is the
    newer write in that case, since writers strip it). Returns counts."""
    stats = {"moved": 0, "already": 0, "entries": 0}
    for path in sorted((Path(index_dir) / "plugins").glob("*/*.yml")):
        with open(path) as f:
            entry = yaml.safe_load(f) or {}
        stats["entries"] += 1
        if not entry.get("metrics"):
            stats["already"] += 1
            continue
        if not dry_run:
            save_entry(index_dir, path, entry)
        stats["moved"] += 1
    log(f"migrate-metrics: {stats['moved']} block(s) moved to {DIRNAME}/, "
        f"{stats['already']} entr(ies) already without one, of {stats['entries']}"
        + (" (dry run: nothing written)" if dry_run else ""))
    return stats
