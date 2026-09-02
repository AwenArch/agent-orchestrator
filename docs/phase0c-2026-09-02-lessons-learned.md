# Phase 0c — Lessons Learned (daemon + Slack layer)

Companion to `phase0a-2026-09-01-lessons-learned.md`. Where 0a/0b were about
whether the pipeline could produce correct work and whether the harness
could be trusted to measure it, 0c was about whether the whole thing could
run *without a laptop command* — file a request from a phone, get a real
answer back. This is the record of building that and watching it work for
the first time.

---

## Decision — 0c before formal 0b, and why

Formal 0b's centerpiece was a Reviewer agent: a second model pass before the
Godot gate. Skipped it, in favor of the daemon, on evidence rather than
guesswork: Phase 0a's Finding 4b/11 showed the dominant failure mode
(whole-file rewrite corruption) is structural — it fires on **100% of tasks
that edit an existing file**, 0% of tasks that only create new ones. A
same-class model reviewing another same-class model's output is a weak bet
to catch a failure mode baked into how that model class rewrites files at
all. The actual fix for that finding is diff-based editing instead of
whole-file rewrites — separate, deferred work, not something a Reviewer
agent naturally provides.

The daemon, by contrast, is infrastructure that pays off regardless of which
model ends up underneath it, and it's the piece that actually matters for
the stated end goal (someone other than the builder filing requests). Build
the thing whose value doesn't depend on solving the open model-quality
question first.

---

## Choosing Slack over Google Chat and Zulip

All three were real options; the deciding factor ended up being who the
*audience* is, not just who's building it.

