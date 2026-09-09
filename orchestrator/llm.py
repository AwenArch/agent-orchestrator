"""The only file that talks to Ollama. Every call is traced to runs/<task>/."""
import hashlib
import json
import threading
import time

import ollama
from pydantic import BaseModel, ValidationError

from orchestrator.config import CFG, RUNS

CALL_TIMEOUT = 180  # seconds. Uses a daemon thread, NOT ThreadPoolExecutor - the executor's atexit hook blocks process exit until every submitted thread finishes, which completely defeated the first version of this timeout (it raised on schedule, then the process hung anyway waiting for the abandoned thread). A daemon thread lets the process actually die the instant we give up.


def call(role: str, task_id: str, step: str, system: str, user: str,
         schema: type[BaseModel]):
    ep = _pick(role)
    client = ollama.Client(host=ep["url"])
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    for attempt in range(2):  # one honest try + one repair try
        t0 = time.time()
        box = {}
        def _target():
            try:
                box["r"] = client.chat(model=ep["model"], messages=msgs,
                                       format=schema.model_json_schema(),
                                       think=False, options=CFG["options"])
            except Exception as e:
                box["e"] = e
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=CALL_TIMEOUT)
        if thread.is_alive():
            raise RuntimeError(
                f"{role}/{step}: Ollama call exceeded {CALL_TIMEOUT}s - likely "
                "a stuck generation. Restart Ollama (brew services restart "
                "ollama) before retrying.")
        if "e" in box:
            raise box["e"]
        r = box["r"]
        text = r["message"]["content"]
        _trace(task_id, step, attempt, ep, msgs, text, time.time() - t0, r)
        try:
            return schema.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as e:
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user",
                      "content": f"Invalid output: {e}. "
                                 "Return ONLY valid JSON matching the schema."}]
    raise RuntimeError(f"{role}/{step}: unparseable after repair attempt")


# Cache of {endpoint_url: last_verified_timestamp}. _pick() was calling
# .list() fresh before EVERY single llm.call() (planner once, coder once
# per attempt, reviewer once per attempt when enabled) - for a 7-attempt
# run that's 14+ redundant reachability checks, each logging its own
# "INFO HTTP Request: GET .../api/tags" line, which is the real source of
# the log noise this open item originally (and slightly incorrectly)
# attributed to the daemon's poll loop. A short TTL skips re-verifying an
# endpoint that answered recently; the 180s CALL_TIMEOUT in call() remains
# the real safety net if Ollama actually goes down mid-run - a cached
# "was reachable" doesn't mean the next chat() call can't still time out
# and raise cleanly, it just stops re-proving the obvious every time.
_endpoint_cache: dict[str, float] = {}
CACHE_TTL = 30  # seconds


def _pick(role: str) -> dict:
    for name in CFG["routing"][role]:
        ep = CFG["endpoints"][name]
        last_ok = _endpoint_cache.get(ep["url"])
        if last_ok is not None and time.time() - last_ok < CACHE_TTL:
            return ep
        try:
            ollama.Client(host=ep["url"]).list()
            _endpoint_cache[ep["url"]] = time.time()
            return ep
        except Exception:
            continue
    raise RuntimeError(f"no endpoint reachable for role '{role}'")


def unload(role: str) -> None:
    """Explicitly frees a model's memory before a competing process (e.g.
    ComfyUI) needs it. Ollama normally keeps a model resident between calls
    (OLLAMA_KEEP_ALIVE) specifically to avoid reload cost - the right
    default most of the time, but it means the model stays loaded even
    during a gap where nothing needs it, competing for memory with
    anything else running concurrently. Confirmed necessary directly: a
    live task run (issue #210) showed Ollama still holding 19GB/24GB right
    when ComfyUI needed its own ~11-15GB, causing a real 180s timeout.

    Best-effort: if the unload call itself fails, this doesn't raise - the
    caller's own timeout is still the real safety net, and a failed unload
    just means the next step fights for memory the way it did before this
    fix existed, not a new failure mode."""
    ep = _pick(role)
    client = ollama.Client(host=ep["url"])
    try:
        client.chat(model=ep["model"], messages=[], keep_alive=0)
    except Exception:
        pass


def _trace(task_id, step, attempt, ep, msgs, text, secs, raw) -> None:
    d = RUNS / str(task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{step}-{attempt}.json").write_text(json.dumps({
        "model": ep["model"], "step": step, "attempt": attempt,
        "secs": round(secs, 1),
        "prompt_tokens": raw.get("prompt_eval_count"),
        "out_tokens": raw.get("eval_count"),
        "prompt_hash": hashlib.sha1(msgs[0]["content"].encode()).hexdigest()[:8],
        "messages": msgs, "response": text,
        "thinking": raw["message"].get("thinking", "")}, indent=1))
