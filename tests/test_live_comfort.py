"""
真调模型测"安抚回话"（要联网、要花几分钱，加 --live 才跑）。

指令 26-A 的验收：同一句"好累"连问 3 次——回复不重复，且至少一次引用记得的事；
轻 / 中 / 重三级说出来的话明显不一样。桩模型只能证明"请求里带没带分寸段"
（那部分在 test_comfort.py 里），"说出来是什么样"只有真调才知道。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, note, section, skip, summary

# 事先塞进记忆的一条事实：回话里提到其中任意一个，就算"引用了记得的事"
MEMORY_LINE = "用户周三加班到晚上 11 点，连着几天都没早睡"
MEMORY_WORDS = ("加班", "11 点", "11点", "周三", "早睡")
REPEAT_TEXT = "好累"
REPEAT_TIMES = 3

# (级别, 情绪分, 这句话)
LEVEL_CASES = (
    ("轻", 0.2, "今天有点累"),
    ("中", 0.5, "最近压力好大，什么都提不起劲"),
    ("重", 0.9, "我快崩溃了，连续失眠好几天"),
)


def main() -> int:
    box, _stub, app = make_env(with_stub=False)
    try:
        if not app.config.get_api_key():
            skip("真模型安抚验收", "这台机器没配 API Key（设置页填一个再来）")
            return summary("安抚回话（真模型）")

        app.memory_store.remember("event", MEMORY_LINE, 0.9)

        section(f"同一句「{REPEAT_TEXT}」连问 {REPEAT_TIMES} 次")
        replies = [
            app.psych_service._compose(REPEAT_TEXT, 0.5).strip()
            for _ in range(REPEAT_TIMES)
        ]
        for index, reply in enumerate(replies, start=1):
            note(f"第 {index} 次：{reply}")
        check(
            "三次回复没有一次重复",
            len(set(replies)) == REPEAT_TIMES,
            f"不重复 {len(set(replies))} / {REPEAT_TIMES} 条",
        )
        hit = [reply for reply in replies if any(w in reply for w in MEMORY_WORDS)]
        check(
            "至少一次引用了记得的事（周三 / 加班 / 11 点）",
            bool(hit),
            (hit[0][:40] + "…") if hit else "三次都没提到",
        )

        section("轻 / 中 / 重三级的回话")
        lines = []
        for label, score, text in LEVEL_CASES:
            reply = app.psych_service._compose(text, score).strip()
            lines.append(reply)
            note(f"{label}（{score}）：{reply}")
        check("三级回复互不相同", len(set(lines)) == len(LEVEL_CASES), f"{len(set(lines))} 种")
        note(
            "重的这一档提到休息/找人的话："
            + ("有" if any(w in lines[-1] for w in ("休息", "睡", "找人", "医生", "陪")) else "没有")
        )
        return summary("安抚回话（真模型）")
    finally:
        box.cleanup()


if __name__ == "__main__":
    sys.exit(main())
