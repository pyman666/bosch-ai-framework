"""Utils module — general utility functions.

Provides:
    - ``exception_detail()``: extract structured detail from exceptions
    - ``utcnow()``: timezone-naive UTC datetime
"""

from bosch_ai_framework.utils.errors import exception_detail, utcnow

__all__ = ["exception_detail", "utcnow"]
