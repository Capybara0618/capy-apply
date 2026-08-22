"""PDF resume ingestion for Capybot Apply.

The parser prefers PaddleOCR for scanned resumes, then falls back to embedded
PDF text extraction so local development stays usable when Paddle wheels are
not available for the current Python/OS combination.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capybot.runtime import ensure_dir, runtime_dir

MAX_RESUME_PDF_BYTES = 8 * 1024 * 1024
MAX_OCR_PAGES = 8
SECTION_KEYWORDS = (
    "教育背景",
    "教育经历",
    "项目经历",
    "项目经验",
    "实习经历",
    "工作经历",
    "专业技能",
    "技能",
    "科研经历",
    "竞赛经历",
    "获奖经历",
    "荣誉奖项",
    "个人总结",
    "自我评价",
    "求职意向",
    "联系方式",
)


class ResumePdfError(ValueError):
    """Raised when a resume PDF cannot be decoded or parsed."""


@dataclass(slots=True)
class ResumePdfParseResult:
    markdown: str
    text: str
    engine: str
    pages: int
    file_path: str
    sha256: str
    warnings: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "text": self.text,
            "engine": self.engine,
            "pages": self.pages,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "warnings": self.warnings,
        }


def parse_resume_pdf_data_url(data_url: str, filename: str | None = None) -> ResumePdfParseResult:
    payload = _decode_pdf_data_url(data_url)
    return parse_resume_pdf_bytes(payload, filename=filename)


def parse_resume_pdf_bytes(data: bytes, filename: str | None = None) -> ResumePdfParseResult:
    if not data:
        raise ResumePdfError("PDF 文件为空")
    if len(data) > MAX_RESUME_PDF_BYTES:
        raise ResumePdfError("PDF 文件超过 8MB，请压缩后再上传")
    if not data.lstrip().startswith(b"%PDF"):
        raise ResumePdfError("请上传 PDF 格式的简历")

    sha256 = hashlib.sha256(data).hexdigest()
    file_path = _persist_resume_pdf(data, filename=filename, sha256=sha256)
    warnings: list[str] = []

    text, pages, engine = _extract_pdf_text(data)
    if not _clean_text(text):
        engine = "paddleocr"
        try:
            text, pages = _extract_with_paddleocr(data)
        except Exception as exc:
            warnings.append(f"PaddleOCR 未启用或识别失败，已切换到 PDF 文本解析：{exc}")
            text, pages, engine = _extract_pdf_text(data)

    cleaned = _clean_text(text)
    if not cleaned:
        raise ResumePdfError(
            "未能从 PDF 中解析出文本。请确认 PaddleOCR 已安装，或上传带文本层的 PDF 简历。"
        )
    markdown = _text_to_markdown(cleaned)
    return ResumePdfParseResult(
        markdown=markdown,
        text=cleaned,
        engine=engine,
        pages=pages,
        file_path=str(file_path),
        sha256=sha256,
        warnings=warnings,
    )


def _decode_pdf_data_url(data_url: str) -> bytes:
    if not isinstance(data_url, str) or "," not in data_url:
        raise ResumePdfError("PDF 上传数据格式错误")
    header, encoded = data_url.split(",", 1)
    if "base64" not in header.lower() or "pdf" not in header.lower():
        raise ResumePdfError("请上传 base64 编码的 PDF 文件")
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ResumePdfError("PDF base64 解码失败") from exc


def _persist_resume_pdf(data: bytes, *, filename: str | None, sha256: str) -> Path:
    resume_dir = ensure_dir(runtime_dir("apply") / "resumes")
    safe_name = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", filename or "resume.pdf").strip("._") or "resume.pdf"
    )
    if not safe_name.lower().endswith(".pdf"):
        safe_name = f"{safe_name}.pdf"
    path = resume_dir / f"{sha256[:12]}_{safe_name}"
    path.write_bytes(data)
    return path


def _extract_with_paddleocr(data: bytes) -> tuple[str, int]:
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "aistudio")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("FLAGS_enable_pir_api", "0")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        raise RuntimeError("缺少 PyMuPDF，无法把 PDF 页面渲染给 PaddleOCR") from exc
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except Exception as exc:  # pragma: no cover - local CI usually lacks Paddle
        raise RuntimeError("缺少 paddleocr/paddlepaddle 依赖") from exc

    doc = fitz.open(stream=data, filetype="pdf")
    page_count = len(doc)
    if page_count == 0:
        return "", 0
    ocr = _build_paddle_ocr(PaddleOCR)
    texts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="capybot_resume_ocr_") as tmp:
        tmp_dir = Path(tmp)
        for index, page in enumerate(doc[:MAX_OCR_PAGES]):
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image_path = tmp_dir / f"page_{index + 1}.png"
            pix.save(str(image_path))
            result = _run_paddle_ocr(ocr, image_path)
            texts.extend(_flatten_paddle_result(result))
    return "\n".join(texts), page_count


def _build_paddle_ocr(paddle_ocr_cls: Any) -> Any:
    for kwargs in (
        {"use_textline_orientation": True, "lang": "ch"},
        {"use_angle_cls": True, "lang": "ch"},
        {"lang": "ch"},
    ):
        try:
            return paddle_ocr_cls(**kwargs)
        except (TypeError, ValueError):
            continue
    return paddle_ocr_cls()


def _run_paddle_ocr(ocr: Any, image_path: Path) -> Any:
    if hasattr(ocr, "predict"):
        return ocr.predict(str(image_path))
    if hasattr(ocr, "ocr"):
        try:
            return ocr.ocr(str(image_path), cls=True)
        except TypeError:
            return ocr.ocr(str(image_path))
    raise RuntimeError("当前 PaddleOCR 对象没有可用的 ocr/predict 方法")


def _flatten_paddle_result(result: Any) -> list[str]:
    lines: list[str] = []
    if not result:
        return lines
    if isinstance(result, dict):
        return _texts_from_paddle_mapping(result)
    for page in result:
        if not page:
            continue
        if isinstance(page, dict):
            lines.extend(_texts_from_paddle_mapping(page))
            continue
        rec_texts = getattr(page, "rec_texts", None)
        if isinstance(rec_texts, list):
            lines.extend(str(text).strip() for text in rec_texts if str(text).strip())
            continue
        json_data = getattr(page, "json", None)
        if isinstance(json_data, dict):
            lines.extend(_texts_from_paddle_mapping(json_data))
            continue
        for item in page:
            text = None
            if isinstance(item, dict):
                text = item.get("text") or item.get("rec_text")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                rec = item[1]
                if isinstance(rec, (list, tuple)) and rec:
                    text = rec[0]
                elif isinstance(rec, str):
                    text = rec
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
    return lines


def _texts_from_paddle_mapping(data: dict[str, Any]) -> list[str]:
    values = data.get("rec_texts") or data.get("texts") or data.get("text")
    if isinstance(values, str):
        return [values.strip()] if values.strip() else []
    if isinstance(values, list):
        return [str(text).strip() for text in values if str(text).strip()]
    return []


def _extract_pdf_text(data: bytes) -> tuple[str, int, str]:
    try:
        import fitz  # type: ignore

        doc = fitz.open(stream=data, filetype="pdf")
        pages = len(doc)
        text = "\n".join(page.get_text("text") for page in doc)
        if text.strip():
            return text, pages, "pymupdf_text"
    except Exception:
        pass

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text, len(reader.pages), "pypdf_text"
    except Exception as exc:
        raise ResumePdfError(f"PDF 文本解析失败：{exc}") from exc


def _clean_text(text: str) -> str:
    lines: list[str] = []
    for raw in text.replace("\u00a0", " ").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _text_to_markdown(text: str) -> str:
    lines = text.splitlines()
    markdown: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        normalized = stripped.strip("：: ")
        if any(keyword == normalized or keyword in normalized[:12] for keyword in SECTION_KEYWORDS):
            if markdown and markdown[-1] != "":
                markdown.append("")
            markdown.append(f"## {normalized}")
            markdown.append("")
            continue
        if re.match(r"^[•·*-]\s+", stripped):
            bullet_text = re.sub(r"^[•·*-]\s+", "", stripped)
            markdown.append(f"- {bullet_text}")
        else:
            markdown.append(stripped)
    return "\n".join(markdown).strip() + "\n"
