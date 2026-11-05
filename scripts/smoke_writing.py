"""P4 冒烟:研报段流水线在 SqliteSaver 下落盘过 CP1/重写/CP2 全流程(离线模型替身)。

重点验证 MemorySaver 测试覆盖不到的两件事:
① interrupt 网关写进 SQLite checkpoint 后跨 invoke 恢复;
② Command(goto=[Send]) 扇出在持久化 checkpointer 下的扇入锚点(advance_group)。
真网络/真模型全部替身;DATA_DIR 指向临时目录,reports/ 不落真实工作区。
"""

import json
import os
import tempfile

tmp = tempfile.mkdtemp(prefix="ds-p4-smoke-")
os.environ["DATA_DIR"] = os.path.join(tmp, "data")  # 先于 import,reports/ 落 tmp
os.environ["BOCHA_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""

import deepsearch.memory.intercept as i
import deepsearch.memory.llm as llm
import deepsearch.memory.profile as pf
import deepsearch.memory.service as s
import deepsearch.tools.search as w
from deepsearch.graph.build import build_graph, interrupts_of, run_turn
from deepsearch.graph.checkpoint import open_checkpointer
from deepsearch.memory.llm import ChatResult
from deepsearch.tools.search import SearchBundle

reviews = {"n": 0}


def fake_chat(messages, *, tier="primary", temperature=0.3, max_tokens=None):
    system = messages[0]["content"]
    if "入口分诊器" in system:
        return ChatResult(content='{"route":"report","confidence":0.9,"topic":"低空经济"}')
    if "投研规划师" in system:
        return ChatResult(content='{"questions": ["市场规模?", "竞争格局?"]}')
    if "报告架构师" in system:
        return ChatResult(content=json.dumps({"chapters": [
            {"title": f"第{i}章", "bullets": [f"要点{i}"]} for i in range(1, 5)]}))
    if "资深行业分析师" in system:
        title = system.split("撰写中文深度研报的一章:")[1].split("。")[0]
        return ChatResult(content=f"{title}的正文,数据见 [1]。")
    if "一致性审稿编辑" in system:
        reviews["n"] += 1
        if reviews["n"] == 1:
            return ChatResult(content=json.dumps({"dirty": [
                {"id": "c2", "dimensions": ["数据"], "note": "统一口径"}], "summary": "x"}))
        return ChatResult(content='{"dirty": [], "summary": "合格"}')
    return ChatResult(content="")


llm.chat = fake_chat
llm.chat_with_tools = lambda *a, **k: ChatResult(content="")
s.post_turn = lambda *a, **k: {}
s.chat_context = lambda *a, **k: [{"role": "user", "content": "x"}]
i.injection_for_node = lambda *a, **k: ""
w.web_search = lambda q, count=5: SearchBundle([], "none", False)
pf.distill = lambda text, stype, ref, **kw: {"fact": 0, "profile": 0}

with open_checkpointer(tmp) as cp:
    g = build_graph(cp)
    out = run_turn(g, "smoke-p4", "低空经济产业链研报", route="report")
    assert interrupts_of(out)[0]["gate"] == "CP1", interrupts_of(out)
    for _ in range(6):
        gates = interrupts_of(out)
        if not gates:
            break
        gate = gates[0]["gate"]
        if gate == "CP1":
            out = run_turn(g, "smoke-p4", resume={"approve": True})
        elif gate == "COST":
            out = run_turn(g, "smoke-p4", resume={"raise_to": 999.0})
        elif gate == "CP2":
            out = run_turn(g, "smoke-p4", resume={
                "edits": {"c1": "冒烟人工首章"}, "resolutions": []})
        else:
            raise AssertionError(f"未知 gate: {gate}")

assert interrupts_of(out) == []
assert len(out["chapters"]) == 4 and "冒烟人工首章" in out["chapters"]["c1"]
assert reviews["n"] == 2, reviews
md = os.path.join(out["report_dir"], "report.md")
html = os.path.join(out["report_dir"], "report.html")
assert os.path.isfile(md) and os.path.isfile(html), (md, html)
body = open(md, encoding="utf-8").read()
assert "冒烟人工首章" in body and "免责声明" in body
print("P4 SqliteSaver 全流水线 OK: CP1→Send 扇出→重写轮→CP2→assemble→export")
print("report_dir =", out["report_dir"])
print("chapters =", len(out["chapters"]), " reviews =", reviews["n"],
      " pdf =", os.path.exists(os.path.join(out["report_dir"], "report.pdf")))
