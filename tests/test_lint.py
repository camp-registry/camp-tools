"""Label heuristics and security lint detection."""

from camp.lint import audit, lint_labels


def _plugin(tmp_path, files: dict[str, str]):
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def test_labels_credential_setting(tmp_path):
    _plugin(tmp_path, {"settings.php": (
        "<?php\n$settings->add(new admin_setting_configpasswordunmask("
        "'local_x/apikey', 'API key', '', ''));\n")})
    report = lint_labels(tmp_path)
    assert "paid-service" in report.suggested_labels
    assert "external-account" in report.suggested_labels


def test_labels_premium_language(tmp_path):
    _plugin(tmp_path, {"lang/en/local_x.php": (
        "<?php\n$string['upsell'] = 'Upgrade to Pro for more features';\n")})
    report = lint_labels(tmp_path)
    assert "freemium" in report.suggested_labels


def test_labels_remote_endpoint_needs_http_client(tmp_path):
    # endpoint string alone (e.g. a docs link) is not enough
    _plugin(tmp_path, {"lib.php": "<?php\n$url = 'https://service.example.com/v1/things';\n"})
    assert lint_labels(tmp_path).suggested_labels == set()

    _plugin(tmp_path, {"client.php": (
        "<?php\n$ch = curl_init();\n"
        "$url = 'https://service.example.com/v1/things';\n")})
    assert "external-account" in lint_labels(tmp_path).suggested_labels


def test_labels_clean_plugin(tmp_path):
    _plugin(tmp_path, {"lib.php": "<?php\nfunction local_x_hello() { return 1; }\n"})
    report = lint_labels(tmp_path)
    assert report.findings == [] and report.suggested_labels == set()


def test_audit_detects_dynamic_execution(tmp_path):
    _plugin(tmp_path, {"evil.php": (
        "<?php\n"
        "eval(base64_decode($payload));\n"
        "shell_exec('rm -rf ' . $dir);\n"
        "include($_GET['page']);\n")})
    report = audit(tmp_path)
    signals = {f.signal for f in report.findings}
    assert "eval()" in signals
    assert "process execution" in signals
    assert "request data in include" in signals
    assert "base64_decode near dynamic execution" in signals


def test_audit_ignores_comments_and_vendor(tmp_path):
    _plugin(tmp_path, {
        "lib.php": "<?php\n// eval() is never used here\n$x = 1;\n",
        "vendor/dep/bad.php": "<?php\neval($x);\n",
    })
    assert audit(tmp_path).findings == []


def test_scaffold_creates_valid_listing(tmp_path):
    from camp.scaffold import scaffold
    from camp.validate import validate_listing
    (tmp_path / "version.php").write_text("<?php\n$plugin->component = 'block_demo';\n")
    (tmp_path / "README.md").write_text("# Demo\nA demo block that does demo things for demos.\n")

    actions, fine = scaffold(tmp_path, check_only=True)
    assert len(actions) == 2 and fine == []

    scaffold(tmp_path)
    assert validate_listing(tmp_path / ".camp" / "listing.yml") == []
    assert "block_demo" in (tmp_path / ".camp" / "listing.yml").read_text()
    assert "export-ignore" in (tmp_path / ".gitattributes").read_text()

    # second run is a no-op
    actions, fine = scaffold(tmp_path, check_only=True)
    assert actions == [] and len(fine) == 2
