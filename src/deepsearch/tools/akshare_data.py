"""AkShare 数据管线(定稿 Q6/Q12):三表 + 行情 → parquet → 基础指标。

面向 A 股(6 位代码,东财接口内部自动加 SH/SZ/BJ 前缀)。全程 best-effort:网络/接口异常一律吞成空结果,
data_pipeline 节点据此标记"数据缺口",绝不让主链路崩溃。真实拉取函数与
纯计算函数分离,后者可离线单测。parquet 落 `{reports_dir}/{task}/data/`。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from deepsearch.config import get_settings

_SYMBOL_RE = re.compile(r"\b(\d{6})\b")


def market_symbol(symbol: str) -> str:
    """6 位裸码 → 带市场前缀码(东财三表接口要求 SH/SZ/BJ 前缀)。

    60/68/75/90 → SH;40/80/82/83/87/88/92 → BJ;其余(0/2/3 开头)→ SZ。
    已带前缀的输入原样返回。
    """
    s = symbol.strip().upper()
    if s[:2] in ("SH", "SZ", "BJ"):
        return s
    code = s[-6:] if len(s) >= 6 else s
    if code.startswith(("60", "68", "75", "90")):
        return f"SH{code}"
    if code.startswith(("40", "80", "82", "83", "87", "88", "92")):
        return f"BJ{code}"
    return f"SZ{code}"

# 常见报表列名别名(东财 em 接口返回英文字段码,历史/其他接口则为中文列名)
_REV_KEYS = ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "营业总收入", "营业收入", "总收入")
_NET_KEY = ("PARENT_NETPROFIT", "NETPROFIT", "净利润", "归属于母公司所有者的净利润", "归母净利润")
_ASSET_KEY = ("TOTAL_ASSETS", "资产总计", "总资产", "资产合计")
_LIAB_KEY = ("TOTAL_LIABILITIES", "负债总计", "总负债", "负债合计")


def extract_symbol(text: str) -> str | None:
    """从主题/问题里抽取 6 位 A 股代码;无则 None(数据段可跳过)。"""
    m = _SYMBOL_RE.search(text or "")
    return m.group(1) if m else None


def fetch_three_statements(symbol: str) -> dict[str, pd.DataFrame]:
    """拉资产负债表 / 利润表 / 现金流量表(年度,E东)。失败返回空 dict。"""
    import akshare as ak

    em_symbol = market_symbol(symbol)  # 东财接口要 SH/SZ/BJ 前缀码
    out: dict[str, pd.DataFrame] = {}
    getters = {
        "balance": ak.stock_balance_sheet_by_yearly_em,
        "income": ak.stock_profit_sheet_by_yearly_em,
        "cashflow": ak.stock_cash_flow_sheet_by_yearly_em,
    }
    for name, getter in getters.items():
        try:
            out[name] = getter(symbol=em_symbol)
        except Exception:
            continue
    return out


def fetch_quotes(symbol: str, start: str, end: str) -> pd.DataFrame:
    """日线行情(前复权);start/end 形如 20240101。失败返回空表。"""
    import akshare as ak

    try:
        return ak.stock_zh_a_hist(
            symbol=symbol, period="daily", start_date=start, end_date=end, adjust="qfq"
        )
    except Exception:
        return pd.DataFrame()


def compute_indicators(statements: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """从三表算基础指标(不做 DCF):最新营收/净利/同比、净利率、资产负债率。"""
    findings: dict[str, Any] = {"symbol_metrics": {}}
    income = statements.get("income")
    balance = statements.get("balance")

    if income is not None and not income.empty:
        rev = _latest(income, _REV_KEYS)
        net = _latest(income, _NET_KEY)
        prev_rev = _series(income, _REV_KEYS).iloc[-2] if len(_series(income, _REV_KEYS)) >= 2 else None
        if rev is not None:
            findings["latest_revenue"] = rev
        if net is not None:
            findings["latest_net_profit"] = net
        if rev is not None and net is not None and rev:
            findings["net_margin"] = round(net / rev * 100, 2)
        if rev is not None and prev_rev not in (None, 0) and prev_rev is not None:
            findings["revenue_yoy_pct"] = round((rev - prev_rev) / abs(prev_rev) * 100, 2)

    if balance is not None and not balance.empty:
        asset = _latest(balance, _ASSET_KEY)
        liab = _latest(balance, _LIAB_KEY)
        if asset and liab is not None:
            findings["debt_ratio_pct"] = round(liab / asset * 100, 2)

    return findings


def quick_metrics(symbol: str) -> dict[str, Any]:
    """不存盘的轻量财务速查(factual 档用):拉三表算指标即返回。"""
    try:
        statements = fetch_three_statements(symbol)
    except Exception as exc:
        return {"status": "error", "reason": str(exc), "symbol": symbol}
    findings = compute_indicators(statements)
    metrics = {k: v for k, v in findings.items() if k != "symbol_metrics"}
    return {"status": "ok" if metrics else "empty", "symbol": symbol, "metrics": metrics}


def save_parquet(
    task_id: str, symbol: str, frames: dict[str, pd.DataFrame]
) -> dict[str, str]:
    """三表/行情落 parquet 到 reports/{task}/data/,返回 {name: path}。"""
    if not frames:
        return {}
    data_dir = _report_data_dir(task_id)
    paths: dict[str, str] = {}
    for name, df in frames.items():
        if df is None or df.empty:
            continue
        path = data_dir / f"{symbol}_{name}.parquet"
        df.to_parquet(path, index=False)
        paths[name] = str(path)
    return paths


def run_data_pipeline(task_id: str, text: str, *, start: str = "20230101", end: str = "20260101") -> dict[str, Any]:
    """编排:抽码→拉三表+行情→算指标→存盘。任一步失败均返回带 status 的结果。"""
    symbol = extract_symbol(text)
    if not symbol:
        return {"status": "skipped", "reason": "未识别到 6 位股票代码", "symbol": None}
    try:
        statements = fetch_three_statements(symbol)
        quotes = fetch_quotes(symbol, start, end)
    except Exception as exc:  # 接口整体不可用
        return {"status": "error", "reason": f"AkShare 拉取失败:{exc}", "symbol": symbol}

    findings = compute_indicators(statements)
    frames = {**statements, "quotes": quotes}
    files = save_parquet(task_id, symbol, frames)
    status = "ok" if statements else "empty"
    return {
        "status": status,
        "symbol": symbol,
        "metrics": findings.get("symbol_metrics", {})
        | {k: v for k, v in findings.items() if k != "symbol_metrics"},
        "files": files,
        "rows": {name: int(df.shape[0]) for name, df in frames.items() if df is not None},
    }


def _report_data_dir(task_id: str) -> Path:
    root = Path(get_settings().data_dir).parent / "reports" / task_id / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _series(df: pd.DataFrame, keys: tuple[str, ...]) -> pd.Series:
    """取首个命中的指标列,并按报告期升序重排(东财接口默认降序,最新在前)。"""
    for k in keys:
        if k in df.columns:
            s = pd.to_numeric(df[k], errors="coerce").dropna()
            if not s.empty and "REPORT_DATE" in df.columns:
                dates = pd.to_datetime(df["REPORT_DATE"], errors="coerce")
                order = dates.loc[s.index].argsort(kind="stable")
                s = s.iloc[order]
            return s
    return pd.Series(dtype="float64")


def _latest(df: pd.DataFrame, keys: tuple[str, ...]) -> float | None:
    s = _series(df, keys)
    return float(s.iloc[-1]) if not s.empty else None
