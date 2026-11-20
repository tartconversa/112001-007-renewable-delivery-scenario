# 居民电气化援助分析

用于排定居民改造援助的合成匿名申请、账户账单、住房情况和安装方案记录。
应用只使用 Python 标准库，输入为 `data/` 下的 CSV，所有判断都保留可追溯的
数据依据，不在缺失资料上补猜。

## 运行环境

需要 Python 3.11 或更新版本。

## 快速开始

```bash
# 完整性、优先分组、三态状态、名额/预算方案比较
python -m residential_aid all

# 单项报告
python -m residential_aid quality       # 数据质量问题与记录数
python -m residential_aid completeness  # 每户完整性、可支持范围
python -m residential_aid priority      # 区间级优先分组（不是家庭排序）
python -m residential_aid status        # 补助 / 安装 / 实测 三态
python -m residential_aid plans         # 不同名额和预算安排的影响
```

`--data DIR` 指定输入目录（默认 `data`）；`--state FILE` 指定会话状态文件
（默认 `<data>/session.state.json`）。只读命令不会写状态文件。

### 登记与追踪

```bash
python -m residential_aid approve A02 --by staff-9
python -m residential_aid install A02 2026-07-05
python -m residential_aid revise-bill E02 2026-01 -120 --correction-of B02 --reason "验表"
python -m residential_aid delay A03 2026-08-15 --reason "服务商改期"
python -m residential_aid publish 2026-Q3-baseline
python -m residential_aid impact
```

登记结果写入会话状态文件，不改写 `data/` 中的 CSV。`publish` 保存当时的
数据指纹与决策依据；之后修改账单或改期，`impact` 会列出需要复核的审批、
需要释放的名额以及需要更正/撤回的已公布成效，而已发布记录本身保持不变。

## 领域规则

- **账单修订不等于新增用能。** 同一账单期间的换发账单只计最新一版；`correction`
  行按签名的差额调整该期间；指向另一期间的冲正不会当作本期间用能。修订目标
  本身已被换发时，该差额暂不生效并标记待人工确认，以免重复计算。
- **共享账户不重复分配。** 未提供 `shared_allocations.csv` 行（账户、户号、
  份额、依据）时，整张账单不归给任何一户，而是报为不可归属；份额合计超过 1
  或未覆盖全部住户时同样不归属。
- **三种状态分开。** 补助获批（`grant_status`）、设备安装（`installed_on`）、
  账单实测变化（`measurement_status`）互不推定。申请表上的 `post_kwh` 只是
  预期值；只有在安装之后的账单期间存在、且账户可归属时，才会与安装前均值比较
  并报告为实测结果。
- **区间收入不外推。** 收入只按 `income_band` 分组；缺失即列为不可排序。组内按
  户号字母序排列，明确标注不是家庭排序。账单高并不自动提升分组，只作为需求
  信号单独列出。
- **同一户上限针对全部措施。** 每户补助上限按该户全部措施合计计算，超出部分
  会削减并注明，不会按措施重复享受上限。
- **已批准是承诺。** 名额安排比较不会撤销已批准记录；预算或名额不足时列入
  `committed_at_risk` 并解释原因。

## 本地验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q residential_aid tests
```

## 数据说明

`data/` 目录包含少量合成输入记录，字段范围见其中的说明文件。所有记录均为虚构。
可选文件 `shared_allocations.csv` 的列为
`account_token,household_token,share,basis`；未提供时共享账户保持不可归属。
