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

## Finding 21c — the 8/10 result confirmed on a second clean run

Reran the identical `qwen3-coder:30b` + tightened-prompt configuration
(`qwen30b-coder-v3`) purely to check Finding 21's 8/10 wasn't a lucky
roll. It wasn't: **8/10 reached Godot again**, with the same 8/1/1 split
between "reached Godot," "edit failed even with fallback," and "edit
ambiguous" as `qwen30b-coder-v2`. Also checked, on the same day, whether
context-window size could plausibly explain any of this project's
failures generally: mined every trace's recorded `prompt_tokens` across
622 calls project-wide. The largest was one outlier at 83.7% of the
16,384 ceiling (an early 27B run); every `qwen3-coder:30b` call this week
sat at 20-25%. Context truncation is ruled out as a cause of anything
seen so far - the failures have specific, already-identified causes
(wrong API calls, transcription mismatches, edit-matching misses), not a
capacity problem.

**This is now the confirmed default**, backed by two independent clean
runs rather than one: `qwen3-coder:30b` reliably reaches real Godot
validation on 8 of 10 tasks - meaning the model's code is complete and
testable, not that it's correct. The honest ceiling moved from "often
can't even produce matchable, testable code" to "reliably produces
testable code that's still frequently wrong" - a meaningfully better
place to be failing from, even though the full autonomous pass rate is
still 0/10.

---

## Finding 22 — task 1 (double-jump), a deep dive: three real root causes found by hand, ending in an actual PASS

Task 1 had "reached Godot" on every `qwen3-coder:30b` run (v1, v2, v3) and
on every 14B run before that, but never once cleared real validation.
Rather than treat that as one more entry in a tally, picked this single
task and debugged it by hand, past the automated 5-attempt budget, to
find out what was actually standing between "reaches Godot" and "passes."
Three genuinely distinct, real causes, layered on top of each other:

**1. Faking `is_on_floor()` instead of building a real floor.** Every
attempt across every run used `player.set_position(...)` and
`set_collision_layer_value(...)` to "simulate" being grounded -
`is_on_floor()` is a real physics result computed during
`move_and_slide()`'s actual collision detection; nothing about position
or collision-mask values can substitute for it. Confirmed precisely from
the failing numbers themselves: expected jump velocities came back as
plain gravity decay (`0.0`, then `16.33` - one physics frame's worth of
fall) - no jump ever fired, because the player was never actually
grounded to jump from.

A CONVENTIONS.md rule naming this exactly, with a code snippet included,
was written and confirmed present in the prompt on the *next* attempt -
and the model still faked the floor with `position.y` anyway. A second,
different mechanism was needed: a hand-written, fully-working exemplar
test file (`test_floor_pattern_example.gd`) added to the coder's
always-shown files. That worked immediately - the very next attempt built
a real `StaticBody2D` + `CollisionShape2D`, copied almost verbatim from
the exemplar. **A worked example succeeded exactly where a specific,
correctly-worded prose rule failed twice in a row** - the clearest
demonstration yet that for structural patterns (not simple wrong-API
knowledge gaps), showing beats telling for this model class.

**2. Missing `Input.action_release()` between repeated presses.** With
the floor fixed, jumps still weren't firing on the second and third
attempts. `Input.is_action_just_pressed()` only fires on the transition
from released to pressed - calling `action_press()` again while the
action is already held is a silent no-op, no new edge, no new jump.
None of the model's test attempts across any run ever released the
button between presses. Confirmed by hand-editing in the releases and
watching the exact failure disappear. Written into CONVENTIONS.md as its
own rule - general enough to plausibly help any future test needing more
than one simulated press.

**3. An assertion checking the wrong thing.** After both fixes, one
assertion still failed: a "third jump should do nothing" test asserted
`velocity.y == exactly -400.0` (the jump-impulse value) instead of
something like "still less negative than a fresh jump" - the correct
game behavior (no third jump, gravity decaying as normal) will never
match an exact equality check written for a moment-of-jump velocity. Not
a hallucination or a missed rule - a self-inconsistent test, asserting
something other than what its own comment said it was testing. Fixed by
hand to `is_greater(-400.0)`.

**A minor methodological note from the debugging itself:** several
attempts to patch the test file by hand failed on exact string matching
before landing - not because the reasoning was wrong, but because real
whitespace (blank lines between statements, a repeated comment appearing
in two functions) didn't match what was assumed. Fixed each time by
printing `repr()` of the actual surrounding text on a failed match rather
than guessing again - the same "read the ground truth, don't reason from
memory" discipline that's been the throughline of this entire project,
just applied to editing a file by hand instead of reading a model's trace.

**Result: `VALIDATE: PASS`, all 5 test cases, all 4 suites.** The fixed
test is now committed in the game repo. Worth stating the honest scope
plainly: **the automated pipeline did not solve this within its 5-attempt
budget on any run** - every one of these three root causes required
manual, line-by-line human debugging past what the harness does on its
own. This is not evidence the model can pass this task unattended today.
What it does prove: `player.gd`'s double-jump implementation (written by
the model, verified correct by hand back when this task was first
diagnosed) was right all along - every blocker was in test code, never in
the actual game logic - and two of the three causes are now permanently
encoded (a CONVENTIONS.md rule, plus a working exemplar file) for whatever
runs into them next.

