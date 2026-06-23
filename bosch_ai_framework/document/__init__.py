"""Document module — PDF VLM parsing + Excel date normalization.

Install: ``pip install bosch-ai-framework[document]``

Provides:
    PDF:
        - ``ask_vlm()``: 4-mode PDF → structured data via VLM
        - ``PdfMode``: AUTO / NATIVE / IMAGE / MARKDOWN

    Excel:
        - ``parse_cell_date()``: best-effort single-cell date parsing
        - ``normalize_column()``: column-level date normalization
        - ``NormalizeReport``: diagnostic report for normalized columns
        - ``java_to_python_format()``: Java date format → Python strftime

Usage::

    from bosch_ai_framework.document import ask_vlm, PdfMode
    from bosch_ai_framework.document.excel import normalize_column

    # PDF extraction
    result = await ask_vlm(pdf_bytes, schema=MySchema, model="gpt-4o", mode=PdfMode.AUTO)

    # Date normalization
    values, report = normalize_column(raw_column, date_column="OrderDate", target_date_format="yyyy/MM/dd")

Extracted from: bosch-idoc
"""

from bosch_ai_framework.document.pdf import PdfMode, ask_vlm

__all__ = [
    "ask_vlm",
    "PdfMode",
]
