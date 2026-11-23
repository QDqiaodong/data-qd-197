# data-qd-197 · 嵌套配方毛净重与成本

由原料净得率、嵌套配方与出品工单，核算采购毛重、加工损耗、成本及原料标签的来源路径。

## 安装与运行

Python >=3.11，实际验证 CPython 3.12.14 + uv 0.12.19。运行依赖仅标准库；构建固定 setuptools 80.9.0、wheel 0.45.1。每个项目独立虚拟环境，公共下载缓存可以复用。

在本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/recipe-yield --source fixtures/input.json --output run/demo.json
```

以上安装命令已在本地公共缓存存在时真实执行。首次使用且缓存为空时须先准备 Python 和锁定构建包，或在允许联网的安装环境移除对应 `--offline`；运行与测试不需要网络。`.venv` 互不共享，未纳入Git。

样例400克汤的采购：番茄毛重400克、净重320克、修整损耗80克；奶油160克，总成本16。

演示标准输出摘要：

```json
{"cost": "16", "ingredients": 2, "orders": 1}
```

完整证据写入 `run/demo.json`。数值输入为契约约定的十进制字符串或整数分数对，输出的精确有理数采用整数或 `n/d` 字符串，避免浮点舍入。

## 离线领料

在只读核算之外，CLI 可管理一个本地领料库，按工单从在库批次整单发料：

```sh
.venv/bin/recipe-yield stock-init --ledger run/ledger --source fixtures/lots.json
.venv/bin/recipe-yield stock-show --ledger run/ledger
.venv/bin/recipe-yield stock-issue --ledger run/ledger --source fixtures/issue.json
```

`stock-init` 只创建新库，不覆盖已有路径；`stock-show` 打印各批次余额（含零余额批次）且不扣料；`stock-issue` 成功时在 stdout 打印逐叶收据并原子提交扣减，缺货或请求冲突返回2。同 ID 同内容重试返回逐字节相同的原始收据且不重复扣料；同 ID 不同内容拒绝。库目录可被同机多个进程并发使用，提交等价于某个串行顺序。领料样例中第二叶禁用 `shared` 标记，第一叶领 B 批 8 克、第二叶领 A 批 8 克，总成本 40，C 批不扣。

分配规则、收据格式、提交与恢复语义、严格校验和退出码见 [数据契约](docs/contract.md) 的离线领料契约一节。

## 返工对账

中央厨房可用 `rework-reconcile` 离线重建某次内部质量核查的实物流，对比后来登记的更正，告诉仓库哪些包件该暂扣、哪些已发包件需要追查、哪些订单要准备补货：

```sh
.venv/bin/recipe-yield rework-reconcile --source fixtures/rework.json --output run/rework.json
```

输入由原 `stock-issue` 收据、发运订单、登记事件（`mix`/`pack`/`ship`/`amend`）、待核批次与两个截点 `compare_to`/`as_known` 组成。命令按实际投料而非理论配方传递归因，在任一可见前缀校验收据额度与半成品产出不透支，分别给出两个截点的包件处置与订单缺口，并列出处置变动。它只供内部运营调度，不作食品安全或合规判断；不改写任何收据或领料库。

以上样例比较第 7 与第 8 条：第 8 条把 M0 由 A4+C4 改为 B4+C4，包件 P2 由 `hold`（归因 1）变为 `ready`（归因 0），是唯一处置变动；订单缺口由 3 降到 1。`fixtures/rework-overdraw.json` 演示后续更正掩盖不了第 9 条的 C 超额，命令退出 2 且不发报告。字段、身份、归因口径、报告形状与退出码见 [数据契约](docs/contract.md) 的返工对账契约一节。

## 货款结算

财务可以用 `settle-*` 三个离线命令维护一个独立的结算账本，把供应商按批次的累计确认货款分摊给实际领用的加工工单和剩余库存，只把期际差额封进凭证：

```sh
.venv/bin/recipe-yield settle-init   --book run/settle --source settle-lots.json
.venv/bin/recipe-yield settle-close  --book run/settle --source close-1.json
.venv/bin/recipe-yield settle-export --book run/settle --period p1 --output run/voucher-p1.json
```

- `settle-init` 用 `{lots:[{lot_id,ingredient,received_grams}]}` 建账，批次表 1–200 条、lot_id 唯一、`received_grams` 为正十进制毛重字符串；账本路径已存在即拒绝，不覆盖。批次表建后不改写、不追加，不处理退料。
- `settle-close` 输入 `{period,previous,receipts,confirmed}`。`receipts` 是 0–100 张原 `stock-issue` 实领收据（不读领料库、不用收据估价）；`confirmed` 每批恰好一条 `{lot_id,cents}`，cents 是截至本期的累计确认分（0 至 10¹² 的 JSON 整数，可下调）。成功在 stdout 返回 `{period,previous}`，凭证只封存在账本内，不直接写投递文件。
- `settle-export` 把指定已结期凭证交付到输出路径：新路径重复导出逐字节相同；已有普通文件只有字节完全一致才算成功；目录、符号链接一律拒绝；失败不撤销结账。

分摊以批次为单位独立计算：归档收据的 `draws.gross` 按加工工单 ID 跨路径、跨收据合并，未领库存权重为买入毛重减累计领用；按重量占买入毛重的比例用最大余数法分整数分，余数同分按 order_id ASCII、库存桶排在工单之后，零重量桶不参与。凭证 entries 是本期累计目标分摊相对上期封存值的**有符号差额**（负差额不截零），库存桶用 `order_id:null`，零差额不列。累计领用超买入毛重、跨期重报 request_id、同收据内重复叶身份 `(order_id,path)`、未知批次或原料不符等整单拒绝，且不移动结账位置。

相同 period 同内容重试返回原结果且不再消耗收据（即使 previous 已不是链头）；同 period 不同内容拒绝；并发争用同一 previous 最多一笔成功。提交前进程终止不留占用的 period、收据或凭证；提交后 stdout 丢失也可重试或继续结下一期。期次链允许在前期凭证尚未投递时继续结账，财务凭凭证内 previous 识别入账顺序。字段、口径、恢复与退出码见 [数据契约](docs/contract.md) 的货款结算契约一节。

## 印前标记核对

印刷站可用 `label-preflight` 离线核对一批订单能否共用同一版商品标记，并解释每个标记来自哪个加工分支：

```sh
.venv/bin/recipe-yield label-preflight --source fixtures/preflight.json --output run/preflight.json
```

输入在原 `ingredients`/`recipes`/`orders` 之外，必填 `thresholds`（阈值标记）、`declarations`（按订单与组件路径的 suppress/restore 声明）和 `print_jobs`（现有印版与订单分批）。命令逐订单给出 `total_net`、应印标记与逐标记依据（有效净重、出现次数、第一条有效叶路径），并对每张印刷批给出 `approved` 及逐单的漏印 `missing`/多印 `extra`。这些标记只用于内部商品披露，不用于食品安全或法定合规判断。

核算按子配方标称产出逐层缩放，分母是全部原料叶净重之和；suppress/restore 沿路径继承、最长前缀匹配，共享子配方的一次出现被声明不影响其他出现。反复嵌套的共享配方不会物化全部叶：只沿声明路径显式展开，其余子树记忆化处理，因此不沿用旧命令每订单 20000 条叶的展开上限，45 层指数嵌套（单订单逾 8.7 万亿有效叶）在单次 15 秒时限内完成。字段、口径、报告形状与退出码见 [数据契约](docs/contract.md) 的印前标记核对契约一节。

## 已执行验证

独立环境离线构建安装、依赖一致性检查、业务测试及安装后的命令行端到端演示通过。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

领料模式另由 tests/test_stock.py 覆盖：受限路径不抢占受限叶唯一可用批次、批次到期日边界、拆分领用分数、缺货整单拒绝且不占用 request_id、重试幂等、并发争用与并发同请求、提交前后进程终止的恢复，以及原命令兼容性。分配算法另用随机小实例与暴力枚举字典序规则交叉核对。

结算模式由 tests/test_settle.py 覆盖：精确分数重量与最大余数同余（库存桶排在同分工单后）、三期目标分摊的跨期冲减与负差额、直接消费 `stock-issue` 原始收据、档案身份（叶身份、同叶批次、request_id 跨期/同请求重复、未知批次与原料不符）、超重拒绝不移动结账位置、同 period 幂等与内容冲突、链头约束、导出字节一致与目录/符号链接/异文件拒绝，以及提交前后与投递后的中断恢复和同 previous 并发争用（故障注入点 `RECIPE_YIELD_CRASHPOINT`，不靠休眠）。

源码入口为 `src/recipe_yield/service.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

按质量线性缩放，不做库存、包装取整、批量排程或食品安全认证。烹饪失水与毛净料修整损耗分开定义。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
