"""UTC 时刻解析与比较工具。

导入文件中的时刻统一使用 ISO-8601 字符串（推荐以 ``Z`` 结尾）。
内部一律按 UTC 处理，存储为秒精度且带 ``+00:00`` 的规范字符串，
比较时转换为 aware datetime，避免任何本地时区参与判断。
"""

from datetime import datetime, timezone

from .errors import ValidationError


def parse_utc(value):
    """把 ISO-8601 字符串解析为 UTC aware datetime。"""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"时刻必须是 ISO-8601 字符串，实际为 {value!r}")
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"时刻无法解析：{value!r}") from exc
    if dt.tzinfo is None:
        # 未带时区偏移时按 UTC 解释（工具只处理 UTC 资料）。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize(value):
    """解析后输出统一存储格式（UTC，秒精度）。"""
    return parse_utc(value).isoformat()


def to_dt(stored):
    """数据库中的规范字符串转回 aware datetime。"""
    return datetime.fromisoformat(stored)


def is_before_or_equal(left, right):
    """left <= right（允许同一时刻连续作业；时间倒置指严格早于）。"""
    return to_dt(left) <= to_dt(right)
