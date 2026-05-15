from __future__ import annotations

from pathlib import Path


def extract_pdf_text(path: str | Path, *, max_pages: int = 10, max_chars: int = 12000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for PDF text extraction") from exc

    reader = PdfReader(str(path))
    chunks: list[str] = []
    remaining = max_chars
    for page in reader.pages[:max_pages]:
        if remaining <= 0:
            break
        text = page.extract_text() or ""
        if not text.strip():
            continue
        chunks.append(text[:remaining])
        remaining -= len(chunks[-1])
    return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
