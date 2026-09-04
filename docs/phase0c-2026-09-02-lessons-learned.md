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

## Finding 17 — a green PR is not proof of anything; a silent scope-filter bug shipped zero test coverage past every existing safeguard

The daemon's first real feature request (a HUD coin counter) produced a
`PR ready` message on attempt 1 with no errors, no corruption-guard trip, no
retry. It looked exactly like a clean success — until the PR's file list
was checked by hand: **one file changed, `scenes/main/main.tscn`. No test.
No new script. Nothing the pipeline's own "no code passes unverified"
guarantee was supposed to prevent.**

Root cause, found by reading the trace: the planner named
`tests/unit/test_coin_counter.gd` as `test_file`, but never listed it in
`files_to_change` or `files_to_create`. The scope filter — built weeks
earlier specifically to stop the model from corrupting files outside a
task's plan (Finding 4) — correctly dropped the test file as "outside plan
scope," per its own logic. The gate then validated only the pre-existing
suite, which passed trivially because nothing new existed to break it. A
technically-correct component (the scope filter) and a technically-correct
gate (validate.sh) combined to produce a false "all clear."

**First fix (necessary but incomplete):** added a check that bounces the
task back for a retry if `plan.test_file` never lands in `written`. Applied
this, watched three attempts fail identically with the exact same "wasn't
written" message every time — and that repetition was itself the signal
that this fix only *detected* the problem, it couldn't *solve* it. The
scope allowlist was computed once from the plan and never revisited; no
number of coder retries could put the test file in scope if the plan never
listed it there to begin with. The bug wasn't in what the model wrote (it
wrote the test correctly every single attempt) - it was in a stale filter
checking against a plan that was never going to change.

**Real fix:** always include `plan.test_file` in the scope allowlist,
regardless of whether the planner also remembered to list it elsewhere -
it's a schema-required field, so it should always be writable. One line.
The detection guard from the first fix stays as a backstop for the case
where `test_file` itself is missing or malformed, but the actual save was
making the filter stop contradicting the schema it was supposed to trust.

**Lesson, in two parts:**
1. **A clean run through the gate is not the same as a verified result.**
   The only thing that caught this was manually reading a PR's diff instead
   of trusting its green status - exactly the discipline the whole project
   has run on since 0a's first false 0/10, applied to a new place (a
   daemon's own success message) where it hadn't yet been tested.
2. **When a bug repeats identically across every retry, stop retrying and
   ask what's frozen.** The coder was never going to fix this because the
   coder wasn't broken - the plan-derived scope filter was, and nothing
   about retrying the *coder* could touch that. The fix belonged one layer
   up from where the symptom appeared, same shape as Finding 5's log-tail
   blindness: read where the retry loop's *inputs* come from before
   assuming the model just needs another chance.

**Bonus finding, folded in from the same debugging pass:** the eventual
real failure on this task (once the scope bug was fixed) was a `:=` type-
inference parse error on an ambiguous right-hand side
(`load(...).instantiate()`), and it **crashed** the gdUnit4 runner rather
than failing cleanly - the same abnormal-exit signature as Finding 2's bare-
class-reference crash. That generalizes Finding 2: it now looks like *any*
parse error during test discovery crashes this Godot/gdUnit4 combination,
not narrowly bare class references. Added to CONVENTIONS.md: prefer
explicit types over `:=` inference whenever the right-hand side's type
isn't statically obvious.

---

## Finding 18 — diff-based editing: built, tested, and it did exactly what it was supposed to do (even though the pass rate stayed 0/10)

The corruption findings (4/4b/11/14/17) all traced to one mechanism: the
coder rewrote entire existing files, and the 14B couldn't reliably
transcribe ~30+ unchanged lines while making one change. Replaced whole-
file rewrites for existing files with a search/replace interface instead -
the model copies a short exact snippet to find and writes what replaces
it, never touching the rest of the file. New files (0% corruption rate per
Finding 11) kept the old full-content path unchanged.

