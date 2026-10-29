"""Document and email ingestion.

Turns raw input into structured text that the rest of the pipeline can work
with.

Documents and email are extracted locally first (tesseract / poppler,
baked into the sandbox image); the vision model is a last-resort fallback.
Email bodies and locally readable attachments feed the same pipeline.

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
