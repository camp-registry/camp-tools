"""Static heuristics over plugin source: disclosure labels and security lint.

Both are warn-only by design. The RFC is explicit that heuristics "flag
likely misdeclarations for human attention" (§4.7) and that the trust floor
includes "basic security lint" (§4.2) — neither is an oracle. Moodle plugins
legitimately shell out (VPL runs student code), call external APIs, and
store keys; findings are evidence for a human, and for Tier 3 review.

Label heuristics (`camp lint-labels`):
  - credential-shaped admin settings (api key, license key, token, secret)
    → suggests `paid-service` or `external-account`
  - premium/upgrade/subscription language in lang strings → suggests `freemium`
  - hardcoded remote service endpoints + HTTP client usage
    → suggests `external-account`

Security lint (`camp audit`):
  - dynamic code execution (eval, create_function, preg_replace /e)
  - process execution (shell_exec, system, exec, passthru, proc_open, popen)
  - obfuscation smells (eval-adjacent base64_decode, very long opaque literals)
  - request data flowing into include/require
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class Finding:
    path: str
    line: int
    signal: str
    excerpt: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    suggested_labels: set[str] = field(default_factory=set)

    def add(self, path: Path, line_no: int, signal: str, line: str,
            label: str | None = None) -> None:
        self.findings.append(Finding(str(path), line_no, signal, line.strip()[:120]))
        if label:
            self.suggested_labels.add(label)


# --- disclosure-label heuristics -------------------------------------------

CREDENTIAL_SETTING = re.compile(
    r"(?:admin_setting_config\w+|get_config|set_config)\s*\([^)]*"
    r"['\"][^'\"]*(api[_-]?key|apikey|license[_-]?key|licen[cs]ekey|secret|"
    r"auth[_-]?token|accesstoken|subscription)", re.IGNORECASE)
CREDENTIAL_NAME = re.compile(
    r"['\"](api[_-]?key|apikey|license[_-]?key|licen[cs]ekey|client[_-]?secret)['\"]",
    re.IGNORECASE)
PREMIUM_LANG = re.compile(
    r"(premium|pro version|upgrade to (?:pro|paid)|subscription required|"
    r"paid plan|purchase a licen[cs]e)", re.IGNORECASE)
REMOTE_ENDPOINT = re.compile(
    r"['\"]https://(?!(?:github\.com|gitlab\.com|moodle\.org|docs\.moodle\.org|"
    r"purl\.org|www\.w3\.org|raw\.githubusercontent\.com)/)"
    r"[a-z0-9.-]+\.[a-z]{2,}/[^'\"]*['\"]", re.IGNORECASE)
HTTP_CLIENT = re.compile(r"\b(curl_init|curl_exec|download_file_content|\\?GuzzleHttp|new\s+curl)\b")


def _php_files(root: Path):
    for path in sorted(root.rglob("*.php")):
        relative = path.relative_to(root)
        if any(part in ("vendor", "node_modules", "tests", ".git") for part in relative.parts):
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        yield path, relative


def lint_labels(source_dir: str | Path) -> Report:
    root = Path(source_dir)
    report = Report()
    uses_http = False
    endpoint_hits: list[tuple[Path, int, str]] = []

    for path, relative in _php_files(root):
        is_settings = relative.name in ("settings.php", "settingslib.php") or "setting" in relative.name
        for line_no, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if CREDENTIAL_SETTING.search(line) or (is_settings and CREDENTIAL_NAME.search(line)):
                report.add(relative, line_no, "credential-shaped setting", line, "paid-service")
                report.suggested_labels.add("external-account")
            if PREMIUM_LANG.search(line) and "/lang/" in f"/{relative}":
                report.add(relative, line_no, "premium/upgrade language", line, "freemium")
            if HTTP_CLIENT.search(line):
                uses_http = True
            match = REMOTE_ENDPOINT.search(line)
            if match:
                endpoint_hits.append((relative, line_no, line))

    if uses_http and endpoint_hits:
        for relative, line_no, line in endpoint_hits[:5]:
            report.add(relative, line_no, "hardcoded remote endpoint + HTTP client", line,
                       "external-account")
    return report


# --- basic security lint -----------------------------------------------------

DANGEROUS = [
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bcreate_function\s*\("), "create_function()"),
    (re.compile(r"\bassert\s*\(\s*\$"), "assert() on variable"),
    (re.compile(r"preg_replace\s*\(\s*['\"][^'\"]*e[^'\"]*['\"]\s*,"), "preg_replace /e"),
    (re.compile(r"\b(shell_exec|passthru|proc_open|popen|pcntl_exec)\s*\("), "process execution"),
    (re.compile(r"\b(system|exec)\s*\("), "process execution"),
    (re.compile(r"\b(include|require)(_once)?\s*\(?\s*\$_(GET|POST|REQUEST|COOKIE)"),
     "request data in include"),
    (re.compile(r"base64_decode\s*\([^)]*\)\s*\)?\s*;?\s*$"), "base64_decode"),
]
OPAQUE_LITERAL = re.compile(r"['\"][A-Za-z0-9+/=\\x]{500,}['\"]")


def audit(source_dir: str | Path) -> Report:
    root = Path(source_dir)
    report = Report()
    for path, relative in _php_files(root):
        text = path.read_text(errors="replace")
        base64_lines: list[int] = []
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*", "#")):
                continue
            for pattern, signal in DANGEROUS:
                if pattern.search(line):
                    if signal == "base64_decode":
                        base64_lines.append(line_no)
                    else:
                        report.add(relative, line_no, signal, line)
            if OPAQUE_LITERAL.search(line):
                report.add(relative, line_no, "very long opaque literal", line)
        # base64_decode alone is common (e.g. data URIs); only flag when the
        # same file also executes code dynamically or spawns processes.
        if base64_lines and any(f.path == str(relative) for f in report.findings):
            for line_no in base64_lines:
                report.add(relative, line_no, "base64_decode near dynamic execution", "")
    return report
