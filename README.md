# merge-scheduler

> **This will change the world.**
>
> For centuries, humanity has asked: *what if my API change merged twenty minutes before the UI change that depends on it, and everyone spent the afternoon convinced the feature was broken?* Today, that question is retired. merge-scheduler brings the hard-won wisdom of operating-system schedulers (gang scheduling, priority inheritance, aging, preemption, deadlock detection) to the one resource every team fights over: **the merge button.**
>
> Is it overkill? Yes. Does it work? Also yes.

merge-scheduler is two [Claude Code](https://claude.com/claude-code) skills and a small Python scheduler for teams whose changes span several GitHub repos. Everyone reviews. **One gate merges.**

| Skill | Who runs it | Does |
|---|---|---|
| `dev-loop` | every developer | reviews teammates' PRs, re-approves after merge-only pushes, fixes requested changes and conflicts on their own PRs. **Never merges.** |
| `merge-gate` | one person (holds a lease) | the only thing that queues or merges, through the scheduler |

## The problem

Several developers, each with an AI loop that reviews, approves and merges. An API PR is approved and merges. Its UI PR is still in review. For the next hour, `main` describes a product that doesn't exist, and someone files a bug against it. Multiply by every cross-repo change, and by every merge queue that tests each PR alone.

GitHub's merge queue serializes PRs *within* a repo. Nothing serializes them *across* repos, or holds the API half until the UI half is ready. That's a scheduling problem, so we treated it like one.

## How the gate schedules

| Operating system | merge-scheduler |
|---|---|
| process | a **merge group**: the open PRs sharing a `Merge-Group:` trailer (or one PR alone) |
| CPU | a repo's GitHub merge queue (`queue_cap` slots), or a direct merge where there's no queue |
| blocked on I/O | waiting on review, CI, requested changes, a rebase, or a `Depends-On:` PR |
| **gang scheduling** | nothing in a group is dispatched until **every** member is approved and green |
| ordering | inside a group, `Depends-On:` edges, else configured repo tiers (libraries, then API, then UI) |
| **priority** | `prio:urgent` / `prio:high` / normal / `prio:low` labels |
| **priority inheritance** | whatever an urgent PR waits on becomes urgent too |
| **aging** | a ready group gains a level per day it waits, so low priority never starves |
| **preemption** | urgent work enqueues with `jump: true` |
| **deadlock detection** | a cycle in `Depends-On:` is reported, never run |
| interrupts | Claude only wakes for events: a dropped queue entry, a torn group, a deadlock |
| kernel mode | infra, workflow or docs-only PRs (configurable) are left for a human |

When a group dispatches, its first member goes into the queue. The moment it merges, the next member is queued with `jump`, so the window where `main` is half-updated is one queue run. If a member fails after its partner has merged, the group is **torn**. It's held for the gate owner, and it never blocks anything else.

Authors see what's happening through labels (`sched:blocked`, `sched:needs-rebase`, `sched:torn`), one sticky "Merge gate:" comment per PR, and a pinned **Merge schedule** issue the gate rewrites every tick.

Most ticks are plain Python, with no model and no cost. Claude runs only when there's an event to judge.

Optional: **contract resync.** When a contract file (say `openapi.json`) changes in one repo and the consuming repo's `main` hasn't been rebuilt since, the gate opens an empty-commit PR in the consumer, which any reviewer can approve.

## Writing PRs for it

```
Merge-Group: ticket-123      # PRs that must land together
Depends-On: api#12           # order, or a hard dependency (aliases come from the config)
```

- Waiting on a decision before merge? Add the `sched:hold` label.
- In a hurry? Add `prio:high` (or `prio:urgent`, which costs everyone behind you a rebuild).
- Add trailers **before** approval. An approved, green PR with no group is a job on its own.

## Install

Needs `python3` (standard library only), the `gh` CLI logged in, and `flock` for the cron scripts.

```bash
git clone https://github.com/zfdawood/merge-scheduler && cd merge-scheduler
make install                                    # copies both skills into ~/.claude/skills
mkdir -p ~/.config/merge-scheduler
cp merge-scheduler.example.json ~/.config/merge-scheduler/config.json   # then edit: org, repos, team
```

- **Developers:** `/loop 5m /dev-loop` in Claude Code, or cron `skills/dev-loop/tick.sh`, which starts Claude only when there's new work.
- **The gate owner:** `python3 ~/.claude/skills/merge-gate/tools/gate.py` shows the schedule, read-only. Add `--apply` to act, or cron `skills/merge-gate/tick.sh`.
- Turn off any other loop that merges. Two mergers is the bug this fixes.

## Honest limitations

- **GitHub only.** It assumes merge queues (where present) are on `main`.
- **No atomic cross-repo merge.** GitHub doesn't offer one. The gate shrinks the half-merged window to one queue run and tells you loudly when a group tears.
- **The lease is per GitHub login.** Don't run two gates under one account.
- **It enforces nothing.** It only works if nobody else merges. Branch rules can make that strict.
- **Lightly used so far:** one team, three developers and about a dozen repos, on the day it was written. It will change the world gradually.

## Tests

```bash
make test        # offline: gang holds, ordering, torn groups, inheritance, aging, deadlocks, drops, holds, resync
```

## License

MIT
