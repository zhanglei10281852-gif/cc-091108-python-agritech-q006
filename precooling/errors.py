"""领域异常。"""


class DomainError(Exception):
    """批次判定服务的基础异常。"""


class LineageError(DomainError):
    """扫码事件违反谱系约束（数量不守恒、筐不存在、类型未知等）。"""


class IdempotencyConflict(LineageError):
    """同一 event_id 携带了不同负载：拒绝后者，保留先到的记录。"""


class UnknownContainer(LineageError):
    """引用了不存在的筐/批号。"""


class ReleaseError(DomainError):
    """放行数量超过合格证据支持的实物数量，或筐状态不允许放行。"""