**First bench (`diffedit-v1`, 0/10):** looked like no progress at all until
the failures were classified individually - 8 of 10 tasks never reached
Godot; they died purely on `search` text not matching the file. Read one
real trace instead of assuming: the model's *wording* was character-
perfect, but it consistently miscounted leading tabs while JSON-escaping
them as `\t` inside a structured output field - missing a tab entirely on
one line, one short on the next. Not hallucination, not paraphrasing - a
narrow, specific, fixable problem.

**The fix, iterated live rather than shipped on the first idea:**
1. First attempt: a whitespace-tolerant fallback matcher (ignore leading/
   trailing whitespace when the exact match fails), reapplying using the
   *file's* real indentation. Verified against the actual captured
   failure from run #63 before it ever touched a real bench task - and a
   second, hand-written test for a multi-line insertion immediately
   exposed a bug in the first version: a naive "copy the last matched
   line's indent onto every added line" heuristic put a new `elif` one
   nesting level too deep.
2. Replaced it with a dedent-and-reindent approach: strip the model's
   `replace` block to its own internal minimum indentation, then rebuild
   it against the file's real base indent at the match site - independent
   of whatever `search`'s indentation bug was. A second self-authored test
   then revealed its own typo (inconsistent hand-typed indentation),
   which the algorithm faithfully reproduced rather than silently
   "fixing" - the honest, documented boundary of the technique: it
   corrects a consistent *absolute* offset, it cannot repair a `replace`
   block whose *relative* nesting is internally wrong. That's a real
   limit, not a bug, and it's stated plainly rather than hidden.

**Second bench (`diffedit-v2`) - the actual verdict:** classified all ten
outcomes by what layer they failed at, not just pass/fail:

| Outcome | v1 | v2 |
|---|---|---|
| Reached Godot (real validation failure) | 1 | 4 |
| Reached the test_file guard (writing succeeded) | 1 | 3 |
| Still failed purely on edit-matching | 8 | 3 |

**7 of 10 tasks got past the exact mechanism that used to block 8 of 10.**
Manually read one of the "reached Godot" failures (task 1): `Cannot call
method 'add_child' on a null value` - an ordinary bad-resource-path
mistake in freshly-written test code, the same *kind* of failure this
project has measured since night one. Not a harness artifact.

**The pass rate stayed 0/10, and that's still the honest number to
report** - but it now means something different than it did on night one.
The remaining failures are model-capability failures (wrong logic, wrong
paths, one case where even the fallback correctly refused a genuinely
non-matching search), not transcription-fidelity failures. Diff-based
editing did not make this model good enough to pass the bench unattended.
It did remove an entire structural failure class that no amount of
prompting was ever going to fix, and left behind the kind of failures that
a stronger model - or more iteration budget, or a Reviewer pass - could
plausibly address. That's the difference between a harness with a hole in
it and a harness that's honestly measuring a model's real ceiling.

**A separate, smaller finding surfaced along the way:** the test_file
guard (Finding 17) fired on 3 of 10 tasks this run, up from occasional -
worth watching whether the two-mechanism output format (`new_files` vs
`edits`) makes it easier for the model to lose track of the required test
file now that it has two lists to keep straight instead of one. Not yet
confirmed as a real pattern; flagged for the open items below.

**Lesson, and maybe the best-earned one of the whole project:** "the
number didn't move" and "nothing improved" are not the same claim, and
conflating them here would have thrown away the actual result. Classifying
*where* a failure happens, not just whether it happened, is what turned an
apparently-flat 0/10 into a clear, evidence-backed confirmation that a
real structural bug got fixed.

---

---

## Finding 19 — the Reviewer agent (formal 0b), built and tested honestly, then correctly turned off

Diff-based editing (Finding 18) removed most of the corruption risk that
originally justified deprioritizing a Reviewer agent - what remained were
ordinary logic/capability failures (phantom scenes, missing `add_child()`
before `get_tree()` use, duplicate declarations), exactly the class of
mistake a second reading of the actual resulting code should be good at
catching. Built it: after files are written but before the Godot cycle, a
reviewer call reads the real current content of every touched file against
the plan and conventions, approves or rejects with specific issues. A
rejection skips Godot (cheap) and retries; an approval still goes on to
real validation - Godot never bypassed, the reviewer only ever adds a
cheaper pre-filter.

