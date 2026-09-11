"""Standalone sanity check for the ComfyUI client - no LLM, no orchestrator,
just the raw text-prompt-to-transparent-PNG pipeline. Run this before
trusting it to anything else. Requires ComfyUI actually running on
localhost:8188 (python3 main.py in the ComfyUI directory)."""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from orchestrator.tools.comfyui import generate_sprite

out = Path("/tmp/artist_test_coin.png")
print("Submitting to ComfyUI - first call may take a while (model load)...")
result = generate_sprite(
    "pixel art coin icon, 16-bit retro game sprite, simple flat colors, "
    "black outline, centered on white background",
    out,
    seed=12345,  # fixed seed - re-running this script should reproduce
                 # the same image, a useful sanity check on its own
)
print(f"Saved to: {result}")

# Basic sanity checks - not a substitute for actually looking at the image,
# but catches "the file is empty/corrupt" before you go look.
from PIL import Image
img = Image.open(result)
print(f"Size: {img.size}, mode: {img.mode}")
assert img.mode == "RGBA", f"expected RGBA (transparent) output, got {img.mode}"
assert img.size[0] > 0 and img.size[1] > 0, "image has zero dimensions"

# Check it's not just a blank/solid-color image (a common silent-failure
# shape: job "succeeds" but produces a flat gray or fully-transparent square)
extrema = img.getextrema()
print(f"Per-channel min/max: {extrema}")
all_flat = all(lo == hi for lo, hi, *_ in [extrema[i] for i in range(3)])
if all_flat:
    print("WARNING: image appears to be a single flat color - check it "
         "manually, something may have gone wrong upstream.")
else:
    print("PASS: image has real variation, not a blank/flat result.")

print(f"\nOpen {result} to actually look at it - the checks above only "
     "catch gross failures, not whether it looks good.")
