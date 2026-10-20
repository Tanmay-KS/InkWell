#!/usr/bin/env python3
"""Pin the model IDs the project uses, from the live catalog.

Phase 0 — Person B.

The catalog publishes the same model under several casings, and Nebius's
own documentation, its web catalog, and its API catalog disagree with
each other about which models exist at all. So the rule is: read the ID
from the API catalog, write it into `.env`, and keep a dated snapshot on
the record.

This script does three things:

1. Fetches the verbose catalog.
2. Picks one model per role, using the *decision* recorded in the plan
   (not a guess), and writes the IDs into `.env`.
3. Writes `docs/model-catalog.md` — the snapshot the decision is auditable
   against.

Usage:

    python eval/pin_models.py                # pin into .env + snapshot
    python eval/pin_models.py --snapshot-only # snapshot only, touch no .env
    python eval/pin_models.py --dry-run      # print what it would write
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.tokenfactory import (
    accepts_images,
    fetch_models,
    find_model,
    load_api_key,
    model_id,
    pricing_per_token,
    write_snapshot,
)

# The roles the project pins, and how each is chosen.
#
# These are *decisions*, not searches. The Phase 0 decision is that
# Nemotron-3.5-Super-VL is the document-reading and main-reasoning model
# — it is the cheapest vision model in the catalog from a major lab
# ($0.30/1M in, $0.90/1M out) and Nebius names it for document
# understanding. Everything else is a comparison or a cost tier.
ROLE_MODEL_IDS = {
    "nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "super": "nvidia/Nemotron-3.5-Super-VL",
    "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b",
    "ocr": "nvidia/Nemotron-3.5-Super-VL",
    "redact": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
}

ENV_KEYS = {
    "nano": "LEDGER_MODEL_NANO",
    "super": "LEDGER_MODEL_SUPER",
    "ultra": "LEDGER_MODEL_ULTRA",
    "ocr": "LEDGER_MODEL_OCR",
    "redact": "LEDGER_MODEL_REDACT",
}

# The decision record needs to survive edits to the constants above, so
# the script says *why* each role holds the model it holds.
ROLE_RATIONALE = {
    "nano": "cheapest text tier — redaction, classification, side-jobs",
    "super": "main reasoning and document reading",
    "ultra": "hardest reasoning, long-horizon agent work",
    "ocr": "same model as the Super tier: it is multimodal and the cheapest vision model available",
    "redact": "same model as the Nano tier: redaction is a short-span classification job",
}


def update_env(env_path: Path, values: dict[str, str]) -> list[str]:
    """Set `KEY=value` lines in `.env`, preserving everything else.

    Returns a list of human-readable notes about what changed.
    """
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["# Ledger — environment template. Copy from .env.example."]

    notes: list[str] = []
    remaining = dict(values)
    output: list[str] = []

    for line in lines:
        match = re.match(r"^([A-Z0-9_]+)=", line)
        key = match.group(1) if match else None
        if key in remaining:
            new_value = remaining.pop(key)
            old_value = line.split("=", 1)[1].strip().strip("'\"")
            if old_value != new_value:
                notes.append(f"{key}: {old_value or '(empty)'} -> {new_value}")
            output.append(f"{key}={new_value}")
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={value}")
        notes.append(f"{key}: (added) -> {value}")

    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot-only", action="store_true", help="do not touch .env")
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--env", default=".env", help="the .env file to write")
    parser.add_argument("--snapshot", default="docs/model-catalog.md", help="snapshot path")
    args = parser.parse_args(argv)

    api_key = load_api_key()
    if not api_key:
        print("NEBIUS_API_KEY is not set (env or .env)", file=sys.stderr)
        return 1

    try:
        models = fetch_models(api_key)
    except RuntimeError as exc:
        print(f"catalog fetch failed: {exc}", file=sys.stderr)
        return 1

    print(f"catalog: {len(models)} models\n")

    # Resolve every role against the catalog. A role whose model is not
    # served is a hard error — a pinned ID that 404s at call time is
    # exactly the failure this script exists to prevent.
    resolved: dict[str, str] = {}
    for role, wanted in ROLE_MODEL_IDS.items():
        found = find_model(models, wanted)
        if not found:
            print(
                f"ERROR: role '{role}' wants {wanted!r}, which the catalog does not serve.",
                file=sys.stderr,
            )
            print(
                "       Available vision models:\n"
                + "\n".join(
                    f"         {model_id(m)}"
                    for m in models
                    if accepts_images(m)
                ),
                file=sys.stderr,
            )
            return 2
        resolved[role] = model_id(found)

    print("pinned roles:")
    for role, model_id_text in resolved.items():
        model = find_model(models, model_id_text)
        prompt, completion = pricing_per_token(model)
        print(
            f"  {role:8s} {model_id_text:45s} ${prompt * 1e6:.2f}/1M in  "
            f"${completion * 1e6:.2f}/1M out   ({ROLE_RATIONALE[role]})"
        )

    # The OCR role must actually take an image. This is checked rather
    # than assumed, because the catalog and Nebius's documentation have
    # already disagreed once about exactly this.
    ocr_model = find_model(models, resolved["ocr"])
    if not accepts_images(ocr_model):
        print(
            f"ERROR: the OCR role ({resolved['ocr']}) is not a vision model in the catalog.",
            file=sys.stderr,
        )
        return 2

    if not args.snapshot_only:
        env_path = Path(args.env)
        values = {ENV_KEYS[role]: model_id_text for role, model_id_text in resolved.items()}
        if args.dry_run:
            print(f"\nwould write to {env_path}:")
            for key, value in values.items():
                print(f"  {key}={value}")
        else:
            notes = update_env(env_path, values)
            print(f"\nwrote {env_path}:")
            for note in notes:
                print(f"  {note}")
            if not notes:
                print("  (already up to date)")

    snapshot_path = Path(args.snapshot)
    if args.dry_run:
        print(f"\nwould write snapshot to {snapshot_path}")
    else:
        written = write_snapshot(models, snapshot_path, api_key)
        print(f"\nwrote {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
