"""Thin client for the Nebius Token Factory model catalog.

Phase 0 — Person B.

Two things need the catalog, and neither of them should hand-type a model
ID:

* `pin_models.py`, which writes the IDs the project uses into `.env`.
* `ocr_bench.py`, which needs to know which models accept images, what
  they cost, and how they are spelled.

The catalog exposes **the same model under several casings** — for example
`nvidia/Nemotron-3.5-Super-VL` and `nvidia/nemotron-3.5-super-vl-120b-a12b`
— and Nebius's own documentation disagrees about which spelling to use.
So the rule for the whole project is: read the ID from the catalog, never
type it.

Usage:

    python eval/tokenfactory.py --list
    python eval/tokenfactory.py --vision
    python eval/tokenfactory.py --show nvidia/Nemotron-3.5-Super-VL
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

# Roles the project pins. The values are *matchers*, not model IDs: the
# catalog decides what actually exists, this module only says which
# catalog entry fills which slot.
ROLE_PATTERNS = {
    "nano": ("nano",),
    "super": ("super",),
    "ultra": ("ultra",),
    "ocr": ("super-vl", "vl", "omni", "vision"),
    "redact": ("nano",),
}


def load_api_key(explicit: str = "") -> str:
    """Return the API key from the argument, the environment, or `.env`.

    The Makefile exports nothing, so `.env` is the usual source. The file
    is read by hand rather than through `python-dotenv` so that these
    scripts have no import-time side effects.
    """
    if explicit:
        return explicit.strip()
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if key:
        return key

    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("NEBIUS_API_KEY="):
                return stripped.split("=", 1)[1].strip().strip("'\"")
    return ""


def base_url() -> str:
    """The API base URL.

    `NEBIUS_BASE_URL` is honoured so the same code runs inside the
    sandbox, but it must not be set there: OpenShell binds the credential
    to the Token Factory host only, and pointing Hermes at another host
    is the cleanest proof that routing is enforced by the runtime.
    """
    return os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def fetch_models(api_key: str, timeout: float = 30.0) -> list[dict]:
    """Fetch the verbose model catalog.

    Returns the list of model objects. Raises `RuntimeError` on an
    transport or API failure, with the response body included — a bare
    "401" is not enough to diagnose a mis-scoped key.
    """
    import httpx

    url = f"{base_url()}/models"
    with httpx.Client(timeout=timeout) as client:
        response = client.get(
            url,
            params={"verbose": "true"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} from {url}: {response.text[:400]}")

    payload = response.json()
    models = payload.get("data", [])
    if not models:
        raise RuntimeError(f"the catalog at {url} returned no models")
    return models


def model_id(model: dict) -> str:
    return str(model.get("id", ""))


def modality(model: dict) -> str:
    """The model's declared modality, e.g. `text->text`."""
    arch = model.get("architecture") or {}
    return str(arch.get("modality", "") or model.get("modality", ""))


def accepts_images(model: dict) -> bool:
    """Whether the catalog says this model takes an image input.

    The catalog's own web UI and its API disagree about Nemotron's
    omni-modal models — the UI labels them "Text-to-text" while the
    cookbook calls them multimodal — so this is deliberately permissive:
    anything that mentions an image modality counts, and the benchmark
    then *proves* it by sending a real image and recording what happens.
    """
    haystack = " ".join(
        [
            modality(model),
            str(model.get("input_modalities", "")),
            str(model.get("modalities", "")),
            str(model.get("capabilities", "")),
        ]
    ).lower()
    return "image" in haystack or "vision" in haystack


def pricing_per_token(model: dict) -> tuple[float, float]:
    """(prompt price, completion price) per token.

    The catalog reports these as per-token strings, e.g. `"0.0000003"`
    for $0.30 per million tokens.
    """
    pricing = model.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt", 0) or 0)
    except (TypeError, ValueError):
        prompt = 0.0
    try:
        completion = float(pricing.get("completion", 0) or 0)
    except (TypeError, ValueError):
        completion = 0.0
    return prompt, completion


