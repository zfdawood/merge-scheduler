---
name: merge-gate
description: The single merge gate for a team's PRs. Runs the scheduler (tools/gate.py), the only thing that queues or merges. It holds cross-repo groups until every member is ready, dispatches them in order by priority, then handles the events it raises (dropped queue entries, torn groups, deadlocks). Only the lease holder runs --apply. Triggers on "merge gate", "run the gate", "merge schedule", "/merge-gate".
---

# Merge gate

One person merges. Everyone else reviews, approves, requests changes and fixes their own PRs through `dev-loop`, which never queues or merges.

`tools/gate.py` is the scheduler. It's deterministic and needs no model. Claude is only the interrupt handler for the events it raises. Read the gate's docstring for the model.

## Running it

```bash
G=<this skill's base directory>/tools
python3 $G/gate.py                          # report: print the schedule, change nothing
python3 $G/gate.py --apply --events /tmp/gate-events.json
```

- `--apply` takes a lease in the pinned **Merge schedule** issue. If someone else holds an unexpired lease, the gate falls back to report mode and says who has it. Never edit the lease to get around that.
- The lease is per GitHub login, so never run two gates under one login.
- **Never discard the gate's output.** Read its Actions and Events every tick.
- `tick.sh` runs one apply tick from cron and only calls Claude when there are events.

## Handling events

Work through `events` in order. Report each in a line, grouped as: merged, needs the gate owner, waiting on others.

- **merged**: report it.
- **dropped** (a PR left the merge queue without merging; the gate holds it at that head):
  - Read the failed CI log.
  - A flake or timeout with `requeues` at 0: run `gate.py requeue <repo#N>`, and the next tick re-dispatches it.
  - A real failure: post one plain comment naming the failing tests. A new head from the author clears the hold.
  - `merge_conflict`: nothing to do. The gate labels it `sched:needs-rebase`.
- **torn** (one member of a group merged and a partner then failed): never fix or revert on your own. Report what merged, what's pending and why, and the options:
  1. The author fixes it, then the owner runs `gate.py torn-resume <group>`.
  2. Revert the merged member.
  3. Accept it with `gate.py torn-clear <group>`.
  A torn group never blocks anything else.
- **deadlock** or **broken-dep**: report the cycle, or the closed dependency. The trailers need fixing.
- **outside-queue**: someone queued a PR outside the gate. Report it once, naming any partner that isn't ready.
- **aborted**: a group went back to waiting before anything merged. Report it only if it repeats.
- **action-failed**: report it with the error.

## Knobs

```bash
gate.py prio api#12 urgent|high|normal|low   # the prio:* label; urgent preempts with jump
gate.py hold api#12 / unhold api#12          # sched:hold: never dispatched
gate.py release api#12                       # let a PR someone dequeued by hand back in
gate.py torn-resume <group> / torn-clear <group>
```

## Never dispatched

- Drafts and PRs labelled `sched:hold`.
- Hand-merged kinds, as set in the config's `manual` section (default: workflow files and docs-only PRs).
- Anything with requested changes, red CI or conflicts.

## Rules

- Only the lease holder runs `--apply`. Report mode is safe for anyone.
- Never approve a PR here.
- A denied tool call stops that action. Report it, don't route around it.