- **Google Chat** turned out to have a genuine no-public-endpoint path (Pub/
  Sub-based event delivery, architecturally similar to Slack's Socket Mode)
  — the initial assumption that it required a public HTTPS endpoint was
  wrong for the current (2025+) integration model, though the *legacy*
  webhook path does still require one. The Pub/Sub path is real but costs
  meaningfully more setup: a GCP project, three enabled APIs, a service
  account with subscription-level IAM, and a topic/subscription pair, versus
  Slack's "create an app, copy two tokens."
- **Zulip** actually matches the codebase's existing shape best — its bot
  API is long-polling (`call_on_each_event`), the same "poll, don't get
  polled" pattern already used for GitHub Issues. But it has by far the
  smallest install base of the three.
- **Slack won** on the axis that mattered most: the project's stated goal is
  rural medical-training programs eventually using this, and Slack's free
  tier and near-universal familiarity is the lowest-friction choice for
  *that* audience, not just for solo development convenience. Google Chat
  generally requires the recipient to be on a Workspace with admin rights to
  install a custom app — real friction for exactly the audience this
  project is aimed at.

**Lesson:** when a tool choice has to serve future users who aren't in the
room, weight their onboarding cost, not just your own setup cost.

---

## Finding 12 — a live-tested secret is a burned secret, no exceptions

Mid-setup, both Slack tokens (`xapp-...` app-level, `xoxb-...` bot) got
pasted directly into the conversation instead of run straight in a
terminal. Stored them in Keychain immediately so the session wasn't
blocked, but treated both as compromised the moment they were visible in
chat history — rotated both (regenerate the Socket Mode token, "Reinstall
to Workspace" for a fresh bot token) before building anything real against
them.

**Lesson, stated as a rule for next time:** any command that embeds a
secret value goes straight into the terminal from the source (the
provider's UI, a password manager), never through an intermediate
paste — even to a trusted assistant, even for a few seconds. The GitHub PAT
setup earlier in the project got this right by design (`security
find-generic-password` reads it back without ever printing the raw value
in a shareable context); the Slack setup didn't, because token creation
naturally displays the value once on-screen and it's easy to just copy the
whole line. Worth remembering that "the token is displayed to me" and "the
token should be copy-pasted elsewhere" are different steps, and the second
one should go straight to `security add-generic-password`, not through
anywhere else first.

---

## Finding 13 — refactor for reuse before adding a second caller, not after

`main.py`'s original `run()` was a Typer CLI command with the whole
pipeline inlined. The daemon needed to call that same logic in-process
(no subprocess spawn, since it's a long-running loop, not a one-shot bench
task). Rather than have the daemon shell out to `orchestrator.main` as a
subprocess (the way `bench.py` does, for good reasons specific to bench
isolation), the pipeline logic was pulled into a plain function —
`run_task(issue_number) -> dict` — with the Typer command reduced to a
three-line wrapper around it.

This was done *before* writing a single line of the daemon, not after
hitting a wall trying to reuse CLI-shaped code. Two callers (the CLI command
and the daemon's poll loop) sharing one real function, rather than one
calling the other through a process boundary, meant no subprocess overhead,
no re-parsing of stdout/stderr to extract a result, and exceptions from
real infrastructure problems (Ollama down, a git failure) propagate as
actual Python exceptions the daemon can catch and label distinctly from an
ordinary task failure — instead of being flattened into a subprocess return
code the way `bench.py` necessarily has to handle them.

**Lesson:** when a second caller for existing logic is on the near horizon,
extract the reusable function first. It's a smaller, safer change made
deliberately than the same extraction done reactively once two call sites
already disagree about what the interface should look like.

---

## Finding 14 — the corruption guard's job doesn't stop at 0a; it's now the daemon's primary failure signal

The first two real (non-CLI-test) daemon-picked-up tasks — a jump-sound
cue and a HUD coin counter — both required editing `scenes/player/player.gd`
and both tripped the corruption guard on all 3 attempts, correctly
escalating to `agent:needs-human` and notifying Slack. One of the two
showed the guard catching a **complete wipeout** (7 comment lines to 0, not
a single dropped line) — a more severe instance of the same failure
pattern than anything seen during 0a's benchmarking, caught exactly the
same way.

This isn't a new finding so much as live confirmation of Finding 11's
statistic outside the controlled bench environment: real, organically-
worded tickets (not the carefully-scoped bench task list) still split
cleanly along the same edit-vs-create line. The daemon's very first
real-world outcomes were exactly what the numbers predicted.

**Lesson:** a benchmark result that predicts live behavior on genuinely new,
unscripted input is a benchmark worth trusting. This is the payoff for all
the gate-calibration work in 0a/0b — the numbers weren't just describing
the bench task list, they were describing the model.

---

## Finding 15 — a git error that hides its own cause is worse than no error handling

`commit_push()`'s error path only captured `stderr` from a failed git
command. The first real post-refactor test hit `git commit` on an issue
that had already been solved and merged weeks earlier — `git add -A` found
no changes, and `git commit` failed with "nothing to commit," a message
`git` sends to **stdout**, not stderr. The resulting traceback showed
`failed:` followed by nothing, which briefly looked like a mysterious
crash rather than the mundane, easily-diagnosed cause it actually was.

Fixed by concatenating stdout and stderr into the error detail. Two-line
patch, but worth noting *why* it happened: the original code was written by
pattern-matching "capture stderr on failure," which is the right instinct
for most CLI tools but not for git, which routes some genuinely important
failure messages (including some of its most common ones) to stdout instead.

**Lesson:** when wrapping a subprocess call generically, capture and surface
both streams on failure unless there's a specific reason not to. "Which
stream carries the useful error" is a per-tool detail, not a safe default
to assume.

---

## Finding 16 — the milestone, and what it actually proved

First live end-to-end cycle, timestamped from Slack and the terminal log
together:

```
16:31  Slack: "Queued #60: Add a coin counter label to the HUD"
16:32  Slack: ":gear: Starting #60"
16:35  Slack: ":x: #60 needs a human - see the issue comments"
```

Three minutes, zero commands typed on the laptop after the daemon was
started. A request filed from Slack was picked up by an unattended poll
loop, run through the full plan → code → corruption-guard → validate →
escalate pipeline, and reported back — the architecture doc's core loop,
working, chained together by code that had never run as one continuous
path before this moment.

The task itself landing on `needs-human` doesn't diminish this — it's the
correct outcome for a task on the known corruption ceiling, and *reporting
that correctly and quickly* is exactly the trustworthy-failure behavior the
whole project has been building toward since Phase 0a night one. A daemon
that silently hangs, or opens a broken PR, or crashes without telling
anyone, would have been a failure of 0c regardless of what the model did.
This one told the truth, fast, on the right channel.

**Lesson, and the real headline of this file:** the milestone was never
"the model solves this task." It was "a person who has never touched this
codebase can ask for something and reliably learn, within minutes, whether
it happened or why it didn't." That's now demonstrated, live, not just
architected on paper.

---

## Open items carried forward

- [ ] Diff-based editing instead of whole-file rewrites — the actual fix
      for the edit-vs-create corruption split (Finding 11/14), not a
      Reviewer agent. Biggest remaining lever on pass rate.
- [ ] The Ollama health check in the poll loop fires every cycle even
      during an active task run — harmless but noisy in logs; could skip
      the check while a task is in flight.
- [ ] No launchd plist yet — the daemon has only been run interactively in
      a foreground terminal. Needed before "always-on" is real rather than
      "on while I'm at my desk with the terminal open."
- [ ] `/task feedback` and `/task retry` are implemented and wired into
      `run_task`'s prompt (prior feedback comments are read back in) but
      not yet exercised end-to-end on a real needs-human task — worth
      doing deliberately: file feedback on #60, watch it get picked back
      up and re-attempted with that context.
- [ ] Concurrency is still hard-coded to one task at a time (by design, per
      the architecture doc's single-writer rule) — fine for now, revisit
      only if queue depth ever becomes the actual bottleneck.
