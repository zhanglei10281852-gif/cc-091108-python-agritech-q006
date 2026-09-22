"""批次判定服务：扫码谱系 + 温度证据 + 判定 + 放行证的门面。

- 扫码事件按 event_id 幂等，弱网重复提交不会制造第二次转移；
- 温湿度记录按 (probe_id, container_id, at) 去重，同键不同内容视为补数更正；
- 任何数据变化都会复核相关放行证：原证保持原证据，只追加更正版；
- 放行数量永远不超过有合格证据的实物数量，与拆分合并顺序无关。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .certificates import RELEASED, REVOKED, Certificate, CertificateStore
from .clock import ClockTable
from .errors import ReleaseError, UnknownContainer
from .judgement import NO_EVIDENCE, PASS, Judge, Judgement, judgement_to_dict
from .lineage import Lineage, canonical_payload
from .model import CoolingUnit, Reading, Rule, ScanEvent, parse_dt


@dataclass
class SubmissionResult:
    applied: bool
    event_id: str | None = None
    accepted_readings: int = 0
    duplicate_readings: int = 0
    corrected_readings: int = 0
    corrections: list[Certificate] = field(default_factory=list)


class PrecoolingService:
    def __init__(
        self,
        *,
        rules: list[Rule],
        units: list[CoolingUnit],
        clocks: ClockTable,
    ) -> None:
        self.rules = list(rules)
        self.clocks = clocks
        self.lineage = Lineage({u.id: u for u in units})
        self.judge_engine = Judge(self.lineage, self.rules)
        self.certificates = CertificateStore()
        self._readings: dict[tuple[str, str, str], Reading] = {}
        self._reading_payloads: dict[tuple[str, str, str], str] = {}

    # ------------------------------------------------------------------ 装载
    @classmethod
    def from_reference(cls, path: str | Path) -> "PrecoolingService":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        svc = cls(
            rules=[Rule.from_dict(r) for r in data.get("rules", [])],
            units=[CoolingUnit.from_dict(u) for u in data.get("cooling_units", [])],
            clocks=ClockTable.from_entries(data.get("clocks", [])),
        )
        for c in data.get("containers", []):
            svc.lineage.add_base_container(
                id=c["id"],
                lot=c["harvest_lot"],
                quantity=int(c["quantity"]),
                unit=c.get("unit"),
                entered_at=parse_dt(c["entered_at"]) if c.get("entered_at") else None,
                produce=c.get("produce"),
                package=c.get("package"),
            )
        events = [(svc._parse_event(e), canonical_payload(e)) for e in data.get("scan_events", [])]
        svc.lineage.apply_all(events)
        svc.submit_readings(data.get("temperature", []))
        return svc

    def _parse_event(self, payload: dict[str, Any]) -> ScanEvent:
        device_id = payload.get("device_id")
        device_at = parse_dt(payload["device_at"])
        return ScanEvent(
            kind=payload["kind"],
            event_id=payload["event_id"],
            at=self.clocks.correct(device_id, device_at),
            device_at=device_at,
            device_id=device_id,
            sources=tuple(payload.get("from", ())),
            targets=tuple(payload.get("to", ())),
            quantities=tuple(int(q) for q in payload.get("quantities", ())),
            containers=tuple(payload.get("containers", ())),
            to_unit=payload.get("to_unit"),
            unit=payload.get("unit"),
        )

    # ------------------------------------------------------------------ 提交
    def submit_scan_event(self, payload: dict[str, Any]) -> SubmissionResult:
        """提交扫码事件；重复提交同一负载返回 applied=False，不产生第二次转移。"""
        event = self._parse_event(payload)
        applied = self.lineage.apply(event, canonical_payload(payload))
        corrections = self._refresh_certificates() if applied else []
        return SubmissionResult(applied=applied, event_id=event.event_id, corrections=corrections)

    def submit_readings(self, payloads: list[dict[str, Any]]) -> SubmissionResult:
        """提交温湿度记录；完全重复的被忽略，同键不同内容视为补数更正并触发复核。"""
        result = SubmissionResult(applied=False)
        changed = False
        for p in payloads:
            key = (p["probe_id"], p["container_id"], p["at"])
            canonical = canonical_payload(p)
            existing = self._reading_payloads.get(key)
            if existing == canonical:
                result.duplicate_readings += 1
                continue
            if p["container_id"] not in self.lineage.containers:
                raise UnknownContainer(f"温度记录引用了未知筐 {p['container_id']}")
            device_id = p.get("device_id") or p["probe_id"]
            device_at = parse_dt(p["at"])
            self._readings[key] = Reading(
                probe_id=p["probe_id"],
                container_id=p["container_id"],
                at=self.clocks.correct(device_id, device_at),
                core_c=float(p["core_c"]),
                quality=p.get("quality", "attached"),
                device_at=device_at,
                device_id=device_id,
                rh_percent=None if p.get("rh_percent") is None else float(p["rh_percent"]),
            )
            self._reading_payloads[key] = canonical
            changed = True
            if existing is None:
                result.accepted_readings += 1
            else:
                result.corrected_readings += 1
        if changed:
            result.applied = True
            result.corrections = self._refresh_certificates()
        return result

    # ------------------------------------------------------------------ 判定
    def readings_for(self, cid: str) -> list[Reading]:
        return sorted(
            (r for r in self._readings.values() if r.container_id == cid),
            key=lambda r: r.at,
        )

    def judge(self, cid: str) -> Judgement:
        if cid not in self.lineage.containers:
            raise UnknownContainer(f"未知筐 {cid}")
        return self.judge_engine.judge(cid, self.readings_for(cid))

    def qualified_quantity(self, cid: str) -> int:
        if cid not in self.lineage.containers:
            raise UnknownContainer(f"未知筐 {cid}")
        return self.judge_engine.qualified_quantity(cid, self._readings_by_container())

    def _readings_by_container(self) -> dict[str, list[Reading]]:
        by_container: dict[str, list[Reading]] = {}
        for r in self._readings.values():
            by_container.setdefault(r.container_id, []).append(r)
        return by_container

    def _release_validity(self, cid: str) -> tuple[bool, int]:
        """(是否可放行, 合格数量)：自身有证据时必须自身判定 PASS。"""
        own = self.readings_for(cid)
        qualified = self.qualified_quantity(cid)
        ok = qualified > 0 and (not own or self.judge(cid).verdict == PASS)
        return ok, qualified

    # ------------------------------------------------------------------ 放行证
    def issue_release(self, cid: str, quantity: int) -> Certificate:
        state = self.lineage.containers.get(cid)
        if state is None:
            raise UnknownContainer(f"未知筐 {cid}")
        if not state.alive:
            raise ReleaseError(f"筐 {cid} 已被拆分/合并消耗，不能放行")
        ok, qualified = self._release_validity(cid)
        current = self.certificates.current(cid)
        already = current.quantity if current and current.status == RELEASED else 0
        if not ok or quantity <= 0 or quantity > qualified - already:
            raise ReleaseError(
                f"放行 {quantity} 筐超过合格证据支持的剩余数量 {max(qualified - already, 0)} 筐"
            )
        judgement = self.judge(cid)
        return self.certificates.issue(
            container_id=cid,
            lot=state.lot,
            quantity=quantity,
            verdict=judgement.verdict,
            evidence=self._evidence_snapshot(judgement),
            issued_at=self._evidence_time(),
        )

    def _refresh_certificates(self) -> list[Certificate]:
        """数据变化后复核：已签发证书保持原样，仅在结论变化时追加更正版。"""
        corrections: list[Certificate] = []
        for cid, current in list(self.certificates.currents().items()):
            state = self.lineage.containers.get(cid)
            if state is None:
                continue
            original_qty = self.certificates.history(cid)[0].quantity
            if not state.alive:
                ok, qualified = False, 0
            else:
                ok, qualified = self._release_validity(cid)
            new_qty = min(original_qty, qualified) if ok else 0
            new_status = RELEASED if ok and new_qty > 0 else REVOKED
            judgement = self.judge(cid)
            snapshot = self._evidence_snapshot(judgement)
            digest_changed = snapshot["curve_digest"] != current.evidence.get("curve_digest")
            if new_status != current.status or new_qty != current.quantity or digest_changed:
                reason = (
                    "筐已被后续扫码事件消耗"
                    if not state.alive
                    else "后续补数触发复核，生成更正版"
                )
                corrections.append(
                    self.certificates.correct(
                        container_id=cid,
                        lot=state.lot,
                        status=new_status,
                        quantity=new_qty,
                        verdict=judgement.verdict,
                        evidence=snapshot,
                        issued_at=self._evidence_time(),
                        reason=reason,
                    )
                )
        return corrections

    def _evidence_snapshot(self, judgement: Judgement) -> dict[str, Any]:
        curve_src = "|".join(
            f"{r.at.isoformat()}:{r.core_c}:{r.quality}"
            for r in self.readings_for(judgement.container_id)
        )
        snapshot = judgement_to_dict(judgement)
        snapshot["qualified_source"] = (
            "own-judgement" if self.readings_for(judgement.container_id) else "inherited"
        )
        snapshot["curve_digest"] = hashlib.sha256(curve_src.encode("utf-8")).hexdigest()
        return snapshot

    def _evidence_time(self) -> datetime:
        """证书签发时间取当前全部证据中最新的参考时间，保证补数版本时间递增。"""
        times = [r.at for r in self._readings.values()]
        times += [e.at for e in self.lineage.events.values()]
        return max(times) if times else datetime.now(timezone.utc)

    # ------------------------------------------------------------------ 反查
    def lot_summary(self, lot: str) -> dict[str, Any]:
        containers = self.lineage.containers
        cids = [c for c, st in containers.items() if st.lot == lot]
        alive = [c for c in cids if containers[c].alive]
        return {
            "lot": lot,
            "containers": sorted(cids),
            "alive_containers": sorted(alive),
            "physical_initial": sum(
                st.initial_quantity for st in containers.values() if st.lot == lot and not st.parents
            ),
            "physical_alive": sum(containers[c].quantity for c in alive),
            "qualified": sum(self.qualified_quantity(c) for c in alive),
            "released": self.certificates.released_quantity(lot),
            "judgements": {c: self.judge(c).verdict for c in sorted(cids)},
        }

    def traceback(self, query: str) -> dict[str, Any]:
        """从拒收批号（或筐号）反查：祖先筐、异常区间、判定规则与相关扫码事件。"""
        containers = self.lineage.containers
        if query in containers:
            kind = "container"
            focus = [query]
            involved = {query, *self.lineage.ancestors(query)}
        else:
            cids = self.lineage.lot_containers(query)
            if not cids:
                raise UnknownContainer(f"无法反查：{query} 既不是筐号也不是采收批号")
            kind = "lot"
            focus = [c for c in sorted(cids) if self.judge(c).verdict == "FAIL"]
            involved = set(cids)
            for c in focus:
                involved |= set(self.lineage.ancestors(c))

        def touches(event: ScanEvent) -> bool:
            ids = set(event.sources) | set(event.targets) | set(event.containers)
            return bool(ids & involved)

        events = sorted(
            (e for e in self.lineage.events.values() if touches(e)),
            key=lambda e: (e.at, e.event_id),
        )
        rules = {
            id(j.rule): j.rule
            for j in (self.judge(c) for c in involved)
            if j.rule is not None
        }
        return {
            "query": query,
            "kind": kind,
            "focus": focus,
            "ancestors": {c: self.lineage.ancestors(c) for c in focus},
            "containers": {
                c: {
                    "lot": containers[c].lot,
                    "initial_quantity": containers[c].initial_quantity,
                    "quantity": containers[c].quantity,
                    "alive": containers[c].alive,
                    "parents": list(containers[c].parents),
                    "children": list(containers[c].children),
                    "qualified": self.qualified_quantity(c),
                }
                for c in sorted(involved)
            },
            "judgements": {c: judgement_to_dict(self.judge(c)) for c in sorted(involved)},
            "events": [
                {
                    "event_id": e.event_id,
                    "kind": e.kind,
                    "at": e.at.isoformat(),
                    "device_at": e.device_at.isoformat(),
                    "device_id": e.device_id,
                    "from": list(e.sources),
                    "to": list(e.targets),
                    "quantities": list(e.quantities),
                    "containers": list(e.containers),
                    "to_unit": e.to_unit,
                }
                for e in events
            ],
            "rules": [
                {
                    "produce": r.produce,
                    "package": r.package,
                    "target_core_c": r.target_core_c,
                    "max_exposure_minutes": r.max_exposure_minutes,
                    "min_cooling_rate_c_per_hour": r.min_cooling_rate_c_per_hour,
                }
                for r in rules.values()
            ],
        }
