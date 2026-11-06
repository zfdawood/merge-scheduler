#!/usr/bin/env python3
"""merge-scheduler: one merge gate for a team's PRs, scheduled like an operating system.

  job       = a merge group: the open PRs sharing a `Merge-Group:` trailer (or one PR alone)
  CPU       = a repo's GitHub merge queue (queue_cap slots), or a direct merge where there is no queue
  blocked   = waiting on review, CI, requested changes, a rebase, or a `Depends-On:` PR in another group
  gang      = a group is dispatched only when every member is ready
  order     = inside a group, Depends-On edges (else the configured repo tiers); the next member is
              queued with `jump` the moment the previous one merges, so the half-merged window is short
  priority  = prio:urgent / prio:high / (normal) / prio:low, plus aging and priority inheritance
  torn      = a member merged and a partner then failed: held for a human, never blocks anyone else

Usage:
  gate.py                     report: print the schedule, change nothing
  gate.py --apply             act: queue/merge, labels, sticky comments, schedule issue, resync
  gate.py --json              machine-readable plan, events included
  gate.py prio api#12 urgent|high|normal|low
  gate.py hold api#12 | unhold api#12     (sched:hold: never dispatched)
  gate.py release api#12                  let a PR someone dequeued by hand back in
  gate.py torn-resume <group> | torn-clear <group>
  gate.py requeue api#12                  clear a flaky drop once, so the next tick re-dispatches it
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import github  # noqa: E402

PRIO = {"prio:urgent": 3, "prio:high": 2, "prio:low": 0}
PRIO_NAME = {3: "urgent", 2: "high", 1: "normal", 0: "low"}
SCHED_LABELS = ("sched:blocked", "sched:needs-rebase", "sched:needs-update", "sched:torn")
STICKY = "<!-- merge-scheduler -->"
LEASE_RE = re.compile(r"<!-- lease: (\S+) (\S+) -->")
STATE_DIR = Path(os.environ.get("MERGE_SCHEDULER_STATE", Path.home() / ".local/state/merge-scheduler"))
WHY = {"review": "needs an approval", "changes": "has requested changes", "dirty": "conflicts with main",
       "red": "CI is red", "ci": "CI is running", "behind": "is behind main", "draft": "is a draft",
       "hold": "is on hold", "manual": "is merged by hand", "unknown": "has an unknown merge state",
       "queued": "is queued outside the schedule", "dropped": "dropped out of the merge queue"}




# ---------------------------------------------------------------- parsing

def resolve(tok, here, known, cfg):
    """'api#12' / 'my-api#12' / '#12' -> 'repo#12', or None."""
    m = re.fullmatch(r"\s*([\w.\-]*)#(\d+)\s*", tok)
    if not m:
        return None
    name, n = m.group(1), int(m.group(2))
    if not name:
        return github.key(here, n)
    alias = {k.lower(): v for k, v in cfg["aliases"].items()}.get(name.lower())
    for cand in (alias, name):
        if cand and cand.lower() in known:
            return github.key(known[cand.lower()], n)
    return github.key(alias or name, n)


def trailers(p, known, cfg):
    """Merge-Group and Depends-On trailers from the PR body."""
    group, deps = None, []
    for line in p["body"].splitlines():
        m = re.match(r"\s*Merge-Group:\s*(\S+)", line, re.I)
        if m:
            group = m.group(1).strip().lower()
        m = re.match(r"\s*Depends-On:\s*(.+)", line, re.I)
        if m:
            deps += [d for d in (resolve(t, p["repo"], known, cfg) for t in re.split(r"[,\s]+", m.group(1)) if t) if d]
    return group, sorted(set(d for d in deps if d != p["key"]))


def base_prio(p):
    return max([PRIO[x] for x in p["labels"] if x in PRIO] or [1])


def is_manual(p, cfg):
    m, files = cfg["manual"], p["files"]
    if any(p["repo"].lower().startswith(x.lower()) for x in m["repo_prefixes"]):
        return True
    if m["title_regex"] and re.search(m["title_regex"], p["title"], re.I):
        return True
    if any(f.startswith(tuple(m["path_prefixes"])) or (m["path_regex"] and re.search(m["path_regex"], f)) for f in files):
        return True
    return bool(m["docs_only"] and files and all(f.endswith(".md") or f.startswith("docs/") for f in files))


def status(p, queue_repos, cfg):
    """Where one PR stands, from the gate's point of view."""
    if is_manual(p, cfg):
        return "manual"
    if p["draft"]:
        return "draft"
    if "sched:hold" in p["labels"]:
        return "hold"
    if p["in_queue"]:
        return "queued"
    if p["rd"] == "CHANGES_REQUESTED":
        return "changes"
    if p["ms"] == "DIRTY":
        return "dirty"
    if p["bad"]:
        return "red"
    if p["ci"] != "SUCCESS" or p["pending"]:
        return "ci"
    if p["rd"] != "APPROVED" or p["ms"] == "BLOCKED":
        return "review"
    if p["ms"] == "BEHIND" and p["repo"] not in queue_repos:
        return "behind"
    if p["ms"] in ("CLEAN", "HAS_HOOKS", "UNSTABLE", "BEHIND"):
        return "ready"
    return "unknown"


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- planning (pure)

