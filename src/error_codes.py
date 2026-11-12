"""HTTP 错误码配置的统一解析与校验。"""

import json
import re
from typing import Any


def parse_error_codes(value: Any) -> list[int]:
    """接受整数数组、逗号分隔文本或 JSON 数组；拒绝部分有效的输入。"""
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if "[" in value or "]" in value:
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("方括号必须成对，数组内用英文逗号分隔，如 [403, 429]") from exc
        else:
            parts = value.split(",")
            if not all(re.fullmatch(r"[0-9]{3}", part.strip()) for part in parts):
                raise ValueError("请用英文逗号分隔完整的三位错误码，如 403, 429；方括号可省略")
            value = [int(part.strip()) for part in parts]

    if not isinstance(value, list):
        raise ValueError("请输入错误码数组或逗号分隔的错误码，如 [403, 429] 或 403, 429")
    if any(type(code) is not int or not 100 <= code <= 599 for code in value):
        raise ValueError("错误码必须是 100–599 的整数，不能包含文字、小数或引号")
    return sorted(set(value))
