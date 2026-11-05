"""P2 冒烟:SqliteSaver 落盘 + 同 thread 跨调用累加续跑(离线模型替身)。"""

import os
import tempfile

import deepsearch.memory.intercept as i
import deepsearch.memory.llm as llm
import deepsearch.memory.service as s
from deepsearch.graph.build import build_graph, run_turn
from deepsearch.graph.checkpoint import open_checkpointer
from deepsearch.memory.llm import ChatResult

llm.chat = lambda *a, **k: ChatResult(content='{"route":"steering","confidence":1.0}')
s.post_turn = lambda *a, **k: {}
s.chat_context = lambda *a, **k: [{"role": "user", "content": "x"}]
i.injection_for_node = lambda *a, **k: ""

d = tempfile.mkdtemp()
with open_checkpointer(d) as cp:
    g = build_graph(cp)
    run_turn(g, "resume-1", "继续补充现金流分析", route="steering")
    out = run_turn(g, "resume-1", "再停一下", route="steering")

texts = [x["text"] for x in out["pending_directives"]]
db = os.path.join(d, "checkpoints.sqlite")
assert texts == ["继续补充现金流分析", "再停一下"], texts
assert out["stop_requested"] is True
assert os.path.exists(db), db
print("P2 SqliteSaver 落盘续跑 OK  directives =", texts)
print("checkpoint db =", db, os.path.getsize(db), "bytes")
