#!/usr/bin/env python3
"""港股三表取数 + 双源交叉核对 + 币种锚定验证（主源脚本）。

架构复刻 stock_aiagent_research 主项目 backend/app/dataflows 港股链路
（providers/hk/sina_hk_provider + providers/em/em_finstmt_provider + normalizer Fix-10），
适配零重依赖 CLI 形态：

- 主源：东财港股 F10 三表（datacenter-web，长表 ITEM_NAME/AMOUNT，REPORT_TYPE=="年报" 过滤）
  —— 即主代码 akshare stock_financial_hk_report_em 的底层直连；
  现金流表名为 RPT_HKSK_FN_CASHFLOW（主代码实测：RPT_HKF10_FN_CASHFLOW 返回 0 行）
- 交叉核对①（同司双源）：东财 GMAININDICATOR 派生指标 vs 三表计算值
  —— 即主代码 akshare stock_financial_hk_analysis_indicator_em 的底层直连；
  绝对额容差 0.5%，超差进 conflicts 不仲裁任选
- 交叉核对②（Fix-10 币种锚）：Yahoo quoteSummary（cookie+crumb）
  financialData.financialCurrency + incomeStatementHistory 年报 totalRevenue
  vs 东财 CURRENCY 标注 —— 腾讯 00700 案例：数值实为人民币但可能错标，
  营收锚比值≈1 → 数值实为财报币种并纠正标注；锚缺失 → currency_unverified 不静默
- 行级 sanity（TD-6）+ CapEx/股息/利息 abs 规范化（TD-5）
- 结构性零值：A37/B24/A38/C08 无此科目填 0（字典总则），不以 MISSING 绕过结构性检查

用法：python3 scripts/fetch_hk.py 00700.HK [--years 5] [--out receipt.json]

合规声明：代码自研；接口为公开事实信息。Yahoo 为校验源，故障不阻塞主数据。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    FetchError, add_conflict, add_error, add_item, add_missing,
    add_structural_zero, get_json, limited_get, normalize_currency,
    run_cli, sanity_reject,
)

SCRIPT = os.path.basename(__file__)
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"Referer": "https://data.eastmoney.com/"}

# Yahoo 校验源（Fix-10 锚）
YH_COOKIE_URL = "https://fc.yahoo.com"
# crumb 双 host 回退：query2/query1 存在独立限流计数（实测单 host 429 时另一 host 可通）
YH_CRUMB_URLS = ("https://query2.finance.yahoo.com/v1/test/getcrumb",
                 "https://query1.finance.yahoo.com/v1/test/getcrumb")
YH_QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"

CROSS_TOLERANCE = 0.005   # 绝对额交叉核对容差（同司双源，对齐 A 股版 0.5% 经验）
ANCHOR_TOLERANCE = 0.15   # Fix-10 币种锚比值容差（主代码 normalizer 同口径）

# 结构性零值字段（字典总则：无此科目填 0，不构成 MISSING）
STRUCTURAL_ZERO_FIELDS = {"A37", "B24", "A38", "C08"}

# ---------------------------------------------------------------- 字段映射（2026-10-19 实测冻结）
# 值形态：[候选科目名链] 取首个非空；
# ("sum"|"sub", [成分]) 推导（note 注明推导式）；
# ("abs", [科目]) / ("sum_abs", [成分]) 为 TD-5 符号规范化（现金流科目符号口径混杂，一律 abs）；
# None 表示该准则口径下不可得（MISSING，不阻塞）。

FIELD_MAP_HK_FZB = {
    "A01": ["现金及等价物"],
    # S1-01 口径含定期存款（含中长期，可提前支取属性）；FVTPL 流动资产并入短期投资
    "A02": ("sum", ["短期存款", "中长期存款", "指定以公允价值记账之金融资产(流动)"]),
    "A04": ["应收帐款"],
    "A05": None,  # 合约资产未单列（查年报附注）
    "A06": ["预付款项"],
    "A07": ["预付款按金及其他应收款"],  # 合并口径（含按金）
    "A08": ["存货"],
    "A10": ["流动资产合计"],
    "A13": ["投资物业"],
    "A14": ["物业厂房及设备"],
    "A15": ["在建工程"],
    "A16": None,  # 使用权资产未单列
    "A17": ["无形资产"],
    "A18": None,  # 资本化开发支出未单列
    "A19": None,  # 商誉未单列（已知缺口，须查年报附注）
    "A21": ["递延税项资产"],
    "A22": ["总资产"],
    # HKFRS 语境「应付票据」为债券性质 notes payable（腾讯美元票据即此科目），计入有息负债；
    # CAS 口径 H 股公司该科目为贸易结算票据，禁止并入（字典 S1 脚注），须人工甄别
    "A23": ("sum", ["短期贷款", "应付票据"]),
    "A25": ["应付帐款"],
    "A26": ["递延收入(流动)"],  # 近似合约负债口径
    "A28": ["流动负债合计"],
    "A29": ("sum", ["长期贷款", "应付票据(非流动)"]),
    "A31": ("sum", ["融资租赁负债(流动)", "融资租赁负债(非流动)"]),
    "A32": ["递延税项负债"],
    "A33": ["总负债"],
    "A34": ["股东权益"],  # 归母口径（总权益=股东权益+少数股东权益）
    "A35": ["少数股东权益"],
    "A36": None,  # 重估储备未单列（查年报权益变动表）
    "A37": ["永久资本证券", "永续债"],  # 结构性检查字段：无此科目 → 填 0
    "A38": ("abs", ["库存股"]),  # 权益抵减项（负数列报）；无此科目 → 填 0
}

FIELD_MAP_HK_LRB = {
    # 营运收入=官方 revenue 口径（实测与 GMAININDICATOR/Yahoo 一致）；营业额为窄口径兜底
    "B01": ["营运收入", "营业额"],
    # 营运支出为销售成本直接披露科目（实测：营运收入−毛利=营运支出，数值验证吻合）
    "B02": ["营运支出"],
    "B03": ("sum", ["销售及分销费用", "行政开支"]),
    "B05": None,  # 研发费用未单列（查年报）
    "B06": ["融资成本"],
    "B06a": None,  # 融资成本主要含利息但非纯利息费用
    "B06b": ["利息收入"],
    # 投资物业公允价值损益：地产公司单列，综合企业常并入其他收益；S3-04 分子下钻用
    "B07": ["投资物业公平值变动", "投资物业公允价值变动", "投资物业重估收益"],
    "B08": None,  # 投资/出售收益并入 B25 其他收益净额
    "B11": ["经营溢利"],
    "B14": ["除税前溢利"],
    "B15": ["税项"],
    "B16": ["除税后溢利"],
    "B17": ["股东应占溢利"],
    "B18": ["少数股东损益"],
    "B19": ["其他全面收益"],
    "B20": ["全面收益总额"],
    "B21": None,  # 以股份为基础的付款未单列（查年报）
    "B23": None,  # Non-IFRS 净利润走业绩公告路径（披露易）
    # 结构性检查字段：利润表无应占合联营行 → 填 0（公司无合联营敞口）
    "B24": ("sum", ["应占联营公司溢利", "应占合营公司溢利", "应占合营企业溢利"]),
    # S3-04 分子完整覆盖：其他收入与其他收益可能分行披露，必须合并（T3 回归实测教训）
    "B25": ("sum", ["其他收益", "其他营业收入"]),
    "B26": ["每股基本盈利"],   # H2-04 每股裂口用
    "B27": ["每股摊薄盈利"],
}

FIELD_MAP_HK_LLB = {
    "C02": ["经营业务现金净额"],
    # CapEx 符号口径混杂（部分期次正数记账），一律 abs（TD-5）
    "C03": ("sum_abs", ["购建固定资产", "购建无形资产及其他资产"]),
    "C04": ["投资业务现金净额"],
    "C05": ("abs", ["已付股息(融资)"]),
    "C06": ["融资业务现金净额"],
    "C08": ("abs", ["回购股份"]),  # 结构性检查字段：无回购 → 填 0
    "C09": ("abs", ["收购附属公司"]),
    # 已付利息是否含资本化部分以年报附注为准（S1-05 分母口径注记）
    "C11": ("abs", ["已付利息(融资)"]),
    "C12": ("abs", ["已收利息(投资)"]),
    # C10（Δ应收/Δ合约负债）由 A04/A05/A26 两期差分计算，属分析层职责，脚本不产出
}

REPORTS_HK = {
    "fzb": ("RPT_HKF10_FN_BALANCE", FIELD_MAP_HK_FZB),
    "lrb": ("RPT_HKF10_FN_INCOME", FIELD_MAP_HK_LRB),
    "llb": ("RPT_HKSK_FN_CASHFLOW", FIELD_MAP_HK_LLB),
}

# GMAININDICATOR 交叉核对集：field_id → (候选列元组, 说明)
# B17 用 HOLDER_PROFITACTUAL 优先（实测=三表股东应占溢利；HOLDER_PROFIT 为宽口径）
GMI_CHECKS = {
    "B01": (("OPERATE_INCOME",), "收益"),
    "B17": (("HOLDER_PROFITACTUAL", "HOLDER_PROFIT"), "归母净利润"),
    "B26": (("BASIC_EPS",), "每股基本盈利"),
    "B27": (("DILUTED_EPS",), "每股摊薄盈利"),
}
# 毛利派生核对：B01−B02（三表）vs GROSS_PROFIT（GMAININDICATOR）

# GMAININDICATOR 派生比率（INFO 级参考，不进 conflicts；裁决以字典公式计算为准）
GMI_INFO = {
    "INFO_ROE_AVG": ("ROE_AVG", "股东权益回报率(平均)"),
    "INFO_ROIC": ("ROIC", "投入资本回报率"),
    "INFO_GROSS_MARGIN": ("GROSS_PROFIT_RATIO", "毛利率"),
    "INFO_DEBT_ASSET": ("DEBT_ASSET_RATIO", "资产负债率"),
}


# ---------------------------------------------------------------- 代码规范化

def normalize_hk_ticker(raw):
    """接受 00700.HK / 700.HK / 00700 → 5 位前导零 + .HK；非法输入抛 ArgError。"""
    from ds_common import ArgError
    text = raw.strip().upper()
    m = re.fullmatch(r"(\d{1,5})(?:\.HK)?", text)
    if not m:
        raise ArgError(f"代码格式非法: {raw!r}（示例：00700.HK / 700.HK / 00700）")
    return f"{m.group(1).zfill(5)}.HK"


# ---------------------------------------------------------------- 东财长表拉取与映射

def _dc_fetch_all(report, secu, endpoint, max_pages=10):
    """长表分页拉全（每页 500 行）。"""
    rows = []
    for page in range(1, max_pages + 1):
        d = get_json(DC_URL, endpoint=endpoint, params={
            "reportName": report, "columns": "ALL", "filter": f'(SECUCODE="{secu}")',
            "pageSize": "500", "pageNumber": str(page),
            "sortColumns": "REPORT_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        }, headers=DC_HEADERS)
        if d.get("success") is False:
            raise FetchError(endpoint, f"业务失败: {d.get('message')}",
                             action_hint="报表名可能已变更，请检查 references/data-source-spec.md")
        data = (d.get("result") or {}).get("data") or []
        rows.extend(data)
        if len(data) < 500:
            break
    return rows


def _resolve(mapping, rec):
    """按映射取值。返回 (value, raw_field, note)；value=None 表示不可得。

    成分科目存在但 AMOUNT 为 None 视为 0 参与推导（披露的科目无金额 = 零）。
    """
    if mapping is None:
        return None, "", ""
    if isinstance(mapping, list):
        for name in mapping:
            v = rec.get(name)
            if v is not None:
                return float(v), name, ""
        return None, mapping[0], ""
    op, parts = mapping
    if op == "abs":
        v = rec.get(parts[0])
        if v is None:
            return None, parts[0], ""
        return abs(float(v)), parts[0], "绝对值规范化（负数列报/符号口径规范化，TD-5）"
    base = rec.get(parts[0])
    if base is None:
        return None, parts[0], ""
    total = float(base)
    used = [parts[0]]
    for p in parts[1:]:
        v = rec.get(p)
        if v is not None:
            total = total + abs(float(v)) if op == "sum_abs" else (
                total + float(v) if op == "sum" else total - float(v))
            used.append(p)
    joiner = {"sum": "+", "sub": "−", "sum_abs": "+|"}[op]
    note = f"推导值：{joiner.join(used)}"
    if op == "sum_abs":
        note += "（成分一律 abs，TD-5）"
    return total, parts[0], note


def fill_statements(env, secu, years):
    """三表填数：年报过滤 → 按期分组 → sanity 行级拒绝 → 字段映射；币种/准则透传。

    返回 {period: {field_id: value}}（供 GMAININDICATOR 交叉核对使用）。
    """
    filled = {}  # period -> {field_id: value}
    for group, (report, fmap) in REPORTS_HK.items():
        endpoint = f"em_dc_{report}"
        rows = _dc_fetch_all(report, secu, endpoint)
        annual = {}
        currency = ""
        standards = ""
        for r in rows:
            if r.get("REPORT_TYPE") != "年报":
                continue  # 年报过滤（港股财年截止日多样：3/31、6/30、12/31 均常见）
            period = str(r.get("REPORT_DATE", ""))[:10]
            rec = annual.setdefault(period, {})
            name = r.get("ITEM_NAME")
            if name and r.get("AMOUNT") is not None:
                rec[name.strip()] = r["AMOUNT"]  # 实测科目名带前导空格（' 其他收益'）
            if not currency:
                # CURRENCY_ABBR（ISO）优先于 CURRENCY（中文），em_finstmt 移植
                currency = normalize_currency(r.get("CURRENCY_ABBR")) or \
                    normalize_currency(r.get("CURRENCY")) or ""
            if not standards and (r.get("ACCOUNT_STANDARD") or r.get("ACCOUNTING_STANDARDS")):
                standards = r.get("ACCOUNT_STANDARD") or r.get("ACCOUNTING_STANDARDS")
        if not annual:
            add_error(env, endpoint, "无年报数据返回（REPORT_TYPE=年报 过滤后为空）",
                      action_hint="请核实代码与 .HK 后缀；或该标的未被收录")
            continue
        if standards:
            env["meta"]["accounting_standards"] = standards
        if currency and not env["meta"].get("currency_raw"):
            env["meta"]["currency_raw"] = currency  # 东财原始标注（Fix-10 锚定前）
        periods = sorted(annual.keys(), reverse=True)[:years]
        if len(periods) < years:
            w = f"年报期数不足：仅取得 {len(periods)} 期（目标 {years} 期），可能为上市未满 {years} 年"
            if w not in env["meta"]["warnings"]:
                env["meta"]["warnings"].append(w)
        hit = 0
        for period in periods:
            rec = annual[period]
            slot = filled.setdefault(period, {})
            # 行级 sanity（TD-6）：先取关键字段试算，命中 → 整期拒绝并审计
            probe = {
                "B01": rec.get("营业额") or rec.get("营运收入"),
                "B17": rec.get("股东应占溢利"),
                "A34": rec.get("股东权益"),
            }
            probe = {k: float(v) for k, v in probe.items() if v is not None}
            reason = sanity_reject(probe)
            if reason:
                env["meta"].setdefault("rejected_periods", []).append(
                    {"period": period, "table": group, "reason": reason})
                continue
            for field_id, mapping in fmap.items():
                value, raw_field, note = _resolve(mapping, rec)
                if value is not None:
                    hit += 1
                    slot[field_id] = value
                    unit = "元/股" if field_id in ("B26", "B27") else (currency or "")
                    add_item(env, field_id, value, period, endpoint, raw_field,
                             unit=unit, currency=currency or "", note=note)
                elif field_id in STRUCTURAL_ZERO_FIELDS and mapping is not None:
                    add_structural_zero(env, field_id, period, endpoint,
                                        mapping[0] if isinstance(mapping, list)
                                        else mapping[1][0],
                                        "结构性检查字段，禁止以 MISSING 绕过",
                                        currency=currency or "")
                    slot[field_id] = 0.0
                else:
                    reason = "该准则口径下不可得（须查年报附注/披露易原文）" if mapping is None \
                        else "科目未披露"
                    add_missing(env, field_id, period, endpoint,
                                raw_field or "（无对应科目）", reason,
                                currency=currency or "")
        env["meta"]["field_coverage"][group.upper()] = f"{hit}/{len(fmap) * len(periods)}"
    return filled


# ---------------------------------------------------------------- 交叉核对①：GMAININDICATOR 同司双源

def _fetch_gmi(secu, years):
    """拉 GMAININDICATOR 年报序列。返回 {period: row}。"""
    rows = _dc_fetch_all("RPT_HKF10_FN_GMAININDICATOR", secu,
                         "em_dc_RPT_HKF10_FN_GMAININDICATOR")
    annual = {}
    for r in rows:
        if r.get("DATE_TYPE") != "年报":
            continue
        period = str(r.get("REPORT_DATE", ""))[:10]
        annual[period] = r
    periods = sorted(annual.keys(), reverse=True)[:years]
    return {p: annual[p] for p in periods}


def cross_check_gmi(env, filled, secu, years):
    """GMAININDICATOR vs 三表：绝对额字段超 0.5% → conflict；派生比率作 INFO 参考输出。"""
    try:
        gmi = _fetch_gmi(secu, years)
    except FetchError as exc:
        # 校验源故障不阻塞主数据（对齐主代码「yfinance 补充失败不阻断」语义）
        add_error(env, exc.endpoint, f"校验源不可用: {exc}", exc.http_status,
                  "三表主源数据照常有效；交叉核对未执行")
        env["meta"]["warnings"].append("GMAININDICATOR 校验源缺失，同司双源交叉核对未执行")
        return
    if not gmi:
        env["meta"]["warnings"].append("GMAININDICATOR 无年报数据，同司双源交叉核对未执行")
        return
    currency = env["meta"].get("currency_raw", "")
    for period, row in gmi.items():
        slot = filled.get(period, {})
        for field_id, (columns, label) in GMI_CHECKS.items():
            stmt_val = slot.get(field_id)
            gmi_val = None
            for col in columns:
                if row.get(col) is not None:
                    gmi_val = row[col]
                    break
            if stmt_val is None or gmi_val is None:
                continue
            gmi_val = float(gmi_val)
            denom = max(abs(stmt_val), abs(gmi_val))
            if denom and abs(stmt_val - gmi_val) / denom > CROSS_TOLERANCE:
                add_conflict(env, field_id, period, "em_三表", stmt_val,
                             "em_GMAININDICATOR", gmi_val)
                for item in env["items"]:
                    if item["field_id"] == field_id and item["period"] == period:
                        item["status"] = "CONFLICT"
                        item["note"] = ("东财三表/GMAININDICATOR 双源偏差 >0.5%，"
                                        "双值见 conflicts；不仲裁任选")
            else:
                for item in env["items"]:
                    if item["field_id"] == field_id and item["period"] == period \
                            and item["status"] == "OK":
                        item["note"] = (item["note"] + "；" if item["note"] else "") \
                            + "GMAININDICATOR 交叉一致"
        # 毛利派生核对：B01−B02 vs GROSS_PROFIT（毛利率是 S3-01/S3-09 核心）
        b01, b02 = slot.get("B01"), slot.get("B02")
        gross_gmi = row.get("GROSS_PROFIT")
        if b01 is not None and b02 is not None and gross_gmi is not None:
            gross_calc = b01 - b02
            gross_gmi = float(gross_gmi)
            denom = max(abs(gross_calc), abs(gross_gmi))
            if denom and abs(gross_calc - gross_gmi) / denom > CROSS_TOLERANCE:
                add_conflict(env, "B02", period, "em_三表(B01−B02 推导毛利)",
                             gross_calc, "em_GMAININDICATOR", gross_gmi)
                for item in env["items"]:
                    if item["field_id"] == "B02" and item["period"] == period:
                        item["status"] = "CONFLICT"
                        item["note"] = ("推导毛利与 GMAININDICATOR 偏差 >0.5%，"
                                        "双值见 conflicts；不仲裁任选")
        # 派生比率 INFO 参考（东财平均口径，裁决以字典公式计算为准）
        for info_id, (column, label) in GMI_INFO.items():
            v = row.get(column)
            if v is not None:
                add_item(env, info_id, float(v), period, "em_GMAININDICATOR", column,
                         unit="%", currency="",
                         note=f"{label}，东财派生口径（平均基准），仅供参考；"
                              "裁决以指标字典公式计算为准")
        # B22 加权稀释股数推导 = 归母净利 ÷ 摊薄 EPS（同源两字段相除）
        hp, deps = row.get("HOLDER_PROFIT"), row.get("DILUTED_EPS")
        if hp and deps:
            add_item(env, "B22", float(hp) / float(deps), period,
                     "em_GMAININDICATOR", "HOLDER_PROFIT ÷ DILUTED_EPS",
                     unit="股", currency="",
                     note="推导值：归母净利÷摊薄每股盈利（加权稀释股数）")
        # 币种与准则透传（GMAININDICATOR 侧，与三表侧互证）
        gmi_cur = normalize_currency(row.get("CURRENCY_ABBR")) or \
            normalize_currency(row.get("CURRENCY"))
        if gmi_cur and currency and gmi_cur != currency:
            env["meta"]["warnings"].append(
                f"币种标注不一致：三表={currency} vs GMAININDICATOR={gmi_cur}（{period}）")
        if row.get("ACCOUNTING_STANDARDS") and not env["meta"].get("accounting_standards"):
            env["meta"]["accounting_standards"] = row["ACCOUNTING_STANDARDS"]


# ---------------------------------------------------------------- 交叉核对②：Yahoo 币种锚（Fix-10 移植）

def _yahoo_session(endpoint):
    """Yahoo cookie + crumb（quoteSummary 鉴权流程，yfinance 同款公开机制）。

    fc.yahoo.com 实测返回 404 但正常 Set-Cookie（yfinance 同款行为）——
    cookie 步骤容忍 404，只关心会话建立。
    """
    try:
        limited_get(YH_COOKIE_URL, endpoint=f"{endpoint}_cookie")
    except FetchError as exc:
        if exc.http_status != 404:
            raise
    crumb = ""
    last_exc = None
    for url in YH_CRUMB_URLS:
        try:
            crumb = limited_get(url, endpoint=f"{endpoint}_crumb").text.strip()
            break
        except FetchError as exc:
            last_exc = exc
            continue
    if not crumb and last_exc:
        raise last_exc
    if not crumb or "<" in crumb:
        raise FetchError(endpoint, "Yahoo crumb 获取失败（返回非预期内容）",
                         action_hint="Yahoo 鉴权流程可能已变更；币种锚不可用不阻塞主数据")
    return crumb


def _yahoo_symbol(secu):
    """00700.HK → 0700.HK（Yahoo 用 4 位代码）。"""
    return f"{int(secu.split('.')[0]):04d}.HK"


def currency_anchor_check(env, filled, secu):
    """Fix-10 移植：东财 CURRENCY 标注 vs Yahoo 财报币种 + 营收锚量级比对。

    判定链：data_cur（东财标注）≠ fin_cur（Yahoo 财报币种）时——
    锚营收比值≈1 → 数值实为财报币种，纠正 meta.currency 并记 warning；
    比值无法解释 → currency_suspect 标记，禁止静默放行；锚缺失 → currency_unverified。
    """
    data_cur = env["meta"].get("currency_raw", "")
    endpoint = "yahoo_quote_summary"
    try:
        crumb = _yahoo_session(endpoint)
        d = get_json(YH_QS_URL.format(symbol=_yahoo_symbol(secu)), endpoint=endpoint,
                     params={"modules": "financialData,incomeStatementHistory,price",
                             "crumb": crumb})
    except FetchError as exc:
        add_error(env, exc.endpoint, f"币种锚校验源不可用: {exc}", exc.http_status,
                  "东财 CURRENCY 标注未经 Yahoo 锚验证；主数据照常有效")
        env["meta"]["warnings"].append("currency_unverified：Yahoo 锚不可用，"
                                       "币种以东财标注为准（未经交叉验证）")
        return
    result = (d.get("quoteSummary") or {}).get("result") or []
    if not result:
        env["meta"]["warnings"].append("currency_unverified：Yahoo 无该标的数据，"
                                       "币种以东财标注为准（未经交叉验证）")
        return
    payload = result[0]
    fin_cur = ((payload.get("financialData") or {}).get("financialCurrency") or "").upper()
    trade_cur = ((payload.get("price") or {}).get("currency") or "").upper()
    if trade_cur:
        env["meta"]["trading_currency"] = trade_cur
    if not data_cur or not fin_cur or data_cur == fin_cur:
        env["meta"]["currency"] = data_cur or fin_cur or ""
        env["meta"]["currency_evidence"] = (
            f"东财标注={data_cur or '缺失'}；Yahoo 财报币种={fin_cur or '缺失'}；一致或锚缺失，按原标注")
        return
    # 标注不一致 → 营收锚量级比对（Fix-10 核心）
    latest_period = max(filled.keys()) if filled else None
    ak_rev = filled.get(latest_period, {}).get("B01") if latest_period else None
    yf_rev = None
    history = ((payload.get("incomeStatementHistory") or {})
               .get("incomeStatementHistory") or [])
    for entry in history:
        end = ((entry.get("endDate") or {}).get("fmt") or "")[:10]
        if end == latest_period:
            raw = (entry.get("totalRevenue") or {}).get("raw")
            yf_rev = float(raw) if raw is not None else None
            break
    if yf_rev is None and history:
        raw = (history[0].get("totalRevenue") or {}).get("raw")
        yf_rev = float(raw) if raw is not None else None
    evidence = (f"东财标注={data_cur}；Yahoo 财报币种={fin_cur}；"
                f"东财最新年营收={ak_rev}；Yahoo 同锚营收={yf_rev}")
    if ak_rev and yf_rev and yf_rev > 0:
        ratio = ak_rev / yf_rev
        evidence += f"；锚比值={ratio:.4f}"
        if abs(ratio - 1.0) < ANCHOR_TOLERANCE:
            env["meta"]["currency"] = fin_cur
            env["meta"]["warnings"].append(
                f"东财 CURRENCY 标注({data_cur})与 Yahoo 财报币种({fin_cur})不符，"
                f"营收锚比值≈1 → 数值实为 {fin_cur}，meta.currency 已纠正 "
                f"（items 内 currency 保留东财原始标注作为证据）")
        else:
            env["meta"]["currency"] = data_cur
            env["meta"]["warnings"].append(
                f"currency_suspect：币种标注矛盾且锚比值({ratio:.4f})无法解释，"
                "请人工核实财报币种（禁止静默放行）")
    else:
        env["meta"]["currency"] = data_cur
        env["meta"]["warnings"].append(
            "currency_unverified：锚营收缺失，币种矛盾无法裁决，以东财标注为准")
    env["meta"]["currency_evidence"] = evidence


# ---------------------------------------------------------------- 主流程

def main_hk(ticker, years, env):
    secu = normalize_hk_ticker(ticker)
    env["ticker"] = secu
    filled = fill_statements(env, secu, years)
    cross_check_gmi(env, filled, secu, years)
    currency_anchor_check(env, filled, secu)
    env["meta"].setdefault("currency", env["meta"].get("currency_raw", ""))


if __name__ == "__main__":
    run_cli(SCRIPT, "HK", main_hk, "港股代码（如 00700.HK，支持 700.HK / 00700 简写）")
