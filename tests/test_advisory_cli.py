"""`camp advisory` scaffold flags for withdrawals (camp-tools#58)."""

import yaml

from camp import cli
from camp.advisory import validate_advisory


def test_scaffold_withdrawal_sets_revoke_and_fixed_in(tmp_path, capsys):
    rc = cli.main(["advisory", str(tmp_path), "mod_x", "--severity", "low",
                   "--affected", "=1.0.0", "--revoke", "--fixed-in", "1.0.1",
                   "--title", "Defective release, withdrawn"])
    assert rc == 0
    out = capsys.readouterr().out
    path = tmp_path / "advisories" / "CAMP-{}-0001.yml".format(
        __import__("datetime").datetime.now(__import__("datetime").UTC).year)
    assert str(path) in out
    doc = yaml.safe_load(path.read_text())
    assert doc["revoke"] is True
    assert doc["fixed-in"] == "1.0.1"
    # schema key order: affected-versions, fixed-in, revoke, published
    keys = list(doc)
    assert keys.index("affected-versions") < keys.index("fixed-in") < keys.index("revoke") < keys.index("published")
    assert validate_advisory(path) == []


def test_scaffold_default_is_not_a_withdrawal(tmp_path):
    assert cli.main(["advisory", str(tmp_path), "mod_x", "--severity", "low"]) == 0
    doc = yaml.safe_load(next((tmp_path / "advisories").glob("*.yml")).read_text())
    assert doc["revoke"] is False
    assert "fixed-in" not in doc