def plan(snap, state, now, cfg):
    """snap: prs{key:pr}, queues{repo:{enabled,entries}}, closed{key:{merged}}, removal{key:reason},
    manual_dequeue{key:True}, resync{}. Returns dict(actions, events, groups, info, eff, state)."""
    prs, queues, closed = snap["prs"], snap["queues"], snap.get("closed", {})
    st = json.loads(json.dumps(state or {}))
    for k in ("ready_since", "inflight", "torn", "requeues", "dropped", "outside", "released", "sticky"):
        st.setdefault(k, {})
    queue_repos = {r for r, q in queues.items() if q["enabled"]}
    queued_now = {k for q in queues.values() for k in q["entries"]}
    known = {p["repo"].lower(): p["repo"] for p in prs.values()} | {r.lower(): r for r in queues}
    actions, events = [], []
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def merged(k):
        return k not in prs and closed.get(k, {}).get("merged", False)

    info = {}
    for k, p in prs.items():
        g, deps = trailers(p, known, cfg)
        s = status(p, queue_repos, cfg)
        if snap.get("manual_dequeue", {}).get(k) and st["released"].get(k) != p["sha"] and s == "ready":
            s = "hold"                                # someone took it out of the queue by hand: respect that
        drop = st["dropped"].get(k)
        if drop and drop["sha"] != p["sha"]:
            del st["dropped"][k]                      # a new head clears a drop
        elif drop and s not in ("queued", "manual", "draft", "hold"):
            s = "dropped"
        info[k] = dict(group=g or k, deps=deps, status=s, prio=base_prio(p))
    for bucket in ("dropped", "released"):
        for k in [k for k in st[bucket] if k not in prs]:
            del st[bucket][k]

    groups = {}
    for k, i in info.items():
        groups.setdefault(i["group"], []).append(k)
    for g in st["inflight"]:
        groups.setdefault(g, [])
    tier = cfg["tiers"]

    def intra_deps(k):
        g = info[k]["group"]
        flm = st["inflight"].get(g, {}).get("members", [])
        explicit = [d for d in info[k]["deps"] if info.get(d, {}).get("group") == g or d in flm]
        implicit = []
        if not any(info.get(d, {}).get("group") == g for d in info[k]["deps"]):
            mine = tier.get(prs[k]["repo"], 3)
            implicit = [m for m in groups[g] if m != k and tier.get(prs[m]["repo"], 3) < mine and k not in info[m]["deps"]]
        return sorted(set(explicit + implicit))

    def closure(k, seen):
        for d in (intra_deps(k) if k in info else []):
            if d not in seen:
                seen.add(d)
                closure(d, seen)
        return seen

    gview = {}
    for g, members in groups.items():
        flm = st["inflight"].get(g, {}).get("members", [])
        ext_open, broken = set(), set()
        for k in members:
            for d in info[k]["deps"]:
                if d in members or d in flm:
                    continue
                if d in prs or d not in closed:
                    ext_open.add(d)
                elif not closed[d]["merged"]:
                    broken.add(d)
        gview[g] = dict(members=sorted(members), ext_open=sorted(ext_open), broken=sorted(broken),
                        prio=max([info[k]["prio"] for k in members] or [1]))

    # cycles and priority inheritance over the group graph
    edges = {g: {info[d]["group"] for d in v["ext_open"] if d in info} for g, v in gview.items()}
    cyclic = set()
    for start in edges:
        stack, seen = [(start, [start])], set()
        while stack:
            node, path = stack.pop()
            for nxt in edges.get(node, ()):
                if nxt == start:
                    cyclic.update(path)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, path + [nxt]))
    cyclic |= {g for g, v in gview.items() if any(k in closure(k, set()) for k in v["members"])}
    for g in sorted(cyclic):
        events.append(dict(type="deadlock", group=g, members=gview[g]["members"],
                           text=f"Dependency cycle through {g}: nothing in it will be scheduled."))
    eff = {g: v["prio"] for g, v in gview.items()}
    for _ in range(len(eff) + 1):
        changed = False
        for g, blockers in edges.items():
            for b in blockers:
                if b in eff and eff[b] < eff[g]:
                    eff[b], changed = eff[g], True
        if not changed:
            break

    for g in st["torn"]:
        if g in gview:
            gview[g]["state"] = "torn"

    # in-flight groups: completion, drops, torn, next tier
    cap_used = {r: len(q["entries"]) for r, q in queues.items()}
    for g in list(st["inflight"]):
        fl = st["inflight"][g]
        members = sorted(set(fl["members"]) | set(gview.get(g, {}).get("members", [])))
        fl["members"] = members
        fl["merged"] = sorted({m for m in members if merged(m)} | set(fl.get("merged", [])))
        open_left = [m for m in members if m not in fl["merged"] and m in prs]
        if not open_left:
            events.append(dict(type="merged", group=g, members=members, text=f"{g} has merged in full."))
            del st["inflight"][g]
            continue
        problems = []
        for m in [m for m in fl.get("dispatched", []) if m in prs and m not in queued_now
                  and m not in fl["merged"] and prs[m]["repo"] in queue_repos]:
            reason = snap.get("removal", {}).get(m, "unknown")
            problems.append((m, f"left the merge queue ({reason})"))
            if reason != "manual":
                st["dropped"][m] = dict(sha=prs[m]["sha"], reason=reason, at=iso)
                info[m]["status"] = "dropped"
            events.append(dict(type="dropped", group=g, pr=m, reason=reason, requeues=st["requeues"].get(m, 0),
                               text=f"{m} left the merge queue without merging: {reason}."))
            fl["dispatched"] = [x for x in fl["dispatched"] if x != m]
        if fl["merged"]:
            problems += [(m, WHY.get(info[m]["status"], info[m]["status"])) for m in open_left
                         if info[m]["status"] not in ("ready", "queued") and m not in fl.get("dispatched", [])
                         and not any(m == p[0] for p in problems)]
        if problems and fl["merged"]:
            why = "; ".join(f"{m} {w}" for m, w in problems)
            st["torn"][g] = dict(since=iso, merged=fl["merged"], pending=open_left, why=why)
            events.append(dict(type="torn", group=g, merged=fl["merged"], pending=open_left,
                               text=f"{g} is torn: {', '.join(fl['merged'])} merged, but {why}. Held; nothing else is blocked."))
            del st["inflight"][g]
            gview.setdefault(g, dict(members=members, ext_open=[], broken=[], prio=1))["state"] = "torn"
            continue
        if problems:
            events.append(dict(type="aborted", group=g, text=f"{g} went back to waiting: "
                               + "; ".join(f"{m} {w}" for m, w in problems)))
            del st["inflight"][g]
            continue
        gview[g]["state"] = "inflight"
        for m in open_left:
            if (m in fl.get("dispatched", []) and prs[m]["repo"] in queue_repos) or info[m]["status"] != "ready":
                continue
            if all(d in fl["merged"] or merged(d) for d in intra_deps(m)):
                actions.append(_dispatch(prs[m], queue_repos, jump=bool(fl["merged"]) or eff.get(g, 1) >= 3,
                                         why=f"{g}: next in its group"))
                if m not in fl.setdefault("dispatched", []):
                    fl["dispatched"].append(m)
                cap_used[prs[m]["repo"]] = cap_used.get(prs[m]["repo"], 0) + 1

    # everything else: classify, then dispatch ready groups by priority
    for g, v in gview.items():
        if v.get("state"):
            continue
        if not v["members"]:                          # a finished in-flight group has no open members left
            v["state"] = "done"
            continue
        sts = [info[k]["status"] for k in v["members"]]
        if g in cyclic:
            v["state"] = "deadlock"
        elif "manual" in sts:
            v["state"] = "manual"
        elif v["broken"]:
            v["state"] = "broken"
            events.append(dict(type="broken-dep", group=g, deps=v["broken"],
                               text=f"{g} depends on {', '.join(v['broken'])}, closed without merging."))
        elif any(s in ("draft", "hold") for s in sts):
            v["state"] = "held"
        elif v["ext_open"]:
            v["state"] = "blocked"
        elif all(s == "ready" for s in sts):
            v["state"] = "ready"
        elif all(s == "queued" for s in sts):
            v["state"] = "outside"
        elif any(s in ("ready", "queued") for s in sts):
            v["state"] = "gang-wait"
        else:
            v["state"] = "waiting"
        if v["state"] == "ready":
            st["ready_since"].setdefault(g, iso)
        else:
            st["ready_since"].pop(g, None)
        for k in v["members"]:
            if info[k]["status"] == "queued" and g not in st["inflight"] and k not in st["outside"]:
                st["outside"][k] = iso
                partners = [m for m in v["members"] if info[m]["status"] != "queued"]
                events.append(dict(type="outside-queue", group=g, pr=k, text=f"{k} was queued outside the gate"
                                   + (f" while {', '.join(partners)} is not ready." if partners else ".")))
    for k in [k for k in st["outside"] if k not in prs or info[k]["status"] != "queued"]:
        del st["outside"][k]

    def age_bonus(g):
        since = st["ready_since"].get(g)
        if not since:
            return 0
        return min(cfg["aging_cap"], int((now - _ts(since)).total_seconds() / 3600 // cfg["aging_hours"]))

    for g in sorted((g for g, v in gview.items() if v["state"] == "ready"),
                    key=lambda g: (-(eff[g] + age_bonus(g)), st["ready_since"][g], g)):
        first = [k for k in gview[g]["members"] if not intra_deps(k)]
        need = {}
        for k in first:
            if prs[k]["repo"] in queue_repos:
                need[prs[k]["repo"]] = need.get(prs[k]["repo"], 0) + 1
        if any(cap_used.get(r, 0) + n > cfg["queue_cap"] for r, n in need.items()):
            gview[g]["state"] = "ready (no queue slot)"
            continue
        level = PRIO_NAME[min(3, eff[g] + age_bonus(g))]
        actions += [_dispatch(prs[k], queue_repos, jump=eff[g] >= 3, why=f"{g}: {level} priority") for k in first]
        for r, n in need.items():
            cap_used[r] = cap_used.get(r, 0) + n
        st["inflight"][g] = dict(started=iso, members=gview[g]["members"], merged=[], dispatched=first)
        gview[g]["state"] = "dispatched"
        st["ready_since"].pop(g, None)

    # signals to authors: labels and one sticky comment per PR
    for k, p in prs.items():
        i, g = info[k], info[k]["group"]
        v = gview.get(g, {})
        want, text = set(), None
        if i["status"] == "dirty":
            want.add("sched:needs-rebase")
            text = "Conflicts with main. The author merges main in; the gate picks it up once it is green and approved again."
        elif i["status"] == "behind":
            want.add("sched:needs-update")
            text = "Behind main on a repo without a merge queue. The author updates the branch; the gate merges it next."
        elif v.get("state") == "torn" and k in st["torn"].get(g, {}).get("pending", []):
            want.add("sched:torn")
            text = f"Torn group `{g}`: {st['torn'][g]['why']}. Held for the gate owner."
        elif i["status"] == "ready" and v.get("state") in ("blocked", "gang-wait"):
            want.add("sched:blocked")
            waits = v["ext_open"] + [m for m in v["members"] if info[m]["status"] != "ready"]
            text = "Approved and green, held so its group merges together. Waiting on: " + ", ".join(
                f"{d} ({WHY.get(info[d]['status'], info[d]['status']) if d in info else 'not found'})" for d in waits) + "."
        have = {x for x in p["labels"] if x in SCHED_LABELS}
        if want != have:
            actions.append(dict(type="labels", pr=k, add=sorted(want - have), remove=sorted(have - want)))
        if st["sticky"].get(k) != text:
            actions.append(dict(type="sticky", pr=k, text=text))
            if text:
                st["sticky"][k] = text
            else:
                st["sticky"].pop(k, None)
    for k in [k for k in st["sticky"] if k not in prs]:
        del st["sticky"][k]

    # contract resync: an empty consumer commit after the contract moved and the consumer hasn't rebuilt
    rs, rc = snap.get("resync") or {}, cfg["resync"]
    if rc["enabled"] and rs.get("contract_at") and rs.get("consumer_at") and not rs.get("open"):
        c_at, u_at, last = _ts(rs["contract_at"]), _ts(rs["consumer_at"]), st.get("last_resync")
        busy = any(p["repo"] == rc["consumer_repo"] and info[k]["status"] in ("ready", "queued") for k, p in prs.items()) \
            or any(prs[k]["repo"] == rc["consumer_repo"] for fl in st["inflight"].values() for k in fl["members"] if k in prs)
        if c_at > u_at and (now - c_at).total_seconds() > rc["grace_minutes"] * 60 and not busy and (
                not last or (now - _ts(last)).total_seconds() > rc["min_gap_hours"] * 3600):
            actions.append(dict(type="resync", why=f"contract changed {rs['contract_at']}, consumer main last moved {rs['consumer_at']}"))
            st["last_resync"] = iso

    for k in [k for k in st["requeues"] if k not in prs]:
        del st["requeues"][k]
    return dict(actions=actions, events=events, groups=gview, info=info, eff=eff, state=st)


def _dispatch(p, queue_repos, jump, why):
    if p["repo"] in queue_repos:
        return dict(type="enqueue", pr=p["key"], id=p["id"], sha=p["sha"], jump=jump, why=why)
    return dict(type="merge", pr=p["key"], sha=p["sha"], why=why)


# ---------------------------------------------------------------- report

def render(res, mode):
    g, info, eff = res["groups"], res["info"], res["eff"]
    inflight = res["state"]["inflight"]
    out = [f"# Merge schedule ({mode}, {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z)", ""]
    sections = [("Dispatched this tick", ("dispatched",)), ("In flight", ("inflight",)), ("Torn", ("torn",)),
                ("Ready, no queue slot", ("ready (no queue slot)",)), ("Held for group", ("gang-wait", "blocked")),
                ("Queued outside the gate", ("outside",)), ("Merged by hand", ("manual",)),
                ("Deadlock / broken", ("deadlock", "broken")), ("On hold / draft", ("held",)),
                ("Waiting on review, CI or author", ("waiting",))]
    for title, states in sections:
        rows = [(n, v) for n, v in g.items() if v.get("state") in states]
        if not rows:
            continue
        out.append(f"## {title}")
        for n, v in sorted(rows, key=lambda x: (-eff.get(x[0], 1), x[0])):
            members = v["members"] or inflight.get(n, {}).get("members", [])
            desc = ", ".join(f"{k} {info[k]['status'] if k in info else 'merged'}" for k in members) or "-"
            extra = f" · waits on {', '.join(v['ext_open'])}" if v.get("ext_open") else ""
            out.append(f"- `{n}` [{PRIO_NAME[min(3, eff.get(n, 1))]}] {desc}{extra}")
        out.append("")
    if res["actions"]:
        out.append("## Actions" + ("" if mode == "apply" else " (not taken: report mode)"))
        for a in res["actions"]:
            if a["type"] in ("enqueue", "merge"):
                out.append(f"- {a['type']} {a['pr']}{' (jump)' if a.get('jump') else ''}: {a['why']}")
            elif a["type"] == "labels" and (a["add"] or a["remove"]):
                out.append(f"- labels {a['pr']}: +{a['add']} -{a['remove']}")
            elif a["type"] == "resync":
                out.append(f"- resync: open an empty-commit consumer PR ({a['why']})")
        out.append("")
    if res["events"]:
        out += ["## Events"] + [f"- {e['type']}: {e['text']}" for e in res["events"]]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- IO

def load_state():
    f = STATE_DIR / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    tmp.replace(STATE_DIR / "state.json")


def snapshot(gh, state, cfg):
    prs = gh.open_prs()
    repos = {p["repo"] for p in prs.values()} | set(cfg["repos"])
    queues = gh.queues(repos)
    known = {p["repo"].lower(): p["repo"] for p in prs.values()} | {r.lower(): r for r in queues}
    look = {d for p in prs.values() for d in trailers(p, known, cfg)[1] if d not in prs}
    look |= {m for fl in (state.get("inflight") or {}).values() for m in fl.get("members", []) if m not in prs}
    closed = {k: gh.pr_state(k) for k in sorted(look)}
    queued_now = {k for q in queues.values() for k in q["entries"]}
    tracked = {m for fl in (state.get("inflight") or {}).values() for m in fl.get("dispatched", [])}
    removal = {m: gh.removal_reason(m) for m in tracked if m in prs and m not in queued_now}
    manual_dequeue = {}
    for k, p in prs.items():
        if not p["in_queue"] and p["rd"] == "APPROVED" and p["ci"] == "SUCCESS" and k not in tracked:
            ev = gh.last_queue_event(k)
            if ev and ev == ("removed", "manual"):
                manual_dequeue[k] = True
    resync, rc = {}, cfg["resync"]
    if rc["enabled"]:
        try:
            c = github.rest("GET", f"repos/{gh.org}/{rc['contract_repo']}/commits?sha=main&path={rc['contract_path']}&per_page=1")
            u = github.rest("GET", f"repos/{gh.org}/{rc['consumer_repo']}/commits?sha=main&per_page=1")
            resync = dict(contract_at=c[0]["commit"]["committer"]["date"], consumer_at=u[0]["commit"]["committer"]["date"],
                          open=any(p["repo"] == rc["consumer_repo"] and "sched:resync" in p["labels"] for p in prs.values()))
        except (RuntimeError, IndexError, KeyError):
            pass
    return dict(prs=prs, queues=queues, closed=closed, removal=removal, manual_dequeue=manual_dequeue, resync=resync)


def ensure_label(org, repo, name, color="5319e7"):
    try:
        github.rest("POST", f"repos/{org}/{repo}/labels", dict(name=name, color=color))
    except RuntimeError:
        pass


def set_sticky(org, k, text):
    repo, n = k.rsplit("#", 1)
    mine = [c for c in github.rest("GET", f"repos/{org}/{repo}/issues/{n}/comments?per_page=100") or []
            if STICKY in (c.get("body") or "")]
    body = f"{STICKY}\n**Merge gate:** {text}" if text else None
    if mine and body:
        github.rest("PATCH", f"repos/{org}/{repo}/issues/comments/{mine[0]['id']}", dict(body=body))
    elif mine:
        github.rest("DELETE", f"repos/{org}/{repo}/issues/comments/{mine[0]['id']}")
    elif body:
        github.rest("POST", f"repos/{org}/{repo}/issues/{n}/comments", dict(body=body))


def schedule_issue(org, repo, state):
    if state.get("schedule_issue"):                   # remembered: the label index lags just after creation
        try:
            issue = github.rest("GET", f"repos/{org}/{repo}/issues/{state['schedule_issue']}")
            if issue.get("state") == "open":
                return issue
        except RuntimeError:
            pass
    found = github.rest("GET", f"repos/{org}/{repo}/issues?labels=sched:schedule&state=open&per_page=5") or []
    if found:
        return found[0]
    ensure_label(org, repo, "sched:schedule", "0e8a16")
    return github.rest("POST", f"repos/{org}/{repo}/issues",
                       dict(title="Merge schedule", body="(written by merge-scheduler)", labels=["sched:schedule"]))


def take_lease(issue, me, now, minutes=20):
    m = LEASE_RE.search(issue.get("body") or "")
    if m and m.group(1) != me and _ts(m.group(2)) > now:
        return False, f"{m.group(1)} holds the merge-gate lease until {m.group(2)}"
    return True, (now + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def resync_pr(org, repo):
    base = github.rest("GET", f"repos/{org}/{repo}/git/ref/heads/main")["object"]["sha"]
    tree = github.rest("GET", f"repos/{org}/{repo}/git/commits/{base}")["tree"]["sha"]
    c = github.rest("POST", f"repos/{org}/{repo}/git/commits",
                    dict(message="Resync with the contract repo's main (empty commit, opened by merge-scheduler)",
                         tree=tree, parents=[base]))
    branch = f"sched/resync-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M}"
    github.rest("POST", f"repos/{org}/{repo}/git/refs", dict(ref=f"refs/heads/{branch}", sha=c["sha"]))
    pr = github.rest("POST", f"repos/{org}/{repo}/pulls", dict(
        title="Resync with the contract repo (empty commit)", head=branch, base="main",
        body="The contract this repo consumes moved and main has not been rebuilt since. This empty commit gives "
             "main a fresh build against it. Opened by merge-scheduler; any reviewer can approve it (no diff)."))
    for name, color in (("sched:resync", "c5def5"), ("prio:high", "d93f0b")):
        ensure_label(org, repo, name, color)
    github.rest("POST", f"repos/{org}/{repo}/issues/{pr['number']}/labels", dict(labels=["sched:resync", "prio:high"]))
    return pr["html_url"]


def apply(gh, res, cfg):
    errors = []
    for a in res["actions"]:
        try:
            if a["type"] == "enqueue":
                gh.enqueue(a["id"], a["sha"], a["jump"])
            elif a["type"] == "merge":
                gh.merge(a["pr"], a["sha"])
            elif a["type"] == "labels":
                repo, n = a["pr"].rsplit("#", 1)
                for x in a["add"]:
                    ensure_label(gh.org, repo, x)
                if a["add"]:
                    github.rest("POST", f"repos/{gh.org}/{repo}/issues/{n}/labels", dict(labels=a["add"]))
                for x in a["remove"]:
                    github.rest("DELETE", f"repos/{gh.org}/{repo}/issues/{n}/labels/{x}")
            elif a["type"] == "sticky":
                set_sticky(gh.org, a["pr"], a["text"])
            elif a["type"] == "resync":
                a["url"] = resync_pr(gh.org, cfg["resync"]["consumer_repo"])
        except RuntimeError as e:
            errors.append(dict(type="action-failed", text=f"{a['type']} {a.get('pr', '')}: {e}"))
    return errors


def tick(args, cfg):
    now = dt.datetime.now(dt.timezone.utc)
    gh, state = github.Client(cfg), load_state()
    snap = snapshot(gh, state, cfg)
    mode, issue, lease, me = ("apply" if args.apply else "report"), None, None, github.me()
    if args.apply:
        issue = schedule_issue(gh.org, cfg["schedule_repo"], state)
        ok, lease = take_lease(issue, me, now)
        if not ok:
            mode, args.apply = f"report ({lease})", False
    res = plan(snap, state, now, cfg)
    if args.apply:
        res["state"]["schedule_issue"] = issue["number"]
        res["events"] += apply(gh, res, cfg)
        save_state(res["state"])
        text = render(res, "apply")
        github.rest("PATCH", f"repos/{gh.org}/{cfg['schedule_repo']}/issues/{issue['number']}",
                    dict(body=f"<!-- lease: {me} {lease} -->\n" + text))
    else:
        text = render(res, mode)
    if args.events:
        Path(args.events).write_text(json.dumps(res["events"], indent=1))
    if args.json:
        print(json.dumps(dict(mode=mode, actions=res["actions"], events=res["events"], groups=res["groups"]),
                         indent=1, default=str))
    else:
        print(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="*")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--events", help="write events JSON here")
    a = ap.parse_args()
    cfg = config.load()
    if not a.cmd:
        return tick(a, cfg)
    gh, st = github.Client(cfg), load_state()
    cmd, rest = a.cmd[0], a.cmd[1:]

    def find(tok):
        prs = gh.open_prs()
        k = resolve(tok, cfg["schedule_repo"], {p["repo"].lower(): p["repo"] for p in prs.values()}, cfg)
        if k not in prs:
            sys.exit(f"{tok}: not an open PR the gate watches")
        return prs[k]

    def label(p, name, on, color="b60205"):
        if on:
            ensure_label(gh.org, p["repo"], name, color)
            github.rest("POST", f"repos/{gh.org}/{p['repo']}/issues/{p['n']}/labels", dict(labels=[name]))
        elif name in p["labels"]:
            github.rest("DELETE", f"repos/{gh.org}/{p['repo']}/issues/{p['n']}/labels/{name}")

    if cmd == "prio" and len(rest) == 2 and rest[1] in ("urgent", "high", "normal", "low"):
        p = find(rest[0])
        want = None if rest[1] == "normal" else f"prio:{rest[1]}"
        for x in [x for x in p["labels"] if x in PRIO and x != want]:
            label(p, x, False)
        if want:
            label(p, want, True, "d93f0b")
    elif cmd in ("hold", "unhold") and len(rest) == 1:
        label(find(rest[0]), "sched:hold", cmd == "hold")
    elif cmd == "release" and len(rest) == 1:
        p = find(rest[0])
        st.setdefault("released", {})[p["key"]] = p["sha"]
        save_state(st)
    elif cmd == "torn-resume" and len(rest) == 1:
        t = st.get("torn", {}).pop(rest[0], None) or sys.exit(f"{rest[0]} is not torn")
        st.setdefault("inflight", {})[rest[0]] = dict(started=t["since"], members=t["merged"] + t["pending"],
                                                      merged=t["merged"], dispatched=[])
        save_state(st)
    elif cmd == "torn-clear" and len(rest) == 1:
        st.get("torn", {}).pop(rest[0], None) or sys.exit(f"{rest[0]} is not torn")
        save_state(st)
    elif cmd == "requeue" and len(rest) == 1:
        p = find(rest[0])
        if st.setdefault("requeues", {}).get(p["key"], 0) >= 1:
            sys.exit(f"{p['key']} was already re-queued once; it goes back to its author")
        if not st.get("dropped", {}).pop(p["key"], None):
            sys.exit(f"{p['key']} is not held as dropped")
        st["requeues"][p["key"]] = 1
        save_state(st)
    else:
        sys.exit(__doc__)
    print("ok")


if __name__ == "__main__":
    main()
