"""领域模型：采收批、周转筐、冷却单元与温湿度记录的静态结构。

约定：
- 数量一律以筐计；
- 时间区间采用左闭右开 [start, end) 语义；
- 商品核心温度与库内环境温度分别记录，只有 quality == "attached"
  的读数才能作为商品核心温度证据。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

QUALITY_ATTACHED = "attached"


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳必须携带时区: {value!r}")
    return dt


@dataclass(frozen=True)
class Rule:
    """品类 + 包装形式对应的工艺阈值。"""

    produce: str
    package: str
    target_core_c: float
    max_exposure_minutes: float
    min_cooling_rate_c_per_hour: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return cls(
            produce=data["produce"],
            package=data["package"],
            target_core_c=float(data["target_core_c"]),
            max_exposure_minutes=float(data["max_exposure_minutes"]),
            min_cooling_rate_c_per_hour=(
                None
                if data.get("min_cooling_rate_c_per_hour") is None
                else float(data["min_cooling_rate_c_per_hour"])
            ),
        )


@dataclass(frozen=True)
class CoolingUnit:
    """冷却单元；cooling=False 表示接收区等非冷却区域。"""

    id: str
    kind: str = "unknown"
    cooling: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CoolingUnit":
        return cls(
            id=data["id"],
            kind=data.get("kind", "unknown"),
            cooling=bool(data.get("cooling", True)),
        )


@dataclass(frozen=True)
class Reading:
    """一条温湿度记录。at 为按设备时钟偏差校正后的参考时间。"""

    probe_id: str
    container_id: str
    at: datetime
    core_c: float
    quality: str
    device_at: datetime
    device_id: str
    rh_percent: float | None = None

    @property
    def is_core_evidence(self) -> bool:
        """探针脱落/离线时读数反映的是环境温度，不能冒充商品核心温度。"""
        return self.quality == QUALITY_ATTACHED


@dataclass(frozen=True)
class ScanEvent:
    """一条扫码事件。at 为校正后的参考时间，device_at 为设备原始时间。"""

    kind: str  # split | merge | transfer
    event_id: str
    at: datetime
    device_at: datetime
    device_id: str | None
    sources: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    quantities: tuple[int, ...] = ()
    containers: tuple[str, ...] = ()
    to_unit: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class DwellInterval:
    """筐在某个单元内的一段实际停留，左闭右开；end 为 None 表示仍在其中。"""

    container_id: str
    unit: str
    start: datetime
    end: datetime | None
