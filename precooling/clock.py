"""设备时钟偏差校正。

资料包中的 clocks 表给出每台设备的 offset_seconds = 设备钟 - 参考钟，
因此 参考时间 = 设备时间 - offset。未登记偏差的设备按 0 处理。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


class ClockTable:
    def __init__(self, offsets: dict[str, float] | None = None) -> None:
        self._offsets = dict(offsets or {})

    @classmethod
    def from_entries(cls, entries: list[dict[str, Any]]) -> "ClockTable":
        return cls({e["device_id"]: float(e.get("offset_seconds", 0.0)) for e in entries})

    def offset(self, device_id: str | None) -> timedelta:
        if device_id is None:
            return timedelta(0)
        return timedelta(seconds=self._offsets.get(device_id, 0.0))

    def correct(self, device_id: str | None, device_at: datetime) -> datetime:
        return device_at - self.offset(device_id)
