#!/usr/bin/env python3
"""Generate the synthetic document corpus.

Phase 0 — Person B.

The corpus exists to make the Phase 2 leak test meaningful. It has to
contain the things that leak: names, account numbers, addresses, email
addresses, phone numbers, and renewal dates — in documents that look
enough like the real thing that the pipeline has to work to extract them.

Every value is fake. The generator uses a fixed seed by default, so the
corpus is reproducible: the same seed gives byte-identical documents, and
the leak test can be re-run and compared against a stored baseline.

Outputs, per document, into the output directory:

    <id>.txt      the document text
    <id>.json     ground truth — what is actually in the document
    <id>.png      a rendered "scan" of the document (with --render)

The ground truth is what makes the benchmark in `ocr_bench.py` and the
extraction accuracy check in the Phase 3 evaluation possible.

Usage:

    python eval/gen_synthetic_docs.py --out eval/synthetic_docs --count 200
    python eval/gen_synthetic_docs.py --out eval/synthetic_docs --count 200 --render
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

# --- page geometry ----------------------------------------------------------
#
# The rendered "scan" has to contain the whole document, or the benchmark
# measures the renderer instead of the models. These constants define the
# page; `wrap_lines` fits the text to it and `render_image` grows the
# page rather than dropping what does not fit.
PAGE_WIDTH = 1000
PAGE_HEIGHT = 1400
MARGIN = 70
LINE_HEIGHT = 24
FONT_SIZE = 15
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# --- fake people and places -------------------------------------------------
#
# Deliberately generic and obviously fictional. "Sam Rivera" and
# "4128 Fenwick Street" could be real; that is the point — the router has
# to treat them as personal data regardless.

HOLDERS = [
    "Sam Rivera",
    "Priya Raman",
    "Alex Chen",
    "Jordan Okafor",
    "Marta Kowalski",
    "Devin Sullivan",
    "Robin Ashworth",
    "Casey Lindqvist",
]

STREETS = [
    "Fenwick Street",
    "Alderwood Lane",
    "Harrow Road",
    "Pellham Avenue",
    "Corvin Court",
    "Marbury Terrace",
    "Kingsway Drive",
    "Thistlewood Place",
    "Braddock Boulevard",
    "Renshaw Circle",
]

CITIES = [
    ("Northgate", "OR", "97418"),
    ("Alderbrook", "MN", "55407"),
    ("Fairhaven", "MA", "02143"),
    ("Cedar Crossing", "TX", "75204"),
    ("Marrow", "CO", "80211"),
    ("Hollis Bay", "NY", "12189"),
]

PROVIDERS = {
    "insurance": [
        "Meridian Mutual Insurance",
        "Cascade Life & Casualty",
        "Ardent Home Insurance",
        "Beacon Auto Assurance",
    ],
    "membership": [
        "Northwind Fitness",
        "Ironwood Athletic Club",
        "Summit Wellness Collective",
        "Harbour Point Tennis Club",
    ],
    "subscription": [
        "Streamline Media",
        "Corvid Cloud Storage",
        "Pinnacle Learning",
        "Kettle & Crumb Coffee Club",
    ],
    "licence": [
        "State Motor Vehicle Registry",
        "City Business Licensing Office",
        "State Board of Professional Licensing",
        "Regional Aviation Authority",
    ],
    "medical": [
        "Lakeshore Family Health Plan",
        "Meridian Dental Partners",
        "Cardinal Vision Care",
        "Northwind Physical Therapy",
    ],
    "utility": [
        "Commonwealth Power & Light",
        "Valley Water Utility",
        "Fiberline Communications",
        "Metropolitan Waste Services",
    ],
}

DOC_KINDS = list(PROVIDERS.keys())

# --- formatting helpers -----------------------------------------------------


def _account_number(rng: random.Random, kind: str) -> str:
    """A plausible account identifier for a document kind.

    The format varies by kind because real ones do, and because a router
    that only recognises one format is not a router.
    """
    style = rng.choice(["plain", "dashed", "grouped", "alnum"])
    if style == "plain":
        return "".join(rng.choice(string.digits) for _ in range(rng.choice([8, 9, 10])))
    if style == "dashed":
        return "-".join(
            "".join(rng.choice(string.digits) for _ in range(4)) for _ in range(2)
        )
    if style == "grouped":
        return " ".join(
            "".join(rng.choice(string.digits) for _ in range(4)) for _ in range(2)
        )
    prefix = {"insurance": "POL", "membership": "MBR", "subscription": "SUB"}.get(
        kind, "ACC"
    )
    return f"{prefix}-{''.join(rng.choice(string.digits) for _ in range(6))}"


def _phone(rng: random.Random) -> str:
    style = rng.choice(["plain", "dashed", "parens", "e164"])
    area = "".join(rng.choice(string.digits) for _ in range(3))
    mid = "".join(rng.choice(string.digits) for _ in range(3))
    last = "".join(rng.choice(string.digits) for _ in range(4))
    if style == "plain":
        return f"{area}{mid}{last}"
    if style == "dashed":
        return f"{area}-{mid}-{last}"
    if style == "parens":
        return f"({area}) {mid}-{last}"
    return f"+1 {area} {mid} {last}"


def _email(rng: random.Random, holder: str) -> str:
    first, last = holder.split(" ")[0].lower(), holder.split(" ")[-1].lower()
    style = rng.choice(["dot", "initial", "plus"])
    if style == "dot":
        return f"{first}.{last}@example.com"
    if style == "initial":
        return f"{first[0]}{last}@example.com"
    return f"{first}+{last}@example.com"


def _address(rng: random.Random) -> tuple[str, str, str, str]:
    number = rng.randint(10, 9899)
    street = rng.choice(STREETS)
    city, state, postcode = rng.choice(CITIES)
    return f"{number} {street}", city, state, postcode


def _money(rng: random.Random) -> tuple[float, str]:
    amount = round(rng.uniform(12, 480), 2)
    if rng.random() < 0.5:
        return amount, f"${amount:,.2f}"
    return amount, f"${amount:.2f} per month"


# --- documents --------------------------------------------------------------


@dataclass(slots=True)
class DocSpec:
    """The ground truth for one synthetic document."""

    doc_id: str
    kind: str
    provider: str
    holder: str
    account_number: str
    email: str
    phone: str
    address: str
    city: str
    state: str
    postcode: str
    renewal_date: str
    amount: str
    days_until_renewal: int
    policy_number: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


KINDS = {
    "insurance": (
        "{provider}\nRenewal Notice\n\nDear {holder},\n\n"
        "Your {kind_title} policy is scheduled to renew on {renewal_date}. "
        "Your current premium is {amount}.\n\n"
        "Policy number: {policy_number}\nAccount number: {account_number}\n"
        "Policy holder: {holder}\nInsured address: {address}, {city}, {state} {postcode}\n"
        "Contact: {email} | {phone}\n\n"
        "To renew, no action is required. To cancel instead, notify us in writing "
        "before {renewal_date}.\n\n"
        "We value your continued business.\n{provider}\n"
    ),
    "membership": (
        "{provider}\nMembership Renewal Notice\n\nHello {holder},\n\n"
        "Your membership runs through {renewal_date}. Your rate is {amount}.\n\n"
        "Member name: {holder}\nMember number: {account_number}\n"
        "Home address: {address}, {city}, {state} {postcode}\n"
        "Email: {email}\nPhone: {phone}\n\n"
        "Membership renews automatically on the date above. Cancel any time before "
        "the renewal date and you will not be charged.\n\n"
        "See you soon,\n{provider}\n"
    ),
    "subscription": (
        "{provider}\nSubscription Update\n\nHi {holder},\n\n"
        "Your {kind_title} plan renews on {renewal_date} at {amount}. "
        "Your plan will continue without interruption.\n\n"
        "Account: {account_number}\nAccount holder: {holder}\n"
        "Billing address: {address}, {city}, {state} {postcode}\n"
        "Receipts are sent to {email}. Questions? Call {phone}.\n\n"
        "Manage or cancel your subscription at any time from your account page.\n\n"
        "Thanks,\nThe {provider} team\n"
    ),
    "licence": (
        "OFFICE OF RECORD KEEPING\nOfficial Notice of Expiry\n\n"
        "This certifies that {holder} holds a {kind_title} registration with the "
        "{provider}, numbered {account_number}.\n\n"
        "Registered holder: {holder}\nRegistration number: {account_number}\n"
        "Registered address: {address}, {city}, {state} {postcode}\n"
        "Date of issue: {issue_date}\nDate of expiry: {renewal_date}\n\n"
        "Correspondence to this office should be directed to {email} or {phone}. "
        "A renewal fee of {amount} applies.\n\n"
        "Failure to renew will result in lapse of the registration.\n"
    ),
    "medical": (
        "{provider}\nPlan Renewal Summary\n\nDear {holder},\n\n"
        "Your {kind_title} coverage renews on {renewal_date}. Your contribution is "
        "{amount}.\n\n"
        "Subscriber: {holder}\nMember ID: {account_number}\n"
        "Home address: {address}, {city}, {state} {postcode}\n"
        "Member services: {email} | {phone}\n\n"
        "To keep your current benefits, renew by {renewal_date}.\n\n"
        "{provider}\n"
    ),
    "utility": (
        "{provider}\nService Notice\n\nDear {holder},\n\n"
        "Your service account renews on {renewal_date}. The monthly charge is {amount}.\n\n"
        "Service address: {address}, {city}, {state} {postcode}\n"
        "Account number: {account_number}\nAccount holder: {holder}\n"
        "Billing contact: {email} | {phone}\n\n"
        "Your tariff ends on {renewal_date}. A new tariff will begin automatically.\n\n"
        "{provider}\n"
    ),
}

KIND_TITLE = {
    "insurance": "homeowners",
    "membership": "athletic club",
    "subscription": "monthly",
    "licence": "professional",
    "medical": "dental",
    "utility": "energy",
}


def make_spec(doc_id: str, rng: random.Random, today: date) -> DocSpec:
    kind = rng.choice(DOC_KINDS)
    holder = rng.choice(HOLDERS)
    provider = rng.choice(PROVIDERS[kind])
    address, city, state, postcode = _address(rng)
    amount, amount_text = _money(rng)

    # A spread of dates: some near, some far, a few already past so the
    # pipeline has to handle a document that arrived late.
    days = rng.randint(-40, 400)
    renewal = today + timedelta(days=days)
    issue = renewal - timedelta(days=365)

    return DocSpec(
        doc_id=doc_id,
        kind=kind,
        provider=provider,
        holder=holder,
        account_number=_account_number(rng, kind),
        email=_email(rng, holder),
        phone=_phone(rng),
        address=address,
        city=city,
        state=state,
        postcode=postcode,
        renewal_date=renewal.isoformat(),
        amount=amount_text,
        days_until_renewal=days,
        policy_number=(
            f"POL-{rng.randint(1000000, 9999999)}-{rng.choice(string.ascii_uppercase)}"
            if kind == "insurance"
            else ""
        ),
        metadata={"amount_value": amount, "issue_date": issue.isoformat()},
    )


def render(spec: DocSpec) -> str:
    template = KINDS[spec.kind]
    return template.format(
        provider=spec.provider,
        kind_title=KIND_TITLE[spec.kind],
        holder=spec.holder,
        renewal_date=spec.renewal_date,
        amount=spec.amount,
        policy_number=spec.policy_number,
        account_number=spec.account_number,
        address=spec.address,
        city=spec.city,
        state=spec.state,
        postcode=spec.postcode,
        email=spec.email,
        phone=spec.phone,
        issue_date=spec.metadata.get("issue_date", ""),
    )


def _load_font():
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        return ImageFont.load_default()


def _max_chars(font, page_width: int = PAGE_WIDTH) -> int:
    """How many characters fit across the usable width of the page.

    Measured from the font rather than guessed: a monospace advance is
    roughly 0.6 em, but the fallback font is not monospace at all, and
    getting this wrong is how text ends up off the edge of the page.
    """
    advance = font.getlength("M") or FONT_SIZE * 0.6
    usable = page_width - 2 * MARGIN
    return max(20, int(usable // advance))


def wrap_lines(text: str, font=None) -> list[str]:
    """Wrap the document to the width the page can actually show.

    A line that does not fit used to be truncated by the renderer, and
    the overflow silently fell off the right edge of the page. That made
    the rendered "scan" a *truncated document* — the amount on a long
    line was simply not there — and every OCR candidate was then judged
    on a page missing text. The benchmark was measuring the renderer's
    bug, not the models.

    Wrapping also makes the image agree with the ground truth, which is
    the property the OCR benchmark's scoring depends on.
    """
    font = font or _load_font()
    width = _max_chars(font)
    wrapped: list[str] = []
    for line in text.splitlines():
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(line, width) or [""])
    return wrapped


def render_image(text: str, path: Path) -> bool:
    """Render text to a PNG so the OCR benchmark has something to read.

    Deliberately crude — a monospaced page on a light background with a
    slight rotation and some noise, so that a vision model is doing
    something more than reading a text file.

    The page grows to fit the wrapped text rather than dropping what does
    not fit. A dropped line is a document the benchmark never sees.

    Returns False if Pillow is not installed.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    font = _load_font()
    lines = wrap_lines(text, font)

    # Grow the page rather than clip it.
    height = max(PAGE_HEIGHT, MARGIN * 2 + len(lines) * LINE_HEIGHT)
    image = Image.new("RGB", (PAGE_WIDTH, height), "#f7f5f0")
    draw = ImageDraw.Draw(image)

    y = MARGIN
    for line in lines:
        draw.text((MARGIN, y), line, fill="#1a1a1a", font=font)
        y += LINE_HEIGHT

    # A little skew and grain, so this is not trivially a text render.
    image = image.rotate(0.4, expand=False, fillcolor="#f7f5f0")
    pixels = image.load()
    for x in range(0, PAGE_WIDTH, 3):
        for yy in range(0, height, 3):
            r, g, b = pixels[x, yy]
            jitter = ((x * 7 + yy * 13) % 9) - 4
            pixels[x, yy] = (
                max(0, min(255, r + jitter)),
                max(0, min(255, g + jitter)),
                max(0, min(255, b + jitter)),
            )

    image.save(path, "PNG")
    return True


