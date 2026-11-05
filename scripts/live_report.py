"""P6 端到端实跑:真模型 + 真检索完整走一篇研报,验证 ¥5 成本护栏链路。

与 smoke_* 的根本区别:零替身——DeepSeek/Qwen 真调用、Bocha/Tavily 真检索、
Dashscope embedding 真蒸馏(蒸馏走候选队列,PG 不可达时节点内自行降级)。

护栏策略(小额度演练):
- CP1/COST 之外的网关自动应答:CP1 认可大纲、CP2 认可成稿;
- COST 网关(¥5 超限自动暂停)按"抬限"脚本最多 3 次(+¥1/次),仍超限则 abort,
  既演练 pause→raise→resume 全链路,也为止损兜底;
- 全程逐超步打印成本进度。

用法:`uv run python scripts/live_report.py [选题文本]`
默认选题较小(单行业格局分析)以控成本;输出 report_dir 与结算摘要。
"""

from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台兼容 ¥ 等字符
sys.path.insert(0, "src")

COST_RAISE_MAX = 3  # 护栏暂停后最多抬限次数,超过即止损 abort


def main() -> int:
    from deepsearch.config import get_settings
    from deepsearch.graph.build import build_graph, interrupts_of, run_turn
    from deepsearch.graph.checkpoint import open_checkpointer

    topic = " ".join(sys.argv[1:]).strip() or "光伏组件环节产能过剩与价格周期深度研报"
    settings = get_settings()
    tid = f"live-{int(time.time())}"
    print(f"[live] 选题: {topic}")
    print(f"[live] thread={tid} 主档={settings.primary_model} 廉价档={settings.utility_model} "
          f"护栏=¥{settings.cost_limit_cny_per_report}")

    cost_hist: list[float] = []
    with open_checkpointer() as cp:
        g = build_graph(cp)
        out = run_turn(g, tid, topic, route="report")
        cost_hist.append(float(out.get("cost_accrued_cny", 0.0)))
        print(f"[live] 首跑后成本快照: ¥{cost_hist[-1]:.4f}")

        raises = 0
        rounds = 0
        while rounds < 24:
            rounds += 1
            gates = interrupts_of(out)
            if not gates:
                break
            payload = gates[0]
            gate = payload["gate"]
            print(f"[live] ⏸ 网关 {gate}: 已用 ¥{out.get('cost_accrued_cny', 0.0):.4f}")
            if gate == "CP1":
                out = run_turn(g, tid, resume={"approve": True})
            elif gate == "CP2":
                out = run_turn(g, tid, resume={"approve": True})
            elif gate == "COST":
                if raises >= COST_RAISE_MAX:
                    print("[live] ✂ 抬限次数用尽,触发止损 abort(护栏演练完成)")
                    out = run_turn(g, tid, resume={"abort": True})
                    break
                raises += 1
                new_limit = float(payload.get("limit_cny", 5.0)) + 1.0
                print(f"[live] ↑ 抬限至 ¥{new_limit:.1f}(第 {raises} 次)")
                out = run_turn(g, tid, resume={"raise_to": new_limit})
            else:
                print(f"[live] 未知网关 {gate},abort 止损")
                out = run_turn(g, tid, resume={"abort": True})
                break
            cost_hist.append(float(out.get("cost_accrued_cny", 0.0)))

        gates = interrupts_of(out)
        values = out if isinstance(out, dict) else {}
        report_dir = values.get("report_dir", "")
        chapters = values.get("chapters") or {}
        print("\n[live] ===== 结算 =====")
        print(f"[live] 终态网关: {gates or '无(流程走完)'}")
        print(f"[live] 章数: {len(chapters)}  素材: {len(values.get('source_ids') or [])} 篇"
              f"  检索: {values.get('search_calls_used', 0)} 次")
        print(f"[live] 成本轨迹: {[round(c, 4) for c in cost_hist]}")
        print(f"[live] 累计: ¥{float(values.get('cost_accrued_cny', 0.0)):.4f} / "
              f"¥{float(values.get('cost_limit_cny', 0.0)):.2f}")
        if report_dir:
            import os
            for name in ("report.md", "report.html", "report.pdf"):
                p = os.path.join(report_dir, name)
                print(f"[live] {name}: {'✔ ' + str(os.path.getsize(p)) + 'B' if os.path.isfile(p) else '(无)'}")
            print(f"[live] report_dir = {report_dir}")
        ok = bool(report_dir) and not gates
        print(f"[live] 结论: {'✔ 完整成稿' if ok else '△ 未完成(见上方状态,护栏/止损均为预期行为)'}")
        return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
