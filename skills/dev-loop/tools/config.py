"""merge-scheduler configuration.

Looked for in order: $MERGE_SCHEDULER_CONFIG, ./merge-scheduler.json, ~/.config/merge-scheduler/config.json.
See merge-scheduler.example.json at the repo root for every key. Keep this file identical in both skills.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "org": None,                       # GitHub owner (org or user) of the repos
    "repos": [],                       # explicit repo names, or...
    "repo_prefixes": [],               # ...every repo whose name starts with one of these
    "authors": [],                     # PR authors the scheduler watches ("dependabot" for the bot)
    "queue_cap": 3,                    # merge-queue entries per repo, counting anyone's
    "aging_hours": 24,                 # +1 priority per this many hours a group has been ready...
    "aging_cap": 2,                    # ...up to this many levels
    "tiers": {},                       # repo -> order inside a group with no Depends-On (lower first; default 3)
    "aliases": {},                     # short name -> repo, for Depends-On: api#12
    "schedule_repo": None,             # repo holding the pinned "Merge schedule" issue (default: first repo)
    "manual": {                        # PRs the scheduler never dispatches; a human merges them
        "repo_prefixes": [],
        "title_regex": None,
        "path_prefixes": [".github/workflows/"],
        "path_regex": None,
        "docs_only": True,
    },
    "resync": {                        # empty-commit consumer PR after the contract repo's contract moves
        "enabled": False,
        "contract_repo": None,
        "contract_path": "openapi.json",
        "consumer_repo": None,
        "grace_minutes": 30,
        "min_gap_hours": 2,
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def path():
    for p in (os.environ.get("MERGE_SCHEDULER_CONFIG"), "merge-scheduler.json",
              Path.home() / ".config/merge-scheduler/config.json"):
        if p and Path(p).is_file():
            return Path(p)
    return None


def load(data=None):
    """Config from a dict (tests) or from the first file found."""
    if data is None:
        p = path()
        if not p:
            raise SystemExit("merge-scheduler: no config. Copy merge-scheduler.example.json to "
                             "~/.config/merge-scheduler/config.json (or set MERGE_SCHEDULER_CONFIG).")
        data = json.loads(p.read_text())
    cfg = _merge(DEFAULTS, data)
    if not cfg["org"] or not (cfg["repos"] or cfg["repo_prefixes"]):
        raise SystemExit("merge-scheduler: config needs 'org' and 'repos' or 'repo_prefixes'.")
    cfg["schedule_repo"] = cfg["schedule_repo"] or (cfg["repos"] or [None])[0]
    return cfg
