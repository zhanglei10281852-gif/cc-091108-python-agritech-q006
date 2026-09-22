"""扫码事件维护采收批、周转筐与冷却单元之间的谱系。

幂等约定：所有事件按 event_id 去重。弱网下重复提交同一负载会被忽略，
不会制造第二次转移；同一 event_id 携带不同负载视为冲突并拒绝，
先到的记录保持不变。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import IdempotencyConflict, LineageError, UnknownContainer
from .model import CoolingUnit, DwellInterval, ScanEvent


def canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class ContainerState:
    id: str
    lot: str
    produce: str | None
    package: str | None
    initial_quantity: int
    quantity: int  # 剩余数量；被拆分/合并消耗后为 0
    unit: str | None
    entered_at: datetime | None
    parents: tuple[str, ...] = ()
    share_of_parent: float | None = None  # 拆分子筐占父筐被消耗前剩余数量的比例
    born_event: str | None = None
    alive: bool = True
    dwell: list[DwellInterval] = field(default_factory=list)
    children: list[str] = field(default_factory=list)


class Lineage:
    def __init__(self, units: dict[str, CoolingUnit]) -> None:
        self.units = dict(units)
        self.containers: dict[str, ContainerState] = {}
        self.events: dict[str, ScanEvent] = {}
        self._payloads: dict[str, str] = {}

    # ------------------------------------------------------------------ 基础筐
    def add_base_container(
        self,
        *,
        id: str,
        lot: str,
        quantity: int,
        unit: str | None,
        entered_at: datetime | None,
        produce: str | None = None,
        package: str | None = None,
    ) -> None:
        if id in self.containers:
            raise LineageError(f"筐 {id} 已存在")
        if unit is not None and unit not in self.units:
            raise LineageError(f"未知冷却单元 {unit}")
        state = ContainerState(
            id=id,
            lot=lot,
            produce=produce,
            package=package,
            initial_quantity=quantity,
            quantity=quantity,
            unit=unit,
            entered_at=entered_at,
        )
        if unit is not None and entered_at is not None:
            state.dwell.append(DwellInterval(id, unit, entered_at, None))
        self.containers[id] = state

    # ------------------------------------------------------------------ 事件
    def apply(self, event: ScanEvent, payload: str) -> bool:
        """应用事件；返回 True 表示状态改变，False 表示重复提交被忽略。"""
        seen = self._payloads.get(event.event_id)
        if seen is not None:
            if seen == payload:
                return False
            raise IdempotencyConflict(f"事件 {event.event_id} 已存在但负载不一致，已拒绝")
        handlers = {
            "split": self._apply_split,
            "merge": self._apply_merge,
            "transfer": self._apply_transfer,
        }
        handler = handlers.get(event.kind)
        if handler is None:
            raise LineageError(f"未知事件类型 {event.kind!r}")
        handler(event)
        self._payloads[event.event_id] = payload
        self.events[event.event_id] = event
        return True

    def apply_all(self, events: list[tuple[ScanEvent, str]]) -> int:
        """按校正后的参考时间排序应用一批事件，返回实际生效的数量。"""
        applied = 0
        for event, payload in sorted(events, key=lambda ep: (ep[0].at, ep[0].event_id)):
            applied += int(self.apply(event, payload))
        return applied

    def _require_alive(self, cid: str, event: ScanEvent) -> ContainerState:
        state = self.containers.get(cid)
        if state is None:
            raise UnknownContainer(f"事件 {event.event_id} 引用了未知筐 {cid}")
        if not state.alive:
            raise LineageError(f"筐 {cid} 已被消耗，不能再次参与事件 {event.event_id}")
        return state

    @staticmethod
    def _close_dwell(state: ContainerState, at: datetime) -> None:
        if state.dwell and state.dwell[-1].end is None:
            last = state.dwell[-1]
            state.dwell[-1] = DwellInterval(state.id, last.unit, last.start, at)

    def _consume(self, state: ContainerState, at: datetime) -> None:
        self._close_dwell(state, at)
        state.quantity = 0
        state.alive = False

    def _apply_split(self, event: ScanEvent) -> None:
        if len(event.sources) != 1:
            raise LineageError("split 事件必须有且仅有一个来源筐")
        src = self._require_alive(event.sources[0], event)
        if not event.targets or len(event.targets) != len(event.quantities):
            raise LineageError("split 事件的 to 与 quantities 必须等长且非空")
        if any(q <= 0 for q in event.quantities):
            raise LineageError("拆分数量必须为正整数")
        if sum(event.quantities) != src.quantity:
            raise LineageError(
                f"拆分数量之和 {sum(event.quantities)} 与来源筐剩余 {src.quantity} 不守恒"
            )
        for tid in event.targets:
            if tid in self.containers:
                raise LineageError(f"目标筐 {tid} 已存在")
        remaining = src.quantity
        self._consume(src, event.at)
        src.children.extend(event.targets)
        for tid, qty in zip(event.targets, event.quantities):
            child = ContainerState(
                id=tid,
                lot=src.lot,
                produce=src.produce,
                package=src.package,
                initial_quantity=qty,
                quantity=qty,
                unit=src.unit,
                entered_at=event.at,
                parents=(src.id,),
                share_of_parent=qty / remaining,
                born_event=event.event_id,
            )
            if src.unit is not None:
                child.dwell.append(DwellInterval(tid, src.unit, event.at, None))
            self.containers[tid] = child

    def _apply_merge(self, event: ScanEvent) -> None:
        if not event.sources or not event.targets:
            raise LineageError("merge 事件的 from/to 均不能为空")
        srcs = [self._require_alive(s, event) for s in event.sources]
        if len(event.targets) != len(event.quantities) or any(q <= 0 for q in event.quantities):
            raise LineageError("merge 事件的 to 与 quantities 必须等长且数量为正")
        total = sum(s.quantity for s in srcs)
        if sum(event.quantities) != total:
            raise LineageError(f"合并数量之和 {sum(event.quantities)} 与来源剩余 {total} 不守恒")
        for tid in event.targets:
            if tid in self.containers:
                raise LineageError(f"目标筐 {tid} 已存在")
        lots = {s.lot for s in srcs}
        produces = {s.produce for s in srcs}
        packages = {s.package for s in srcs}
        unit = event.unit or srcs[0].unit
        for s in srcs:
            self._consume(s, event.at)
            s.children.extend(event.targets)
        for tid, qty in zip(event.targets, event.quantities):
            child = ContainerState(
                id=tid,
                lot=lots.pop() if len(lots) == 1 else "MIXED",
                produce=produces.pop() if len(produces) == 1 else None,
                package=packages.pop() if len(packages) == 1 else None,
                initial_quantity=qty,
                quantity=qty,
                unit=unit,
                entered_at=event.at,
                parents=tuple(s.id for s in srcs),
                share_of_parent=None,
                born_event=event.event_id,
            )
            if unit is not None:
                child.dwell.append(DwellInterval(tid, unit, event.at, None))
            self.containers[tid] = child

    def _apply_transfer(self, event: ScanEvent) -> None:
        if not event.containers or not event.to_unit:
            raise LineageError("transfer 事件必须给出 containers 与 to_unit")
        if event.to_unit not in self.units:
            raise LineageError(f"未知冷却单元 {event.to_unit}")
        for cid in event.containers:
            state = self._require_alive(cid, event)
            self._close_dwell(state, event.at)
            state.unit = event.to_unit
            state.entered_at = event.at
            state.dwell.append(DwellInterval(cid, event.to_unit, event.at, None))

    # ------------------------------------------------------------------ 谱系查询
    def ancestors(self, cid: str) -> list[str]:
        """全部祖先筐，父先子后的拓扑序。"""
        seen: set[str] = set()
        order: list[str] = []

        def visit(x: str) -> None:
            for parent in self.containers[x].parents:
                if parent not in seen:
                    seen.add(parent)
                    visit(parent)
                    order.append(parent)

        visit(cid)
        return order

    def descendants(self, cid: str) -> list[str]:
        seen: set[str] = set()
        order: list[str] = []

        def visit(x: str) -> None:
            for child in self.containers[x].children:
                if child not in seen:
                    seen.add(child)
                    order.append(child)
                    visit(child)

        visit(cid)
        return order

    def lot_containers(self, lot: str) -> list[str]:
        return [cid for cid, st in self.containers.items() if st.lot == lot]

    def lots(self) -> set[str]:
        return {st.lot for st in self.containers.values()}
