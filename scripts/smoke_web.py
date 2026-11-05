"""P5 冒烟:生产装配(TestClient 触发 lifespan:SqliteSaver+队列+后台 worker
loop 真跑),替身模型走 report→CP1→approve→CP2→approve→done 全链。

与 pytest 的区别:这里不手动 run_once,靠 app 生命周期里自启的 asyncio
worker 串行消费,验证线程/loop 事件发布与 requeue 恢复的真实并发路径。
DATA_DIR 指向临时目录,模型/网络全替身,零成本零真实外呼。
"""

import json
import os
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="ds-p5-smoke-")
os.environ["DATA_DIR"] = os.path.join(tmp, "data")
os.environ["BOCHA_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""

import deepsearch.memory.intercept as i
import deepsearch.memory.llm as llm
import deepsearch.memory.profile as pf
import deepsearch.memory.service as s
import deepsearch.tools.search as w
from fastapi.testclient import TestClient
from deepsearch.graph.checkpoint import checkpoint_db_path
from deepsearch.memory.llm import ChatResult

reviews = {"n": 0}


def fake_chat(messages, **k):
    sysm = messages[0]["content"]
    if "入口分诊器" in sysm:
        return ChatResult(content='{"route":"report","confidence":0.9,"topic":"固态电池"}')
    if "投研规划师" in sysm:
        return ChatResult(content='{"questions": ["技术路线?", "量产时点?"]}')
    if "报告架构师" in sysm:
        return ChatResult(content=json.dumps({"chapters": [
            {"title": f"第{n}章", "bullets": [f"要点{n}"]} for n in range(1, 4)]}))
    if "资深行业分析师" in sysm:
        title = sysm.split("撰写中文深度研报的一章:")[1].split("。")[0]
        return ChatResult(content=f"{title}正文,数据见 [1]。")
    if "一致性审稿编辑" in sysm:
        reviews["n"] += 1
        return ChatResult(content='{"dirty": [], "summary": "合格"}')
    return ChatResult(content="冒烟回复。")


llm.chat = fake_chat
llm.chat_with_tools = lambda *a, **k: ChatResult(content="冒烟回复。")
s.post_turn = lambda *a, **k: {}
s.chat_context = lambda *a, **k: [{"role": "user", "content": "x"}]
i.injection_for_node = lambda *a, **k: ""
w.web_search = lambda q, count=5: w.SearchBundle([], "none", False)
pf.distill = lambda *a, **k: {"fact": 0, "profile": 0}

from deepsearch.web.main import create_app  # noqa: E402  (替身先行)


def wait_for(client, tid, want, timeout=30.0):
    """轮询 panel 片段直到包含期望状态,返回面板文本。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = client.get(f"/threads/{tid}/panel").text
        if want in text:
            return text
        time.sleep(0.2)
    raise AssertionError(f"等待 {want!r} 超时,最后面板:\n{text[-800:]}")


client = TestClient(create_app())
with client:  # 触发 lifespan:真后台 worker 循环启动
    r = client.post("/tasks", data={"topic": "固态电池产业链", "route": ""},
                    follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    print("thread =", tid)

    # 后台 worker 自动消费:自动分诊 → 调研段 → outline_gen → CP1 必停
    panel = wait_for(client, tid, "CP1 · 大纲确认闸")
    assert "认可大纲" in panel
    print("[1/4] 自动分诊+调研段+大纲 → 后台停在 CP1 OK")

    # SSE 端点可建流(读响应头即退,不阻塞)
    with client.stream("GET", f"/threads/{tid}/events") as resp:
        assert resp.status_code == 200, resp.status_code
    print("[2/4] SSE /events 建流 OK")

    r = client.post(f"/threads/{tid}/gate", data={"action": "approve"},
                    follow_redirects=False)
    assert r.status_code == 303
    panel = wait_for(client, tid, "CP2 · 成稿确认闸")  # 写作+审稿+停在 CP2
    assert "edit_c1" in panel
    print("[3/4] resume CP1 → 3 章写作(1 组)→ 审稿 → CP2 OK")

    client.post(f"/threads/{tid}/gate",
                data={"action": "submit", "edit_c1": "冒烟人工首章"},
                follow_redirects=False)
    panel = wait_for(client, tid, "badge done")
    report_md = None
    for base in (os.path.join(tmp, "reports"),):
        if os.path.isdir(base):
            for d in os.listdir(base):
                cand = os.path.join(base, d, "report.md")
                if os.path.isfile(cand):
                    report_md = cand
    assert report_md, "report.md 未落盘"
    body = open(report_md, encoding="utf-8").read()
    assert "冒烟人工首章" in body and "免责声明" in body
    r = client.get("/reports")
    assert r.status_code == 200 and "report.md" in r.text
    print("[4/4] resume CP2 → 装配导出落盘 + /reports 浏览 OK")
    print("report_md =", report_md)

print("P5 web smoke 全部通过(checkpoints =",
      str(checkpoint_db_path(os.path.join(tmp, 'data'))), ")")
