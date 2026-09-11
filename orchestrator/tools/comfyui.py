"""Client for the local ComfyUI server: text prompt -> transparent PNG.

Talks to ComfyUI's HTTP API the same way llm.py talks to Ollama - a plain
client module, no framework. The base workflow (config/comfyui_workflow_api.json)
was exported directly from the real, hand-verified ComfyUI graph (Z-Image-
Turbo diffusion model + Qwen3-4B text encoder + the elusarca pixel-art LoRA,
confirmed via a real before/after comparison to actually improve edge
quality before this client was written).

Node IDs below (NODE_* constants) are specific to that exported workflow -
if the workflow is ever rebuilt/re-exported in ComfyUI, these will need
updating to match the new node IDs.
"""
import copy
import json
import random
import time
import uuid
from pathlib import Path

import requests
from PIL import Image

COMFYUI_URL = "http://127.0.0.1:8188"
WORKFLOW_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "comfyui_workflow_api.json"

# Node IDs in the exported workflow - see WORKFLOW_PATH for the full graph.
NODE_PROMPT = "57:27"          # CLIPTextEncode.inputs.text
NODE_LATENT_SIZE = "57:13"     # EmptySD3LatentImage.inputs.{width,height}
NODE_SAMPLER = "57:3"          # KSampler.inputs.seed
NODE_LORA = "57:63"            # LoraLoaderModelOnly.inputs.strength_model
NODE_SAVE = "57:64"            # SaveImage - where the output image comes from

POLL_INTERVAL = 2      # seconds between /history checks
POLL_TIMEOUT = 180     # seconds - matches llm.py's CALL_TIMEOUT convention;
                       # a stuck ComfyUI job fails cleanly instead of hanging
                       # the whole pipeline indefinitely (same lesson as
                       # Finding 21's Ollama timeout, applied here up front
                       # instead of learned the hard way a second time)


def _load_workflow() -> dict:
    return json.loads(WORKFLOW_PATH.read_text())


def _submit(workflow: dict) -> str:
    client_id = str(uuid.uuid4())
    r = requests.post(f"{COMFYUI_URL}/prompt",
                      json={"prompt": workflow, "client_id": client_id},
                      timeout=30)
    r.raise_for_status()
    return r.json()["prompt_id"]


def _wait_for_result(prompt_id: str) -> dict:
    """Polls /history until the job completes. Raises on timeout or if
    ComfyUI reports an execution error, rather than hanging silently."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        r = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=30)
        r.raise_for_status()
        history = r.json()
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI job {prompt_id} failed: "
                                   f"{status.get('messages')}")
            if entry.get("outputs"):
                return entry
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"ComfyUI job {prompt_id} exceeded {POLL_TIMEOUT}s - "
                       "likely stuck. Check the ComfyUI server directly.")


def _download_image(image_info: dict) -> bytes:
    r = requests.get(f"{COMFYUI_URL}/view", params={
        "filename": image_info["filename"],
        "subfolder": image_info.get("subfolder", ""),
        "type": image_info.get("type", "output"),
    }, timeout=30)
    r.raise_for_status()
    return r.content


def generate_sprite(prompt: str, out_path: Path, *, width: int = 1024,
                    height: int = 1024, seed: int | None = None,
                    lora_strength: float = 1.0,
                    remove_background: bool = True) -> Path:
    """Generates one image from `prompt` and saves it to `out_path`.

    Uses the real, hand-verified workflow (Z-Image-Turbo + the pixel-art
    LoRA). Generates large (1024x1024 by default, matching what actually
    got tested) rather than at final sprite size - downscaling is a
    separate, later step, same as any real pixel-art pipeline; generating
    small directly tends to produce mush, not clean pixel edges.

    Background removal (rembg) is on by default since every raw generation
    so far has come out on a flat white background, and real sprites need
    alpha transparency to composite into a scene.

    Raises on any real failure (ComfyUI unreachable, job errors, timeout) -
    never returns a silently-broken result.
    """
    workflow = copy.deepcopy(_load_workflow())
    workflow[NODE_PROMPT]["inputs"]["text"] = prompt
    workflow[NODE_LATENT_SIZE]["inputs"]["width"] = width
    workflow[NODE_LATENT_SIZE]["inputs"]["height"] = height
    workflow[NODE_SAMPLER]["inputs"]["seed"] = (
        seed if seed is not None else random.randint(0, 2**32 - 1))
    workflow[NODE_LORA]["inputs"]["strength_model"] = lora_strength

    prompt_id = _submit(workflow)
    entry = _wait_for_result(prompt_id)

    images = entry["outputs"].get(NODE_SAVE, {}).get("images", [])
    if not images:
        raise RuntimeError(f"ComfyUI job {prompt_id} produced no images "
                           f"at node {NODE_SAVE} - workflow may have changed.")

    raw_bytes = _download_image(images[0])
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if remove_background:
        from rembg import remove
        import io
        img = Image.open(io.BytesIO(raw_bytes))
        result = remove(img)
        result.save(out_path)
    else:
        out_path.write_bytes(raw_bytes)

    return out_path
