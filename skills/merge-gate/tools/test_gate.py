#!/usr/bin/env python3
"""Offline tests for gate.plan. Run: python3 test_gate.py"""
import datetime as dt
import filecmp
import unittest
from pathlib import Path

import config
import gate as G

API, UI, LIB = "acme-api", "acme-ui", "acme-models"
NOW = dt.datetime(2026, 11, 6, 21, 0, tzinfo=dt.timezone.utc)
CFG = config.load({"org": "acme", "repos": [API, UI, LIB], "authors": [], "tiers": {LIB: 0, API: 2, UI: 4},
                   "aliases": {"api": API, "ui": UI},
                   "manual": {"repo_prefixes": ["tf-"], "title_regex": r"^Config \("},
                   "resync": {"enabled": True, "contract_repo": API, "consumer_repo": UI}})


def pr(repo, n, *, body="", title="t", status="ready", labels=(), files=("src/a.py",), sha=None):
    base = dict(key=f"{repo}#{n}", id=f"ID{n}", repo=repo, n=n, title=title, body=body, author="alice", draft=False,
                base="main", head=f"b{n}", sha=sha or f"sha{n}", ms="CLEAN", rd="APPROVED", in_queue=False,
                ci="SUCCESS", bad=[], pending=[], created="", updated="", labels=list(labels), files=list(files),
                reviews=[])
    base.update({"review": dict(rd="REVIEW_REQUIRED", ms="BLOCKED"), "changes": dict(rd="CHANGES_REQUESTED"),
                 "dirty": dict(ms="DIRTY"), "red": dict(bad=["build"]), "queued": dict(in_queue=True),
                 "ready": {}}[status])
    return base


def snap(*prs, queued=(), closed=None, removal=None, resync=None, queue_repos=(API, UI), **extra):
    q = {r: dict(enabled=r in queue_repos, entries=[k for k in queued if k.startswith(r + "#")])
         for r in {p["repo"] for p in prs} | {API, UI}}
    return dict(prs={p["key"]: p for p in prs}, queues=q, closed=closed or {}, removal=removal or {},
                resync=resync or {}, **extra)


def plan(s, st=None):
    return G.plan(s, st or {}, NOW, CFG)


def kinds(res, t):
    return [a for a in res["actions"] if a["type"] == t]


class Gang(unittest.TestCase):
    def test_api_waits_for_its_ui_partner(self):
        r = plan(snap(pr(API, 1, body="Merge-Group: c1"), pr(UI, 2, body="Merge-Group: c1", status="review")))
        self.assertEqual(kinds(r, "enqueue"), [])
        self.assertEqual(r["groups"]["c1"]["state"], "gang-wait")
        self.assertEqual({a["pr"]: a["add"] for a in kinds(r, "labels")}[f"{API}#1"], ["sched:blocked"])

    def test_group_goes_by_tier_then_next_member_jumps_once_merged(self):
        r1 = plan(snap(pr(API, 1, body="Merge-Group: c1"), pr(UI, 2, body="Merge-Group: c1")))
        self.assertEqual([a["pr"] for a in kinds(r1, "enqueue")], [f"{API}#1"])
        self.assertFalse(kinds(r1, "enqueue")[0]["jump"])
        r2 = plan(snap(pr(UI, 2, body="Merge-Group: c1"), closed={f"{API}#1": dict(merged=True)}), r1["state"])
        self.assertEqual([(a["pr"], a["jump"]) for a in kinds(r2, "enqueue")], [(f"{UI}#2", True)])

    def test_explicit_depends_on_overrides_tiers(self):
        r = plan(snap(pr(UI, 2, body="Merge-Group: g"), pr(API, 1, body="Merge-Group: g\nDepends-On: ui#2")))
        self.assertEqual([a["pr"] for a in kinds(r, "enqueue")], [f"{UI}#2"])


class Done(unittest.TestCase):
    def test_a_merged_group_reports_once_and_is_not_redispatched(self):
        r1 = plan(snap(pr(API, 1)))
        r2 = plan(snap(closed={f"{API}#1": dict(merged=True)}), r1["state"])
        self.assertEqual([e["type"] for e in r2["events"]], ["merged"])
        self.assertEqual(r2["state"]["inflight"], {})
        r3 = plan(snap(), r2["state"])
        self.assertEqual(r3["events"], [])


