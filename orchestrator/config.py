"""Loads config/*.yaml once; everything reads from here."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
SLACK_CFG = yaml.safe_load((ROOT / "config" / "slack.yaml").read_text())
RUNS = ROOT / "runs"
WORK = ROOT / "work"
