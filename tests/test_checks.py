"""Code-check helpers (camp checks)."""
from pathlib import Path

from camp.checks import _is_thirdparty, thirdparty_locations


def _plugin(tmp_path, manifest):
    root = tmp_path / "mod_x"
    root.mkdir()
    (root / "thirdpartylibs.xml").write_text(manifest)
    return root


def test_declared_locations_are_normalised(tmp_path):
    root = _plugin(tmp_path, """<?xml version="1.0"?>
<libraries>
  <library><location>vendor/league</location><name>league</name><license>MIT</license></library>
  <library><location>./amd/src/lib.js/</location><name>lib</name><license>MIT</license></library>
  <library><location>../boost/scss</location><name>outside</name><license>GPL</license></library>
  <library><location></location><name>empty</name><license>MIT</license></library>
</libraries>""")
    assert thirdparty_locations(root) == ["vendor/league", "amd/src/lib.js"]
    assert _is_thirdparty(root / "vendor/league/x.php", root, ["vendor/league"])
    assert _is_thirdparty(root / "amd/src/lib.js", root, ["amd/src/lib.js"])
    assert not _is_thirdparty(root / "vendor/leagueX/x.php", root, ["vendor/league"])
    assert not _is_thirdparty(root / "lib.php", root, ["vendor/league"])


def test_missing_or_broken_manifest_skips_nothing(tmp_path):
    root = tmp_path / "mod_y"
    root.mkdir()
    assert thirdparty_locations(root) == []
    (root / "thirdpartylibs.xml").write_text("<libraries><library>")
    assert thirdparty_locations(root) == []
