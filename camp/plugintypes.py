"""Moodle plugin-type knowledge: every type prefix a component can carry,
per branch (camp-tools#16, #24).

A component's frankenstyle prefix is only meaningful against the set of
plugin types that actually exist, and that set is per-branch data with two
core sources: the plugintypes map in lib/components.json (plus the
deprecatedplugintypes map newer branches add), and the subplugin types
core plugins declare in their db/subplugins.json (quizaccess from
mod_quiz, tiny from editor_tiny, factor from tool_mfa, ...). The committed
table in plugintypes.json records, for every prefix either source names
across the tracked branches, the branches where it exists, its parent
component when it is a subplugin type, and the branches that list it as
deprecated.

Third-party subplugin families (customcertelement from mod_customcert and
kin) are registry decisions, not upstream facts, so they live outside this
file: discovery/subplugin-families.yml in the index tree, one record per
family established by human review (camp-tools#16). load_established()
reads that file; known_prefixes() folds it in.

Display names and browse categories are curated here, not generated:
components.json carries no human names. Sources are the old directory's
tree (camp-tools#24) and Moodle's own admin UI terms.

Like standardplugins.json, the table is committed and hand-verifiable;
`camp check-plugin-types` alerts when it drifts from upstream and --write
refreshes it (a reviewed registry act, never automatic). The tracked
window is moodleversions.BRANCHES: this table answers "is this prefix a
real plugin type" and "what do we call it", not lifecycle history, and
every prefix the index has ever listed either appears in-window or is
third-party material for the established-families file.
"""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import yaml

from . import standardplugins
from .moodleversions import BRANCHES

DATA_PATH = Path(__file__).parent / "plugintypes.json"
ESTABLISHED_PATH = "discovery/subplugin-families.yml"
_RAW_BASE = "https://raw.githubusercontent.com/moodle/moodle"

# Curated display names for core-derived prefixes, seeded from the old
# directory's category tree (camp-tools#24) and Moodle's admin UI.
# Established third-party families carry their name in their own record;
# anything unnamed falls back to the raw prefix at the display layer.
DISPLAY_NAMES = {
    "aiplacement": "AI placements",
    "aiprovider": "AI providers",
    "antivirus": "Antivirus scanners",
    "assignfeedback": "Assignment feedback",
    "assignment": "Assignment types (legacy)",
    "assignsubmission": "Assignment submissions",
    "atto": "Atto editor plugins",
    "auth": "Authentication methods",
    "availability": "Availability restrictions",
    "bbbext": "BigBlueButton extensions",
    "block": "Blocks",
    "booktool": "Book tools",
    "cachelock": "Cache locks",
    "cachestore": "Cache stores",
    "calendartype": "Calendar types",
    "communication": "Communication providers",
    "contenttype": "Content bank types",
    "coursereport": "Course reports (legacy)",
    "customfield": "Custom fields",
    "datafield": "Database fields",
    "dataformat": "Data formats",
    "datapreset": "Database presets",
    "editor": "Text editors",
    "enrol": "Enrolment methods",
    "factor": "MFA factors",
    "fileconverter": "Document converters",
    "filter": "Filters",
    "format": "Course formats",
    "forumreport": "Forum reports",
    "gradeexport": "Grade exports",
    "gradeimport": "Grade imports",
    "gradepenalty": "Grade penalties",
    "gradereport": "Grade reports",
    "gradingform": "Grading methods",
    "h5plib": "H5P libraries",
    "local": "Local plugins",
    "logstore": "Log stores",
    "ltiservice": "LTI services",
    "ltisource": "LTI sources",
    "media": "Media players",
    "message": "Notification outputs",
    "mlbackend": "Machine learning backends",
    "mnetservice": "MNet services",
    "mod": "Activity modules",
    "paygw": "Payment gateways",
    "plagiarism": "Plagiarism detectors",
    "portfolio": "Portfolios",
    "profilefield": "User profile fields",
    "qbank": "Question bank plugins",
    "qbehaviour": "Question behaviours",
    "qformat": "Question formats",
    "qtype": "Question types",
    "quiz": "Quiz reports",
    "quizaccess": "Quiz access rules",
    "report": "Admin reports",
    "repository": "Repositories",
    "scormreport": "SCORM reports",
    "search": "Search engines",
    "smsgateway": "SMS gateways",
    "theme": "Themes",
    "tiny": "TinyMCE plugins",
    "tinymce": "TinyMCE (legacy) plugins",
    "tool": "Admin tools",
    "webservice": "Web service protocols",
    "workshopallocation": "Workshop allocation strategies",
    "workshopeval": "Workshop evaluation plugins",
    "workshopform": "Workshop grading forms",
}

