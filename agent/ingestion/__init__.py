"""Document and email ingestion.

Turns raw input into structured text that the rest of the pipeline can work
with.

The extraction path is a vision model on Nebius Token Factory
(`nvidia/NVIDIA-Nemotron-3-Nano-Omni`, pinned via `LEDGER_MODEL_OCR` in
`.env`). A local extractor (tesseract / poppler, baked into the sandbox
image) is the fallback and the zero-egress path for pages that must not
leave the machine at all.

Phase 1 — Person B.
"""

from agent.ingestion.parse_documents import Document, extract_text, parse_document
from agent.ingestion.parse_email import ParsedEmail, parse_email

__all__ = [
    "Document",
    "ParsedEmail",
    "extract_text",
    "parse_document",
    "parse_email",
]
