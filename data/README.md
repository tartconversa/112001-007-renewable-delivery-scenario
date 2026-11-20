# 合成输入数据

用于排定居民改造援助的合成匿名申请、账户账单、住房情况和安装方案记录。

所有记录均为虚构，仅供本地开发使用。日期与数值均为示例。

## 文件

| 文件 | 内容 | 关键规则 |
| --- | --- | --- |
| `households_accounts.csv` | 匿名家庭、收入区间、人数、供电账户、共用账户标记、同意状态 | `income_band` 只有区间；`shared_account=yes` 且多户共用账户时不自动分户 |
| `bills.csv` | 供电账户分期账单 | 同一期间换发只计最新一版；`adjustment_of` 指向的冲正按差额调整，跨期间冲正不计用能 |
| `applications_installations.csv` | 措施、报价、服务商、可用时段、补助状态、安装日期、`post_kwh` | `grant_status`、`installed_on`、实测变化是三件事；`post_kwh` 在安装后有账单前只是预期 |
| `program_limits.csv` | 期间预算、每户上限、最大同时安装数、优先组 | 每户上限按该户全部措施合计 |
| `shared_allocations.csv`（可选） | 共用账户的分户依据 | 列：`account_token,household_token,share,basis`；份额合计须为 1 且覆盖全部住户才能分户 |

## 可选分户文件示例

```csv
account_token,household_token,share,basis
E02,H02,0.6,floor area share
E02,H03,0.4,floor area share
```

没有该文件时，`E02` 上的 H02/H03 都不会被分摊到整张账单，而是报告为不可归属。
