#!/usr/bin/env python3
"""港股公告检索与 PDF 下载（披露易 HKEXnews，官方披露平台，S 级来源）。

链路（2026-10-19 实测冻结）：
1. prefix.do 联想接口：股票代码 → 内部 stockId
2. titleSearchServlet.do（仅支持 GET）：stockId + 标题关键词 + 日期区间 → 公告清单
3. PDF 下载：www1.hkexnews.hk + FILE_LINK，%PDF 头校验落盘

职责边界：脚本只到「公告清单 + PDF 落地」为止；D01 核数师意见 / D05 关连交易 /
D06 担保诉讼 / D21 永续债条款 / H0-01 供股合股折价明细等字段值提取，
由 Agent 阅读 PDF 完成并自行生成回执（来源 = 公告 PDF 文件名 + 页码）。

用法：
  python3 scripts/fetch_hk_announce.py 00700.HK --preset yearly --download
  python3 scripts/fetch_hk_announce.py 00700.HK --searchkey 供股 --days 1825

合规声明：代码自研；披露易为联交所官方披露平台（S 级）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    EXIT_API, EXIT_ARG, EXIT_BLOCKED, ArgError, BlockedError, FetchError, HKT,
    add_error, add_item, add_missing, limited_get, make_envelope,
)
from fetch_hk import normalize_hk_ticker  # noqa: E402

SCRIPT = os.path.basename(__file__)
HKEX_BASE = "https://www1.hkexnews.hk"
PREFIX_URL = HKEX_BASE + "/search/prefix.do"
SEARCH_URL = HKEX_BASE + "/search/titleSearchServlet.do"
HKEX_HEADERS = {"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh"}

# 预设检索矩阵（披露易标题为繁体，关键词必须用繁体；存在召回局限——
# Agent 做 H0-01/S5-02 等统计时可基于清单初筛扩大检索词复检）
PRESETS = {
    "yearly_results": "年度業績",   # 年度业绩公告（D01 核数师意见载体之一）
    "yearly_report":  "年報",       # 年报全文（D04–D07/D21 附注载体）
    "rights":         "供股",       # H0-01① / H2-01 供股频率
    "placing":        "配售",       # H0-01⑤ 折价配售
    "consolidation":  "股份合併",   # H0-01① 合股
    "auditor":        "核數師",     # S5-03 核数师变更/辞任
    "name_change":    "公司名稱",   # H0-01③ 频繁更名
    "dividend":       "股息",       # D13 派息公告（币种核验）
    "repurchase":     "購回",       # S4-07 回购（翌日披露报表）
    "connected":      "關連交易",   # S5-05 关连交易
}


def hkex_stock_id(code5):
    """prefix.do 联想：股票代码 → stockId。JSONP 包裹，需剥壳。"""
    resp = limited_get(PREFIX_URL, endpoint="hkex_prefix", params={
        "callback": "cb", "lang": "ZH", "type": "A", "name": code5, "market": "SEHK",
    }, headers=HKEX_HEADERS)
    text = resp.text.strip()
    m = text[text.find("(") + 1:text.rfind(")")]
    try:
        payload = json.loads(m)
    except ValueError as exc:
        raise FetchError("hkex_prefix", f"JSONP 剥壳解析失败（疑似接口漂移）: {exc}") from exc
    # 实测结构：{"more":"1","stockInfo":[{"stockId":7609,"code":"00700","name":...}]}
    arr = (payload.get("stockInfo") or []) if isinstance(payload, dict) else []
    if not arr:
        raise ArgError(f"披露易查无此代码: {code5}（请核实是否为港交所上市标的）")
    # 优先精确匹配 5 位代码
    for entry in arr:
        if str(entry.get("code", "")).zfill(5) == code5:
            return str(entry.get("stockId")), entry.get("name", "")
    first = arr[0]
    return str(first.get("stockId")), first.get("name", "")


def search_announcements(stock_id, title, from_date, to_date, row_range):
    """titleSearchServlet.do（仅 GET）。返回公告列表（result 为嵌套 JSON 字符串）。"""
    resp = limited_get(SEARCH_URL, endpoint="hkex_title_search", params={
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
        "market": "SEHK", "stockId": stock_id, "documentType": "-1",
        "fromDate": from_date, "toDate": to_date, "title": title,
        "searchType": "1", "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
        "rowRange": str(row_range), "lang": "zh",
    }, headers=HKEX_HEADERS)
    try:
        d = resp.json()
    except ValueError as exc:
        raise FetchError("hkex_title_search", f"JSON 解析失败（疑似接口漂移）: {exc}") from exc
    result_raw = d.get("result")
    if not result_raw:
        return []
    try:
        return json.loads(result_raw)
    except ValueError as exc:
        raise FetchError("hkex_title_search",
                         f"result 嵌套 JSON 解析失败（疑似接口漂移）: {exc}") from exc


def _hkex_date(raw):
    """'15/10/2026 17:29' → '2026-10-15'；异常原样截断。"""
    try:
        return datetime.strptime(str(raw or "")[:10], "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return str(raw or "")[:10]


def download_pdf(env, code5, ann, dest_dir):
    """下载并校验 %PDF 头；返回本地路径（失败记 error 返回空串）。"""
    link = ann.get("FILE_LINK") or ""
    if not link:
        return ""
    url = HKEX_BASE + link
    ann_id = str(ann.get("NEWS_ID") or "unknown")
    path = os.path.join(dest_dir, code5, f"{ann_id}.pdf")
    try:
        resp = limited_get(url, endpoint="hkex_pdf", timeout=30)
        content = resp.content
        if not content.startswith(b"%PDF"):
            raise FetchError("hkex_pdf", f"PDF 校验失败（非 %PDF 头）: {link}",
                             action_hint="链接可能已失效或被拦截")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path
    except FetchError as exc:
        add_error(env, exc.endpoint, str(exc), exc.http_status, exc.action_hint)
        return ""


def main():
    parser = argparse.ArgumentParser(
        prog=f"scripts/{SCRIPT}", description="内置数据源：披露易公告检索与 PDF 下载")
    parser.add_argument("ticker", help="港股代码（如 00700.HK）")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="yearly_results",
                        help="预设检索：" + " / ".join(f"{k}={v}" for k, v in sorted(PRESETS.items())))
    parser.add_argument("--searchkey", default=None, help="自定义标题关键词（覆盖 preset，须繁体）")
    parser.add_argument("--days", type=int, default=1825, help="回溯天数（默认 1825=5 年）")
    parser.add_argument("--row-range", type=int, default=30, help="返回条数上限（默认 30）")
    parser.add_argument("--download", action="store_true", help="下载公告 PDF 到 ./announcements/<代码>/")
    parser.add_argument("--max-download", type=int, default=5, help="PDF 下载条数上限（默认 5）")
    parser.add_argument("--out", default="", help="结果落盘路径（可选）")
    args = parser.parse_args()

    env = make_envelope(args.ticker, "HK", SCRIPT)
    try:
        secu = normalize_hk_ticker(args.ticker)
        env["ticker"] = secu
        code5 = secu.split(".")[0]
        stock_id, stock_name = hkex_stock_id(code5)
        env["meta"]["hkex_stock_id"] = stock_id
        if stock_name:
            env["meta"]["hkex_stock_name"] = stock_name

        title = args.searchkey if args.searchkey is not None else PRESETS[args.preset]
        end = datetime.now(HKT)
        start = end - timedelta(days=args.days)
        anns = search_announcements(stock_id, title,
                                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
                                    args.row_range)
        if not anns:
            add_missing(env, "ANN", "", "hkexnews", "titleSearchServlet",
                        f"检索无结果（title={title!r} days={args.days}）",
                        unit="text", currency="")
        downloaded = 0
        for ann in anns:
            headline = (ann.get("TITLE") or "").replace("<br/>", " ")
            long_text = (ann.get("LONG_TEXT") or "").replace("<br/>", " ")
            extra = {
                "category": long_text,
                "pdf_url": HKEX_BASE + (ann.get("FILE_LINK") or ""),
                "news_id": str(ann.get("NEWS_ID") or ""),
                "file_info": ann.get("FILE_INFO") or "",
            }
            if args.download and ann.get("FILE_LINK") and downloaded < args.max_download:
                local = download_pdf(env, code5, ann, "./announcements")
                if local:
                    extra["local_path"] = local
                    downloaded += 1
            add_item(env, "ANN", headline, _hkex_date(ann.get("DATE_TIME")),
                     "hkexnews", "TITLE", unit="text", currency="", extra=extra)
        env["meta"]["field_coverage"] = {"ANN": str(len(anns))}
    except ArgError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        sys.exit(EXIT_ARG)
    except BlockedError as exc:
        add_error(env, exc.endpoint, str(exc), exc.http_status, exc.action_hint)
        _emit(env, args.out)
        print(f"[风控] {exc} | {exc.action_hint}", file=sys.stderr)
        sys.exit(EXIT_BLOCKED)
    except FetchError as exc:
        add_error(env, exc.endpoint, str(exc), exc.http_status, exc.action_hint)
        _emit(env, args.out)
        print(f"[接口故障] {exc} | {exc.action_hint}", file=sys.stderr)
        sys.exit(EXIT_API)
    _emit(env, args.out)
    sys.exit(0)


def _emit(env, out_path):
    text = json.dumps(env, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
