# 内置源自检（selftest）

> 冒烟测试清单：改动 `scripts/` 任意脚本后，按本文件逐项执行并核对预期。
> 基准环境：macOS / Python 3.9+ / 仅依赖 `requests`。基准标的：腾讯控股 00700.HK。
> 最近全量实测：2026-10-19（结果见各 TC「实测基线」）。

## TC-1 三表主源 + 双交叉核对

```bash
python3 scripts/fetch_hk.py 00700.HK
```

预期：
- exit 0；stdout 为单个 envelope JSON（items/conflicts/errors/meta）
- `items ≥ 300`；`meta.field_coverage` ≈ FZB 119/150、LRB 81/115、LLB 45/45（东财科目收录随时间可能略增，MISSING 减少为正）
- `meta.currency = "CNY"`（东财标注 CNY；Yahoo 锚可用时经 Fix-10 交叉验证）
- `conflicts ≤ 1`：允许存在 `B01 @2021-12-31` 一条——东财该期未收录「营运收入」科目（真实数据缺口，回退窄口径「营业额」，与 GMAININDICATOR 差 1.37% 超 0.5% 容差），属如实呈现而非脚本缺陷；**除此之外出现任何新 conflict 必须排查**
- B17/B26 与 GMAININDICATOR 交叉一致；B22 由 B17÷B27 推导（≈95.14 亿股 FY2025）；A37=0（结构性零值）

实测基线（2026-10-19）：items=335，OK=275 / MISSING=59 / CONFLICT=1（即上述 2021 期 B01），exit 0。

### TC-1a Yahoo 币种锚降级（预期内行为）

Yahoo getcrumb 对单 IP 有短期限流（429）。触发时：
- 脚本 **不失败**：exit 仍为 0；`errors` 记录 `yahoo_quote_summary_crumb HTTP 429`；`meta.warnings` 含 `currency_unverified`
- 主数据（东财三表 + GMI 交叉）照常有效；稍候（分钟级）重跑即恢复锚验证
- 锚可用时实测（2026-10-19 早些时候）：`financialCurrency=CNY`，最新年报 `totalRevenue=7517.66 亿` 与东财 `OPERATE_INCOME` 完全一致 → 比值≈1 通过

## TC-2 治理数据三通道

```bash
python3 scripts/fetch_hk_governance.py 00700.HK
```

预期：exit 0；D09 上市日期=2004-06-16、D20 板块=主板、D12 核数师含「罗兵咸永道」、D13 每股股利（FY2025=5.3 HKD）+派息比率、D14 股权融资推导额（FY2025≈33.03 亿）。三通道独立成败，单通道故障不影响其余。

## TC-3 披露易公告检索 + PDF 落盘

```bash
python3 scripts/fetch_hk_announce.py 00700.HK --preset yearly_report --download
```

预期：exit 0；`meta.hkex_stock_id=7609`、`meta.hkex_stock_name` 含「騰訊控股」；检索到年报公告 ≥1 条；`./announcements/00700/<NEWS_ID>.pdf` 落盘且通过 `%PDF` 头校验（实测 2 份：5.2MB / 4.4MB）。测试后删除 `announcements/` 目录。

## TC-4 参数错误退出码

```bash
python3 scripts/fetch_hk.py abc            ; echo $?   # 预期 3
python3 scripts/fetch_hk_governance.py abc ; echo $?   # 预期 3
python3 scripts/fetch_hk_announce.py abc   ; echo $?   # 预期 3
```

退出码语义：0=成功（允许 MISSING/CONFLICT）/ 3=参数错误 / 4=接口故障 / 5=风控（HTTP 403）。

## TC-5 断网故障语义（可选，需断网环境）

断网后执行 TC-1：预期 exit 4，`errors` 含连接失败与 action_hint，**禁止**输出任何编造数值。

## TC-6 静态检查：HTTP 唯一出口

```bash
grep -rnE 'requests\.(get|post)\(' scripts/   # 预期 0 命中
```

全部请求必须经 `ds_common._request`（内部用 `_SESSION.request`）：统一限流（同域 ≥1s+抖动）、429/5xx 指数退避（0.5s→1s，共 3 次尝试）、15s 超时、403→BlockedError。新增脚本/新端点不得绕过。
