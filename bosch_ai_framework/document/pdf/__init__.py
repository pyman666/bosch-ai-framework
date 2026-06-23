"""PDF submodule — VLM extraction with 4 modes.

Provides:
    - ``ask_vlm()``: core PDF → structured data extraction
    - ``PdfMode``: AUTO / NATIVE / IMAGE / MARKDOWN

Extracted from: bosch-idoc
"""

from bosch_ai_framework.document.pdf.vlm import PdfMode, ask_vlm

__all__ = ["ask_vlm", "PdfMode"]