**A loose thread that turned out not to be one, plus a real bug found
while chasing it:** the original version of this finding claimed the
`test_file` guard fired on attempts 2 and 4 specifically, every time,
across all three bench runs. Checked that claim properly against the
actual bench traces (issues #114, #124, #146) before writing it down for
good, and it doesn't hold: the guard never fired at all in two of the
three runs, and fired once, on attempt 3, in the third. No attempt-2/4
pattern exists in the real data - that claim was an overgeneralization
from the two manual `#156` runs done during this same debugging session,
which happened to both show the guard firing on two non-adjacent
attempts.

Tracking down *why* those two manual runs disagreed with each other
(attempts 2 & 4 the first time, attempts 3 & 5 the second) surfaced a
real, previously-unknown bug instead: **`runs/<issue>/` is keyed only by
issue number, not by invocation.** Re-running the same issue by hand -
exactly what this whole debugging session did, twice, on #156 - silently
overwrites the prior run's trace files with no warning. What looked like
"issue 156's attempt history" was only ever the second manual run's data;
the first run's real trace files were already gone by the time they were
read. Fixed by archiving any pre-existing `runs/<issue>/` directory under
a timestamp suffix before a fresh run starts, rather than restructuring
the storage layout - every trace-reading script written this project has
assumed a flat `runs/<issue>/*.json` path, and this preserves that for
whichever run is current while never destroying the ones before it.

**Lesson:** the same discipline that's run through this whole project -
check the claim against real evidence before writing it down, don't
generalize from two data points - applies to the project's own
documentation, not just to the model's behavior. A "mystery" is worth
naming provisionally, but confirming it before it becomes a citation is
what separates a real finding from an anecdote that happened twice.

---

## Finding 23 — the Reviewer agent, revisited with a stronger model: individually correct, collectively unable to converge

Finding 19 shelved the Reviewer agent after a 14B reviewing a 14B produced
a self-contradiction (issue #93: told the coder to revert a task's own
goal, then reversed itself). Worth a clean re-test once `qwen3-coder:30b`
became the default - same model reviewing itself, avoiding cross-model
swap latency, and genuinely more capable than the 14B that failed the
first test.

**First clean run (`reviewer-30b-v2`, MAX_ATTEMPTS=5) came back stark:**
"reached Godot" collapsed from the no-reviewer baseline's 8/10 down to
1/10. The reviewer rejected 34 of 37 review calls - a 92% rejection rate.
That number alone looked like Finding 19 repeating itself. It wasn't:
reading actual rejections (task 6/issue 184's three review cycles) showed
**specific, correct, previously-confirmed catches** - a bare class
reference that crashes the gdUnit4 runner (the exact Finding 2 pattern),
a `get_tree()`-before-`add_child()` null-reference bug (the exact Finding
22 pattern, discovered via manual debugging on the very same day), and a
plausible, appropriately-hedged concern about missing a physics-frame
await. None of it resembled #93's flat self-contradiction. **The
reviewer's individual judgment was sound.** The collapse in "reached
Godot" had a different, more mundane explanation: every rejection
consumes one of a fixed 5-attempt budget the coder also needs for
ordinary validation-fix cycles, so a task needing 3 real review-fix
rounds had only 2 attempts left to ever reach Godot at all.

**Tested that theory directly** rather than assume it: gave
reviewer-enabled runs 2 extra attempts (7 instead of 5), reasoning that
review and validation shouldn't have to compete for the same slots.
`reviewer-30b-v3` came back **worse, not better**: tasks ending in
permanent reviewer deadlock rose from 5/10 to 7/10, "reached Godot" stayed
flat at 1/10, and one task (#4, issue 192) burned 1578 seconds - 26
minutes - of real, successful inference calls and still ended rejected.
Traced both outliers to their real causes rather than leaving them as
loose threads: task 8's early cutoff was the 180s call timeout correctly
catching a genuine stuck generation (the safety net from earlier this
week working exactly as intended, for the first time under real
adversarial conditions); task 4's marathon was simply the doubled call
volume (coder + reviewer, every attempt) compounding with ordinary
session-length slowdown - no crash, just full price paid for zero result.

**Conclusion: this was never a budget problem, it's a convergence
problem.** More room to iterate didn't help the coder and reviewer reach
agreement - it just let them spend longer failing to. Disabled again,
with both rounds of evidence documented inline in `config/models.yaml`
rather than just the original.

**Lesson, and it sharpens Finding 19 rather than repeating it:** a
reviewer being *right* is necessary but not sufficient for a review loop
to be worth running. Two models (or one model in two roles) each behaving
reasonably on their own turn can still fail to converge as a pair -
correct-and-stuck is a different, subtler failure mode than
wrong-and-contradictory, and it took two full bench runs and reading real
rejection text (not just counting them) to tell the difference. The
budget fix was worth testing rather than assuming it wouldn't work -
it was a clean, disprovable hypothesis, and disproving it cleanly closes
this question for now rather than leaving it as an assumption. Revisiting
this again would need an actually different reviewer model, not more
attempts of the same one talking to itself.

---

## Finding 24 — the test_file guard, root-caused and fixed: 0/30 → 4/20 → 0/9

An open item's own framing ("up from occasional") got the same treatment
Finding 22 taught was necessary: checked against real evidence before
trusting it. It held up, and turned out to understate the case - the
guard fired on **0 of 30** tasks across the entire whole-file-rewrite era
(never, not once), then **4 of 20** once diff-based editing shipped. A
real, confirmed, timed-exactly-with-the-architecture-change pattern, not
an overgeneralization this time.

**Root cause, found by reading the actual model output from all four
hits, not guessing:** every single one showed the identical shape -
`new_files: []`, `edits` populated with only the pre-existing files being
changed (`player.gd`, sometimes `test_player.gd`). Not a wrong-list mixup
(the original hypothesis) - a clean omission. Whenever a task needed both
an edit to an existing file and a brand-new file, the model reliably
produced the edit and silently dropped the new file from its response
entirely, 4 for 4.

**Fix:** added an explicit, mandatory cross-check to the end of
`prompts/coder.md` - check the response's `new_files` and `edits` arrays
against the plan's `files_to_create`/`files_to_change` lists before
replying, and name directly that both are commonly non-empty in the same
response.

**First test (issue #199, one task) was honestly inconclusive** - 2 of 5
attempts still hit the guard, 3 of 5 got past it. Correctly not declared
a win on one noisy sample. The real test was a full bench run
(`diffedit-v3`): **9 of 9 valid tasks reached real Godot validation. Zero
test_file guard hits. Zero edit-mechanics failures of any kind** - better
than every prior diff-editing run, and better than the no-reviewer
`qwen3-coder:30b` baselines too (which each had 1-2 edit-mechanics
failures of their own). The cleanest result this specific failure class
has ever produced.

**Caveat worth keeping attached to this result:** the run wasn't perfectly
clean end-to-end. Task 4 hit the 180s call timeout (working correctly,
unrelated to the fix being tested) during a stretch where the machine was
genuinely maxed - `top` showed 23G/24G used, 124M free - after a full
day cycling four different models through Ollama. That same session also
produced eight Godot crash reports in under an hour, all matching Finding
17's known parse-error-crashes-gdUnit4 signature exactly (`EXC_BAD_ACCESS`
at a small offset, the same doubled `recursive_mutex::lock()` frame seen
in every prior instance of this bug) - the first time that generalization
has been confirmed as genuinely *recurring* rather than seen once or
twice. Both are plausibly explained by memory pressure making an
already-known engine bug fire more often, not a new problem - but neither
is fully proven, and the coder-output fix and the memory-pressure/crash
question are separate mechanisms that happened to share a session.
Suppressed the crash dialog system-wide (`defaults write
com.apple.CrashReporter DialogType none`) so a future crash can't block
an unattended run, and restarted Ollama fresh. A fully quiet confirmation
run of the `new_files`/`edits` fix, on a machine with real headroom, is
still worth doing for full rigor - but the result already stands on its
own as strong evidence, not proof pending an asterisk.

**Lesson:** the same discipline applied twice in one investigation -
verify the claim before trusting it, read real output before guessing the
mechanism, don't declare victory on one sample, and be honest about what
context (a maxed-out machine) might be riding along with a result even
when that context doesn't actually explain the specific thing being
measured. Four separate checks, one real, well-earned finding.

---

## Finding 25 — confirmed: ANY parse error crashes the gdUnit4 runner, not just the two originally-seen cases

Finding 17 generalized from two observed crashes (a bare class reference,
an ambiguous `:=` type inference) to a guess: maybe *any* parse error
crashes gdUnit4's test discovery on this Godot 4.7.2 install, not just
those two. Left open rather than assumed, per this project's whole
practice of not trusting a pattern until it's checked.

Built a proper sweep: six structurally distinct broken test files, each
isolated (write, run just the gdUnit4 discovery stage, check for a crash
signature, delete, move to the next) - the same isolation method as the
original Finding 2 repro. Cases: the two known crashers (bare class
reference, ambiguous inference) as controls, plus four new categories -
a missing colon after a function signature (plain syntax error), an
`extends` clause naming a class that doesn't exist, two functions in one
file sharing the same name, and a bare undefined identifier used
directly.

**Result: 6 for 6. Every single case crashed**, with the identical
signature each time (the same `Abort trap: 6` / `recursive_mutex::lock()`
double-frame pattern seen throughout the project). Not narrow to the
original two cases - this looks structural to how gdUnit4's test
discovery handles a script that fails to parse *at all*, regardless of
which specific mistake caused the failure.

**Practical upshot:** this doesn't need a new CONVENTIONS.md rule the way
most findings have - there's no single fix for "don't write a script with
a parse error," since that covers every mistake a model could possibly
make while writing GDScript. What it does confirm is that the
crash-suppression response (system-wide `defaults write
com.apple.CrashReporter DialogType none`, done in Finding 24) was the
right kind of fix - not narrowly for one bug pattern, but for the whole
class. The gate's own handling was already correct too: `validate.sh`
checks both exit code and log content regardless of *how* a stage failed,
so a crash has never silently passed as a PASS at any point in this
project - this was purely about the OS popping a blocking dialog, now
handled.

**Lesson:** a "worth confirming" item sat open for a while rather than
getting silently upgraded to "confirmed" on the strength of a plausible
guess - and when it finally got tested properly, the guess turned out
right, cleanly, with real evidence backing it instead of inference. That
distinction (tested-and-right vs. assumed-and-right) is worth the couple
of days it sat as an open item rather than a closed one.

---

## Finding 26 — the Artist agent: built from raw tool install to a proven live pipeline run

Built the third and final agent role from the original architecture doc -
Coder and Reviewer existed; Artist never did until this session. Full
arc, same discipline as every other capability added this project: prove
the raw integration standalone before wiring it into `run_task()`.

**Tooling, chosen and verified rather than assumed:** ComfyUI (native
install, MPS backend - `Total VRAM 24576 MB` confirmed matching this
machine's unified memory exactly) over Draw Things, since the whole
point is programmatic control from Python, and ComfyUI's API-format
workflow export is the mature, well-documented path for that. Checkpoint:
Z-Image-Turbo (a split architecture - separate diffusion model, Qwen3-4B
text encoder, and VAE, not a single fused file like SDXL - discovered by
reading the actual downloaded filenames rather than assuming). Style:
the `elusarca-pixel-art-style-lora-zimage-turbo` LoRA, verified legitimate
before downloading (Apache 2.0, HF's own "Safe" scan, purpose-built for
this exact checkpoint) and then verified *useful* with a real controlled
comparison - same prompt, same seed, LoRA on vs. off - showing genuine
blocky pixel edges replacing the base model's smooth anti-aliased output.

**The client (`orchestrator/tools/comfyui.py`):** submit → poll → download
→ background-removal, built against the real exported workflow JSON (node
IDs pulled from an actual screenshot of the graph, not guessed), with a
180s poll timeout from the start this time - Finding 21's Ollama-timeout
lesson (a fix that isn't verified to actually let the process exit isn't
a real fix) applied proactively instead of learned the hard way twice.
Verified standalone first (`test_comfyui_manual.py`, a fixed seed, real
sanity checks on the output) before touching `run_task()` at all.

**Wired into the pipeline:** `Plan` gained `needs_art`/`sprite_description`
/`sprite_path` fields (the planner decides whether a task needs a new
visual asset); a new `artist` role turns the rough description into a
real generation prompt; the sprite generates once, before the coder loop
starts, since a bad generation isn't something Godot validation can
explain how to fix the way a code error is. A `_build_context()` helper
ensures the coder is told a sprite already exists at its real path on
every context rebuild in the retry loop, not just the first one.

**A real infrastructure bug found and fixed under live conditions, not in
isolation:** the very first live run timed out at 180s. `ollama ps` showed
`qwen3-coder:30b` still resident at 19GB, `top` showed 23G/24G used - the
planner and artist LLM calls had left the model loaded (by design, for
reload-cost avoidance) right when ComfyUI needed its own 11-15GB on the
same 24GB machine. The earlier standalone test never caught this, because
nothing else was competing for memory at the time it ran. Fixed with
`llm.unload()` - an explicit `keep_alive: 0` call right after the artist
step finishes, before ComfyUI starts - confirmed working on the next two
live runs, no further timeouts.

**Result:** a real sprite (`assets/sprites/coin/coin.png`) generated
inside an actual task run and committed to the game repo - the first
agent-generated game asset in the project's history, produced entirely
end to end: planner decided art was needed, artist wrote the prompt,
ComfyUI generated it, the coder referenced the resulting file while
writing real game logic.

**Lesson:** the standalone-test-first discipline caught most things, but
not everything - a resource-contention bug between two entirely different
subsystems (an LLM server and an image model) only showed up under real,
combined load. Worth remembering for any future addition that shares this
machine's memory with what's already running: proving a piece works in
isolation is necessary, not sufficient, once it has to coexist with
something else.

---

## Finding 27 — the .tscn exemplar test was invalid; the redesigned fix actually works

Finding 22 established a strong pattern: when a prose rule alone failed
twice, a real working exemplar file fixed it on the very next attempt
(the floor-faking problem). Built a `.tscn` scene by hand in the actual
Godot editor - guaranteed byte-correct syntax - wired it into the
coder's always-shown exemplars, and added a CONVENTIONS.md rule.

**The first result looked like a clean negative** - the very next
attempt's `coin.tscn` opened with `extends Area2D` (GDScript class
syntax, not scene format) and old, Godot-3-flavored resource references,
resembling nothing in the supposed exemplar. Read at the time as "the
model reached for memory instead of the working example in front of it."

**That reading was wrong, and the error was mine, not the model's:**
checking `git log --all -- scenes/_template/pickup_template.tscn` came
back completely empty. The template had been built locally in the editor
but never committed or pushed - `_build_context()` reads from a fresh
clone of `origin/main`, so the coder's context almost certainly showed
"(does not exist yet)" for the exemplar, not real content. There was
nothing to reach past. Same class of mistake this project has caught
before (an untracked file quietly breaking a test's premise, same shape
as the trace-overwrite bug in Finding 22) - just this time in the
exemplar-authoring step, not the harness.

**Rather than just fix the commit gap and rerun the original three-node
design, redesigned around a real insight while already in there:** the
model has repeatedly proven reliable at building node trees *in code*
(`test_floor_pattern_example.gd`'s `CollisionShape2D.new()` pattern,
proven since Finding 22) - the actual failure was specifically hand-
authoring raw `.tscn` resource syntax, not building scene structure in
general. Shrunk the template to a near-empty `.tscn` (one root node, two
`ext_resource` lines, zero `sub_resource` blocks) with all child-node
construction moved into the script's `_ready()`. Committed it for real
this time, confirmed with `git log` that a real commit existed before
testing again.

**Result: the redesign works.** A full task run produced a `coin.tscn`
that parsed and loaded cleanly - zero scene-syntax errors anywhere across
five attempts, a first. The task still didn't pass, but for reasons
worth being precise about, since two wrong theories got chased before
finding the real one: first suspected the coder left the script pointing
at `pickup_template.gd` instead of the new script (checked the actual
`.tscn` - wrong, it was correctly retargeted); then suspected the test
file was loading the exemplar's scene path instead of the new one
(checked the actual test file - wrong, every `load()` correctly pointed
at `coin.tscn`). Pulling the *exact* attempt-2 response from its trace
file (not a later attempt's leftover disk state) finally showed the real
bug: `var circle := CircleShape2D.new()` followed by `pcircle.radius =
...` - a plain undefined-variable typo, unrelated to scene-writing,
exemplars, or anything this investigation was actually about. Ordinary
noise, the same class of mistake found throughout this whole project.

**Lesson, really two of them:** first, the same "verify before trusting
a negative result" discipline that corrected Finding 22's test_file-guard
overgeneralization applies here too - a clean negative test is only
informative if the test actually ran the way it was assumed to, and
"the file exists locally" is not the same claim as "the file is in the
repo the harness actually reads from." Second, a wrong first guess (or
second) at a root cause is a normal, low-cost part of debugging as long
as each guess gets checked against real evidence and discarded when
wrong, rather than patched over blind - exactly what happened here,
three times over, before landing on the real answer.

---

## Finding 28 — the newline-collapse transcription defect: confirmed recurring, not a one-off

First seen three days earlier (issue #211's original run): `search` and
`replace` blocks with a `\n` missing immediately after a block-opening
`:` - `func _physics_process(delta: float) -> void:\t# NOTE: ...` where a
real newline should separate the colon from the next line. Read at the
time as "likely a one-off, not enough evidence to draw a rule from" per
Finding 22's own lesson about not overgeneralizing from a single sample.

**It recurred**, in a different file, in a different session, in a
different shape: a malformed nested class inside `test_coin.gd` -
`class TestPlayer extends CharacterBody2D:\tfunc _on_coin_collected()
-> void:\t\tprint(...)` - same defect, a colon immediately followed by a
tab instead of a newline. This is what caused the "confusing" repeated
edit failure in the same run: the coder searched `player.gd` three times
for `func _on_coin_collected() -> void:` because that method only
existed inside this malformed, improperly-nested class the model had
accidentally generated in the *test* file - not because the model was
stuck, but because it was accurately, repeatedly searching for something
that genuinely didn't exist where it believed it did.

**Two independent occurrences now confirmed** - this crosses the line
from "maybe noise" to a real, trackable defect worth a name, even without
a fix yet. Not yet understood: why specifically the character after a
block-opening colon, and not elsewhere; whether it correlates with
response length, generation speed, or something else measurable in the
traces already being collected.

**Lesson:** the same discipline that upgraded the test_file guard's
"up from occasional" claim into a real, checked pattern (Finding 24)
applies here in reverse - a plausible one-off is worth writing down
provisionally and watching, not chasing prematurely with a rule built on
a single data point. Two occurrences is still a small sample, but it's
no longer zero evidence.

---

## Finding 29 — two real planner/harness bugs found chasing one task, both fixed with hard evidence, landing at a clean ordinary ceiling

Continuing the same issue #212 investigation (coin pickup - the task
behind Findings 27 and the corrected .tscn redesign), two further rounds
of "guess, then check, then correct the guess" turned up two genuinely
new, real bugs - neither about scene-writing, both about the planner's
file-scoping decisions.

**Bug 1: prior feedback can describe a file from a never-merged attempt,
misleading the planner into thinking it already exists.** After leaving
feedback about a typo in `coin.gd`, the very next run failed identically
across all 5 attempts - `files_to_change` listed `scenes/coin/coin.gd`,
but a fresh checkout of `main` never had it (every prior run of this
issue had ended in `NEEDS HUMAN`, nothing ever merged). The coder
correctly tried to write it as new, the scope filter correctly rejected
it since only `files_to_change` listed it. Confirmed directly from the
real plan JSON before fixing anything. Fixed structurally, not with a
prompt tweak: right after the plan is made, any `files_to_change` entry
that doesn't actually exist on disk gets silently reclassified into
`files_to_create`. This checks a plain fact instead of trusting the
model's inference about ambiguous context - genuinely more robust than
asking the planner to reason about it correctly, and confirmed working
twice now, on two different files (`coin.gd`, then independently
`test_coin.gd` a run later), not a narrow one-off patch.

**Bug 2: the planner can omit the scene file entirely for a task that
obviously needs one.** With bug 1 fixed, the next failure was `Cannot
open file 'res://scenes/coin/coin.tscn'` - checked the actual plan JSON
directly rather than guess, and `coin.tscn` was never in `files_to_create`
or `files_to_change` at all. Not a coder mistake - the coder correctly
never touched a file outside its plan's scope; the plan itself simply
never asked for it. Fixed with an explicit planner.md instruction: any
task creating a new interactive object must include both its script AND
its scene file, not just the script.

**Result: with both fixed, every one of 5 attempts reached real Godot
validation** - zero scope rejections, zero test_file guard hits, zero
unrecovered edit failures. That's the cleanest possible failure shape,
the same honest ceiling this whole project has measured since task 1:
ordinary code-logic iteration, not a harness or planning problem anymore.

**A loose thread, deliberately not chased further:** the final remaining
error was the identical text seen three investigation-turns earlier -
`Invalid access to property or key 'collected' on a base object of type
'Area2D (pickup_template.gd)'` - which that earlier turn *already proved*
was misleading (the real cause then was an unrelated `pcircle` typo, not
an actually-wrong script reference). Given how many times a plausible-
looking error text turned out to have a different real cause across this
same investigation, this was deliberately left unread rather than
re-diagnosed on pattern-match alone - worth checking properly next time
this task is revisited, not worth assuming now.

**Lesson:** this whole arc - Finding 27's corrected negative result, this
Finding's two further bugs - is really one continuous demonstration of
the same discipline applied at increasing depth: a clean failure is only
informative once its actual cause is confirmed, not inferred from what
the error message *sounds like* it's saying. Three separate wrong guesses
got corrected in this investigation alone by going back to source
evidence every time - the plan JSON, the exact trace file, the real file
list - rather than trusting a plausible story. Two of those corrections
turned into real, durable fixes; the field is meaningfully more solid for
it than if the first plausible-sounding explanation had been accepted
and shipped.

---

## Finding 30 — issue #212 finished by hand: a wrong assumed API, and a real GDScript language gotcha, both isolated by methodical debug tracing

Finding 29 left issue #212 at a clean ceiling - all 5 attempts reaching
real Godot validation, zero harness or planner failures left. Rather than
keep spending automated retries on ordinary code bugs, finished it by
hand, the same way task 1's double-jump got finished months earlier -
and the process surfaced two more real, worth-knowing bugs before the
task became this project's first genuine merged, agent-illustrated
feature.

**Bug 1: a wrong assumption, caught by checking rather than trusting
memory.** Coin code called `body.add_score(1)`, matching what an earlier
session's trace had shown existing on `player.gd`. It failed silently
(`has_method("add_score")` returned false) - checking the actual current
`player.gd` directly showed the real method is `increment_score(amount)`,
with no `get_score()` getter, just a public `score` var. The earlier
memory was from a different `run_task()` invocation's fresh checkout;
trusting it instead of re-verifying against *this* run's actual file was
the same class of mistake Finding 27 already caught once this
investigation - checked and corrected the same way.

**Bug 2, the real find: GDScript lambda closures capture local variables
by value, not by reference.** With the API fixed, a test still failed:
`assert_bool(collected_emitted).is_true()` reported false, even though
the coin/player interaction seemed obviously correct. Rather than guess
again, added a print statement at every single step of the real code path
- `_on_body_entered` firing, the name/method checks, `collect()`,
`emit_signal()`, `queue_free()`. Every one printed, in the right order,
proving the actual game logic was completely correct. That left only one
place the bug could be: the test's own signal-detection pattern -
`var collected_emitted := false; signal.connect(func(): collected_emitted
= true)`. GDScript's lambdas capture outer local variables *by value* -
the lambda sets its own private copy, never the real outer variable. Not
a project-specific bug; a genuine, well-known-if-you-already-know-it
GDScript language quirk that silently breaks the single most natural way
anyone (human or model) would write "did this signal fire." Fixed by
capturing a single-element Array instead (a reference type in GDScript),
setting/reading `flag[0]` rather than `flag` directly.

**Two smaller fixes alongside it:** `assert_bool(coin.is_inside_tree())`
threw a runtime error once the coin was genuinely, fully freed - calling
*any* method on a truly-freed object errors rather than returning a
value; the correct check is `is_instance_valid(coin)`, which handles a
freed reference safely. And the remaining two tests needed a third
`physics_frame` await (2 wasn't reliably enough, matching what fixed the
first test) - the same timing margin, applied consistently once proven.

**Result: `VALIDATE: PASS`, real PR opened, reviewed, and merged** -
[#213](https://github.com/AwenArch/zac-godot-sandbox/pull/213) - this
project's first feature combining agent-generated art, agent-attempted
logic, and a human-finished fix, actually landing on `main`. The lambda-
capture rule got written into CONVENTIONS.md immediately, since it's
exactly the kind of thing a future coder attempt would independently
rediscover and lose time to, the same way this investigation did.

**Lesson:** full debug-tracing - printing every single step of a
suspected code path, not just checking the final assertion - is a real,
distinct technique worth naming alongside this project's other debugging
habits (checking claims against source, reading exact trace content).
It's the only way this specific bug got isolated correctly: the
alternative, plausible-sounding guesses (wrong overlap timing, wrong
collision layers) would have led to real but irrelevant fixes, the same
trap this whole investigation had already fallen into and climbed back
out of several times over. When a full trace proves every step of the
suspected code fired correctly, the bug isn't there anymore - it's
somewhere else, and that's worth trusting over a plausible-sounding guess
about where it "should" be.

---

## Finding 31 — a real engine crash traced to the coder violating an explicit "don't touch this file" instruction, with two ruled-out theories along the way

Same day as the double-jump task passing clean on its first fully
automated attempt (the CONVENTIONS.md rules mined from that task's own
past failures finally proving themselves against a fresh try) - the next
task, assembling the coin and HUD into the actual main playable scene,
produced a genuinely new kind of failure worth its own record.

**All 5 attempts reached real Godot validation** - zero scope rejections,
zero test_file guard hits, zero edit-mechanics misses. That clean shape
usually means an ordinary remaining logic bug. It wasn't one - the final
state produced a real engine crash (signal 11) during test teardown, with
no assertion report, just a raw C++ backtrace.

**First theory, tested directly and cleanly ruled out:** memory pressure.
`ollama ps` showed `qwen3-coder:30b` still resident at 19GB, `PhysMem`
showed only 133M unallocated - a plausible cause, matching Finding 26's
earlier real memory-contention bug. Explicitly unloaded the model
(`llm.unload()`), confirmed 18GB free via `top`, reran - and got the
**byte-for-byte identical crash backtrace**. That's a clean, hard
negative result: memory pressure is not the cause, confirmed by testing
rather than left as an assumption once it looked plausible.

**Second question, also tested directly:** was this a cross-test
resource interaction (something from an earlier test suite in the same
process contaminating this one), or specific to this one test alone? Ran
`test_main_scene.gd` in isolation via a scoped gdUnit4 `--add` path
instead of the whole directory. Same crash, confirming it wasn't
cross-test contamination - genuinely specific to this scene.

**The isolated run's debug output (added the same way as Finding 30 -
print() at every step) revealed the real cause before the crash even
hit:** `main.tscn` contained **two separate, duplicate `[node
name="Coin" type="Area2D"]` blocks** and a bare `HUD` stub, none of them
scripted - meaning an earlier coder attempt had hand-edited `main.tscn`'s
raw text directly, in direct violation of the task's explicit
instruction not to ("Do NOT hand-edit main.tscn's raw text to add new
scene-instance nodes - build this entirely in GDScript code"). A later
attempt then *also* added the correct, instructed code-based
instantiation in `main.gd` - leaving two competing construction paths
colliding in the same file, evidently confusing the engine's node
cleanup enough to trigger the crash (a `WARNING: Detected 1 possible
orphan nodes` line appeared immediately before it).

**Fixed by rebuilding `main.tscn` clean** - floor and Player only,
matching exactly what was actually asked for, letting the code-based
approach in `main.gd` be the sole source of truth. That surfaced one more
real, small bug: `project.godot`'s `run/main_scene` had hardcoded the
scene's old UID, which broke once the rewritten `.tscn` no longer
declared one - fixed by switching that reference to a plain `res://`
path instead, sidestepping UID staleness for this setting entirely.
Final result: `VALIDATE: PASS`, 11/11 test cases across all 7 suites.

**Why this is a genuinely different finding from anything earlier today:**
Finding 27 already confirmed the model can reliably write a *new* `.tscn`
from scratch, given the redesigned minimal-template pattern. This is a
different, harder question entirely - not "can it author valid scene
syntax," but "does it reliably respect an explicit prohibition when
editing an already-existing, already-complex, hand-built scene file."
The answer here, on one sample, was no. Worth distinguishing from Finding
25's crash mechanism too - that was any *parse* error crashing test
discovery; this crash happened with a scene that parsed and imported
completely cleanly, only failing during runtime node cleanup. Two
distinct crash triggers now confirmed, not one.

**Lesson:** the order these three hypotheses got tested in - cheapest
and most likely-seeming first (memory pressure, matching a very recent
precedent), then a structural isolation test, then real debug tracing -
each one either confirmed or cleanly falsified before moving to the next,
is the same discipline that's carried this whole project. The eventual
answer (a hand-edit violating an explicit instruction) wasn't the first
guess, or even the second - it took ruling out two entirely reasonable
alternatives with real evidence to get there honestly.

---

## Open items carried forward

- [ ] Finding 31: does the coder reliably respect an explicit "don't
      hand-edit this file" instruction when working on an existing,
      already-complex scene file? One confirmed violation (main.tscn,
      issue #218) - not yet enough evidence to know if this is a real,
      recurring pattern or a one-off. Watch for recurrence on any future
      task that edits (not creates) a hand-built .tscn.
- [x] Finding 27: `.tscn` scene-writing gap - the original negative test
      was invalid (exemplar was never committed). Redesigned as a near-
      empty scene + code-built children, confirmed working: zero scene-
      syntax errors across a full 5-attempt run. The bug that remained
      in that run was an unrelated ordinary typo, not a scene-writing
      failure.
- [ ] Finding 28: the newline-collapse transcription defect (missing `\n`
      after a block-opening `:`) - confirmed recurring across two
      independent sessions/files, mechanism still not understood. Worth a
      dedicated look if it appears a third time.
- [x] Finding 29: two planner scoping bugs found chasing issue #212 -
      files_to_change entries that don't exist on disk (feedback
      describing a never-merged file misled the planner), and the
      planner omitting a needed .tscn from either file list entirely.
      Both fixed and confirmed - a full run reached real Godot validation
      on all 5 attempts with zero scope/guard/edit failures. The loose
      thread (final error repeated Finding 27's misleading error text)
      was chased down in Finding 30 - not the same cause at all: a wrong
      assumed player API plus a genuine GDScript lambda-capture-by-value
      bug in the test. Issue #212 finished by hand, merged as PR #213 -
      this project's first real, complete, merged feature.

- [x] Finding 22's original claim (test_file guard on attempts 2 & 4
      specifically) was checked against the real bench traces and found
      to be a false pattern - overgeneralized from two manual runs.
      Correcting it surfaced a real bug instead: runs/<issue>/ silently
      overwrote its own trace history on repeated manual invocations.
      Fixed by archiving stale run directories before a fresh run starts.

- [x] `/task feedback` and `/task retry` exercised end-to-end on a real
      needs-human task (#60) — confirmed working: feedback comment posted,
      re-queued, planner incorporated it, produced a different (better)
      approach that avoided the known player.gd corruption ceiling entirely.
      Uncovered Finding 17 in the process.
- [x] Diff-based editing instead of whole-file rewrites (Finding 18) — done;
      cut edit-matching failures from 8/10 to 3/10 tasks. Remaining gap is
      model capability, not the editing mechanism.
- [x] The test_file guard (Finding 17, root-caused and fixed in Finding
      24) — was a clean omission (new_files left empty), not a wrong-list
      mixup. Fixed with a mandatory cross-check in the coder prompt;
      confirmed 0/9 valid tasks hit it on the follow-up bench run, down
      from 4/20 in the diff-editing era.
- [x] "The Ollama health check in the poll loop fires every cycle even
      during an active task" — checked the real code before fixing it and
      the framing was slightly wrong: `daemon.py`'s poll loop is fully
      synchronous and blocks on `run_task()`, so the daemon's own check
      genuinely doesn't re-fire mid-task. The real noise source was
      `llm.py`'s `_pick()`, which called `.list()` fresh before every
      single `llm.call()` - 14+ redundant checks on a 7-attempt reviewer
      run, each logging its own HTTP line. Fixed with a 30s reachability
      cache; verified directly (first call 14.1s including the real
      check, second call 2.8s with it skipped). The 180s call timeout
      remains the real safety net if Ollama actually goes down mid-run.
- [ ] No launchd plist yet — the daemon has only been run interactively in
      a foreground terminal. Needed before "always-on" is real rather than
      "on while I'm at my desk with the terminal open."
- [ ] Concurrency is still hard-coded to one task at a time (by design, per
      the architecture doc's single-writer rule) — fine for now, revisit
      only if queue depth ever becomes the actual bottleneck.
- [x] Confirm whether ANY parse error during gdUnit4 test discovery crashes
      the runner (Finding 17's generalization of Finding 2) — CONFIRMED in
      Finding 25 via a 6-case sweep: every parse-error category tested
      crashed, identical signature each time. Not narrow to the original
      two cases; structural to gdUnit4 handling any unparseable script.
      Crash dialog suppression (Finding 24) is the correct, general fix -
      no single CONVENTIONS.md rule can cover "don't make any mistake."
- [x] Reviewer agent (Finding 19, revisited in Finding 23) — re-tested
      with the stronger qwen3-coder:30b reviewing itself, and again with a
      bigger attempt budget to rule out starvation. Both came back worse
      than no reviewer at all: "reached Godot" stuck at 1/10 regardless of
      budget, individual rejections were mostly correct but the loop
      doesn't converge. Disabled again; only worth revisiting with a
      genuinely different reviewer model, not more of the same one.
- [x] Ollama calls had no timeout (open since night one) — fixed, and fixed
      TWICE (Finding 21): the first attempt used ThreadPoolExecutor, which
      raised the right exception but still let the process hang for
      15+ minutes waiting on an abandoned thread via the executor's atexit
      hook. Real fix is a plain daemon thread. Verified by confirming the
      process actually exits, not just that it raises the right error.
- [x] `qwen3-coder:30b` + the tightened coder prompt (Finding 21/21c) is
      the confirmed default — 8/10 reaching real validation on TWO
      independent clean runs, competitive speed with the 14B. Set as the
      default in config/models.yaml. (The prompt tightening does NOT
      transfer to the 14B - tested directly, Finding 21b - so this is
      specifically a 30B-configuration recommendation, not a global
      prompt change.)
- [x] Two competing Ollama services running simultaneously since project
      start (Finding 20) — found and fixed; the multi-day-old stale
      process was the likely cause of several "mystery" hangs blamed on
      specific tasks or models. Standardized on `brew services` only.

---
