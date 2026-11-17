"""Display formatting: Indian digit grouping (1,41,578.70) and strike labels."""

from __future__ import annotations

import math
from typing import Optional, Union

Number = Union[int, float]

# Excel custom formats that reproduce lakh/crore grouping for non-negative values
EXCEL_INDIAN_INT = r'[>=10000000]##\,##\,##\,##0;[>=100000]##\,##\,##0;##,##0'
EXCEL_INDIAN_2DP = r'[>=10000000]##\,##\,##\,##0.00;[>=100000]##\,##\,##0.00;##,##0.00'


def indian_number(value: Optional[Number], decimals: int = 0) -> str:
    """1415787.5 -> '14,15,787.50' (decimals=2). None/NaN -> ''."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    negative = value < 0
    text = f"{abs(value):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    result = f"{whole}.{fraction}" if fraction else whole
    return f"-{result}" if negative else result


def strike_label(strike: float) -> str:
    """25250.0 -> '25250', 102.5 -> '102.5'."""
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"
