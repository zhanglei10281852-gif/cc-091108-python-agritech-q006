import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from precooling import (  # noqa: E402
    ClockTable,
    CoolingUnit,
    IdempotencyConflict,
    LineageError,
    PrecoolingService,
    ReleaseError,
    Rule,
    UnknownContainer,
)
from precooling.lineage import canonical_payload  # noqa: E402
from precooling.model import parse_dt  # noqa: E402

REFERENCE = Path(__file__).parents[1] / "reference" / "domain.json"
LOT = "LOT-26-0911"


def reference_data():
    return json.loads(REFERENCE.read_text(encoding="utf-8"))


def event_payload(event_id):
    return next(e for e in reference_data()["scan_events"] if e["event_id"] == event_id)


def make_service():
    rules = [
        Rule(
            produce="鲜食玉米",
            package="周转筐",
            target_core_c=4.0,
            max_exposure_minutes=35,
            min_cooling_rate_c_per_hour=3.0,
        )
    ]
    units = [
        CoolingUnit("UNIT-RECV", "receiving", False),
        CoolingUnit("UNIT-VC-1", "vacuum_cooling", True),
    ]
    return PrecoolingService(rules=rules, units=units, clocks=ClockTable({}))


def add_base(svc, cid, qty, unit="UNIT-VC-1", entered="2026-09-11T10:00:00+08:00"):
    svc.lineage.add_base_container(
        id=cid,
        lot="LOT-T",
        quantity=qty,
        unit=unit,
        entered_at=parse_dt(entered),
        produce="鲜食玉米",
        package="周转筐",
    )


def submit_curve(svc, cid, rows, probe="P-1"):
    return svc.submit_readings(
        [
            {
                "probe_id": probe,
                "container_id": cid,
                "at": f"2026-09-11T{at}+08:00",
                "core_c": core,
                "quality": quality,
            }
            for at, core, quality in rows
        ]
    )


PASSING_CURVE = [("10:05:00", 8.0, "attached"), ("10:15:00", 5.0, "attached"), ("10:25:00", 3.8, "attached")]


class ClockSkewTest(unittest.TestCase):
    def setUp(self):
        self.svc = PrecoolingService.from_reference(REFERENCE)

    def test_scanner_clock_is_corrected(self):
        # SCANNER-A 快 90 秒：设备 11:35:00 -> 参考 11:33:30
        scan3 = self.svc.lineage.events["SCAN-3"]
        self.assertEqual(scan3.at.isoformat(), "2026-09-11T11:33:30+08:00")
        # 未登记设备号的 SCAN-1 不校正
        scan1 = self.svc.lineage.events["SCAN-1"]
        self.assertEqual(scan1.at.isoformat(), "2026-09-11T11:10:00+08:00")

    def test_probe_clock_is_corrected(self):
        # CORE-7 慢 60 秒：设备 11:45:00 -> 参考 11:46:00
        readings = {r.device_at.isoformat(): r for r in self.svc.readings_for("CRATE-101-A")}
        self.assertEqual(
            readings["2026-09-11T11:45:00+08:00"].at.isoformat(),
            "2026-09-11T11:46:00+08:00",
        )


