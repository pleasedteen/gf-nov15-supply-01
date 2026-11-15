"""JSON 规范化与基础类型检查。"""

import json

from .errors import ValidationError


def dumps(obj):
    """稳定序列化：键排序、无多余空白，便于做“同内容重发”比较。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def require(obj, key, expected_types, label):
    """取出必填字段并做类型检查。"""
    if not isinstance(obj, dict) or key not in obj:
        raise ValidationError(f"{label}缺少必填字段：{key}")
    value = obj[key]
    if not isinstance(value, expected_types):
        names = (
            "/".join(t.__name__ for t in expected_types)
            if isinstance(expected_types, tuple)
            else expected_types.__name__
        )
        raise ValidationError(f"{label}字段 {key} 类型应为 {names}，实际为 {type(value).__name__}")
    return value


def require_nonempty_str(obj, key, label):
    value = require(obj, key, str, label)
    if not value.strip():
        raise ValidationError(f"{label}字段 {key} 不能为空字符串")
    return value.strip()
