#!/usr/bin/env python3
"""Sort the team's open PRs into one developer's work. Reads only; changes nothing.

Buckets (for the person `gh` is logged in as, or --as LOGIN):
  fix         your PR: requested changes on its head, conflicts, sched:needs-rebase, or red CI
  rereview    you requested changes and the author has pushed since
  review      a teammate PR you haven't reviewed (and nobody else has approved)
  reapprove   your approval was dismissed by a push or a base move
  resync      the merge gate's empty-commit resync PR
  update      your PR on a repo without a merge queue is behind main
  ask         your PR needs a reviewer, or a re-review after your push
  hint        your PR may be missing Merge-Group / Depends-On / sched:hold
  waiting     nothing for you right now
Usage: devq.py [--json] [--as LOGIN]
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import github  # noqa: E402

CARD = re.compile(r"\b(?:[A-Z][A-Z0-9]+-\d+|E\d+-S\d+|#\d{3,})\b")      # ticket-like tokens in titles


def manual(p, cfg):
    m = cfg["manual"]
    return any(p["repo"].lower().startswith(x.lower()) for x in m["repo_prefixes"]) or bool(
        m["title_regex"] and re.search(m["title_regex"], p["title"], re.I))


def classify(p, me, all_prs, cfg):
    revs = {r["by"]: r for r in p["reviews"]}
    mine = revs.get(me)
    cr_head = [r["by"] for r in p["reviews"] if r["state"] == "CHANGES_REQUESTED" and r["sha"] == p["sha"]]
    cr_old = [r["by"] for r in p["reviews"] if r["state"] == "CHANGES_REQUESTED" and r["sha"] != p["sha"]]
    if p["author"] == me:
        if p["draft"]:
            return "waiting", "draft"
        if cr_head:
            return "fix", f"changes requested on the head by {', '.join(cr_head)}"
        if p["ms"] == "DIRTY" or "sched:needs-rebase" in p["labels"]:
            return "fix", "conflicts with main: merge main in"
        if p["bad"]:
            return "fix", f"CI red: {', '.join(p['bad'][:3])}"
        if "sched:needs-update" in p["labels"]:
            return "update", "behind main on a repo without a merge queue"
        if cr_old:
            return "ask", f"pushed after changes requested by {', '.join(cr_old)}: ask them to re-review"
        if p["rd"] != "APPROVED":
            return "ask", "needs an approval" + (" (CI still running)" if p["ci"] != "SUCCESS" else "")
        h = hint(p, all_prs)
        return ("hint", h) if h else ("waiting", "approved; the merge gate schedules it")
    if p["draft"] or manual(p, cfg):
        return "waiting", "draft" if p["draft"] else "merged by hand"
    if "sched:resync" in p["labels"]:
        return ("waiting", "approved") if mine and mine["state"] == "APPROVED" else ("resync", "empty resync commit")
    if p["ms"] == "DIRTY":
        return "waiting", "conflicts with main: the author's to rebase"
    if p["bad"]:
        return "waiting", f"CI red ({', '.join(p['bad'][:2])}): the author's to fix"
    if mine is None or mine["state"] == "COMMENTED":
        others = [b for b, r in revs.items() if b != me and r["state"] == "APPROVED" and r["sha"] == p["sha"]]
        if p["rd"] == "APPROVED" and others:
            return "waiting", f"approved by {', '.join(others)}"
        if [b for b in cr_head if b != me]:
            return "waiting", f"changes requested by {', '.join(cr_head)}: the author's to fix"
        return "review", "not reviewed by you yet"
    if mine["state"] == "CHANGES_REQUESTED":
        return ("rereview", "the author pushed since your requested changes") if mine["sha"] != p["sha"] \
            else ("waiting", "your requested changes are still open")
    if mine["state"] == "DISMISSED" or (mine["state"] == "APPROVED" and p["rd"] != "APPROVED" and mine["sha"] != p["sha"]):
        if [b for b in cr_head if b != me]:
            return "waiting", f"changes requested by {', '.join(cr_head)}"
        return "reapprove", f"your approval was on {mine['sha'][:8]}, head is {p['sha'][:8]}"
    return "waiting", "approved by you"


def hint(p, all_prs):
    out = []
    cards = set(CARD.findall(p["title"])) - {f"#{p['n']}"}
    partners = [q["key"] for q in all_prs.values() if q["key"] != p["key"] and q["repo"] != p["repo"]
                and cards & set(CARD.findall(q["title"]))]
    if partners and not re.search(r"^\s*Merge-Group:", p["body"], re.I | re.M):
        out.append(f"shares a ticket with {', '.join(partners)}: add `Merge-Group:` (and `Depends-On:`) if they must land together")
    if re.search(r"confirm[^.\n]{0,80}before (?:this )?merg", p["body"], re.I) and "sched:hold" not in p["labels"]:
        out.append("the body asks for a confirmation before merge: add the `sched:hold` label until it's given")
    return "; ".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--as", dest="who")
    a = ap.parse_args()
    cfg = config.load()
    gh = github.Client(cfg)
    me = a.who or github.me()
    prs = gh.open_prs()
    rows = []
    for p in prs.values():
        bucket, why = classify(p, me, prs, cfg)
        rows.append(dict(bucket=bucket, why=why, key=p["key"], repo=p["repo"], n=p["n"], author=p["author"],
                         sha=p["sha"][:8], title=p["title"], url=f"https://github.com/{gh.org}/{p['repo']}/pull/{p['n']}"))
    order = ["fix", "rereview", "review", "reapprove", "resync", "update", "ask", "hint", "waiting"]
    rows.sort(key=lambda r: (order.index(r["bucket"]), r["repo"], r["n"]))
    if a.json:
        print(json.dumps(dict(me=me, rows=rows), indent=1))
        return
    print(f"# Dev queue for {me}")
    for b in order:
        rs = [r for r in rows if r["bucket"] == b]
        if rs:
            print(f"== {b} ({len(rs)})")
            for r in rs:
                print(f"  {r['key']:32} {r['sha']} {r['author'][:12]:12} {r['title'][:48]:48} | {r['why']}")


if __name__ == "__main__":
    main()
