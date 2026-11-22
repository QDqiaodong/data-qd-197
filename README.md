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

## 已执行验证

独立环境离线构建安装、依赖一致性检查、业务测试及安装后的命令行端到端演示通过。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

领料模式另由 tests/test_stock.py 覆盖：受限路径不抢占受限叶唯一可用批次、批次到期日边界、拆分领用分数、缺货整单拒绝且不占用 request_id、重试幂等、并发争用与并发同请求、提交前后进程终止的恢复，以及原命令兼容性。分配算法另用随机小实例与暴力枚举字典序规则交叉核对。

源码入口为 `src/recipe_yield/service.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

按质量线性缩放，不做库存、包装取整、批量排程或食品安全认证。烹饪失水与毛净料修整损耗分开定义。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
