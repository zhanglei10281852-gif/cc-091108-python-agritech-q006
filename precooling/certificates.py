"""放行证：追加式存储。

已签发的放行证保持签发时刻的原证据不变；后续补数触发复核时，
只追加更正版（version 递增、supersedes 指向上一版），历史版本永不修改。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

RELEASED = "RELEASED"
REVOKED = "REVOKED"


@dataclass(frozen=True)
class Certificate:
    cert_id: str
    container_id: str
    lot: str
    version: int
    status: str  # RELEASED | REVOKED
    quantity: int
    verdict: str
    evidence: dict[str, Any]  # 签发时刻的判定快照（含曲线摘要指纹），不可变
    issued_at: datetime
    supersedes: str | None
    reason: str = ""


class CertificateStore:
    def __init__(self) -> None:
        self._certs: list[Certificate] = []

    def currents(self) -> dict[str, Certificate]:
        """每个筐的当前版本（追加式存储，后写者为最新）。"""
        out: dict[str, Certificate] = {}
        for cert in self._certs:
            out[cert.container_id] = cert
        return out

    def current(self, container_id: str) -> Certificate | None:
        return self.currents().get(container_id)

    def history(self, container_id: str) -> list[Certificate]:
        return [c for c in self._certs if c.container_id == container_id]

    def all(self) -> list[Certificate]:
        return list(self._certs)

    def issue(
        self,
        *,
        container_id: str,
        lot: str,
        quantity: int,
        verdict: str,
        evidence: dict[str, Any],
        issued_at: datetime,
    ) -> Certificate:
        cert = Certificate(
            cert_id=f"CERT-{container_id}-v1",
            container_id=container_id,
            lot=lot,
            version=1,
            status=RELEASED,
            quantity=quantity,
            verdict=verdict,
            evidence=evidence,
            issued_at=issued_at,
            supersedes=None,
            reason="首次签发",
        )
        self._certs.append(cert)
        return cert

    def correct(
        self,
        *,
        container_id: str,
        lot: str,
        status: str,
        quantity: int,
        verdict: str,
        evidence: dict[str, Any],
        issued_at: datetime,
        reason: str,
    ) -> Certificate:
        prev = self.current(container_id)
        version = 1 if prev is None else prev.version + 1
        cert = Certificate(
            cert_id=f"CERT-{container_id}-v{version}",
            container_id=container_id,
            lot=lot,
            version=version,
            status=status,
            quantity=quantity,
            verdict=verdict,
            evidence=evidence,
            issued_at=issued_at,
            supersedes=None if prev is None else prev.cert_id,
            reason=reason,
        )
        self._certs.append(cert)
        return cert

    def released_quantity(self, lot: str | None = None) -> int:
        """当前仍有效的放行总量（只统计最新版且状态为 RELEASED 的证书）。"""
        return sum(
            c.quantity
            for c in self.currents().values()
            if c.status == RELEASED and (lot is None or c.lot == lot)
        )
