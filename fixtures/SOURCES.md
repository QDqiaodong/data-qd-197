# 样例来源

全部样例为作者自制合成数据，采用本仓MIT许可。遵循本仓docs/contract.md明确的有界模型，不来自客户、真实样本或生产作业。

- `input.json`：原核算命令的汤品样例（番茄、奶油、酱汁、汤）。
- `lots.json`：领料库初始化样例，三个 flour 批次 A/B/C，各 8 克、到期日依次顺延、单价 2/3/4、唯一标记 plant/shared/reserve。
- `issue.json`：领料请求样例，soup/sauce/flour 嵌套配方与 lunch 工单，仅第二叶 `["soup:1","sauce:0"]` 禁用 shared 标记；与 lots.json 组合演示受限分配（第一叶领 B、第二叶领 A、总成本 40）。
- `preflight.json`：印前标记核对样例，base 带 local、plain 不带；mix（4 克）与 tray（8 克，两个 mix）嵌套，A/B 两张 8 克 tray 工单共用只印 local 的印版。A 在 `["tray:0"]` suppress local、在 `["tray:0","mix:0"]` restore，B 无声明；两单有效 local 净重均为 6、出现 2 次，印刷批放行。
- `rework.json`：返工对账样例。R1 发出 A4/B4/C7，R2 另发 A1；M0 由 R1 的 A4+C4 混出 6 克，M1 用 M0 的 2 克、R2 的 A1、R1 的 C1 混出 3 克，M2 用 R1 的 C2 混出 2 克，P1/P2/P3 分别从 M1/M0/M2 打包 1/2/2 克归需求 5 克的订单 O，P1 已发；第 8 条把 M0 改为 B4+C4。比较 7 与 8：待核批 A 下 P1 归因 1/2→1/4 仍追查，P2 由归因 1 的暂扣变归因 0 的备货，P3 始终归因 0，处置变动只有 P2。
- `rework-overdraw.json`：承接上一时间线，第 9 条把 M0 改为 B3+C5、第 10 条恢复 B4+C4，`as_known` 为 10。第 9 条单笔 C5 不超 C7，但加上 M1、M2 已用的 C3 达 8，故即使第 10 条恢复也须拒绝退出 2、不发报告。
- `settle-lots.json`：结算账本样例，单批 L（flour），买入毛重 7 克。
- `settle-close-1.json` / `settle-close-2.json` / `settle-close-3.json`：同一账本的三期结账样例。p1 归档收据 R1（工单 A 跨两条叶共领 3 克、工单 B 领 3 克）并确认累计货款 3 分，凭证 A、B、未领库存各 +1；p2 无新收据、累计确认升到 4 分，差额为 A +1、B +1、库存 −1；p3 归档 R3（工单 C 领剩余 1 克）并把累计确认降回 3 分，差额为 A −1、B −1、C +1，库存桶因全部领完而消失。