def find_model(models: list[dict], needle: str) -> dict | None:
    """Find a model by exact ID, then by case-insensitive suffix match."""
    for model in models:
        if model_id(model) == needle:
            return model
    lowered = needle.lower()
    for model in models:
        if model_id(model).lower() == lowered:
            return model
    for model in models:
        if model_id(model).lower().endswith("/" + lowered):
            return model
    return None


def find_by_pattern(models: list[dict], patterns: tuple[str, ...]) -> list[dict]:
    """Every model whose ID contains one of the patterns, case-insensitively.

    Order follows the catalog, which is stable, so the result is
    deterministic.
    """
    matches = []
    for model in models:
        lowered = model_id(model).lower()
        if any(pattern in lowered for pattern in patterns):
            matches.append(model)
    return matches


def write_snapshot(models: list[dict], path: str | Path, api_key: str) -> Path:
    """Write a dated, human-readable snapshot of the catalog.

    The snapshot is committed. The catalog moves — models are added,
    renamed, and re-spelled — and a decision made today is only auditable
    if the catalog it was made against is on the record.
    """
    from datetime import UTC, datetime

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for model in sorted(models, key=model_id):
        prompt, completion = pricing_per_token(model)
        rows.append(
            {
                "id": model_id(model),
                "modality": modality(model) or "—",
                "vision": "yes" if accepts_images(model) else "no",
                "context": model.get("context_length") or "—",
                "prompt_per_1m": round(prompt * 1_000_000, 6),
                "completion_per_1m": round(completion * 1_000_000, 6),
            }
        )

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Nebius Token Factory — model catalog snapshot",
        "",
        f"**Taken:** {stamp} · **Endpoint:** `{base_url()}` · **Models:** {len(rows)}",
        "",
        "Generated by `make model-catalog`. Do not edit by hand — re-run the target.",
        "",
        "Model IDs must come from this file, never from memory or from Nebius's",
        "documentation: the catalog publishes the same model under several",
        "casings, and the spellings disagree across Nebius's own sources.",
        "",
        "| Model ID | Modality | Vision | Context | $/1M in | $/1M out |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['id']}` | {row['modality']} | {row['vision']} | "
            f"{row['context']:,} | {row['prompt_per_1m']} | {row['completion_per_1m']} |"
            if isinstance(row["context"], int)
            else f"| `{row['id']}` | {row['modality']} | {row['vision']} | "
            f"{row['context']} | {row['prompt_per_1m']} | {row['completion_per_1m']} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Pinned roles",
        "",
        "| Role | Model ID |",
        "|---|---|",
    ]
    for role, model in read_pinned_roles().items():
        lines.append(f"| `{role}` | `{model}` |")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def read_pinned_roles(env_path: str | Path = ".env") -> dict[str, str]:
    """Read the `LEDGER_MODEL_*` roles out of `.env`."""
    roles: dict[str, str] = {}
    path = Path(env_path)
    if not path.exists():
        return roles
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("LEDGER_MODEL_") and "=" in stripped:
            key, value = stripped.split("=", 1)
            roles[key.removeprefix("LEDGER_MODEL_").lower()] = value.strip().strip("'\"")
    return roles


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print every model ID")
    parser.add_argument("--vision", action="store_true", help="print only vision models")
    parser.add_argument("--show", default="", help="print the full record for one model")
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

    if args.show:
        found = find_model(models, args.show)
        if not found:
            print(f"no model matching {args.show!r}", file=sys.stderr)
            return 1
        print(json.dumps(found, indent=2, sort_keys=True))
        return 0

    if args.vision:
        for model in models:
            if accepts_images(model):
                prompt, completion = pricing_per_token(model)
                print(
                    f"{model_id(model):55s} {modality(model) or '—':18s} "
                    f"${prompt * 1e6:.2f}/1M in  ${completion * 1e6:.2f}/1M out"
                )
        return 0

    for model in models:
        prompt, completion = pricing_per_token(model)
        print(
            f"{model_id(model):55s} {modality(model) or '—':18s} "
            f"${prompt * 1e6:.2f}/1M in  ${completion * 1e6:.2f}/1M out"
            f"{'  [vision]' if accepts_images(model) else ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
