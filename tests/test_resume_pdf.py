from __future__ import annotations

import base64

import pytest

from capybot.apply.resume_pdf import parse_resume_pdf_data_url


def test_resume_pdf_text_layer_is_converted_to_markdown() -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72), "Education\nZhejiang University CS Master\nProjects\nCapybot Apply Agent System"
    )
    data = doc.tobytes()
    data_url = "data:application/pdf;base64," + base64.b64encode(data).decode("ascii")

    result = parse_resume_pdf_data_url(data_url, filename="resume.pdf")

    assert result.engine in {"pymupdf_text", "pypdf_text", "paddleocr"}
    assert "Zhejiang University" in result.markdown
    assert "Capybot Apply Agent System" in result.markdown
