from __future__ import annotations

import hashlib
import io

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter

from powercontext.sources import SourceDefinitionRegistry
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_pdf_page_identity_and_projection_preserve_document_id() -> None:
    raw = _blank_pdf()
    page = parse_pdf_document(PDFDocumentInput(document_id=" report ", content=raw))[0]

    assert page.document_id == " report "
    assert page.document_sha256 == hashlib.sha256(raw).hexdigest()

    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = await registry.resolve(page)
    assert len(source.name) <= 256
    assert await registry.read(source) == page
    assert registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY) == {
        "source_type": "pdf-page",
        "source_id": source.name,
        "content": " report \n",
        "metadata": {
            "document_id": " report ",
            "document_sha256": page.document_sha256,
            "page_number": 1,
        },
    }


def test_pdf_document_id_rejects_only_empty_or_whitespace_values() -> None:
    with pytest.raises(ValidationError):
        PDFDocumentInput(document_id=" \t ", content=_blank_pdf())
