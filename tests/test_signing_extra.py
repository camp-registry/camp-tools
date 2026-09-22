"""`camp tuf` and `camp rekor` need the `signing` extra (python-tuf,
securesystemslib, cryptography); without it they must say so instead of
tracing back, and no other command may import those modules."""

import subprocess
import sys

import pytest

import camp
from camp.cli import main


@pytest.mark.parametrize("module, argv", [
    ("camp.tuf_repo", ["tuf", "verify", "m", "t"]),
    ("camp.rekor", ["rekor", "artifact.zip"]),
])
def test_missing_signing_extra_is_reported(monkeypatch, capsys, tmp_path, module, argv):
    # If the import unexpectedly succeeds, `camp rekor` would write a key
    # into the working directory; keep any such regression out of the repo.
    monkeypatch.chdir(tmp_path)
    # A None entry in sys.modules makes `from . import x` raise ImportError,
    # which is what a base install without the extra produces. Earlier tests
    # may have imported the module already, which also binds it as an
    # attribute of the package; `from . import x` returns that binding
    # without consulting sys.modules, so drop it too.
    monkeypatch.delattr(camp, module.rsplit(".", 1)[1], raising=False)
    monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setitem(sys.modules, module, None)
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "signing extra" in err
    assert "pip install 'camp-tools[signing]'" in err


def test_base_import_does_not_pull_signing_modules():
    # Importing the CLI (what `camp --help` does) must not touch the
    # signing modules; a fresh interpreter proves it.
    code = ("import sys, camp.cli; "
            "print(sorted(m for m in sys.modules "
            "if m in ('tuf', 'securesystemslib', 'cryptography')))")
    out = subprocess.run([sys.executable, "-c", code], check=True,
                         capture_output=True, text=True).stdout.strip()
    assert out == "[]"
