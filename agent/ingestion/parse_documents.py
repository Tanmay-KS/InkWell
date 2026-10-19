"""Document parsing and text extraction.

Phase 1 — Person B.

The OCR/vision model was chosen in Phase 0; see `make ocr-bench` for the
comparison table that decided it. Whatever wins becomes `DEFAULT_OCR_MODEL`
here.

Design notes:

* **Extraction is separate from interpretation.** This module returns text
  and layout hints. Deciding that a document is an insurance policy with a
  renewal date belongs in `agent/deadlines/`, not here.
* **The raw bytes never leave the sandbox except through the vision model
  call**, and that call is one of the destinations the privacy router
  inspects in Phase 2. A document containing a policy number is exactly the
  kind of payload the router exists for.
* **The local extractor is not a fallback of last resort** — for a page
  that must not leave the machine it is the primary path, because it has
  no egress at all.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Model ID for document understanding. Pinned from the live Token Factory
# catalog — see `make pin-models`. Never hand-type a model ID: the casing is
# inconsistent across Nebius's own documentation.
DEFAULT_OCR_MODEL = os.environ.get("LEDGER_MODEL_OCR", "")

# The redaction tier. Cheapest model that is good enough to strip personal
# details out of a span we could not classify locally.
DEFAULT_REDACT_MODEL = os.environ.get("LEDGER_MODEL_REDACT", "")

TOKEN_FACTORY_BASE_URL = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")


@dataclass(slots=True)
class Document:
    """A parsed document.

    Attributes:
        source_path: Where the document came from on disk.
        text: The extracted plain text.
        pages: Per-page text, when the extractor can segment the document.
        media_type: The MIME type of the original bytes.
        metadata: Anything the extractor learned that is not text — page
            count, detected language, whether the page was scanned.
        warnings: Non-fatal problems (a page that failed to OCR, a truncated
            extraction). A document with warnings is still usable; a document
            with no text is not.
    """

    source_path: Path
    text: str
    pages: list[str] = field(default_factory=list)
    media_type: str = "application/octet-stream"
    metadata: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing usable was extracted."""
        return not self.text.strip()

    @property
    def page_count(self) -> int:
        return int(self.metadata.get("page_count", len(self.pages) or 1))


def parse_document(path: str | Path, *, model: str | None = None) -> Document:
    """Parse a document from disk into text.

    Args:
        path: The file to read.
        model: Override the OCR model. Defaults to `LEDGER_MODEL_OCR`.

    Returns:
        A `Document`. Never raises for a document it cannot read — an
        unreadable document is a `Document` with `is_empty` True and a
        warning, so the pipeline can report it to the user instead of
        crashing on one bad file in a batch.

    Phase 1 implementation: dispatch on media type to the local extractor
    (poppler for text PDFs, tesseract for scans) and to the vision model for
    images and layout-heavy PDFs.
    """
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")


def extract_text(data: bytes, *, media_type: str, model: str | None = None) -> str:
    """Extract plain text from raw bytes.

    Used for documents that arrive over the wire (an email attachment, an
    upload) rather than from disk.
    """
    raise NotImplementedError("Phase 1 — see IMPLEMENTATION.md")


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
