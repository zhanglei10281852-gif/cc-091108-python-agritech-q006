"""python -m precooling [资料包路径] — 打印每个采收批的判定汇总与反查报告。"""
from __future__ import annotations

import json
import sys

from .service import PrecoolingService


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "reference/domain.json"
    svc = PrecoolingService.from_reference(path)
    for lot in sorted(svc.lineage.lots()):
        summary = svc.lot_summary(lot)
        print(f"== 采收批 {lot} ==")
        print(
            f"实物 {summary['physical_alive']}/{summary['physical_initial']} 筐，"
            f"合格证据支持 {summary['qualified']} 筐，已放行 {summary['released']} 筐"
        )
        for cid, verdict in summary["judgements"].items():
            print(f"  {cid}: {verdict}")
        report = svc.traceback(lot)
        for cid in report["focus"]:
            j = report["judgements"][cid]
            print(f"-- 拒收反查 {cid}（祖先筐: {', '.join(report['ancestors'][cid]) or '无'}）")
            for reason in j["reasons"]:
                print(f"   原因: {reason}")
            for a in j["anomalies"]:
                print(f"   异常区间: [{a['start']}, {a['end']}) {a['kind']} {a['detail']}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
