# Phase 0a — Lessons Learned (first benchmark night)

One session, start to finish: built the orchestrator skeleton, ran the first
real issue end-to-end, then spent most of the night trying to get a trustworthy
benchmark number out of two models. The number mattered less than what broke
along the way — this is the record of that.

---

## The headline result

| Model | Speed | Unattended pass rate | Failure character |
|---|---|---|---|
| qwen2.5-coder:14b | fast (2–4 min/task) | 0/10 (calibrated gate) | knowledge gaps — invented APIs, ignored a repeated rule |
| qwen3.6:35b-a3b-coding | slow (12–45 min/task on this machine) | partial run, but proved capable (1 clean PASS) then 1 logic-error FAIL | reasoning gaps — correct-shaped code, wrong state-machine logic |

Neither model cleared the bar for fire-and-forget, unattended task completion
on the M4 Pro tonight. That is a real, useful answer, not a failed experiment —
Phase 0a exists specifically to produce this number before any hardware is
bought.

---

## Finding 1 — the first benchmark run (0/10) was not trustworthy, and proving that mattered more than the number

The very first full bench run came back 0/10 and looked damning. It wasn't
until reading traces issue-by-issue that the real story emerged: **the gate
itself had two independent bugs**, and roughly half the "failures" were false
positives.

- **Bug A — cross-task contamination.** `git checkout -B` resets *tracked*
  files but leaves untracked ones alone. A failed task's leftover test file
  survived into the next task's working copy and got swept into its `git add
  -A`. Fixed by adding `git reset --hard` + `git clean -fdx` to the checkout
  step — every task now starts from a byte-identical tree.
- **Bug B — grep matched Godot's own exit-time noise.** Godot prints lines
  like `ERROR: N RID allocations of type 'P11GodotBody2D' were leaked at
  exit` as routine bookkeeping whenever a test frees physics nodes — normal,
  not a real failure. The validate.sh grep for `ERROR:` matched it anyway,
  silently failing tasks whose actual test suite had passed clean. Fixed by
  adding a noise filter (`grep -vE "leaked at exit|resources still in
  use|ObjectDB instances|RIDs of type"`) to every grep stage.

**Lesson:** a benchmark is only as good as the instrument measuring it. The
first 0/10 was not evidence about the model — it was evidence the harness
hadn't been calibrated for the *passing* case, only the failing one (the
commissioning checklist in Step 1 only tested deliberately-broken code, never
deliberately-*correct*-but-noisy code). Every gate needs both directions
verified before its numbers are trusted.

---

## Finding 2 — a real Godot/gdUnit4 engine bug: bare class references crash the runner instead of failing cleanly

Two bench tasks (score autoload, pause manager) segfaulted the headless test
runner with a generic backtrace. Isolated in a 10-line repro script:

```gdscript
var r := ReproThing.new()  # ReproThing never declared with class_name or preload()
```

Godot prints the correct parse error (`Identifier "ReproThing" not declared`)
and then segfaults anyway — confirmed reproducible in isolation, independent
of any task content. This is an actual engine/gdUnit4 bug on Godot 4.7.2, not
a harness or model problem.

**Fix:** since the parser can't be patched, the workaround lives in
CONVENTIONS.md — a hard rule that bare class references are banned, and every
class must be declared with `class_name` or `preload()`'d. Worth revisiting
on a Godot point-release in case it's fixed upstream.

**Lesson:** not every crash is the model's fault or the harness's fault.
Reproducing a failure in the smallest possible isolated case is the fastest
way to find out which layer actually owns the bug — and it took five minutes
once tried, versus much longer spent guessing from bench-task noise.

---

## Finding 3 — small models will violate a rule that's already in the prompt, even after violating it once before

Task #1 (double-jump) invented the assert method `is_greater_than` — it
doesn't exist in gdUnit4 (`is_greater()` does). A CONVENTIONS.md rule was
added naming the exact correct method names. Several tasks later, a
*different* task independently invented `is_greater_than` / `is_less_than`
again, with the rule sitting directly in its prompt the whole time.

