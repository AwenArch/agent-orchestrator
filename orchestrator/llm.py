"""The only file that talks to Ollama. Every call is traced to runs/<task>/."""
import hashlib
import json
import time

import ollama
from pydantic import BaseModel, ValidationError

from orchestrator.config import CFG, RUNS


def call(role: str, task_id: str, step: str, system: str, user: str,
         schema: type[BaseModel]):
    ep = _pick(role)
    client = ollama.Client(host=ep["url"])
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    for attempt in range(2):  # one honest try + one repair try
        t0 = time.time()
        r = client.chat(model=ep["model"], messages=msgs,
                        format=schema.model_json_schema(),
                        think=False,
                        options=CFG["options"])
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


def _pick(role: str) -> dict:
    for name in CFG["routing"][role]:
        ep = CFG["endpoints"][name]
        try:
            ollama.Client(host=ep["url"]).list()
            return ep
        except Exception:
            continue
    raise RuntimeError(f"no endpoint reachable for role '{role}'")


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
