"""温湿度证据处理：核心温度曲线、暴露时长、降温速率与异常区间。

只有 attached 读数进入核心温度曲线；脱落/离线读数只用于标记异常区间，
绝不充当商品温度。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import Reading


@dataclass(frozen=True)
class Anomaly:
    kind: str  # probe-detached | above-target | exposure-exceeded | ...
    start: datetime | None
    end: datetime | None
    detail: str


def attached_curve(readings: list[Reading], since: datetime | None = None) -> list[Reading]:
    """进入冷却单元之后的有效核心温度曲线（按时间升序）。"""
    curve = [r for r in readings if r.is_core_evidence and (since is None or r.at >= since)]
    return sorted(curve, key=lambda r: r.at)


def detached_intervals(readings: list[Reading]) -> list[Anomaly]:
    """每段探针脱落区间：从脱落读数起，到下一条 attached 读数止（左闭右开）。"""
    anomalies: list[Anomaly] = []
    open_from: datetime | None = None
    for r in sorted(readings, key=lambda r: r.at):
        if r.is_core_evidence:
            if open_from is not None:
                anomalies.append(
                    Anomaly("probe-detached", open_from, r.at, "探针脱落，期间读数为环境温度")
                )
                open_from = None
        elif open_from is None:
            open_from = r.at
    if open_from is not None:
        anomalies.append(Anomaly("probe-detached", open_from, None, "探针脱落后再无有效读数"))
    return anomalies


def crossing_time(curve: list[Reading], target: float) -> datetime | None:
    """核心温度首次降至 target 的时刻（相邻读数线性插值，取整到秒）。

    曲线从未达到 target 时返回 None。
    """
    prev: Reading | None = None
    for r in curve:
        if r.core_c <= target:
            if prev is None:
                return r.at
            frac = (prev.core_c - target) / (prev.core_c - r.core_c)
            span = (r.at - prev.at).total_seconds()
            return prev.at + timedelta(seconds=round(frac * span))
        prev = r
    return None


def cooling_rate_c_per_hour(
    curve: list[Reading], target: float, crossing: datetime | None
) -> float | None:
    """降温速率：达标时为 首读数→达标时刻 的平均速率，否则为 首→末读数 的速率。"""
    if len(curve) < 2:
        return None
    start = curve[0]
    if crossing is not None:
        end_at, end_c = crossing, target
    else:
        end_at, end_c = curve[-1].at, curve[-1].core_c
    hours = (end_at - start.at).total_seconds() / 3600
    if hours <= 0:
        return None
    return (start.core_c - end_c) / hours
