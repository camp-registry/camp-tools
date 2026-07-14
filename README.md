# camp-tools

The `camp` command-line tool: registry tooling for **camp**, a
community-governed plugin repository for Moodle.

```
pip install "git+https://github.com/camp-registry/camp-tools@v0.1.0"
```

What it does (RFC references throughout are to the
[camp RFC](https://github.com/camp-registry/camp-docs/blob/main/rfc-community-plugin-repository.md)):

- `camp validate` / `camp ledger-check` — index entry schema + append-only
  release ledger enforcement (§4.1, §4.2)
- `camp build` / `camp verify` / `camp release` — deterministic canonical
  ZIPs, rebuild-and-verify from tagged source, release record computation
- `camp composer` / `camp site` — Composer repository metadata (§6.1) and
  the static browse website (§4.5)
- `camp scan` / `camp scan-gitlab` / `camp enrich` — ecosystem discovery
  (Tier 0 seeding, §4.4) and upstream activity metrics
- `camp scaffold` / `camp lint-labels` / `camp audit` — author-side
  manifest scaffolding and warn-only heuristics (§4.7)
- `camp advisory` / `camp scan-malware` / `camp tuf` / `camp rekor` —
  security advisories (§5), malware scanning, signing and transparency
  (§4.3)

The JSON Schemas for index entries, listing manifests, and advisories ship
inside the package (`camp/schema/`) — they are the tool's contract with
[camp-index](https://github.com/camp-registry/camp-index).

Python ≥3.11. GPL-3.0-or-later. Tests: `python -m pytest tests`.
