"""Branch derivation from version.php declarations."""

from camp.moodleversions import branch_from_requires, branches_from_supported


def test_supported_range_expands():
    assert branches_from_supported([311, 401]) == ["3.11", "4.0", "4.1"]
    assert branches_from_supported([403, 501]) == ["4.3", "4.4", "4.5", "5.0", "5.1"]
    assert branches_from_supported([405, 405]) == ["4.5"]


def test_supported_range_invalid():
    assert branches_from_supported([405]) is None
    assert branches_from_supported([1, 2]) is None


def test_requires_maps_to_containing_branch():
    assert branch_from_requires(2023100900) == "4.3"   # exactly 4.3
    assert branch_from_requires(2023101200) == "4.3"   # a 4.3 build
    assert branch_from_requires(2024100700) == "4.5"
    assert branch_from_requires(2010000000) is None    # pre-3.9


def test_release_derives_supported_from_fixture(plugin_repo, entry_path, tmp_path):
    """End-to-end: camp release on a new tag derives from $plugin->requires
    (fixture declares requires=2024100700 -> 4.5) when no flag is passed."""
    import yaml
    from camp.cli import main
    from conftest import git

    (plugin_repo / "version.php").write_text(
        (plugin_repo / "version.php").read_text().replace("'1.0.0'", "'1.1.0'"))
    git(plugin_repo, "add", "-A")
    git(plugin_repo, "commit", "-q", "-m", "1.1.0")
    git(plugin_repo, "tag", "v1.1.0")

    assert main(["release", str(entry_path), "v1.1.0", "--source", str(plugin_repo)]) == 0
    entry = yaml.safe_load(entry_path.read_text())
    assert entry["releases"][-1]["supported-moodle"] == ["4.5"]
