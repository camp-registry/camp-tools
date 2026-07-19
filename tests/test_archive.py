"""The append-only artifact archive (D22): deposit and audit."""

import yaml

from camp.archive import audit, deposit


class FakeStore:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def head(self, key):
        obj = self.objects.get(key)
        return None if obj is None else {"sha256": obj["sha256"]}

    def put(self, key, data, sha256):
        assert key not in self.objects, "append-only: overwrite attempted"
        self.objects[key] = {"data": data, "sha256": sha256}
        self.puts.append(key)


def _point_at_local_repo(entry_path, plugin_repo):
    entry = yaml.safe_load(entry_path.read_text())
    entry["source"] = str(plugin_repo)
    entry_path.write_text(yaml.safe_dump(entry, sort_keys=False))
    return entry


def test_deposit_builds_verifies_and_writes(index_dir, entry_path, plugin_repo):
    entry = _point_at_local_repo(entry_path, plugin_repo)
    store = FakeStore()
    result = deposit(index_dir, store, log=lambda *a: None)
    assert result.ok and result.deposited == 1
    key = "mod_example/mod_example-1.0.0.zip"
    assert store.objects[key]["sha256"] == entry["releases"][0]["zip-sha256"]

    # idempotent: second run touches nothing
    again = deposit(index_dir, store, log=lambda *a: None)
    assert again.ok and again.deposited == 0 and again.present == 1
    assert store.puts == [key]


def test_deposit_refuses_on_archived_hash_mismatch(index_dir, entry_path, plugin_repo):
    _point_at_local_repo(entry_path, plugin_repo)
    store = FakeStore()
    store.objects["mod_example/mod_example-1.0.0.zip"] = {
        "data": b"tampered", "sha256": "f" * 64}
    result = deposit(index_dir, store, log=lambda *a: None)
    assert not result.ok
    assert any("REFUSING" in p for p in result.problems)
    assert store.puts == []                      # nothing written over it


def test_deposit_includes_revoked_releases(index_dir, entry_path, plugin_repo, tmp_path):
    _point_at_local_repo(entry_path, plugin_repo)
    adv = index_dir / "advisories" / "mod"
    adv.mkdir(parents=True)
    (adv / "CAMP-2026-T.yml").write_text(
        "id: CAMP-2026-T\ncomponent: mod_example\ntitle: t\nseverity: high\n"
        'affected-versions: "= 1.0.0"\nrevoke: true\npublished: "2026-07-19"\n')
    store = FakeStore()
    result = deposit(index_dir, store, log=lambda *a: None)
    assert result.ok and result.deposited == 1   # archive keeps everything


def test_audit_flags_missing_and_mismatched(index_dir, entry_path, plugin_repo):
    entry = _point_at_local_repo(entry_path, plugin_repo)
    store = FakeStore()
    result = audit(index_dir, store, log=lambda *a: None)
    assert not result.ok and any("MISSING" in p for p in result.problems)

    store.objects["mod_example/mod_example-1.0.0.zip"] = {
        "data": b"x", "sha256": "f" * 64}
    result = audit(index_dir, store, log=lambda *a: None)
    assert not result.ok and any("!=" in p for p in result.problems)

    store.objects["mod_example/mod_example-1.0.0.zip"]["sha256"] = \
        entry["releases"][0]["zip-sha256"]
    result = audit(index_dir, store, log=lambda *a: None)
    assert result.ok and result.present == 1


def test_artifacts_base_switches_urls(index_dir, tmp_path):
    from camp.composer import generate as composer_generate
    from camp.site import generate as site_generate

    doc = composer_generate(index_dir, "https://repo.test",
                            artifacts_base="https://artifacts.test")
    url = next(iter(next(iter(doc["packages"].values())).values()))["dist"]["url"]
    assert url.startswith("https://artifacts.test/mod_example/")

    default = composer_generate(index_dir, "https://repo.test")
    url = next(iter(next(iter(default["packages"].values())).values()))["dist"]["url"]
    assert url.startswith("https://repo.test/artifacts/")

    out = tmp_path / "site"
    site_generate(index_dir, "https://repo.test", out,
                  artifacts_base="https://artifacts.test")
    html = (out / "plugin" / "mod_example.html").read_text()
    assert "https://artifacts.test/mod_example/mod_example-1.0.0.zip" in html
    assert "https://repo.test/artifacts/" not in html
