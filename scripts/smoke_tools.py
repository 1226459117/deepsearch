"""P3 冒烟:fts 任务池离线必查;Bocha/Tavily/AkShare 真实链路按 key 有无自动跳。

用法:
    uv run python scripts/smoke_tools.py

任何一步硬失败退出码非 0;未配置 key 的网络步骤打印 SKIP,不算失败。
"""

from __future__ import annotations

import sys
import tempfile
import uuid


def main() -> int:
    from deepsearch.config import get_settings
    from deepsearch.tools import akshare_data, fts, search

    settings = get_settings()
    data_dir = tempfile.mkdtemp(prefix="ds_smoke_")

    # 1. FTS 任务池(离线必查):索引→短词 LIKE→长词 FTS
    tok = fts.ensure_schema(data_dir)
    task = f"smoke-{uuid.uuid4().hex[:6]}"
    doc = fts.index_document(
        task, title="冒烟材料", content="营收 增长 12% 宁德时代", source="bocha",
        data_dir=data_dir,
    )
    assert fts.search(task, "营收", data_dir=data_dir), "短词降级路失效"
    assert fts.search(task, "宁德时代", data_dir=data_dir), "长词 FTS 失效"
    print(f"[1/4] fts OK (tokenizer={tok}, doc={doc[:8]}…)")

    # 2. 网络检索降级边
    if settings.bocha_api_key or settings.tavily_api_key:
        bundle = search.web_search("宁德时代 2025 年报 营收", count=3)
        print(f"[2/4] web_search provider={bundle.provider_used} "
              f"fell_back={bundle.fell_back} results={len(bundle.results)}")
        if bundle.provider_used == "none":
            print("    ⚠️ 两源皆不可达(网络/额度),流水线会按证据缺口处理")
    else:
        print("[2/4] web_search SKIP(未配置 BOCHA/TAVILY key)")

    # 3. AkShare 财务速查(需公网)
    if settings.bocha_api_key or settings.tavily_api_key:  # 有外网迹象才试
        metrics = akshare_data.quick_metrics("600519")
        print(f"[3/4] quick_metrics 600519 status={metrics['status']} "
              f"keys={list(metrics.get('metrics', {}))}")
    else:
        print("[3/4] akshare SKIP(与检索同闸门)")

    # 4. 纯计算指标(离线必查)
    import pandas as pd

    f = akshare_data.compute_indicators({
        "income": pd.DataFrame({"营业总收入": [100.0, 120.0], "净利润": [10.0, 18.0]}),
        "balance": pd.DataFrame({"资产总计": [300.0], "负债总计": [150.0]}),
    })
    assert f["net_margin"] == 15.0 and f["revenue_yoy_pct"] == 20.0
    print("[4/4] compute_indicators OK")

    print("P3 smoke 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
