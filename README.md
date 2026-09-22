# 果蔬预冷数据约定

这里维护采收批、周转筐、冷却设备和温度探针之间的关系。数量统一以筐计，时间区间采用左闭右开语义，商品核心温度与库内环境温度分别记录。

`reference/domain.json` 包含一次拆分、转移和重新合并的扫码链。每个事件都有稳定编号与设备产生时间，接收时间只用于描述传输过程。放行规则依据品类和包装形式区分。

## 资料包结构

- `rules`：工艺阈值（目标核心温度、最大暴露时长、最小降温速率），按品类 + 包装形式匹配。
- `clocks`：设备时钟偏差，`offset_seconds = 设备钟 - 参考钟`，校正时间 = 设备时间 - 偏差；未登记的设备按 0 处理。
- `cooling_units`：冷却单元；`cooling: false` 表示接收区等非冷却区域，停留其中不计入预冷过程。
- `containers`：基础筐（采收批、数量、初始单元与进入时间）。
- `scan_events`：`split` / `merge` / `transfer` 三类事件；`transfer` 按 `to_unit` 切换筐的停留区间，跨设备转移按实际停留区间结算。
- `temperature`：探针温湿度记录；`quality` 不为 `attached` 的读数只是环境温度，不能充当商品核心温度。

## 批次判定服务

`precooling` 包提供完整的批次判定服务：

- **谱系**：扫码事件按 `event_id` 幂等，弱网重复提交不会制造第二次转移；同号不同负载视为冲突并拒绝。拆分、合并全程保持数量守恒。
- **判定**：仅用 `attached` 读数绘制核心温度曲线，判断达标时刻（线性插值）、暴露时长（进入冷却单元 → 达标）与降温速率；探针脱落区间记为异常，不参与判定。
- **放行证**：追加式存储，已签发证书保持原证据不变；后续补数触发复核时只生成更正版（版本递增、`supersedes` 链接）。
- **数量守恒**：合格数量沿谱系流动（拆分按比例、合并按求和），任意拆分合并顺序下放行数量都不超过有合格证据的实物数量。
- **反查**：`traceback(批号或筐号)` 返回祖先筐、异常区间、判定规则与相关扫码事件。

```python
from precooling import PrecoolingService

svc = PrecoolingService.from_reference("reference/domain.json")
svc.judge("CRATE-101-A")            # PASS：暴露 34.8 分钟、速率 7.05°C/h
svc.judge("CRATE-201")              # FAIL：有效读数最低 4.4°C，从未达标
svc.issue_release("CRATE-101-A", 12)
svc.traceback("LOT-26-0911")        # 从拒收批号反查祖先筐与异常区间
```

命令行查看批次汇总与反查报告：

```bash
python -m precooling
```

执行资料检查：

```bash
python -m unittest discover -s tests
```
