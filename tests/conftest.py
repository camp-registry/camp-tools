import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

FIXED_DATE = "2026-01-15T12:00:00 +0000"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo),
             "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE,
             "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.org",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.org"},
    )
    return result.stdout.strip()


VERSION_PHP = textwrap.dedent("""\
    <?php
    defined('MOODLE_INTERNAL') || die();
    $plugin->version   = 2026011500;
    $plugin->requires  = 2024100700;
    $plugin->component = 'mod_example';
    $plugin->release   = '1.0.0';
    $plugin->php       = '8.1.0';
    """)

LISTING_YML = textwrap.dedent("""\
    name: Example Activity
    summary: An example plugin for tests.
    description: |
      Does example things.

      - bullet one
      - bullet two
    labels:
      - fully-free
    """)


@pytest.fixture
def plugin_repo(tmp_path: Path) -> Path:
    """A minimal tagged Moodle plugin repository."""
    repo = tmp_path / "plugin-src"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "version.php").write_text(VERSION_PHP)
    (repo / "lib.php").write_text("<?php // lib\n")
    (repo / ".camp").mkdir()
    (repo / ".camp" / "listing.yml").write_text(LISTING_YML)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "tag", "v1.0.0")
    return repo


@pytest.fixture
def index_dir(tmp_path: Path, plugin_repo: Path) -> Path:
    """An index tree with one Tier 2 (source-verified) entry for the fixture
    plugin, with correct commit and hashes computed by the real build code."""
    from camp.build import build_zip, file_sha256_at_commit, resolve_tag

    commit = resolve_tag(str(plugin_repo), "v1.0.0")
    artifact = build_zip(str(plugin_repo), "v1.0.0", "mod_example")
    listing_hash = file_sha256_at_commit(str(plugin_repo), commit, ".camp/listing.yml")

    entry = {
        "component": "mod_example",
        "source": f"https://example.org/mod_example",
        "maintainers": [{"github": "tester"}],
        "security-contact": "security@example.org",
        "tier": 2,
        "labels": ["fully-free"],
        "status": "active",
        "releases": [{
            "version": "1.0.0",
            "tag": "v1.0.0",
            "commit": commit,
            "moodle-version": 2026011500,
            "supported-moodle": ["4.5", "5.0"],
            "php-min": "8.1.0",
            "zip-sha256": artifact.sha256,
            "listing-sha256": listing_hash,
            "published": "2026-01-15T12:30:00Z",
        }],
    }
    index = tmp_path / "index"
    entry_dir = index / "plugins" / "mod"
    entry_dir.mkdir(parents=True)
    with open(entry_dir / "mod_example.yml", "w") as f:
        yaml.safe_dump(entry, f, sort_keys=False)
    return index


@pytest.fixture
def entry_path(index_dir: Path) -> Path:
    return index_dir / "plugins" / "mod" / "mod_example.yml"