def generate_corpus(
    out_dir: str | Path,
    *,
    count: int = 200,
    seed: int = 20261019,
    today: date | str = "",
    render_images: bool = False,
    clean: bool = False,
) -> Path:
    """Generate the corpus into `out_dir`.

    Returns the output directory. Split out of `main` so the tests can
    generate a corpus without going through the command line — the
    reproducibility and clipping properties are worth asserting on every
    run, not just when someone remembers to run the generator by hand.

    The parameter is `render_images`, not `render`: the module already has
    a `render(spec)` function that turns a record into text, and a
    parameter named `render` shadows it inside this scope.
    """
    if isinstance(today, str):
        reference_date = date.fromisoformat(today) if today else date.today()
    else:
        reference_date = today

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if clean:
        for pattern in ("*.txt", "*.json", "*.png"):
            for old in out.glob(pattern):
                old.unlink()

    rng = random.Random(seed)
    manifest_path = out / "manifest.jsonl"

    rendered = 0
    with manifest_path.open("w", encoding="utf-8") as manifest:
        for index in range(count):
            doc_id = f"doc-{index:04d}"
            spec = make_spec(doc_id, rng, reference_date)
            text = render(spec)

            (out / f"{doc_id}.txt").write_text(text, encoding="utf-8")
            (out / f"{doc_id}.json").write_text(
                json.dumps(asdict(spec), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest.write(json.dumps(asdict(spec), sort_keys=True) + "\n")

            if render_images:
                if render_image(text, out / f"{doc_id}.png"):
                    rendered += 1
                else:
                    print(
                        "warning: Pillow is not installed — skipping image rendering.\n"
                        "         pip install pillow, then re-run with --render.",
                        file=sys.stderr,
                    )
                    break

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="eval/synthetic_docs", help="output directory")
    parser.add_argument("--count", type=int, default=200, help="number of documents")
    parser.add_argument("--seed", type=int, default=20261019, help="RNG seed")
    parser.add_argument(
        "--today",
        default="",
        help="reference date, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="also render each document to a PNG (needs Pillow)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove existing generated files first",
    )
    args = parser.parse_args(argv)

    out_dir = generate_corpus(
        args.out,
        count=args.count,
        seed=args.seed,
        today=args.today,
        render_images=args.render,
        clean=args.clean,
    )

    manifest_path = out_dir / "manifest.jsonl"
    pngs = list(out_dir.glob("doc-*.png"))

    print(f"wrote {args.count} documents to {out_dir}")
    print(f"manifest: {manifest_path}")
    if args.render:
        if len(pngs) == args.count:
            print(f"rendered {len(pngs)} PNGs")
        else:
            print(f"rendered {len(pngs)}/{args.count} PNGs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