**Lesson:** a short, explicit rule document does not reliably override a
small model's training-data habits under task pressure — even for a mistake
it was already told about. This is a genuine capability ceiling, not a
prompting problem, and it's a different category from the "model doesn't
know the right API" failures that conventions rules *did* successfully fix
(Godot 3 vs 4 syntax, invented SceneTree properties). Some gaps are knowledge
gaps (fixable with one line); some are instruction-following limits (not
fixable that way).

---

## Finding 4 — the exemplar file was echoed back and corrupted repeatedly, cascading into unrelated task failures

The single most expensive recurring bug of the night. `player.gd`'s reference
docstring:

```gdscript
## This file is the reference implementation for agents. It demonstrates
## every rule in CONVENTIONS.md — imitate its style exactly.
```

Multiple times across multiple tasks — including tasks that had no reason to
touch `player.gd` at all — the model's rewrite dropped the `##` on the second
line, turning a comment into bare code. Instant parse error → the whole
script fails to load → every downstream test referencing the player
(including previously-*merged*, previously-*passing* tests) cascades into
failure with a confusing secondary error (`Invalid access to property
'speed'`) that obscures the real, one-character root cause.

Two contributing causes, addressed differently:

1. **Harness bug:** `apply()` wrote *any* file the model returned, regardless
   of whether the task's plan actually asked for it. Fixed by filtering
   `code.files` down to only `plan.files_to_change + plan.files_to_create`
   before writing — a task that doesn't need to touch `player.gd` now can't
   corrupt it, even if the model echoes it back unprompted.
2. **Genuine model limit, not fixed:** for tasks that *do* legitimately need
   to edit `player.gd` (e.g. adding a JumpSound node), whole-file rewrites
   remain a transcription-fidelity risk with these models. No conventions
   rule fixes this — it's an inherent cost of choosing rewrites over diffs
   for models too weak to produce reliable diffs.

**Lesson:** when a bug's signature repeats across unrelated tasks, look for a
harness-level cause before assuming it's model bad luck each time. One 5-line
patch fixed roughly half the board's failures instantly.

---

## Finding 5 — log-tail truncation blinded the retry loop to the actual root cause

`validate.sh`'s output was fed back to the model as its own error-repair
prompt, but only the last 40 lines. When a parse error early in the log
triggered a cascade of downstream test failures, the tail showed only the
*symptoms* (property-not-found errors in unrelated tests), and the model
spent all 3 retry attempts chasing the cascade instead of the 1-character
root cause sitting above the visible window.

**Not yet fixed** — flagged for 0b: a smarter filter that captures the
*first* `SCRIPT ERROR`/`Parse Error` block plus the tail, rather than tail
alone, so the model's retry budget is spent on the actual problem.

**Lesson:** the quality of what you feed back into a retry loop matters as
much as the loop existing at all. A model given the wrong error will retry
confidently and uselessly.

---

## Finding 6 — a thinking-mode model silently burned its whole budget and returned nothing

Switching to `qwen3.6:35b-a3b-coding` (a "thinking" model) produced a task
that used 29,015 output tokens and returned an **empty** `message.content`.
The model was spending its entire generation budget on internal chain-of-
thought reasoning — exposed by Ollama in a separate `message.thinking` field
that the harness's `llm.py` never read — and running out before emitting the
actual JSON answer.

**Fix:** added `think=False` to the `client.chat()` call, and widened the
trace writer to capture `message.thinking` if it appears anyway, so this
never goes invisible again.

**Result after the fix:** the same task that had taken ~34 minutes (thinking
mode on, but happened to finish in time) dropped to ~12 minutes with thinking
off — real evidence the fix worked — but a *later* identical task that had
previously PASSED came back FAIL on a rerun, on a genuine off-by-one logic
bug in the model's own new code (not corruption, not the harness — the
`player.gd` docstring was intact and unrelated tests passed clean). Whether
`think=False` traded away real reasoning quality for speed is an open
question worth testing deliberately (bench the same task both ways) rather
than concluding from one data point.

**Lesson:** always check where a model's tokens actually went before
concluding it "failed" or "is slow." An empty response with a huge token
count is a configuration bug wearing a model-quality costume.

---

## Finding 7 — memory pressure silently downgrades inference, and Ollama will tell you if you ask