class JudgementTest(unittest.TestCase):
    def setUp(self):
        self.svc = PrecoolingService.from_reference(REFERENCE)

    def test_pass_judgement_uses_only_attached_readings(self):
        j = self.svc.judge("CRATE-101-A")
        self.assertEqual(j.verdict, "PASS")
        self.assertEqual(j.cooling_entry.isoformat(), "2026-09-11T11:33:30+08:00")
        self.assertEqual(j.target_reached_at.isoformat(), "2026-09-11T12:08:20+08:00")
        self.assertAlmostEqual(j.exposure_minutes, 34 + 50 / 60, places=3)
        self.assertAlmostEqual(j.cooling_rate_c_per_hour, 7.0515, places=3)
        self.assertEqual(j.readings_used, 6)  # 脱落读数不计入
        self.assertEqual(j.min_core_c, 3.3)
        detached = [a for a in j.anomalies if a.kind == "probe-detached"]
        self.assertEqual(len(detached), 1)
        self.assertEqual(detached[0].start.isoformat(), "2026-09-11T11:51:00+08:00")
        self.assertEqual(detached[0].end.isoformat(), "2026-09-11T11:56:00+08:00")

    def test_fail_judgement_never_reached_target(self):
        j = self.svc.judge("CRATE-201")
        self.assertEqual(j.verdict, "FAIL")
        self.assertIsNone(j.target_reached_at)
        self.assertEqual(j.min_core_c, 4.4)  # 脱落的 3.8°C 不能冒充商品温度
        self.assertEqual(j.readings_used, 6)
        self.assertAlmostEqual(j.exposure_minutes, 96.5, places=3)
        self.assertAlmostEqual(j.cooling_rate_c_per_hour, 2.8, places=3)
        kinds = [a.kind for a in j.anomalies]
        self.assertIn("probe-detached", kinds)
        self.assertIn("above-target", kinds)
        self.assertIn("exposure-exceeded", kinds)

    def test_detached_reading_cannot_fake_target_crossing(self):
        # 唯一一条低于目标的读数来自脱落探针：若被采信会伪造出 15 分钟达标
        svc = make_service()
        add_base(svc, "C1", 10)
        submit_curve(
            svc,
            "C1",
            [
                ("10:05:00", 6.0, "attached"),
                ("10:15:00", 3.5, "detached"),
                ("10:25:00", 5.2, "attached"),
                ("10:35:00", 4.8, "attached"),
            ],
        )
        j = svc.judge("C1")
        self.assertEqual(j.verdict, "FAIL")
        self.assertIsNone(j.target_reached_at)
        self.assertEqual(j.min_core_c, 4.8)
        self.assertEqual(j.readings_used, 3)

    def test_cross_unit_settlement_by_actual_dwell(self):
        j = self.svc.judge("CRATE-101-A")
        units = [(s.unit, s.attached_readings, s.detached_readings) for s in j.settlement]
        self.assertEqual(
            units,
            [("UNIT-RECV", 0, 0), ("UNIT-VC-1", 5, 1), ("UNIT-COLD-2", 1, 0)],
        )
        # 停留区间首尾相接、左闭右开
        for first, second in zip(j.settlement, j.settlement[1:]):
            self.assertEqual(first.end, second.start)
        self.assertIsNone(j.settlement[-1].end)
        j201 = self.svc.judge("CRATE-201")
        units201 = [(s.unit, s.attached_readings, s.detached_readings) for s in j201.settlement]
        self.assertEqual(units201, [("UNIT-RECV", 0, 0), ("UNIT-COLD-2", 6, 1)])


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = PrecoolingService.from_reference(REFERENCE)

    def test_duplicate_merge_is_ignored(self):
        before = len(self.svc.lineage.events)
        result = self.svc.submit_scan_event(event_payload("SCAN-2"))
        self.assertFalse(result.applied)
        self.assertEqual(len(self.svc.lineage.events), before)
        self.assertEqual(self.svc.lineage.containers["CRATE-201"].quantity, 28)

    def test_duplicate_transfer_does_not_create_second_interval(self):
        result = self.svc.submit_scan_event(event_payload("SCAN-4"))
        self.assertFalse(result.applied)
        dwell = self.svc.lineage.containers["CRATE-201"].dwell
        self.assertEqual(len(dwell), 2)
        self.assertEqual([d.unit for d in dwell], ["UNIT-RECV", "UNIT-COLD-2"])

    def test_conflicting_payload_with_same_event_id_is_rejected(self):
        bad = dict(event_payload("SCAN-2"), quantities=[29])
        with self.assertRaises(IdempotencyConflict):
            self.svc.submit_scan_event(bad)
        self.assertEqual(self.svc.lineage.containers["CRATE-201"].quantity, 28)

    def test_duplicate_reading_is_ignored(self):
        reading = reference_data()["temperature"][0]
        result = self.svc.submit_readings([reading])
        self.assertFalse(result.applied)
        self.assertEqual(result.duplicate_readings, 1)
        self.assertEqual(result.accepted_readings, 0)

    def test_non_conserving_split_is_rejected(self):
        svc = make_service()
        add_base(svc, "P", 24)
        with self.assertRaises(LineageError):
            svc.submit_scan_event(
                {
                    "event_id": "S-BAD",
                    "kind": "split",
                    "from": ["P"],
                    "to": ["X", "Y"],
                    "quantities": [11, 12],
                    "device_at": "2026-09-11T11:00:00+08:00",
                }
            )


class QualifiedFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = PrecoolingService.from_reference(REFERENCE)

    def test_qualified_quantities_in_reference_pack(self):
        self.assertEqual(self.svc.qualified_quantity("CRATE-101-A"), 12)
        self.assertEqual(self.svc.qualified_quantity("CRATE-201"), 0)
        self.assertEqual(self.svc.qualified_quantity("CRATE-101-B"), 0)
        summary = self.svc.lot_summary(LOT)
        self.assertEqual(summary["physical_initial"], 40)
        self.assertEqual(summary["physical_alive"], 40)
        self.assertEqual(summary["qualified"], 12)
        self.assertEqual(summary["released"], 0)

    def test_split_children_inherit_parent_qualification(self):
        svc = make_service()
        add_base(svc, "P", 24)
        submit_curve(svc, "P", PASSING_CURVE)
        svc.submit_scan_event(
            {
                "event_id": "S1",
                "kind": "split",
                "from": ["P"],
                "to": ["A", "B"],
                "quantities": [12, 12],
                "device_at": "2026-09-11T11:00:00+08:00",
            }
        )
        self.assertEqual(svc.qualified_quantity("A"), 12)
        self.assertEqual(svc.qualified_quantity("B"), 12)
        svc.issue_release("A", 12)
        svc.issue_release("B", 12)
        self.assertEqual(svc.certificates.released_quantity("LOT-T"), 24)
        with self.assertRaises(ReleaseError):
            svc.issue_release("B", 1)

    def test_merge_inherits_sum_of_qualified_parents(self):
        svc = make_service()
        add_base(svc, "A", 10)
        submit_curve(svc, "A", PASSING_CURVE)
        add_base(svc, "B", 6)  # 无温度证据
        svc.submit_scan_event(
            {
                "event_id": "M1",
                "kind": "merge",
                "from": ["A", "B"],
                "to": ["M"],
                "quantities": [16],
                "device_at": "2026-09-11T11:00:00+08:00",
            }
        )
        self.assertEqual(svc.qualified_quantity("M"), 10)
        svc.issue_release("M", 10)
        with self.assertRaises(ReleaseError):
            svc.issue_release("M", 1)

    def test_release_then_split_revokes_and_conserves(self):
        svc = make_service()
        add_base(svc, "P", 24)
        submit_curve(svc, "P", PASSING_CURVE)
        svc.issue_release("P", 24)
        result = svc.submit_scan_event(
            {
                "event_id": "S1",
                "kind": "split",
                "from": ["P"],
                "to": ["A", "B"],
                "quantities": [12, 12],
                "device_at": "2026-09-11T11:00:00+08:00",
            }
        )
        # 原放行证被更正版撤销，放行额度随谱系流入子筐
        self.assertEqual(len(result.corrections), 1)
        self.assertEqual(result.corrections[0].status, "REVOKED")
        self.assertEqual(result.corrections[0].quantity, 0)
        self.assertEqual(svc.certificates.released_quantity("LOT-T"), 0)
        svc.issue_release("A", 12)
        svc.issue_release("B", 12)
        self.assertEqual(svc.certificates.released_quantity("LOT-T"), 24)
        with self.assertRaises(ReleaseError):
            svc.issue_release("A", 1)

    def test_event_and_reading_order_does_not_change_outcome(self):
        data = reference_data()
        svc1 = PrecoolingService.from_reference(REFERENCE)
        svc2 = PrecoolingService(
            rules=[Rule.from_dict(r) for r in data["rules"]],
            units=[CoolingUnit.from_dict(u) for u in data["cooling_units"]],
            clocks=ClockTable.from_entries(data["clocks"]),
        )
        for c in data["containers"]:
            svc2.lineage.add_base_container(
                id=c["id"],
                lot=c["harvest_lot"],
                quantity=int(c["quantity"]),
                unit=c.get("unit"),
                entered_at=parse_dt(c["entered_at"]) if c.get("entered_at") else None,
                produce=c.get("produce"),
                package=c.get("package"),
            )
        events = [(svc2._parse_event(e), canonical_payload(e)) for e in reversed(data["scan_events"])]
        svc2.lineage.apply_all(events)
        svc2.submit_readings(list(reversed(data["temperature"])))
        self.assertEqual(svc1.lot_summary(LOT), svc2.lot_summary(LOT))


