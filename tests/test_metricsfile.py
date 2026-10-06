"""Metrics live in a sidecar beside the entry (camp-index#522, camp-tools#70)."""
import yaml

from camp import metricsfile


def _index(tmp_path, metrics=True):
    index = tmp_path / "index"
    d = index / "plugins" / "mod"
    d.mkdir(parents=True)
    entry = {"component": "mod_x", "source": "https://github.com/u/mod_x",
             "maintainers": [{"github": "u"}], "tier": 0, "status": "active",
             "releases": [], "license": "GPL-3.0"}
    if metrics:
        entry["metrics"] = {"updated": "2026-01-01T00:00:00Z", "stars": 3, "forks": 1,
                            "open-issues": 0, "archived": False, "checked": "2026-09-01"}
    (d / "mod_x.yml").write_text(yaml.safe_dump(entry, sort_keys=False))
    return index, d / "mod_x.yml", entry


def test_load_reads_the_sidecar_only(tmp_path):
    index, path, entry = _index(tmp_path)
    assert metricsfile.load(index, entry) == {}                  # a block in the entry is not read
    assert "metrics" not in metricsfile.attach(index, dict(entry))
    metricsfile.write(index, "mod_x", {"stars": 9, "checked": "2026-10-01"})
    assert metricsfile.load(index, entry)["stars"] == 9
    assert metricsfile.load(index, {"component": "mod_none"}) == {}


def test_validate_rejects_a_block_left_in_the_entry(tmp_path):
    from camp.validate import validate_entry
    index, path, entry = _index(tmp_path)
    problems = validate_entry(path)
    assert any("'metrics' was unexpected" in p for p in problems)
    assert any("migrate-metrics" in p for p in problems)
    assert metricsfile.stray_blocks(index) == ["mod_x"]
    metricsfile.migrate(index, log=lambda *a: None)
    assert validate_entry(path) == []


def test_save_entry_moves_the_block_out(tmp_path):
    index, path, entry = _index(tmp_path)
    metricsfile.save_entry(index, path, entry)
    on_disk = yaml.safe_load(path.read_text())
    assert "metrics" not in on_disk
    assert yaml.safe_load(metricsfile.path_for(index, "mod_x").read_text())["stars"] == 3
    assert metricsfile.attach(index, on_disk)["metrics"]["stars"] == 3
    # an entry saved without a block leaves the sidecar alone
    on_disk["summary"] = "edited"
    metricsfile.save_entry(index, path, on_disk)
    assert yaml.safe_load(metricsfile.path_for(index, "mod_x").read_text())["stars"] == 3


def test_migrate_round_trips_with_no_semantic_change(tmp_path):
    index, path, entry = _index(tmp_path)
    before = yaml.safe_load(path.read_text())
    stats = metricsfile.migrate(index, dry_run=True, log=lambda *a: None)
    assert stats == {"moved": 1, "already": 0, "entries": 1}
    assert "metrics" in yaml.safe_load(path.read_text())          # dry run wrote nothing
    stats = metricsfile.migrate(index, log=lambda *a: None)
    assert stats["moved"] == 1
    after = yaml.safe_load(path.read_text())
    sidecar = yaml.safe_load(metricsfile.path_for(index, "mod_x").read_text())
    assert {**after, "metrics": sidecar} == before
    assert metricsfile.migrate(index, log=lambda *a: None) == {"moved": 0, "already": 1, "entries": 1}
    assert metricsfile.stray_blocks(index) == []


def test_orphans_and_remove(tmp_path):
    index, path, entry = _index(tmp_path)
    metricsfile.migrate(index, log=lambda *a: None)
    metricsfile.write(index, "mod_gone", {"checked": "2026-10-01"})
    assert metricsfile.orphans(index) == ["mod_gone"]
    assert metricsfile.remove(index, "mod_gone") is True
    assert metricsfile.remove(index, "mod_gone") is False
    assert metricsfile.orphans(index) == []


def test_validate_routes_sidecars_and_flags_orphans(tmp_path):
    from camp.validate import validate_entry, validate_metrics
    index, path, entry = _index(tmp_path)
    metricsfile.migrate(index, log=lambda *a: None)
    assert validate_entry(path) == []
    side = metricsfile.path_for(index, "mod_x")
    assert validate_metrics(side) == []
    side.write_text("stars: -1\nchecked: 2026-10-01\n")
    assert any("stars" in p for p in validate_metrics(side))
    assert any("metrics sidecar" in p for p in validate_entry(path))
    orphan = metricsfile.write(index, "mod_other", {"checked": "2026-10-01"})
    assert any(p.startswith("orphan") for p in validate_metrics(orphan))
