# 样例来源

全部样例为作者自制合成数据，采用本仓MIT许可。遵循本仓docs/contract.md明确的有界模型，不来自客户、真实样本或生产作业。

- `input.json`：原核算命令的汤品样例（番茄、奶油、酱汁、汤）。
- `lots.json`：领料库初始化样例，三个 flour 批次 A/B/C，各 8 克、到期日依次顺延、单价 2/3/4、唯一标记 plant/shared/reserve。
- `issue.json`：领料请求样例，soup/sauce/flour 嵌套配方与 lunch 工单，仅第二叶 `["soup:1","sauce:0"]` 禁用 shared 标记；与 lots.json 组合演示受限分配（第一叶领 B、第二叶领 A、总成本 40）。
- `preflight.json`：印前标记核对样例，base 带 local、plain 不带；mix（4 克）与 tray（8 克，两个 mix）嵌套，A/B 两张 8 克 tray 工单共用只印 local 的印版。A 在 `["tray:0"]` suppress local、在 `["tray:0","mix:0"]` restore，B 无声明；两单有效 local 净重均为 6、出现 2 次，印刷批放行。
