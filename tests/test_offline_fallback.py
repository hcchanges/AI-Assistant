"""
断网兜底（v2-offline-fallback）的自动测试。

统一 LLM 调用层（brain/llm/factory.py）在遇到网络层失败（Connection error / 超时）时，
不再抛「调用失败」让上层报「没听懂」，而是转成 LLMOfflineError、标记一段冷却期，让三个
调用点各走各的本地兜底：

- 主聊天（解析那条腿，life/chatter.py）→ 按熟悉度挑一句陪伴寒暄；
- 社交帮回（life/screen/social.py）→ 本地候选，保留「候选 + 解析」结构，
  并按熟悉度挑（越熟说得越随便；等级取不到才回模块里那条默认）；
- 抓屏笔记（life/screen/notes.py）→ 提示「断网暂无法整理，联网后重试」，这批字塞回队首。

要验的只有一条分界线：**网络层失败 = 断网 = 该兜底；4xx/5xx（Key 错 / 限流 / 接口报错）
不是断网，照旧走老报错路。** 联网恢复后自动回 API，不用重启（冷却期一过就回）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

import brain.llm.factory as factory
from brain.llm.base import LLMError, LLMOfflineError
from brain.llm.factory import LLMFactory, OpenAICompatibleProvider
from openai import APIConnectionError, APIStatusError
from life.screen import social as smod


class _OfflineProvider:
    """模拟断网：所有模型调用都抛 LLMOfflineError（等于 Connection error 已被工厂转成离线）。

    名字里带「断网」是给断言看的——真机上是「deepseek/deepseek-chat 断网: Connection error.」。
    """

    name = "offline"
    model_name = "offline-model"

    def chat(self, prompt, system=None):
        raise LLMOfflineError("offline/offline-model 断网: Connection error.")

    def chat_json(self, prompt, system=None):
        raise LLMOfflineError("offline/offline-model 断网: Connection error.")

    def chat_with_image(self, prompt, image, system=None):
        raise LLMOfflineError("offline/offline-model 断网: Connection error.")

    def chat_messages(self, *args, **kwargs):
        raise LLMOfflineError("offline/offline-model 断网: Connection error.")


def main() -> int:
    # ---------- S1 工厂层：网络失败识别 + 冷却 ----------
    section("S1 工厂层：Connection error → 离线；4xx/5xx 不算；冷却期内不发 API")
    factory._offline_until = 0.0

    class _FakeModel:
        def invoke(self, messages):
            return "hi"

        def bind(self, **kwargs):
            return self

    prov = OpenAICompatibleProvider(_FakeModel(), "deepseek", "deepseek-chat")

    check("正常调用原样返回", prov._invoke(lambda: "ok", "调用失败") == "ok", "")

    factory._offline_until = 0.0

    def conn_fail():
        raise APIConnectionError(request=None)

    try:
        prov._invoke(conn_fail, "调用失败")
        check("网络失败应抛 LLMOfflineError", False, "没抛")
    except LLMOfflineError as exc:
        check("网络失败抛 LLMOfflineError", "断网" in str(exc), str(exc))
    check("网络失败后进入离线冷却", factory._offline_now() is True, "")

    called = []
    try:
        prov._invoke(lambda: called.append(1) or "x", "调用失败")
        check("冷却期内应抛 LLMOfflineError", False, "没抛")
    except LLMOfflineError:
        check("冷却期内抛 LLMOfflineError", True, "")
    check("冷却期内没真的发 API", called == [], str(called))

    factory._offline_until = 0.0

    def status_fail():
        raise APIStatusError("err", response=None, body=None)

    try:
        prov._invoke(status_fail, "调用失败")
        check("4xx/5xx 应抛 LLMError", False, "没抛")
    except LLMError as exc:
        check("4xx/5xx 抛的是 LLMError（不是离线）", not isinstance(exc, LLMOfflineError),
              type(exc).__name__)
    check("4xx/5xx 不进离线冷却（不是断网，兜底兜不住）", factory._offline_now() is False, "")

    factory._offline_until = time.monotonic() + 999
    check("冷却期内确实算离线", factory._offline_now() is True, "")
    factory._offline_until = time.monotonic() - 1
    check("冷却期一过 → 自动回 API（无需重启）", factory._offline_now() is False, "")
    check("冷却期过了 → 调用真的发下去了", prov._invoke(lambda: "back", "调用失败") == "back", "")

    factory._offline_until = 0.0

    # ---------- S2 调用点：断网走本地、联网走 API ----------
    box, stub, app = make_env()
    spoken: list[str] = []
    toasts: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.toast.connect(toasts.append)

    offline = _OfflineProvider()

    section("S2 联网时：主聊天照常走 API（回归，不是断网也走本地）")
    spoken.clear()
    toasts.clear()
    app.chatter_service.handle("晚上好啊")
    wait_for(lambda: bool(spoken))
    check("联网时正常回话、不冒「没听懂」", bool(spoken) and "没听懂" not in spoken[-1],
          str(spoken[-1:]))

    section("S2 断网时：主聊天改说本地陪伴寒暄，不报「没听懂」")
    LLMFactory.create = staticmethod(lambda config, provider_name=None: offline)
    spoken.clear()
    toasts.clear()
    app.chatter_service.handle("晚上好啊")
    wait_for(lambda: bool(spoken) or bool(toasts))
    check("断网 → 宠物说了本地陪伴寒暄（带「联网」）",
          bool(spoken) and "联网" in spoken[-1], str(spoken[-1:]))
    check("断网 → 没冒「宠物没听懂」toast",
          not any("没听懂" in t for t in toasts), str(toasts))

    section("S2 断网时：软表达仍先走那套调过的本地关怀（比一句寒暄贴心）")
    from life.psych.crisis import SOFT_CARE_REPLY

    spoken.clear()
    app.psych_service.respond("最近真的撑不下去了")
    wait_for(lambda: bool(spoken))
    check("断网 + 软表达 → 本地关怀（不是陪伴寒暄）",
          spoken[-1:] == [SOFT_CARE_REPLY], str(spoken[-1:]))

    spoken.clear()
    app.psych_service.respond("今天中午吃了点面条")
    wait_for(lambda: bool(spoken))
    check("断网 + 普通话 → 本地陪伴寒暄",
          bool(spoken) and "联网" in spoken[-1], str(spoken[-1:]))

    section("S2 断网时：社交帮回改走本地候选（按熟悉度挑，保留候选+解析结构）")
    from life.mind.intimacy import OFFLINE_SOCIAL_BY_STAGE

    level = int(app.mind_intimacy.state()["level"])
    result = app.social_reply._write("帮我回一下，同事问我周末加不加班", chat_text="")
    one = (result.get("candidates") or [{}])[0]
    check("断网 → 本地候选，结构照旧（1 条，draft + why）",
          len(result.get("candidates") or []) == 1
          and bool(one.get("draft")) and bool(one.get("why")), str(result))
    check("断网那句是按当前熟悉度挑的（接线通了，不是写死那一条）",
          (one.get("draft"), one.get("why")) == OFFLINE_SOCIAL_BY_STAGE[level],
          f"level={level} {one}")
    check("断网那句不报「没听懂」、点明联网就帮你拟",
          "没听懂" not in str(one.get("draft"))
          and "联网" in str(one.get("draft")), str(one))
    # 沙箱库（不是真库）里把亲密度推到挚友：接线是活的就该当场换句——若 bootstrap 是建厂
    # 时抄了一份等级，这里会停在上面那句上。
    app.mind_store.save_intimacy(5, 999)
    check("沙箱推到挚友 → 断网那句当场换成挚友档",
          app.mind_intimacy.offline_social_candidates()
          == {"candidates": [{"draft": OFFLINE_SOCIAL_BY_STAGE[5][0],
                              "why": OFFLINE_SOCIAL_BY_STAGE[5][1]}]},
          str(app.mind_intimacy.offline_social_candidates()))
    app.mind_store.save_intimacy(1, 0)

    section("S1 五档表：五句各不相同、每句都带解析")
    check("五档齐（键同 STAGE_NAMES）",
          set(OFFLINE_SOCIAL_BY_STAGE) == {1, 2, 3, 4, 5}, str(sorted(OFFLINE_SOCIAL_BY_STAGE)))
    check("五档的 draft 两两不同（真分了档，不是同一句抄五遍）",
          len({v[0] for v in OFFLINE_SOCIAL_BY_STAGE.values()}) == 5, "")
    check("五档的 why 也各不相同",
          len({v[1] for v in OFFLINE_SOCIAL_BY_STAGE.values()}) == 5, "")
    check("每档的解析都点明是断网兜底",
          all(w.startswith("断网兜底") for _, w in OFFLINE_SOCIAL_BY_STAGE.values()), "")

    saved_state = app.mind_intimacy.state
    seen = []
    try:
        for lv in range(1, 6):
            app.mind_intimacy.state = lambda lv=lv: {"level": lv}
            got = app.mind_intimacy.offline_social_candidates() or {}
            seen.append((got.get("candidates") or [{}])[0].get("draft"))
    finally:
        app.mind_intimacy.state = saved_state
    check("走一遍五档 → 依次给出五句不同的本地候选（真按等级分支）",
          seen == [OFFLINE_SOCIAL_BY_STAGE[i][0] for i in range(1, 6)], str(seen))

    app.mind_intimacy.state = lambda: {"level": 99}
    check("等级超出表 → 回 None，交给调用方用默认那条",
          app.mind_intimacy.offline_social_candidates() is None, "")
    app.mind_intimacy.state = lambda: (_ for _ in ()).throw(RuntimeError("库读不了"))
    check("等级读不出来也不抛（兜底文案不该两级都塌）",
          app.mind_intimacy.offline_social_candidates() is None, "")
    app.mind_intimacy.state = saved_state

    saved_provider = app.social_reply._offline_candidates_provider
    app.social_reply.set_offline_candidates_provider(None)
    check("没接来源（纯模块级）→ 仍是 OFFLINE_CANDIDATES 那一条",
          app.social_reply._offline_candidates() == smod.OFFLINE_CANDIDATES, "")
    app.social_reply.set_offline_candidates_provider(
        lambda: (_ for _ in ()).throw(RuntimeError("取不出来"))
    )
    check("来源自己炸了 → 落到 OFFLINE_CANDIDATES，不往上抛",
          app.social_reply._offline_candidates() == smod.OFFLINE_CANDIDATES, "")
    app.social_reply.set_offline_candidates_provider(saved_provider)

    profile = app.social_reply._extract("她喜欢喝咖啡")
    check("断网 → 抽对方信息跳过（本地无此能力）", profile == "", str(profile))

    section("S2 断网时：笔记整理提示重试 + 这批字塞回队首")
    notes = app.screen_notes
    notes._running = True
    notes._inflight = ["甲", "乙"]
    notes._buffer = ["旧"]
    toasts.clear()
    notes._on_failed(LLMOfflineError("offline/offline-model 断网"))
    check("断网 → toast 提示「联网后重试」",
          any("断网暂无法整理" in t for t in toasts), str(toasts))
    check("断网 → 这批字塞回队首、没丢",
          notes._inflight is None and notes._buffer[:2] == ["甲", "乙"], str(notes._buffer))
    check("断网 → 忙态清干净（下一轮能再试）", notes._running is False, "")

    drain_threads()
    box.cleanup()
    return summary("断网兜底")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
