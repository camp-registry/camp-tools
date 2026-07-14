"""camp command-line interface.

    camp validate <entry.yml>...          validate index entries
    camp verify <entry.yml> [--source P]  rebuild + verify every release
    camp build <repo> <tag> <component>   build a canonical ZIP (writes file, prints sha256)
    camp release <entry.yml> <tag> [--source P]
                                          compute and append a release to an entry (author/PR tooling)
    camp composer <index-dir> <base-url> <out.json>
                                          generate Composer packages.json
"""

from __future__ import annotations

import argparse
import json
import datetime
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from . import build as build_mod
from . import composer as composer_mod
from .validate import load_entry, validate_entry, validate_listing
from .verify import verify_entry


def _cmd_validate(args: argparse.Namespace) -> int:
    failed = False
    for path in args.entries:
        problems = validate_entry(path)
        if problems:
            failed = True
            print(f"FAIL {path}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"ok   {path}")
    return 1 if failed else 0


def _cmd_validate_listing(args: argparse.Namespace) -> int:
    failed = False
    for path in args.listings:
        problems = validate_listing(path)
        if problems:
            failed = True
            print(f"FAIL {path}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"ok   {path}")
    return 1 if failed else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    results = verify_entry(args.entry, source_override=args.source)
    failed = False
    for result in results:
        marker = "ok  " if result.ok else "FAIL"
        print(f"{marker} {result.version}")
        for check in result.checks:
            print(f"  + {check}")
        for problem in result.problems:
            print(f"  - {problem}")
        failed = failed or not result.ok
    if not results:
        print("no releases to verify (tier 0 entry?)")
    return 1 if failed else 0


def _cmd_build(args: argparse.Namespace) -> int:
    artifact = build_mod.build_zip(args.repo, args.tag, args.component)
    out = args.out or f"{args.component}-{args.tag}.zip"
    Path(out).write_bytes(artifact.data)
    print(f"{out}  {artifact.file_count} files  commit {artifact.commit[:12]}")
    print(f"sha256 {artifact.sha256}")
    return 0


def _version_php_field(repo: str, commit: str, field: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{commit}:version.php"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    match = re.search(rf"\$plugin->{field}\s*=\s*['\"]?([^'\";]+)['\"]?\s*;", result.stdout)
    return match.group(1).strip() if match else None


def derive_supported_moodle(repo: str, commit: str) -> list[str]:
    """Supported branches from version.php: the explicit $plugin->supported
    range when declared, else just the branch $plugin->requires maps to
    (conservative — the registry doesn't invent claims the author didn't
    make). Empty list if version.php declares neither."""
    from .moodleversions import branch_from_requires, branches_from_supported

    supported_raw = _version_php_field(repo, commit, "supported")
    if supported_raw:
        codes = [int(n) for n in re.findall(r"\d+", supported_raw)]
        branches = branches_from_supported(codes)
        if branches:
            return branches
    requires_raw = _version_php_field(repo, commit, "requires")
    if requires_raw and requires_raw.isdigit():
        branch = branch_from_requires(int(requires_raw))
        if branch:
            return [branch]
    return []


def _cmd_release(args: argparse.Namespace) -> int:
    """Compute a new release record and append it to the entry file."""
    entry_path = Path(args.entry)
    entry = load_entry(entry_path)
    component = entry["component"]

    with tempfile.TemporaryDirectory(prefix="camp-release-") as tmp:
        if args.source:
            repo = args.source
        else:
            repo = str(Path(tmp) / "source")
            subprocess.run(
                ["git", "clone", "--quiet", entry["source"], repo],
                check=True,
            )

        artifact = build_mod.build_zip(repo, args.tag, component)
        release_field = _version_php_field(repo, artifact.commit, "release") or args.tag.lstrip("v")
        version = release_field.split(" ")[0]
        moodle_version = _version_php_field(repo, artifact.commit, "version")
        php_min = _version_php_field(repo, artifact.commit, "php")
        listing_hash = build_mod.file_sha256_at_commit(repo, artifact.commit, ".camp/listing.yml")
        released_ts = build_mod.commit_timestamp(repo, artifact.commit)

        supported = (args.supported_moodle.split(",") if args.supported_moodle
                     else derive_supported_moodle(repo, artifact.commit))

    if any(r["tag"] == args.tag for r in entry["releases"]):
        print(f"error: tag {args.tag} is already in the ledger; releases are immutable", file=sys.stderr)
        return 1
    if not supported:
        print("error: version.php declares neither $plugin->supported nor a mappable "
              "$plugin->requires; pass --supported-moodle explicitly", file=sys.stderr)
        return 1

    record: dict = {
        "version": version,
        "tag": args.tag,
        "commit": artifact.commit,
        "moodle-version": int(moodle_version) if moodle_version else 0,
        "supported-moodle": supported,
        "zip-sha256": artifact.sha256,
        "published": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "released": datetime.datetime.fromtimestamp(released_ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if php_min:
        record["php-min"] = php_min
    if listing_hash:
        record["listing-sha256"] = listing_hash

    entry["releases"].append(record)
    with open(entry_path, "w") as f:
        yaml.safe_dump(entry, f, sort_keys=False, allow_unicode=True)
    print(f"appended {version} ({args.tag} @ {artifact.commit[:12]}) to {entry_path}")
    print(f"zip sha256 {artifact.sha256}")

    problems = validate_entry(entry_path)
    if problems:
        print("entry now INVALID:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def _print_report(report, limit: int = 40) -> None:
    for finding in report.findings[:limit]:
        location = f"{finding.path}:{finding.line}"
        print(f"  {location:<48} {finding.signal}")
        if finding.excerpt:
            print(f"    | {finding.excerpt}")
    if len(report.findings) > limit:
        print(f"  … and {len(report.findings) - limit} more findings")


def _cmd_rekor(args: argparse.Namespace) -> int:
    from . import rekor
    key_path = Path(args.key)
    if not key_path.exists():
        rekor.generate_key(key_path)
        print(f"generated signing key {key_path} (dev key — see module docstring)")
    entry = rekor.build_entry(args.artifact, key_path)
    assert rekor.verify_entry_locally(entry, args.artifact), "entry self-check failed"
    print(json.dumps(entry, indent=2))
    if not args.submit:
        print("\nDRY RUN — entry verified locally but NOT submitted. "
              "Submission to the public log is permanent; use --submit to publish.")
        return 0
    receipt = rekor.submit(entry)
    print(f"submitted: {json.dumps(receipt)[:400]}")
    return 0


def _cmd_tuf(args: argparse.Namespace) -> int:
    from . import tuf_repo
    if args.tuf_command == "init":
        written = tuf_repo.init_keys(args.keys_dir, root_keys=args.root_keys,
                                     threshold=args.threshold)
        for path in written:
            print(f"wrote {path}")
        print(f"root threshold: {args.threshold} of {args.root_keys}")
        print("WARNING: plaintext dev/staging keys — see WARNING.txt")
        return 0
    if args.tuf_command == "sign":
        versions = tuf_repo.sign_repository(args.targets_dir, args.keys_dir,
                                            args.metadata_dir)
        for role, version in versions.items():
            print(f"{role}: v{version}" if role != "target-files"
                  else f"{version} target files signed")
        return 0
    if args.tuf_command == "verify":
        problems = tuf_repo.verify_repository(args.metadata_dir, args.targets_dir)
        if problems:
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("ok: signature chain valid, all targets match signed hashes")
        return 0
    raise AssertionError(args.tuf_command)


def _cmd_scan_malware(args: argparse.Namespace) -> int:
    from .malware import scan
    if args.entry:
        entry = load_entry(args.entry)
        if not entry["releases"]:
            print("no releases to scan (tier 0)")
            return 0
        release = entry["releases"][-1]
        with tempfile.TemporaryDirectory(prefix="camp-malware-") as tmp:
            repo = args.source or str(Path(tmp) / "source")
            if not args.source:
                subprocess.run(["git", "clone", "--quiet", entry["source"], repo], check=True)
            artifact = build_mod.build_zip(repo, release["tag"], entry["component"])
            zip_path = Path(tmp) / "artifact.zip"
            zip_path.write_bytes(artifact.data)
            result = scan(zip_path, entry["component"], release["zip-sha256"])
    else:
        result = scan(args.path)

    for warning in result.warnings:
        print(f"  ! {warning}")
    for detection in result.detections:
        print(f"  - DETECTED: {detection}")
    print(f"engines run: {', '.join(result.engines_run) or 'none'}")
    if not result.clean:
        print("MALWARE DETECTED — this is a hard failure")
        return 1
    if args.require_engine and not result.engines_run:
        print("no scan engine available and --require-engine set")
        return 1
    print("clean" + (" (no engine ran — advisory only)" if not result.engines_run else ""))
    return 0


def _cmd_scaffold(args: argparse.Namespace) -> int:
    from .scaffold import scaffold
    actions, fine = scaffold(args.source_dir, component=args.component,
                             check_only=args.check)
    for item in fine:
        print(f"ok   {item}")
    for item in actions:
        print(f"{'MISSING' if args.check else 'wrote'}  {item}")
    if args.check and actions:
        print("run `camp scaffold` (without --check) to create these, then review and commit")
        return 1
    return 0


def _cmd_lint_labels(args: argparse.Namespace) -> int:
    from .lint import lint_labels
    report = lint_labels(args.source_dir)
    declared = set(args.declared.split(",")) if args.declared else None

    print(f"{len(report.findings)} signal(s) in {args.source_dir}")
    _print_report(report)
    if report.suggested_labels:
        print(f"suggested labels: {', '.join(sorted(report.suggested_labels))}")
    else:
        print("no label signals found (consistent with: fully-free)")

    if declared is not None:
        missing = report.suggested_labels - declared
        if missing:
            print(f"POSSIBLE MISDECLARATION: heuristics suggest {sorted(missing)} "
                  f"but declared labels are {sorted(declared)} — needs human review")
            return 1 if args.strict else 0
        print("declared labels are consistent with heuristics")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    from .lint import audit
    report = audit(args.source_dir)
    if not report.findings:
        print(f"no security-lint findings in {args.source_dir}")
        return 0
    print(f"{len(report.findings)} finding(s) in {args.source_dir} (warn-only; "
          "legitimate uses exist — this is evidence for a human, not a verdict)")
    _print_report(report)
    return 1 if args.strict else 0


def _cmd_ledger_check(args: argparse.Namespace) -> int:
    """Enforce the append-only release ledger (RFC §4.2): every release
    present in the base version of an entry must appear byte-identical in the
    head version. New releases may only be appended."""
    base = load_entry(args.base) if Path(args.base).exists() else {"releases": []}
    head = load_entry(args.head)
    base_releases = base.get("releases", [])
    head_releases = head.get("releases", [])

    if head_releases[: len(base_releases)] != base_releases:
        print("FAIL: existing release records were modified or removed; the ledger is append-only")
        return 1
    added = len(head_releases) - len(base_releases)
    print(f"ok: ledger intact ({len(base_releases)} existing, {added} appended)")
    return 0


def _cmd_composer(args: argparse.Namespace) -> int:
    count = composer_mod.write(args.index_dir, args.base_url.rstrip("/"), args.out)
    print(f"wrote {args.out} ({count} packages)")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import ingest_entry
    failed = False
    for entry_path in args.entries:
        with tempfile.TemporaryDirectory(prefix="camp-ingest-") as tmp:
            if args.source:
                repo = args.source
            else:
                entry = load_entry(entry_path)
                repo = str(Path(tmp) / "source")
                subprocess.run(["git", "clone", "--quiet", entry["source"], repo], check=True)
            result = ingest_entry(entry_path, repo, args.out)
        marker = "ok  " if result.ok else "FAIL"
        print(f"{marker} {result.component}")
        for path in result.wrote:
            print(f"  + {path}")
        for warning in result.warnings:
            print(f"  ! {warning}")
        for problem in result.problems:
            print(f"  - {problem}")
        failed = failed or not result.ok
    return 1 if failed else 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from . import scan as scan_mod
    results = scan_mod.scan(args.index_dir, queries=args.query or None,
                            limit=args.limit, dry_run=args.dry_run,
                            recheck_days=args.recheck_days)
    by_outcome: dict[str, int] = {}
    for result in results:
        by_outcome[result.outcome] = by_outcome.get(result.outcome, 0) + 1
    print(f"\n{len(results)} candidates: " +
          ", ".join(f"{count} {outcome}" for outcome, count in sorted(by_outcome.items())))
    if args.dry_run:
        print("(dry run: nothing written)")
    return 0


def _cmd_advisory_new(args: argparse.Namespace) -> int:
    from .advisory import ADVISORIES_RELPATH, next_id
    year = int(args.year) if args.year else datetime.datetime.now(datetime.UTC).year
    advisory_id = next_id(args.index_dir, year)
    out_path = (Path(args.index_dir) / ADVISORIES_RELPATH / args.component
                / f"{advisory_id}.yml")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    scaffold = {
        "id": advisory_id,
        "component": args.component,
        "title": args.title or "TODO: one-line summary",
        "severity": args.severity,
        "affected-versions": args.affected or "<0.0.1",
        "revoke": False,
        "published": now,
        "description": "TODO: impact and what administrators should do.\n",
    }
    with open(out_path, "w") as f:
        yaml.safe_dump(scaffold, f, sort_keys=False, allow_unicode=True)
    print(f"scaffolded {out_path}")
    print("edit it, then run: camp validate-advisory " + str(out_path))
    return 0


def _cmd_validate_advisory(args: argparse.Namespace) -> int:
    from .advisory import validate_advisory
    failed = False
    for path in args.advisories:
        problems = validate_advisory(path, index_dir=args.index_dir)
        if problems:
            failed = True
            print(f"FAIL {path}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"ok   {path}")
    return 1 if failed else 0


def _cmd_scan_gitlab(args: argparse.Namespace) -> int:
    from . import scan as scan_mod
    results = scan_mod.scan_gitlab(args.index_dir, terms=args.term or None,
                                   limit=args.limit, dry_run=args.dry_run,
                                   recheck_days=args.recheck_days)
    by_outcome: dict[str, int] = {}
    for result in results:
        by_outcome[result.outcome] = by_outcome.get(result.outcome, 0) + 1
    print(f"\n{len(results)} candidates: " +
          ", ".join(f"{count} {outcome}" for outcome, count in sorted(by_outcome.items())))
    if args.dry_run:
        print("(dry run: nothing written)")
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    from .scan import enrich
    enrich(args.index_dir, limit=args.limit, force=args.force, readme=not args.no_readme)
    return 0


def _cmd_recheck_licenses(args: argparse.Namespace) -> int:
    from .scan import recheck_noassertion
    results = recheck_noassertion(args.index_dir, dry_run=args.dry_run)
    by_outcome: dict[str, int] = {}
    for result in results:
        by_outcome[result.outcome] = by_outcome.get(result.outcome, 0) + 1
    print(f"\n{len(results)} re-checked: " +
          ", ".join(f"{count} {outcome}" for outcome, count in sorted(by_outcome.items())))
    if args.dry_run:
        print("(dry run: nothing written)")
    return 0


def _cmd_scan_report(args: argparse.Namespace) -> int:
    from .scan import load_ledger
    ledger = load_ledger(args.index_dir)
    if not ledger:
        print("scan ledger is empty — run `camp scan` first")
        return 0
    by_outcome: dict[str, list[tuple[str, dict]]] = {}
    for repo, record in ledger.items():
        by_outcome.setdefault(record["outcome"], []).append((repo, record))
    print(f"{len(ledger)} repositories evaluated:")
    for outcome, records in sorted(by_outcome.items()):
        print(f"  {len(records):4d} {outcome}")
    if args.outcome:
        print()
        for repo, record in sorted(by_outcome.get(args.outcome, [])):
            print(f"  {repo:<55} {record['detail']}  (last checked {record['last-checked']})")
    return 0


def _cmd_site(args: argparse.Namespace) -> int:
    from . import site as site_mod
    count = site_mod.generate(args.index_dir, args.base_url.rstrip("/"), args.out_dir,
                              listings_dir=args.listings)
    print(f"generated site in {args.out_dir} ({count} plugins)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="camp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate index entries against schema and invariants")
    p.add_argument("entries", nargs="+")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("validate-listing", help="validate .camp/listing.yml manifests")
    p.add_argument("listings", nargs="+")
    p.set_defaults(func=_cmd_validate_listing)

    p = sub.add_parser("verify", help="rebuild and verify every release of an entry")
    p.add_argument("entry")
    p.add_argument("--source", help="use a local checkout instead of cloning")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("build", help="build a canonical distribution ZIP")
    p.add_argument("repo")
    p.add_argument("tag")
    p.add_argument("component")
    p.add_argument("--out")
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("release", help="compute and append a release record to an entry")
    p.add_argument("entry")
    p.add_argument("tag")
    p.add_argument("--source", help="use a local checkout instead of cloning")
    p.add_argument("--supported-moodle", help="comma-separated branches, e.g. 4.3,4.4,4.5")
    p.set_defaults(func=_cmd_release)

    p = sub.add_parser("ledger-check", help="enforce the append-only ledger between two entry versions")
    p.add_argument("base", help="entry file at the PR base (may not exist for new plugins)")
    p.add_argument("head", help="entry file at the PR head")
    p.set_defaults(func=_cmd_ledger_check)

    p = sub.add_parser("composer", help="generate Composer packages.json from the index")
    p.add_argument("index_dir")
    p.add_argument("base_url")
    p.add_argument("out")
    p.set_defaults(func=_cmd_composer)

    p = sub.add_parser("rekor", help="build a Rekor transparency-log entry (dry-run unless --submit)")
    p.add_argument("artifact")
    p.add_argument("--key", default="rekor-signing.pem",
                   help="ECDSA P-256 signing key (generated if missing)")
    p.add_argument("--submit", action="store_true",
                   help="actually publish to the public log — PERMANENT")
    p.set_defaults(func=_cmd_rekor)

    p = sub.add_parser("tuf", help="TUF metadata signing over the published file tree")
    tuf_sub = p.add_subparsers(dest="tuf_command", required=True)
    q = tuf_sub.add_parser("init", help="generate role keys (dev/staging)")
    q.add_argument("keys_dir")
    q.add_argument("--root-keys", type=int, default=1)
    q.add_argument("--threshold", type=int, default=1)
    q.set_defaults(func=_cmd_tuf)
    q = tuf_sub.add_parser("sign", help="sign the target tree, writing versioned metadata")
    q.add_argument("targets_dir")
    q.add_argument("keys_dir")
    q.add_argument("metadata_dir")
    q.set_defaults(func=_cmd_tuf)
    q = tuf_sub.add_parser("verify", help="client-style verification of metadata + targets")
    q.add_argument("metadata_dir")
    q.add_argument("targets_dir")
    q.set_defaults(func=_cmd_tuf)

    p = sub.add_parser("scan-malware", help="malware-scan an artifact or an entry's latest release")
    p.add_argument("path", nargs="?", help="ZIP or directory to scan")
    p.add_argument("--entry", help="index entry: rebuild latest release and scan it")
    p.add_argument("--source", help="local checkout for --entry (skips cloning)")
    p.add_argument("--require-engine", action="store_true",
                   help="fail if no scan engine is available (CI mode)")
    p.set_defaults(func=_cmd_scan_malware)

    p = sub.add_parser("scaffold", help="scaffold .camp/listing.yml + .gitattributes in a plugin repo")
    p.add_argument("source_dir")
    p.add_argument("--component", help="frankenstyle name (default: read from version.php)")
    p.add_argument("--check", action="store_true", help="report what's missing without writing")
    p.set_defaults(func=_cmd_scaffold)

    p = sub.add_parser("lint-labels", help="heuristic disclosure-label check over plugin source")
    p.add_argument("source_dir")
    p.add_argument("--declared", help="comma-separated declared labels to compare against")
    p.add_argument("--strict", action="store_true", help="exit 1 on possible misdeclaration")
    p.set_defaults(func=_cmd_lint_labels)

    p = sub.add_parser("audit", help="basic security lint over plugin source (warn-only)")
    p.add_argument("source_dir")
    p.add_argument("--strict", action="store_true", help="exit 1 when findings exist")
    p.set_defaults(func=_cmd_audit)

    p = sub.add_parser("ingest", help="ingest .camp/listing.yml (+screenshots) at the released commit")
    p.add_argument("entries", nargs="+")
    p.add_argument("--source", help="local checkout to read from instead of cloning (single entry only)")
    p.add_argument("--out", default="dist/listings", help="output listings directory")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("scan", help="discover Moodle plugins on GitHub and write Tier 0 entries")
    p.add_argument("index_dir")
    p.add_argument("--query", action="append",
                   help="GitHub search query (repeatable; default: topic + naming convention)")
    p.add_argument("--limit", type=int, default=30, help="max results per query (default 30)")
    p.add_argument("--dry-run", action="store_true", help="report without writing entries")
    p.add_argument("--recheck-days", type=int, default=30,
                   help="re-evaluate ledger-rejected repos older than this (0 = always recheck)")
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("advisory", help="scaffold a security advisory (RFC §5.3)")
    p.add_argument("index_dir")
    p.add_argument("component")
    p.add_argument("--severity", choices=["low", "medium", "high", "critical"], required=True)
    p.add_argument("--title")
    p.add_argument("--affected", help='constraint, e.g. ">=1.0,<1.4.2"')
    p.add_argument("--year", help="advisory-id year (default: current)")
    p.set_defaults(func=_cmd_advisory_new)

    p = sub.add_parser("validate-advisory", help="validate advisory files")
    p.add_argument("advisories", nargs="+")
    p.add_argument("--index-dir", help="also cross-check the component exists in this index")
    p.set_defaults(func=_cmd_validate_advisory)

    p = sub.add_parser("scan-gitlab", help="discover Moodle plugins on GitLab.com and write Tier 0 entries")
    p.add_argument("index_dir")
    p.add_argument("--term", action="append",
                   help="GitLab project search term (repeatable; default: frankenstyle prefixes)")
    p.add_argument("--limit", type=int, default=50, help="max results per term (default 50)")
    p.add_argument("--dry-run", action="store_true", help="report without writing entries")
    p.add_argument("--recheck-days", type=int, default=30,
                   help="re-evaluate ledger-rejected repos older than this (0 = always recheck)")
    p.set_defaults(func=_cmd_scan_gitlab)

    p = sub.add_parser("enrich",
                       help="backfill/refresh Tier 0 metrics + README-derived summaries")
    p.add_argument("index_dir")
    p.add_argument("--limit", type=int, default=None,
                   help="cap repos contacted (for sampling); default: all un-enriched entries")
    p.add_argument("--force", action="store_true",
                   help="refresh entries even if they already have metrics/summary")
    p.add_argument("--no-readme", action="store_true",
                   help="skip the README summary fallback (metrics only)")
    p.set_defaults(func=_cmd_enrich)

    p = sub.add_parser("recheck-licenses",
                       help="re-check NOASSERTION rejections by classifying license file text")
    p.add_argument("index_dir")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_recheck_licenses)

    p = sub.add_parser("scan-report", help="summarize the scan ledger (rejections and why)")
    p.add_argument("index_dir")
    p.add_argument("--outcome", help="list all repos with this outcome (e.g. bad-license)")
    p.set_defaults(func=_cmd_scan_report)

    p = sub.add_parser("site", help="generate the static browse/detail website from the index")
    p.add_argument("index_dir")
    p.add_argument("base_url")
    p.add_argument("out_dir")
    p.add_argument("--listings", help="directory of <component>.yml listing manifests "
                                      "(preview flag until release-time ingestion lands)")
    p.set_defaults(func=_cmd_site)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
