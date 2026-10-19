#!/usr/bin/env python3
"""港股治理数据取数：公司资料 / 派息记录 / 股权融资推导。

三通道独立成败、互不阻塞（对齐主代码「任一通道失败走降级不拖垮主流程」语义）：

- 公司资料（东财港股 F10 CompanyProfile）：D09 上市日期、板块（D20 辅助）、
  核数师（D12 稳定性基线）、面值、港股通标的标记、注册地
- 派息（东财 GMAININDICATOR）：D13 每股股利(DPS_HKD) + 派息比率(DIVI_RATIO)
- 股权融资推导（东财现金流量表，TD-5 abs 规范化）：D14 发行股份（供股/配售/ IPO 到账
  须结合披露易公告核验折价率与频率——H0-01/H2-01 主路径走 fetch_hk_announce.py）

用法：python3 scripts/fetch_hk_governance.py 00700.HK [--years 5] [--out receipt.json]

合规声明：代码自研；接口为公开事实信息。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    FetchError, add_error, add_item, add_missing, get_json, normalize_currency,
    run_cli,
)
from fetch_hk import normalize_hk_ticker  # noqa: E402

SCRIPT = os.path.basename(__file__)
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"Referer": "https://data.eastmoney.com/"}
PROFILE_URL = "https://emweb.securities.eastmoney.com/PC_HKF10/CompanyProfile/PageAjax"
PROFILE_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/"}


def _date10(raw):
    return str(raw or "")[:10]


# ---------------------------------------------------------------- 公司资料

def fill_profile(env, code5):
    d = get_json(PROFILE_URL, endpoint="em_hk_profile", params={"code": code5},
                 headers=PROFILE_HEADERS)
    if not isinstance(d, dict) or not (d.get("zqzl") or d.get("gszl")):
        raise FetchError("em_hk_profile", "返回结构漂移：zqzl/gszl 缺失",
                         action_hint="CompanyProfile 接口结构可能已变更")
    zqzl = d.get("zqzl") or {}
    gszl = d.get("gszl") or {}

    listed = _date10(zqzl.get("ssrq")).replace("/", "-")
    if listed:
        add_item(env, "D09", listed, "", "em_hk_profile", "ssrq 上市日期",
                 unit="date", currency="",
                 note="反向收购(RTO)完成日须以披露易公告另行核验（G0-03）")
    else:
        add_missing(env, "D09", "", "em_hk_profile", "ssrq 上市日期",
                    "字段未返回", unit="date", currency="")

    name = zqzl.get("zqjc") or gszl.get("ywmc")
    if name:
        add_item(env, "INFO_NAME", name, "", "em_hk_profile", "zqjc 证券简称",
                 unit="text", currency="")
    board = zqzl.get("bk")
    if board:
        add_item(env, "D20", board, "", "em_hk_profile", "bk 板块",
                 unit="text", currency="",
                 note="板块口径（主板/GEM）；WVR(-W)/18A(-B)/第二上市(-S) 标记"
                      "须按股名后缀与招股书另行判定（H0-02）")
    else:
        add_missing(env, "D20", "", "em_hk_profile", "bk 板块", "字段未返回",
                    unit="text", currency="")

    auditor = gszl.get("hss")
    if auditor:
        add_item(env, "D12", auditor, "", "em_hk_profile", "hss 核数师",
                 unit="text", currency="",
                 note="现任核数师（变更历史与辞任函须走披露易公告，S5-03）")
    if zqzl.get("mgmz"):
        add_item(env, "INFO_PAR_VALUE", str(zqzl["mgmz"]), "", "em_hk_profile",
                 "mgmz 每股面值", unit="text", currency="")
    if gszl.get("zcd"):
        add_item(env, "INFO_DOMICILE", gszl["zcd"], "", "em_hk_profile",
                 "zcd 注册地", unit="text", currency="")
    for key, fid, label in (("sfhgtbd", "INFO_SH_CONNECT", "沪港通标的"),
                            ("sfsgtbd", "INFO_SZ_CONNECT", "深港通标的")):
        if zqzl.get(key):
            add_item(env, fid, zqzl[key], "", "em_hk_profile", f"{key} {label}",
                     unit="text", currency="")


# ---------------------------------------------------------------- D13 派息（GMAININDICATOR）

def fill_dividend(env, secu, years):
    d = get_json(DC_URL, endpoint="em_dc_RPT_HKF10_FN_GMAININDICATOR", params={
        "reportName": "RPT_HKF10_FN_GMAININDICATOR", "columns": "ALL",
        "filter": f'(SECUCODE="{secu}")', "pageSize": "100", "pageNumber": "1",
        "sortColumns": "REPORT_DATE", "sortTypes": "-1", "source": "WEB", "client": "WEB",
    }, headers=DC_HEADERS)
    if d.get("success") is False:
        raise FetchError("em_dc_RPT_HKF10_FN_GMAININDICATOR",
                         f"业务失败: {d.get('message')}",
                         action_hint="报表名可能已变更，请检查 references/data-source-spec.md")
    rows = [r for r in ((d.get("result") or {}).get("data") or [])
            if r.get("DATE_TYPE") == "年报"]
    rows = sorted(rows, key=lambda r: str(r.get("REPORT_DATE", "")), reverse=True)[:years]
    hit = 0
    for r in rows:
        period = _date10(r.get("REPORT_DATE"))
        dps = r.get("DPS_HKD")
        ratio = r.get("DIVI_RATIO")
        if dps is not None:
            hit += 1
            add_item(env, "D13", float(dps), period,
                     "em_dc_RPT_HKF10_FN_GMAININDICATOR", "DPS_HKD",
                     unit="HKD/股", currency="HKD",
                     note="每股股利（港币，东财口径）；以非港币宣告派息的公司"
                          "须查披露易派息公告原文核验币种")
        else:
            add_item(env, "D13", 0.0, period,
                     "em_dc_RPT_HKF10_FN_GMAININDICATOR", "DPS_HKD",
                     unit="HKD/股", currency="HKD",
                     note="无派息记录（零值属数据真相；S4-07 股东回报检验用）")
        if ratio is not None:
            add_item(env, "INFO_DIVI_RATIO", float(ratio), period,
                     "em_dc_RPT_HKF10_FN_GMAININDICATOR", "DIVI_RATIO",
                     unit="%", currency="", note="派息比率（东财派生口径，仅供参考）")
    if hit == 0 and rows:
        env["meta"]["warnings"].append("GMAININDICATOR 全部年报期 DPS_HKD 均为空："
                                       "可能从未派息，也可能接口未收录，须以披露易派息公告复核")
    env["meta"]["field_coverage"]["D13"] = f"{hit}/{len(rows)}"


# ---------------------------------------------------------------- D14 股权融资推导（现金流量表）

def fill_equity_financing(env, secu, years):
    d = get_json(DC_URL, endpoint="em_dc_RPT_HKSK_FN_CASHFLOW", params={
        "reportName": "RPT_HKSK_FN_CASHFLOW", "columns": "ALL",
        "filter": f'(SECUCODE="{secu}")', "pageSize": "500", "pageNumber": "1",
        "sortColumns": "REPORT_DATE", "sortTypes": "-1", "source": "WEB", "client": "WEB",
    }, headers=DC_HEADERS)
    if d.get("success") is False:
        raise FetchError("em_dc_RPT_HKSK_FN_CASHFLOW", f"业务失败: {d.get('message')}",
                         action_hint="报表名可能已变更，请检查 references/data-source-spec.md")
    annual = {}
    currency = ""
    for r in (d.get("result") or {}).get("data") or []:
        if r.get("REPORT_TYPE") != "年报":
            continue
        period = _date10(r.get("REPORT_DATE"))
        rec = annual.setdefault(period, {})
        name = r.get("ITEM_NAME")
        if name and r.get("AMOUNT") is not None:
            rec[name] = r["AMOUNT"]
        if not currency:
            currency = normalize_currency(r.get("CURRENCY_ABBR")) or \
                normalize_currency(r.get("CURRENCY")) or ""
    periods = sorted(annual.keys(), reverse=True)[:years]
    hit = 0
    for period in periods:
        rec = annual[period]
        issued = rec.get("发行股份")
        if issued is not None:
            hit += 1
            add_item(env, "D14", abs(float(issued)), period,
                     "em_dc_RPT_HKSK_FN_CASHFLOW", "发行股份",
                     unit=currency or "", currency=currency or "",
                     note="现金流口径（绝对值规范化 TD-5）：含 IPO/配售/供股/购股权行权到账；"
                          "折价率与频率（H0-01/H2-01）须走披露易公告核验")
        else:
            add_item(env, "D14", 0.0, period, "em_dc_RPT_HKSK_FN_CASHFLOW", "发行股份",
                     unit=currency or "", currency=currency or "",
                     note="无此科目（该年无股权融资现金流入，零值属数据真相）")
    env["meta"]["field_coverage"]["D14"] = f"{hit}/{len(periods)}"


# ---------------------------------------------------------------- 主流程（各通道独立成败）

def main(ticker, years, env):
    secu = normalize_hk_ticker(ticker)
    env["ticker"] = secu
    code5 = secu.split(".")[0]
    for name, fn in (("公司资料", lambda: fill_profile(env, code5)),
                     ("D13 派息", lambda: fill_dividend(env, secu, years)),
                     ("D14 股权融资", lambda: fill_equity_financing(env, secu, years))):
        try:
            fn()
        except FetchError as exc:
            add_error(env, exc.endpoint, f"{name}取数失败: {exc}", exc.http_status,
                      exc.action_hint or "该通道缺失不阻塞其他通道；可稍后重试或走披露易公告补齐")


if __name__ == "__main__":
    run_cli(SCRIPT, "HK", main, "港股代码（如 00700.HK）")
