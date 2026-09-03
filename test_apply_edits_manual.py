"""Standalone sanity check for apply_edits() - no LLM, no Godot, just the
function. Run this after installing, before trusting it to a real task."""
import sys
sys.path.insert(0, ".")
from pathlib import Path
import tempfile
from orchestrator.tools.repo import apply_edits
from orchestrator.schemas import EditBlock

original = """extends CharacterBody2D
## Player movement: run left/right, jump on ui_accept.
@export var speed: float = 200.0
@export var jump_velocity: float = -400.0

func _physics_process(delta: float) -> void:
\tif not is_on_floor():
\t\tvelocity.y += 900.0 * delta
\tmove_and_slide()
"""

with tempfile.TemporaryDirectory() as td:
    workdir = Path(td)
    (workdir / "scenes" / "player").mkdir(parents=True)
    fp = workdir / "scenes" / "player" / "player.gd"
    fp.write_text(original)

    # Case 1: a normal, unique, valid edit - should apply cleanly
    e1 = EditBlock(path="scenes/player/player.gd",
                   search="@export var jump_velocity: float = -400.0",
                   replace="@export var jump_velocity: float = -400.0\n@export var double_jump_velocity: float = -300.0")
    changed, errors = apply_edits(workdir, [e1])
    assert changed == ["scenes/player/player.gd"], changed
    assert errors == [], errors
    new_text = fp.read_text()
    assert "double_jump_velocity" in new_text
    assert "## Player movement" in new_text  # untouched content survived
    print("PASS: valid unique edit applied, unrelated content untouched")

    # Case 2: search text not found at all
    fp.write_text(original)  # reset
    e2 = EditBlock(path="scenes/player/player.gd",
                   search="this text does not exist in the file",
                   replace="anything")
    changed, errors = apply_edits(workdir, [e2])
    assert changed == [], changed
    assert len(errors) == 1 and "not found" in errors[0]
    assert fp.read_text() == original  # file untouched on failure
    print("PASS: non-matching search rejected, file left untouched")

    # Case 3: search text matches multiple times (ambiguous)
    fp.write_text(original)
    e3 = EditBlock(path="scenes/player/player.gd",
                   search="\t",  # a bare tab - matches many lines
                   replace="\t\t")
    changed, errors = apply_edits(workdir, [e3])
    assert changed == [], changed
    assert len(errors) == 1 and "matches" in errors[0] and "times" in errors[0]
    assert fp.read_text() == original
    print("PASS: ambiguous (multi-match) search rejected, file left untouched")

    print("\nAll apply_edits() checks passed.")
