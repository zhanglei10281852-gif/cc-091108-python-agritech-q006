"""批次判定：核心温度、降温速率、暴露时长三项指标，以及合格数量流。

判定语义：
- 暴露时长 = 首次进入冷却单元 → 核心温度首次降至目标值（左闭右开区间内的实测曲线）；
- 跨设备转移按各单元的实际停留区间结算，不重复、不遗漏；
- 合格数量沿谱系流动：拆分时按子筐占父筐的比例分配（向下取整，保守），
  合并时按来源求和（不超过合并后实物量）；筐自身有温度证据时以自身判定为准。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .evidence import (
    Anomaly,
    attached_curve,
    cooling_rate_c_per_hour,
    crossing_time,
    detached_intervals,
)
from .lineage import Lineage
from .model import Reading, Rule

PASS = "PASS"
FAIL = "FAIL"
NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class UnitSettlement:
    """跨设备转移后，按实际停留区间结算的温度证据。"""

    unit: str
    start: datetime
    end: datetime | None
    attached_readings: int
    detached_readings: int
    first_core_c: float | None
    last_core_c: float | None


@dataclass(frozen=True)
class Judgement:
    container_id: str
    lot: str
    verdict: str  # PASS | FAIL | NO_EVIDENCE
    reasons: tuple[str, ...]
    anomalies: tuple[Anomaly, ...]
    rule: Rule | None
    cooling_entry: datetime | None
    target_reached_at: datetime | None
    exposure_minutes: float | None
    cooling_rate_c_per_hour: float | None
    min_core_c: float | None
    readings_used: int
    settlement: tuple[UnitSettlement, ...]


class Judge:
    def __init__(self, lineage: Lineage, rules: list[Rule]) -> None:
        self.lineage = lineage
        self.rules = list(rules)

    def rule_for(self, produce: str | None, package: str | None) -> Rule | None:
        for rule in self.rules:
            if rule.produce == produce and rule.package == package:
                return rule
        return self.rules[0] if len(self.rules) == 1 else None

    def cooling_entry(self, cid: str) -> datetime | None:
        """首次进入冷却单元的时刻；从未进入则为 None。"""
        for interval in self.lineage.containers[cid].dwell:
            unit = self.lineage.units.get(interval.unit)
            if unit is not None and unit.cooling:
                return interval.start
        return None

    def judge(self, cid: str, readings: list[Reading]) -> Judgement:
        state = self.lineage.containers[cid]
        rule = self.rule_for(state.produce, state.package)
        readings = sorted(readings, key=lambda r: r.at)
        anomalies = detached_intervals(readings)
        settlement = self._settlement(cid, readings)
        attached = [r for r in readings if r.is_core_evidence]

        if rule is None:
            return Judgement(
                cid, state.lot, NO_EVIDENCE, ("缺少匹配的工艺规则",), tuple(anomalies),
                None, None, None, None, None, None, 0, tuple(settlement),
            )

        entry = self.cooling_entry(cid)
        if entry is None:
            verdict = FAIL if attached else NO_EVIDENCE
            reason = "有温度读数但从未进入冷却单元" if attached else "从未进入冷却单元且无有效温度证据"
            return Judgement(
                cid, state.lot, verdict, (reason,), tuple(anomalies), rule,
                None, None, None, None, None, 0, tuple(settlement),
            )

        curve = attached_curve(readings, since=entry)
        if not curve:
            return Judgement(
                cid, state.lot, NO_EVIDENCE, ("进入冷却单元后无有效核心温度读数",),
                tuple(anomalies), rule, entry, None, None, None, None, 0, tuple(settlement),
            )

        cross = crossing_time(curve, rule.target_core_c)
        rate = cooling_rate_c_per_hour(curve, rule.target_core_c, cross)
        window_end = cross if cross is not None else curve[-1].at
        exposure = (window_end - entry).total_seconds() / 60
        min_core = min(r.core_c for r in curve)

        verdict = PASS
        reasons: list[str] = []
        if cross is None:
            verdict = FAIL
            reasons.append(
                f"核心温度从未降至目标 {rule.target_core_c}°C（有效读数最低 {min_core}°C）"
            )
            anomalies.append(
                Anomaly("above-target", entry, curve[-1].at, "预冷窗口内核心温度始终高于目标")
            )
        if exposure > rule.max_exposure_minutes:
            verdict = FAIL
            reasons.append(f"暴露时长 {exposure:.1f} 分钟超过上限 {rule.max_exposure_minutes} 分钟")
            anomalies.append(
                Anomaly("exposure-exceeded", entry, window_end, "从进入冷却单元到达标耗时超限")
            )
        if rule.min_cooling_rate_c_per_hour is not None and (
            rate is None or rate < rule.min_cooling_rate_c_per_hour
        ):
            verdict = FAIL
            shown = "无法计算" if rate is None else f"{rate:.2f}°C/h"
            reasons.append(
                f"降温速率 {shown} 低于要求 {rule.min_cooling_rate_c_per_hour}°C/h"
            )
        if verdict == PASS:
            reasons.append(
                f"核心温度在 {exposure:.1f} 分钟内降至 {rule.target_core_c}°C，"
                f"降温速率 {rate:.2f}°C/h"
            )
        return Judgement(
            cid, state.lot, verdict, tuple(reasons), tuple(anomalies), rule,
            entry, cross, exposure, rate, min_core, len(curve), tuple(settlement),
        )

    def _settlement(self, cid: str, readings: list[Reading]) -> list[UnitSettlement]:
        out: list[UnitSettlement] = []
        for interval in self.lineage.containers[cid].dwell:
            inside = [
                r
                for r in readings
                if interval.start <= r.at and (interval.end is None or r.at < interval.end)
            ]
            attached = [r for r in inside if r.is_core_evidence]
            out.append(
                UnitSettlement(
                    unit=interval.unit,
                    start=interval.start,
                    end=interval.end,
                    attached_readings=len(attached),
                    detached_readings=len(inside) - len(attached),
                    first_core_c=attached[0].core_c if attached else None,
                    last_core_c=attached[-1].core_c if attached else None,
                )
            )
        return out

    # ------------------------------------------------------------------ 合格数量流
    def qualified_quantity(
        self,
        cid: str,
        readings_by_container: dict[str, list[Reading]],
        _memo: dict[str, int] | None = None,
    ) -> int:
        """有合格证据支撑的实物数量（筐）。

        自身有温度证据时以自身判定为准；无证据时沿谱系继承：
        拆分按比例、合并按求和，保证任意拆分合并顺序下合格量不超过实物量。
        """
        memo = {} if _memo is None else _memo
        if cid in memo:
            return memo[cid]
        state = self.lineage.containers[cid]
        own = readings_by_container.get(cid, [])
        if own:
            quantity = (
                state.initial_quantity if self.judge(cid, own).verdict == PASS else 0
            )
        elif not state.parents:
            quantity = 0
        elif len(state.parents) == 1 and state.share_of_parent is not None:
            parent_q = self.qualified_quantity(state.parents[0], readings_by_container, memo)
            quantity = int(parent_q * state.share_of_parent)  # 向下取整，保守
        else:
            quantity = min(
                state.initial_quantity,
                sum(
                    self.qualified_quantity(p, readings_by_container, memo)
                    for p in state.parents
                ),
            )
        memo[cid] = quantity
        return quantity


def judgement_to_dict(j: Judgement) -> dict:
    def iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt is not None else None

    return {
        "container_id": j.container_id,
        "lot": j.lot,
        "verdict": j.verdict,
        "reasons": list(j.reasons),
        "rule": None
        if j.rule is None
        else {
            "produce": j.rule.produce,
            "package": j.rule.package,
            "target_core_c": j.rule.target_core_c,
            "max_exposure_minutes": j.rule.max_exposure_minutes,
            "min_cooling_rate_c_per_hour": j.rule.min_cooling_rate_c_per_hour,
        },
        "cooling_entry": iso(j.cooling_entry),
        "target_reached_at": iso(j.target_reached_at),
        "exposure_minutes": j.exposure_minutes,
        "cooling_rate_c_per_hour": j.cooling_rate_c_per_hour,
        "min_core_c": j.min_core_c,
        "readings_used": j.readings_used,
        "anomalies": [
            {"kind": a.kind, "start": iso(a.start), "end": iso(a.end), "detail": a.detail}
            for a in j.anomalies
        ],
        "settlement": [
            {
                "unit": s.unit,
                "start": iso(s.start),
                "end": iso(s.end),
                "attached_readings": s.attached_readings,
                "detached_readings": s.detached_readings,
                "first_core_c": s.first_core_c,
                "last_core_c": s.last_core_c,
            }
            for s in j.settlement
        ],
    }