`ollama ps` mid-run showed `31%/69% CPU/GPU` for the 22GB
`qwen3.6:35b-a3b-coding` model on the 24GB M4 Pro — meaning roughly a third
of the model's compute was falling back to CPU execution because there
wasn't enough clean memory for full GPU residency (`PhysMem: 23G used, 103M
unused`). This is not a bug or a setting — it's a direct measurement of the
model not fitting the machine, and it's the single most concrete piece of
evidence from the whole night for the hardware ladder's larger-RAM rung.

**Lesson:** `ollama ps`'s processor column is a cheap, direct way to check
whether a model actually fits before spending hours benchmarking it. Should
be a standard first check when swapping models, not something discovered
mid-run by accident.

---

## Finding 8 — sleep/interruption handling needs a real answer before Phase 0c, not just tonight's workaround

The MacBook went to sleep mid-bench-run once, and a `Ctrl+C` cut off another
run. Both left an orphaned open GitHub issue and (potentially) an unpushed
branch — the runner's cleanup code only runs on a clean return, not on
interruption. Recovery was manual each time (`gh issue close`, `git branch
-D`, `git push origin --delete`) and needs to be repeatable, not
re-derived.

For tonight, the practical choice was to run in short batches (`--only 4,5`,
then `--only 6,7`, etc.) rather than change power settings — a reasonable
call for supervised benchmark runs, but explicitly **not** a solution for
Phase 0c, where the whole point is unattended operation. That phase will need
either the no-sleep power setting or a genuinely always-on box.

**Lesson worth generalizing into the harness (not done yet):** `bench.py`
and the daemon should wrap each task in a cleanup-on-any-exit handler
(`try/finally` or a signal handler) so an interrupted run never needs manual
GitHub janitorial work.

---

## Process lessons, not code lessons

- **A "shakeout" label before the real run earns its keep.** The very first
  labeled run (`shakeout`) surfaced a gdUnit4 headless-mode flag requirement
  before it could contaminate a real comparison. Cheap insurance.
- **Read the trace before trusting the verdict.** Every single "mystery"
  tonight — the false 0/10, the segfaults, the empty response, the memory
  split — was solved by going straight to `runs/<n>/*.json` rather than
  guessing from the summary line. The summary line is for triage; the trace
  is for truth.
- **One variable per run.** Conventions changes and model changes were never
  mixed in the same labeled run tonight — that discipline is what made it
  possible to say *which* fix caused *which* improvement.
- **A calibrated gate is worth more than a clever one.** Nearly every hour
  spent tonight was gate-calibration, not model evaluation. That ratio will
  invert for every future bench run — this was the one-time cost of
  commissioning the equipment properly.
- **"The number is bad" and "the number is wrong" are different findings,
  and conflating them wastes the most time.** Every suspicious result
  tonight got the same treatment: reproduce, isolate, read the raw trace,
  *then* decide if it's real. Skipping that step on the very first 0/10
  would have produced a false conclusion that a fine model was worthless.

---

## Open items carried into 0b / next session

- [ ] First-error-block log filtering (Finding 5) — likely the single
      highest-leverage remaining harness fix.
- [ ] Structural guard against transcription corruption: reject a rewrite of
      an existing file whose comment lines changed or line count shrank
      unexpectedly, bounce it back as feedback instead of applying it.
- [ ] `try/finally` cleanup in `bench.py` for interrupted runs.
- [ ] Deliberate A/B of `think=True` vs `think=False` on the same task set,
      to settle whether thinking mode was trading quality for speed.
- [ ] A finished 10/10 run for `qwen3.6:35b-a3b-coding` once a machine with
      enough RAM for full GPU residency is available — tonight's partial
      run is suggestive, not conclusive, on raw capability.
- [ ] Re-run the frozen 14B bench once more, unchanged, as the final
      recorded baseline number for the eventual writeup.

---

## The one-paragraph version, for the eventual blog post

Tonight's real result isn't a pass-rate table — it's that an unattended local
coding pipeline surfaces failures in four different layers (model knowledge,
model reasoning, test-harness bugs, and the underlying game engine itself),
and distinguishing which layer owns a given failure is most of the actual
work. Two models were benchmarked; along the way, one real Godot engine bug
was found and reproduced in isolation, two harness bugs were fixed, and a
concrete, measured case for more RAM (a 31/69 CPU/GPU split, not a guess) was
built from a single `ollama ps` command. That's what "commissioning the
equipment" turned out to mean in practice.