# Browse-page grouping (camp-tools#24, grouped-hierarchy filter). Order is
# the display order; prefixes missing from CATEGORIES render under Other.
CATEGORY_ORDER = [
    "Activities", "Course", "Content", "Grades", "Administration",
    "Messaging", "Reports", "AI", "Themes", "Local plugins", "Other",
]
CATEGORIES = {
    "mod": "Activities",
    "quiz": "Activities", "quizaccess": "Activities",
    "qtype": "Activities", "qbank": "Activities",
    "qbehaviour": "Activities", "qformat": "Activities",
    "assignsubmission": "Activities", "assignfeedback": "Activities",
    "assignment": "Activities",
    "booktool": "Activities",
    "datafield": "Activities", "datapreset": "Activities",
    "forumreport": "Activities",
    "ltisource": "Activities", "ltiservice": "Activities",
    "scormreport": "Activities",
    "workshopallocation": "Activities", "workshopeval": "Activities",
    "workshopform": "Activities",
    "bbbext": "Activities",
    "block": "Course", "format": "Course", "enrol": "Course",
    "availability": "Course", "customfield": "Course",
    "editor": "Content", "atto": "Content", "tiny": "Content",
    "tinymce": "Content", "filter": "Content", "repository": "Content",
    "plagiarism": "Content", "portfolio": "Content",
    "fileconverter": "Content", "contenttype": "Content",
    "media": "Content", "h5plib": "Content",
    "gradereport": "Grades", "gradeexport": "Grades",
    "gradeimport": "Grades", "gradingform": "Grades",
    "gradepenalty": "Grades",
    "tool": "Administration", "auth": "Administration",
    "cachestore": "Administration", "cachelock": "Administration",
    "search": "Administration", "profilefield": "Administration",
    "logstore": "Administration", "webservice": "Administration",
    "calendartype": "Administration", "dataformat": "Administration",
    "paygw": "Administration", "antivirus": "Administration",
    "factor": "Administration", "mlbackend": "Administration",
    "mnetservice": "Administration",
    "message": "Messaging", "communication": "Messaging",
    "smsgateway": "Messaging",
    "report": "Reports", "coursereport": "Reports",
    "aiprovider": "AI", "aiplacement": "AI",
    "theme": "Themes",
    "local": "Local plugins",
}


def _branch_ref(code: int) -> str:
    return f"MOODLE_{code}_STABLE"


@lru_cache(maxsize=1)
def load() -> dict:
    with open(DATA_PATH) as f:
        return json.load(f)


def load_established(index_dir) -> dict:
    """Established third-party subplugin families from the index tree:
    {prefix: {parent, name, ...}}. Missing file means none established;
    a malformed file raises — the registry's own data must parse."""
    path = Path(index_dir) / ESTABLISHED_PATH
    if not path.exists():
        return {}
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping of prefix -> record")
    for prefix, record in doc.items():
        if not isinstance(record, dict) or "parent" not in record:
            raise ValueError(f"{path}: family '{prefix}' needs a mapping "
                             "with at least 'parent'")
    return doc


def known_prefixes(established: dict | None = None) -> set[str]:
    """Every prefix the registry recognizes as a real plugin type: core
    table plus established third-party families. The scanner's gate."""
    return set(load()["types"]) | set(established or {})


def parent(prefix: str, established: dict | None = None) -> str | None:
    """Parent component for subplugin types, None for top-level types
    (and for unknown prefixes, which have no facts at all)."""
    record = load()["types"].get(prefix)
    if record is not None:
        return record.get("parent")
    return ((established or {}).get(prefix) or {}).get("parent")


