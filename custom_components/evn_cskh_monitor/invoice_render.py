"""Render official EVN invoice PDFs to PNG previews.

The PDF itself remains the source of truth.  This module only rasterizes the
first page so integrations such as Zalo Bot can send an image preview when EVN
returns PDF only.  Rendering is intentionally synchronous and must always be
called through Home Assistant's executor.
"""

from __future__ import annotations

from io import BytesIO
import logging

from .invoice import detect_invoice_type

_LOGGER = logging.getLogger(__name__)

# 2x PDF points = roughly 144 DPI.  It keeps invoice text readable while
# avoiding very large image buffers on small Home Assistant hardware.
_PDF_RENDER_SCALE = 2.0
_MAX_RENDERED_PNG_BYTES = 20 * 1024 * 1024


def render_pdf_first_page_png(content: bytes) -> bytes | None:
    """Rasterize the first page of a valid PDF into PNG bytes.

    EVN invoices are normally one page.  For multi-page documents the original
    PDF is still stored unchanged; the PNG is deliberately only a lightweight
    first-page preview.  The function returns ``None`` instead of raising so a
    PDF rendering problem can never break the EVN data refresh path.
    """
    if detect_invoice_type(content) != "pdf":
        return None

    document = None
    page = None
    bitmap = None
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(content)
        if len(document) < 1:
            return None
        page = document[0]
        bitmap = page.render(scale=_PDF_RENDER_SCALE, rotation=0)
        image = bitmap.to_pil()
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")

        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if not data or len(data) > _MAX_RENDERED_PNG_BYTES:
            return None
        if detect_invoice_type(data) != "png":
            return None
        return data
    except Exception as err:  # noqa: BLE001 - malformed PDFs must be isolated
        _LOGGER.debug("Could not render EVN invoice PDF preview: %s", err)
        return None
    finally:
        for obj in (bitmap, page, document):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:  # noqa: BLE001 - cleanup must never propagate
                pass