**The bench run itself was methodologically compromised and that's worth
owning plainly:** it went out with two new variables at once (the reviewer,
and a MAX_ATTEMPTS bump from 3 to 5) against the last clean baseline,
breaking the one-variable-per-run discipline this project held to all
week. That made the raw 0/10 pass rate uninterpretable on its own - so the
question asked instead was narrower and more honest: is the reviewer's
*judgment* any good, independent of whether it moved the pass rate?

**The answer, read from the actual rejection text, was no - and the
evidence is unambiguous.** Issue #93 ("Extract game config autoload" -
the whole point was moving hardcoded values INTO a GameConfig autoload)
got reviewed five times:
- review-1: *"`GameConfig` is not defined - revert to hardcoded `200.0`,
  `-400.0`, `9.81`"*
- review-2/3: *"these should NOT be hardcoded - they should read from
  `GameConfig`"*

The reviewer told the coder to undo the task's own stated goal, then
reversed itself on the very next attempt - directly counterproductive, not
merely unhelpful. A separate task (#84) showed a second failure pattern: a
confidently-stated "unused variable" flag on `gravity`, which is used on
the very next line of the same function in every version of this file seen
all week - a plausible false positive stated with the same confidence as
the genuinely correct catches sitting right next to it (an indentation
error, a wrong gdUnit assertion) in the same review.

**Disabled by default**, reasoning documented inline in `config/
models.yaml` rather than silently commented out. The code stays - schema,
prompt, wiring are all real, working infrastructure, not wasted effort.

**Lesson:** this is the same-class-review risk that motivated skipping 0b
in the first place, now confirmed with hard evidence instead of inferred
from first principles. A reviewer's rejections read exactly as confident
and well-formatted whether they're right or wrong - the self-contradiction
between review-1 and review-2 on #93 wasn't hedged or uncertain either
time, it was two opposite, equally confident verdicts on the same code.
That's the real danger of same-class review: not that it's obviously
unreliable, but that unreliable and reliable output are indistinguishable
by tone alone, which is exactly why this needed a bench run and a
transcript read rather than a judgment call from the architecture. A
plausible next step, not attempted tonight: route review through a
different or stronger model than the coder, since a second opinion is
only worth something if it's actually independent.

---

## Finding 20 — two competing Ollama services, one silently serving every request for days

Recurring, unexplained hangs (a stall on task 7 one night, task 4 the next
morning) were chased as model- or task-specific problems before the real
cause surfaced: `launchctl list | grep ollama` showed **two separate
registered services** - `homebrew.mxcl.ollama` (what every `brew services
restart ollama` command all week had actually been targeting) and
`com.zac.ollama` (a custom LaunchAgent built by hand back in 0-prep,
day one, specifically to control env vars like `OLLAMA_KEEP_ALIVE`).
`ps aux` confirmed it: PID 1528, running continuously since **Sunday 6PM**,
was the one actually bound to port 11434 and answering every single
request all week. Every `brew services restart` had been restarting a
service that was never serving anything - the real, multi-day-old process
was untouched by any of it.

This plausibly explains both prior hangs on its own: a single inference
router process alive for days, cycling through four different models
dozens of times without ever restarting clean, is exactly the kind of
thing that accumulates state and eventually stalls.

**Fix:** killed the stale process, disabled the old custom LaunchAgent
(`launchctl bootout` + renamed the plist to `.disabled` so it can't
silently reappear on next login), and standardized on `brew services`
going forward as the single source of truth.

**Lesson:** infrastructure set up once at the start of a project (the
custom LaunchAgent, built for a real reason in 0-prep) can silently
outlive its purpose once later tooling (`brew services`, adopted for
convenience mid-project) is layered on top without retiring the old path.
Two things that both look like "how Ollama is running" were true at once
for weeks, and only `launchctl list`'s raw registry - not `ollama --version`,
not `curl`, not anything that talks to the service through its normal
interface - revealed that there were two.

---

## Finding 21 — qwen3-coder:30b: worse than the 14B on edit-mechanics at first, then the single best result of the whole project after one prompt fix

**Speed, settled immediately and cleanly.** `qwen3-coder:30b` (30B MoE,
3.3B active, 20GB, 12%/88% CPU/GPU split - the best split of any model
tried) ran every task in the 104-249s range, fully competitive with the
14B and nothing like the 12-56 *minute* ordeals of the two prior larger
models. First model above 14B to look genuinely practical on this
hardware, exactly matching what Finding 10's dense-vs-MoE theory predicted
a smaller, cleanly-fitting MoE model should do.

**Quality, on the first bench (`qwen30b-coder-v1`), was a real surprise -
and not a good one.** Edit-mechanics failures: 6 of 10 tasks, *worse* than
the 14B's most recent clean showing (3/10). Reading the actual failures
showed two distinct new habits this model has that the 14B didn't: it
would sometimes write a `search` block spanning most of a file (defeating
diff-based editing's whole purpose from the inside), and it hit the same
repeated-test-boilerplate ambiguity the 14B hit weeks earlier - meaning
the existing prompt guidance for that case (a soft "if this commonly
happens, consider...") wasn't reliably followed by *either* model family.

**Fix:** tightened `prompts/coder.md` from a suggestion to a requirement -
disambiguating via the enclosing `func test_...` line became mandatory,
not optional, whenever setup code repeats across functions; a 1-4 line
cap on `search` length was stated as a hard rule, not encouraged.

**Measuring it required fixing something else first - a timeout bug with
its own real story.** A prior fix (night one's long-standing "Ollama calls
have no timeout" gap) had been patched using `ThreadPoolExecutor` and
verified with a quick test that showed it raising the right exception.
That test was insufficient: a live bench run hung for 965 seconds with the
timeout message never appearing anywhere. The actual bug was subtle -
`ThreadPoolExecutor` registers a cleanup hook that blocks process exit
until every submitted thread finishes, so the 180s timeout *did* fire and
raise `RuntimeError` internally, and then the process sat waiting anyway
for the now-abandoned Ollama call to finish on its own, which could take
15+ minutes. The fix that had "passed its test" was actually still fully
broken under real load.

Real fix: a plain daemon thread instead of an executor - `thread.join
(timeout=...)`, and on timeout the process can actually exit immediately
because a daemon thread never blocks shutdown. Verified properly this
time: not "does it raise the right error" but "does the whole process
return control to the shell afterward," using an artificially short
timeout to force the real code path and watching for the process to
actually end.

**With both fixes in place, `qwen30b-coder-v2` (identical tasks, one
prompt change from v1) produced the cleanest result of the entire
project:** edit-mechanics failures dropped from 6/10 to **2/10**;
"reached Godot" (meaning the model's code was actually complete and
running through real validation, whatever its outcome) rose from 4/10 to
**8/10**. That is the single largest swing any harness or prompt change
has produced. The remaining two failures were the fallback matcher
correctly refusing content that genuinely wasn't in the file - the honest
limit, not a bug.

**Lesson, and it's really about method, not models:** a fix that looks
correct and passes a quick check can still be broken in a way only real,
sustained load reveals - "the code is right" and "the fix works" are not
the same claim, and only one of them was actually verified the first
time. The timeout bug wasn't a distraction from measuring the prompt fix;
it was the thing standing between "we have an idea that might help" and
"we have a number that proves it," and only got found by refusing to
accept a plausible-looking exception message as proof the underlying
process behavior was correct.

---

## Finding 21b — the same prompt fix, tested on the 14B: essentially no effect

The obvious next question after Finding 21's 4/10 → 8/10 swing: is the
tightened prompt (mandatory boilerplate disambiguation, hard search-length
cap) a universal improvement, or something specific to the 30B? Reran the
full ten-task bench on `qwen2.5-coder:14b` with the identical tightened
prompt (`conv-v7-14b-tightened`). Result: **4/10 reached Godot** - flat
against the 14B's own recent history, and actually worse than the
`diffedit-v2` baseline's 7/10 clearing edit-mechanics (equivalently, 6/10
still stuck here vs. 3/10 there).

**Not a wasted or disappointing result - a real, controlled negative that
sharpens Finding 21 instead of undermining it.** Reading what each model
actually did wrong explains the gap: the 30B's edit-mechanics failures
included a habit the 14B never really showed - `search` blocks spanning
most of a file, defeating diff-based editing from the inside. The hard
length cap is a precise antidote to exactly that behavior. The 14B's edit
failures were always more about short-snippet whitespace mismatches and
generic ambiguity - problems already substantially handled by the
whitespace-tolerant fallback matcher and the pre-existing softer guidance.
The fix targeted a mistake that happens to be a 30B habit, not a 14B one.

**Lesson:** a prompt change validated on one model is a claim about that
model, not a claim about models in general, until it's actually tested
elsewhere - the instinct to check was worth having, and the negative
result is exactly as informative as the positive one from Finding 21.
`qwen3-coder:30b` + the tightened prompt remains the strongest
configuration found this project; that conclusion holds. What doesn't
hold is any assumption that the tightening was a free win applicable
everywhere - it demonstrably was not, on hard evidence rather than guess.

---

## Open items carried forward

- [x] `/task feedback` and `/task retry` exercised end-to-end on a real
      needs-human task (#60) — confirmed working: feedback comment posted,
      re-queued, planner incorporated it, produced a different (better)
      approach that avoided the known player.gd corruption ceiling entirely.
      Uncovered Finding 17 in the process.
- [x] Diff-based editing instead of whole-file rewrites (Finding 18) — done;
      cut edit-matching failures from 8/10 to 3/10 tasks. Remaining gap is
      model capability, not the editing mechanism.
- [ ] The test_file guard (Finding 17) fired on 3/10 tasks in diffedit-v2,
      up from occasional before — worth confirming whether the two-
      mechanism output format (new_files vs edits) makes the model more
      likely to drop the required test file now that it has two lists to
      track instead of one.
- [ ] The Ollama health check in the poll loop fires every cycle even
      during an active task run — harmless but noisy in logs; could skip
      the check while a task is in flight.
- [ ] No launchd plist yet — the daemon has only been run interactively in
      a foreground terminal. Needed before "always-on" is real rather than
      "on while I'm at my desk with the terminal open."
- [ ] Concurrency is still hard-coded to one task at a time (by design, per
      the architecture doc's single-writer rule) — fine for now, revisit
      only if queue depth ever becomes the actual bottleneck.
- [ ] Confirm whether ANY parse error during gdUnit4 test discovery crashes
      the runner (Finding 17's generalization of Finding 2), or just the
      two specific cases seen so far (bare class refs, ambiguous `:=`
      inference). Worth a deliberate repro sweep if this keeps recurring.
- [ ] Reviewer agent (Finding 19) — disabled by default; worth one clean
      re-test with a stronger/different review model than the coder before
      concluding the whole approach is dead, since same-class review was
      the specific thing that failed, not necessarily review in general.
      Must run as a single isolated variable next time, not bundled with
      other changes.
- [x] Ollama calls had no timeout (open since night one) — fixed, and fixed
      TWICE (Finding 21): the first attempt used ThreadPoolExecutor, which
      raised the right exception but still let the process hang for
      15+ minutes waiting on an abandoned thread via the executor's atexit
      hook. Real fix is a plain daemon thread. Verified by confirming the
      process actually exits, not just that it raises the right error.
- [ ] `qwen3-coder:30b` + the tightened coder prompt (Finding 21) is the
      best-performing configuration found all project — 8/10 reaching real
      validation, competitive speed with the 14B. Worth making this the
      default in config/models.yaml once a couple more clean runs confirm
      the 8/10 wasn't a lucky run. (The prompt tightening does NOT transfer
      to the 14B - tested directly, Finding 21b - so this is specifically
      a 30B-configuration recommendation, not a global prompt change.)
- [x] Two competing Ollama services running simultaneously since project
      start (Finding 20) — found and fixed; the multi-day-old stale
      process was the likely cause of several "mystery" hangs blamed on
      specific tasks or models. Standardized on `brew services` only.

---