def display_name(prefix: str, established: dict | None = None) -> str | None:
    """Curated name, or the established family's recorded name, or None —
    display code falls back to the raw prefix, never invents a name."""
    name = DISPLAY_NAMES.get(prefix)
    if name:
        return name
    return ((established or {}).get(prefix) or {}).get("name")


def category(prefix: str, established: dict | None = None) -> str:
    """Browse-page group. Established families inherit their parent's
    category unless their record names one; unknowns land in Other."""
    if prefix in CATEGORIES:
        return CATEGORIES[prefix]
    record = (established or {}).get(prefix)
    if record:
        if record.get("category"):
            return record["category"]
        parent_prefix = (record.get("parent") or "").partition("_")[0]
        if parent_prefix in CATEGORIES:
            return CATEGORIES[parent_prefix]
    return "Other"


# --- generation / drift check ------------------------------------------------


def _fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "camp-tools"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def fetch_branch(code: int, fetch=_fetch) -> dict[str, dict]:
    """{prefix: {parent, deprecated}} for one branch: the type maps from
    lib/components.json, plus the subplugin types found by probing
    db/subplugins.json in every standard plugin's directory. The standard
    list comes from upstream on the spot (standardplugins.fetch_branch),
    never from the committed table, so this check cannot inherit staleness
    from the other one. Probing beats a recursive tree listing because the
    tree API truncates on newer branches. A 404 just means a plugin
    declares no subplugins; anything else raises — a partial table must
    never be written silently."""
    ref = _branch_ref(code)
    status, body = fetch(f"{_RAW_BASE}/{ref}/lib/components.json")
    if status != 200:
        raise RuntimeError(f"cannot fetch lib/components.json for {ref}")
    doc = json.loads(body)
    plugintypes = doc.get("plugintypes") or {}
    deprecated = doc.get("deprecatedplugintypes") or {}
    found: dict[str, dict] = {}
    for prefix in plugintypes:
        found[prefix] = {"parent": None, "deprecated": False}
    for prefix in deprecated:
        found[prefix] = {"parent": None, "deprecated": True}

    standard, _ = standardplugins.fetch_branch(code, fetch=fetch)
    typepaths = {**plugintypes, **deprecated}
    probes = []
    for component in sorted(standard):
        ptype, _, name = component.partition("_")
        root = typepaths.get(ptype)
        if root:
            probes.append((component,
                           f"{_RAW_BASE}/{ref}/{root}/{name}/db/subplugins.json"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda p: (p[0], *fetch(p[1])), probes)
    for component, status, body in results:
        if status == 404:
            continue
        if status != 200:
            raise RuntimeError(f"cannot probe subplugins of {component} "
                               f"for {ref} (HTTP {status})")
        # both spellings carry the same type names; older branches have
        # only the path-valued plugintypes map
        sub = json.loads(body)
        for prefix in (sub.get("subplugintypes") or sub.get("plugintypes") or {}):
            found[prefix] = {"parent": component, "deprecated": False}
    return found


def build_table(fetch=_fetch, log=lambda *_: None) -> dict:
    types: dict[str, dict] = {}
    branch_names = []
    for code, branch, _ in BRANCHES:
        branch_names.append(branch)
        found = fetch_branch(code, fetch=fetch)
        subplugins = sum(1 for r in found.values() if r["parent"])
        log(f"  {branch}: {len(found)} types ({subplugins} subplugin)")
        for prefix, record in found.items():
            entry = types.setdefault(prefix, {"branches": []})
            entry["branches"].append(branch)
            if record["parent"]:
                entry["parent"] = record["parent"]
            if record["deprecated"]:
                entry.setdefault("deprecated", []).append(branch)
    ordered = {}
    for prefix in sorted(types):
        record = types[prefix]
        ordered[prefix] = {k: record[k] for k in ("parent", "branches",
                                                  "deprecated") if k in record}
    return {"branches": branch_names, "types": ordered}


def write_table(table: dict) -> None:
    with open(DATA_PATH, "w") as f:
        json.dump(table, f, indent=1, sort_keys=False)
        f.write("\n")
    load.cache_clear()