class Torn(unittest.TestCase):
    def test_partner_failing_after_merge_is_torn_and_blocks_nothing(self):
        r1 = plan(snap(pr(API, 1, body="Merge-Group: g"), pr(UI, 2, body="Merge-Group: g")))
        r2 = plan(snap(pr(UI, 2, body="Merge-Group: g", status="changes"), pr(API, 9),
                       closed={f"{API}#1": dict(merged=True)}), r1["state"])
        self.assertIn("g", r2["state"]["torn"])
        self.assertIn(f"{API}#9", [a["pr"] for a in kinds(r2, "enqueue")])
        self.assertIn("sched:torn", {x for a in kinds(r2, "labels") for x in a["add"]})


class Priority(unittest.TestCase):
    busy = [f"{API}#90", f"{API}#91"]                                  # one free slot in the API queue

    def crowd(self):
        return [pr(API, 90, status="queued"), pr(API, 91, status="queued")]

    def test_inheritance_beats_a_higher_label_and_preempts(self):
        s = snap(pr(API, 4), pr(API, 6, labels=["prio:high"]),
                 pr(UI, 5, labels=["prio:urgent"], body="Depends-On: api#4", status="review"), *self.crowd(), queued=self.busy)
        r = plan(s, {"outside": {k: "x" for k in self.busy}})
        self.assertEqual([(a["pr"], a["jump"]) for a in kinds(r, "enqueue")], [(f"{API}#4", True)])

    def test_aging_lifts_an_old_low_group_over_a_fresh_normal_one(self):
        s = snap(pr(API, 1, labels=["prio:low"]), pr(API, 2), *self.crowd(), queued=self.busy)
        r = plan(s, {"ready_since": {f"{API}#1": "2026-11-04T12:00:00Z"}, "outside": {k: "x" for k in self.busy}})
        self.assertEqual([a["pr"] for a in kinds(r, "enqueue")], [f"{API}#1"])


class Hazards(unittest.TestCase):
    def test_cycle_is_reported_and_not_run(self):
        r = plan(snap(pr(API, 1, body="Depends-On: ui#2"), pr(UI, 2, body="Depends-On: api#1")))
        self.assertTrue(any(e["type"] == "deadlock" for e in r["events"]))
        self.assertEqual(kinds(r, "enqueue"), [])

    def test_dropped_entry_is_held_until_its_head_changes(self):
        r1 = plan(snap(pr(API, 1)))
        r2 = plan(snap(pr(API, 1), removal={f"{API}#1": "failed_checks"}), r1["state"])
        self.assertEqual((kinds(r2, "enqueue"), r2["info"][f"{API}#1"]["status"]), ([], "dropped"))
        self.assertEqual(kinds(plan(snap(pr(API, 1)), r2["state"]), "enqueue"), [])
        self.assertEqual(len(kinds(plan(snap(pr(API, 1, sha="new")), r2["state"]), "enqueue")), 1)

    def test_hand_dequeued_pr_is_held_until_released(self):
        s = snap(pr(API, 5), manual_dequeue={f"{API}#5": True})
        self.assertEqual(kinds(plan(s), "enqueue"), [])
        self.assertEqual(len(kinds(plan(s, {"released": {f"{API}#5": "sha5"}}), "enqueue")), 1)

    def test_holds_and_manual_kinds_are_never_dispatched(self):
        r = plan(snap(pr(LIB, 28, labels=["sched:hold"]), pr(API, 1, files=["docs/a.md"]),
                      pr(UI, 2, files=[".github/workflows/x.yml"]), pr(API, 3, title="Config (prod): x"),
                      pr("tf-infra", 4)))
        self.assertEqual(kinds(r, "enqueue") + kinds(r, "merge"), [])

    def test_repo_without_a_queue_merges_directly(self):
        self.assertEqual([a["pr"] for a in kinds(plan(snap(pr(LIB, 16))), "merge")], [f"{LIB}#16"])

    def test_resync_after_a_contract_change(self):
        rs = dict(contract_at="2026-11-06T19:00:00Z", consumer_at="2026-11-06T18:00:00Z", open=False)
        self.assertEqual(len(kinds(plan(snap(resync=rs)), "resync")), 1)
        self.assertEqual(kinds(plan(snap(resync=dict(rs, contract_at="2026-11-06T20:50:00Z"))), "resync"), [])


class Packaging(unittest.TestCase):
    def test_shared_modules_are_identical_in_both_skills(self):
        here = Path(__file__).resolve().parent
        other = here.parent.parent / "dev-loop" / "tools"
        for f in ("github.py", "config.py"):
            if (other / f).exists():
                self.assertTrue(filecmp.cmp(here / f, other / f, shallow=False), f"{f} differs; run `make sync`")


if __name__ == "__main__":
    unittest.main(verbosity=1)
