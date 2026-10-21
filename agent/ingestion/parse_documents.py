"""Document parsing and text extraction.

Phase 1 — Person B.

The Phase 0 decision (`docs/ocr-decision.md`) is **Option C**: extract
locally, redact locally, then let the model read only the redacted text.
This module is the first half of that.

    bytes ──► local extraction ──► plain text ──► local redaction ──► safe text
                                     │                                   │
                                     └──► the real values stay here ────┘

The order is not a style preference. The vision model is a Super-tier
model, and the project's headline claim is that personal details never
reach a Super/Ultra tier. Extracting locally and redacting locally
means the model call carries no personal detail at all, so the claim
holds literally rather than with an exception attached.

**The local extractor is the primary path, not a fallback of last
resort.** For a page that must not leave the machine at all it is the
only path, because it has no egress.

Two things come out of the other end, and they have to travel together:

* `Document.text` — the redacted text. Safe to send to a model.
* `Document.redactions` — the placeholder → real value map. Stays in the
  sandbox, and is what makes a draft with the real account number in it
  possible without the value ever travelling.

Design notes:

* **Extraction is separate from interpretation.** This module returns text
  and layout hints. Deciding that a document is an insurance policy with a
  renewal date belongs in `agent/deadlines/`, not here.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agent.ingestion.ocr_text import repair_ocr_text
from privacy_router.redact import RedactionMap, redacted_text

# Model ID for document understanding. Pinned from the live Token Factory
# catalog — see `make pin-models`. Never hand-type a model ID: the casing
# is inconsistent across Nebius's own documentation.
DEFAULT_OCR_MODEL = os.environ.get("LEDGER_MODEL_OCR", "")

# The redaction tier. Cheapest model that is good enough to strip personal
# details out of a span we could not classify locally.
DEFAULT_REDACT_MODEL = os.environ.get("LEDGER_MODEL_REDACT", "")

TOKEN_FACTORY_BASE_URL = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")

# How long a local extractor may take on one page.
EXTRACT_TIMEOUT = 120.0


@dataclass(slots=True)
class Document:
    """A parsed document.

    Attributes:
        source_path: Where the document came from on disk.
        text: The extracted text, **redacted**. This is the only text
            that may leave the sandbox.
        pages: Per-page text, when the extractor can segment the document.
        media_type: The MIME type of the original bytes.
        metadata: Anything the extractor learned that is not text — page
            count, detected language, whether the page was scanned.
        warnings: Non-fatal problems (a page that failed to OCR, a
            truncated extraction). A document with warnings is still
            usable; a document with no text is not.
        redactions: The placeholder → real value map. It never leaves
            the sandbox, and it is deliberately not part of the
            serialisable form of the document.
    """

    source_path: Path
    text: str
    pages: list[str] = field(default_factory=list)
    media_type: str = "application/octet-stream"
    metadata: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    redactions: RedactionMap = field(default_factory=RedactionMap)

    @property
    def is_empty(self) -> bool:
        """True when nothing usable was extracted."""
        return not self.text.strip()

    @property
    def page_count(self) -> int:
        return int(self.metadata.get("page_count", len(self.pages) or 1))

    def resolve(self, text: str) -> str:
        """Put the real values back into `text`.

        The only way a personal detail leaves the redaction layer. Call
        it at the last moment, when the text is going to the user in
        their own channel.
        """
        return self.redactions.resolve(text)

    def serialisable(self) -> dict[str, object]:
        """The document's form, without the redaction map.

        This is what may be logged, stored, or sent anywhere. The map is
        not in it, and `resolve` is not reachable from it.
        """
        return {
            "source_path": str(self.source_path),
            "text": self.text,
            "media_type": self.media_type,
            "metadata": dict(self.metadata),
            "warnings": list(self.warnings),
        }


# --- media types ------------------------------------------------------------


def guess_media_type(path: Path) -> str:
    """Guess the media type from the file's magic bytes, then its name.

    The extension is checked second because it lies more often than the
    bytes do — a `.jpg` that is really a PDF is common enough that
    trusting the extension produces a confusing error much later.
    """
    header = b""
    with contextlib.suppress(OSError):
        header = path.read_bytes()[:8]
    if header.startswith(b"%PDF"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"GIF8"):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith(b"II*\x00") or header.startswith(b"MM\x00*"):
        return "image/tiff"

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}:
        return f"image/{suffix.lstrip('.')}"
    if suffix in {".txt", ".text", ".md"}:
        return "text/plain"
    if suffix in {".eml", ".msg"}:
        return "message/rfc822"
    if suffix in {".html", ".htm"}:
        return "text/html"
    return "application/octet-stream"


# --- local extraction -------------------------------------------------------


def _run(command: list[str], timeout: float = EXTRACT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ocr_image(path: Path, *, timeout: float = EXTRACT_TIMEOUT) -> tuple[str, str]:
    """OCR an image locally.

    Returns:
        (text, backend)

    Tesseract is preferred because it is what the sandbox image ships.
    RapidOCR is the pip-only fallback for a machine that cannot install
    system packages. Both are local, and both have **no egress at all**,
    which is the property that matters here.
    """
    if shutil.which("tesseract"):
        proc = _run(["tesseract", str(path), "stdout"], timeout)
        if proc.returncode == 0:
            return proc.stdout, "tesseract"
        raise RuntimeError(f"tesseract exited {proc.returncode}: {proc.stderr[:200]}")

    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "no local OCR available: install tesseract, or `pip install rapidocr-onnxruntime`"
        ) from exc

    engine = RapidOCR()
    result, _elapse = engine(str(path))
    if not result:
        return "", "rapidocr"
    return "\n".join(str(line[1]) for line in result), "rapidocr"


def pdf_text(path: Path, *, timeout: float = EXTRACT_TIMEOUT) -> str:
    """Extract the text layer of a PDF with poppler."""
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is not installed")
    proc = _run(["pdftotext", "-layout", str(path), "-"], timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext exited {proc.returncode}: {proc.stderr[:200]}")
    return proc.stdout


def pdf_page_count(path: Path) -> int:
    """How many pages a PDF has."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def pdf_render(path: Path, out_dir: Path, *, timeout: float = EXTRACT_TIMEOUT) -> list[Path]:
    """Render every page of a PDF to PNG, for a PDF with no text layer."""
    if not shutil.which("pdftoppm"):
        raise RuntimeError("pdftoppm is not installed")
    prefix = out_dir / "page"
    proc = _run(["pdftoppm", "-r", "200", "-png", str(path), str(prefix)], timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftoppm exited {proc.returncode}: {proc.stderr[:200]}")
    return sorted(out_dir.glob("page-*.png"))


def extract_text(
    data: bytes,
    *,
    media_type: str = "application/octet-stream",
    model: str | None = None,
) -> str:
    """Extract plain text from raw bytes.

    Used for documents that arrive over the wire (an email attachment, an
    upload) rather than from disk.

    The `model` argument is accepted for interface symmetry with the
    vision path, but **the local extractor is used first regardless**:
    a page that can be read locally must be read locally, because the
    alternative is the raw document leaving the sandbox.
    """
    suffix = _suffix_for(media_type)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"document{suffix}"
        path.write_bytes(data)
        text, _warnings = _extract_local(path, media_type)
        return text


def _suffix_for(media_type: str) -> str:
    return {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/tiff": ".tif",
        "text/plain": ".txt",
    }.get(media_type, ".bin")


def _extract_local(path: Path, media_type: str) -> tuple[str, list[str]]:
    """Extract text locally. Returns (text, warnings)."""
    warnings: list[str] = []

    if media_type == "application/pdf":
        text = pdf_text(path).strip()
        if text:
            return text, warnings

        # A PDF with no text layer is a scan. Render it and OCR the
        # pages. This is the case the OCR model exists for, and it is
        # the case where the local path can still win.
        warnings.append("PDF has no text layer — rendering and OCRing the pages")
        with tempfile.TemporaryDirectory() as tmp:
            pages = pdf_render(path, Path(tmp))
            chunks = []
            for page in pages:
                try:
                    page_text, _backend = ocr_image(page)
                except Exception as exc:
                    warnings.append(f"{page.name}: {exc}")
                    continue
                chunks.append(page_text)
            return "\n\n".join(chunks), warnings

    if media_type.startswith("image/"):
        text, backend = ocr_image(path)
        warnings.append(f"OCR backend: {backend}")
        return text, warnings

    if media_type == "text/plain":
        return path.read_text(encoding="utf-8", errors="replace"), warnings

    return "", [f"no local extractor for {media_type}"]


# --- the public entry point -------------------------------------------------


def parse_document(
    path: str | Path,
    *,
    model: str | None = None,
    household_names: Iterable[str] = (),
    redact: bool = True,
) -> Document:
    """Parse a document from disk into redacted text.

    Args:
        path: The file to read.
        model: Override the OCR model. Defaults to `LEDGER_MODEL_OCR`.
            Unused on the local path, which is the point: the local
            extractor is tried first and the model is only reached for
            what it cannot read.
        household_names: Names known to be personal — the household's
            names, from the memory layer. Passed to the redactor so it
            does not have to recognise a name in prose.
        redact: Whether to redact. Off only for tests that need the raw
            text; the pipeline always redacts.

    Returns:
        A `Document`. Never raises for a document it cannot read — an
        unreadable document is a `Document` with `is_empty` True and a
        warning, so the pipeline can report it to the user instead of
        crashing on one bad file in a batch.
    """
    source = Path(path)
    document = Document(source_path=source, text="")

    if not source.exists():
        document.warnings.append("file does not exist")
        return document

    media_type = guess_media_type(source)
    document.media_type = media_type

    try:
        raw_text, warnings = _extract_local(source, media_type)
    except Exception as exc:
        document.warnings.append(f"extraction failed: {exc}")
        return document

    document.warnings.extend(warnings)
    document.pages = [p for p in raw_text.split("\f") if p.strip()] or ([raw_text] if raw_text else [])
    document.metadata["page_count"] = len(document.pages)

    if not raw_text.strip():
        document.warnings.append("no text extracted")
        return document

    # The repair runs *before* the redaction, so the redactor sees
    # labels it can actually match. Repairing afterwards would be too
    # late: a redactor that could not find `Account number:` because the
    # OCR dropped a space has already failed to remove the account.
    repaired = repair_ocr_text(raw_text)
    if repaired != raw_text:
        document.warnings.append("repaired OCR spacing")

    if redact:
        document.text, document.redactions = redacted_text(repaired, names=household_names)
        document.metadata["redacted"] = True
        document.metadata["redaction_count"] = len(document.redactions.values)
    else:
        document.text = repaired
        document.metadata["redacted"] = False

    return document


def chunk_text(text: str, *, size: int = 4000, overlap: int = 200) -> Iterable[str]:
    """Split text into overlapping chunks for the extraction model.

    Split on paragraph boundaries where possible so a chunk does not cut a
    date away from the sentence that gives it meaning. The overlap keeps a
    field that straddles a boundary from being lost.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return

    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for para in paragraphs:
        if length + len(para) > size and current:
            chunks.append("\n\n".join(current))
            # Carry the tail of the previous chunk forward as overlap.
            tail: list[str] = []
            tail_len = 0
            for prev in reversed(current):
                if tail_len + len(prev) > overlap:
                    break
                tail.insert(0, prev)
                tail_len += len(prev)
            current = list(tail)
            length = tail_len

        current.append(para)
        length += len(para)

    if current:
        chunks.append("\n\n".join(current))

    return chunks