class CertificateTest(unittest.TestCase):
    def setUp(self):
        self.svc = PrecoolingService.from_reference(REFERENCE)

    def test_over_release_is_rejected(self):
        with self.assertRaises(ReleaseError):
            self.svc.issue_release("CRATE-101-A", 13)
        self.svc.issue_release("CRATE-101-A", 12)
        with self.assertRaises(ReleaseError):
            self.svc.issue_release("CRATE-101-A", 1)
        with self.assertRaises(ReleaseError):
            self.svc.issue_release("CRATE-201", 1)  # 判定 FAIL
        with self.assertRaises(ReleaseError):
            self.svc.issue_release("CRATE-101", 1)  # 已被拆分消耗

    def test_late_data_only_creates_corrected_version(self):
        cert1 = self.svc.issue_release("CRATE-101-A", 12)
        self.assertEqual(cert1.version, 1)
        self.assertEqual(cert1.status, "RELEASED")
        self.assertAlmostEqual(cert1.evidence["exposure_minutes"], 34.8333, places=3)

        # 补数：两条关键读数被探针巡检更正为脱落状态
        result = self.svc.submit_readings(
            [
                {
                    "probe_id": "CORE-7",
                    "container_id": "CRATE-101-A",
                    "at": "2026-09-11T12:04:00+08:00",
                    "core_c": 4.2,
                    "quality": "detached",
                },
                {
                    "probe_id": "CORE-7",
                    "container_id": "CRATE-101-A",
                    "at": "2026-09-11T12:09:00+08:00",
                    "core_c": 3.9,
                    "quality": "detached",
                },
            ]
        )
        self.assertEqual(result.corrected_readings, 2)
        self.assertEqual(len(result.corrections), 1)
        cert2 = result.corrections[0]
        self.assertEqual(cert2.version, 2)
        self.assertEqual(cert2.status, "REVOKED")
        self.assertEqual(cert2.quantity, 0)
        self.assertEqual(cert2.supersedes, cert1.cert_id)
        self.assertAlmostEqual(cert2.evidence["exposure_minutes"], 45.0, places=3)

        # 原证保持签发时的原证据，不被改写
        history = self.svc.certificates.history("CRATE-101-A")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], cert1)
        self.assertEqual(history[0].status, "RELEASED")
        self.assertEqual(history[0].quantity, 12)
        self.assertNotEqual(
            history[0].evidence["curve_digest"], history[1].evidence["curve_digest"]
        )
        self.assertEqual(self.svc.certificates.released_quantity(LOT), 0)
        with self.assertRaises(ReleaseError):
            self.svc.issue_release("CRATE-101-A", 1)


class TracebackTest(unittest.TestCase):
    def setUp(self):
        self.svc = PrecoolingService.from_reference(REFERENCE)

    def test_traceback_from_rejected_lot(self):
        report = self.svc.traceback(LOT)
        self.assertEqual(report["kind"], "lot")
        self.assertEqual(report["focus"], ["CRATE-201"])
        self.assertEqual(
            set(report["ancestors"]["CRATE-201"]),
            {"CRATE-101", "CRATE-101-B", "CRATE-102"},
        )
        j201 = report["judgements"]["CRATE-201"]
        kinds = [a["kind"] for a in j201["anomalies"]]
        self.assertIn("probe-detached", kinds)
        self.assertIn("above-target", kinds)
        self.assertEqual(report["rules"][0]["target_core_c"], 4.0)
        event_ids = {e["event_id"] for e in report["events"]}
        self.assertEqual(event_ids, {"SCAN-1", "SCAN-2", "SCAN-3", "SCAN-4", "SCAN-5"})

    def test_traceback_from_container(self):
        report = self.svc.traceback("CRATE-201")
        self.assertEqual(report["kind"], "container")
        self.assertEqual(
            set(report["ancestors"]["CRATE-201"]),
            {"CRATE-101", "CRATE-101-B", "CRATE-102"},
        )
        # SCAN-4 是 CRATE-201 自身的转移事件；SCAN-3/SCAN-5 只涉及 CRATE-101-A，不在其谱系内
        self.assertEqual(
            {e["event_id"] for e in report["events"]}, {"SCAN-1", "SCAN-2", "SCAN-4"}
        )
        with self.assertRaises(UnknownContainer):
            self.svc.traceback("LOT-NOPE")


if __name__ == "__main__":
    unittest.main()
