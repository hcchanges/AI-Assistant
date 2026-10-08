"""
聊天回复情绪与字数镜像对方（v2-chat-mirror）的自动测试。

用户真机反馈：回复"情绪平、字数恒定"——对方兴致勃勃说一大段，和只回一个"嗯"，
宠物都是同一套恒温的软萌短句。根子在两处：人设里写死"每次只说 1~2 句"
（pet/persona.py 的 style_rules），生成参数又固定 max_tokens=300（life/psych/casual.py）。

这一轮按"这一句的情绪能量"把对方的节奏认出来（积极 / 平淡 / 低落 / 愤怒 / 中性），
写进提示词并给出字数预算。三条对应用户任务书 S4：

① 用户积极多聊 → 宠物积极长回应；
② 用户平淡"嗯/哦" → 宠物简短平淡、不硬凑情绪；
③ 回复情绪随对方起伏（低落→安抚、愤怒→不火上浇油）。

v2-chat-mirror 的字数跟随补强（⑥）：光有"劲头 → 粗预算"短期看还是压不住——实机里短句照样
被抻长。这一补按对方这句的**字数**给出回话该落在的字数区间：≤8 字 ≈1~1.5 倍、硬顶 +8 字；
短句另压一道 token 顶（`budget_for_text`）。v2-pet-persona-rename 起人格尾缀没了，
区间就是正文本身，提示词里不再有「尾缀不算字数」那条豁免。

红线：pet.db / mind.db 零写入、零新依赖。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary

from life.dialog import mood
from life.dialog.router import DialogRoute, INTENT_CHAT, INTENT_VENT
from pet.persona import resolve_voice

ROOT = Path(__file__).resolve().parent.parent

# 用户真机会说的话：一条一条对上去，看认出来的能量对不对
LONG_BRIGHT = "我跟你讲，今天公司楼下新开了家店，我中午去吃了，那个味道是真的可以，下次带你去"
SHORT_FLAT = ("嗯", "哦", "好", "在的", "知道了")
NEUTRAL_TEXT = "我做了红烧肉"                     # 6 字：中性**短句**（走短句顶）
# 中性**长句**（>8 字所以不吃短句顶；<14 字所以不被算成"积极"）。energy_of 里
# "字数 ≥ 14 就认作积极"那条，决定了"中性长句"只可能落在 9~13 字这一小段。
LONG_NEUTRAL = "我买了个新键盘手感还行"   # 11 字：中性、不吃短句顶 → 照旧 300
LOW_TEXT = "今天好累啊，什么都不想干"
ANGRY_TEXT = "气死我了，这人怎么这样"


def _system_of(messages) -> str:
    return "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    # ---------- ① 情绪能量认得出来 ----------

    section("① 认出对方这一句的情绪能量（纯规则）")

    for line in SHORT_FLAT:
        check(f"「{line}」→ 平淡", mood.energy_of(line) == mood.ENERGY_FLAT,
              str(mood.energy_of(line)))
    check("一段起劲的长话 → 积极", mood.energy_of(LONG_BRIGHT) == mood.ENERGY_BRIGHT,
          str(mood.energy_of(LONG_BRIGHT)))
    check("「哈哈哈哈今天太开心了」→ 积极",
          mood.energy_of("哈哈哈哈今天太开心了") == mood.ENERGY_BRIGHT,
          str(mood.energy_of("哈哈哈哈今天太开心了")))
    check("「今天好累啊」→ 低落", mood.energy_of(LOW_TEXT) == mood.ENERGY_LOW,
          str(mood.energy_of(LOW_TEXT)))
    check("「我好紧张」→ 低落（焦虑同走安抚）",
          mood.energy_of("明天要面试，我好紧张") == mood.ENERGY_LOW,
          str(mood.energy_of("明天要面试，我好紧张")))
    check("「气死我了」→ 愤怒", mood.energy_of(ANGRY_TEXT) == mood.ENERGY_ANGRY,
          str(mood.energy_of(ANGRY_TEXT)))
    check("一句平常话 → 中性", mood.energy_of(NEUTRAL_TEXT) == mood.ENERGY_NEUTRAL,
          str(mood.energy_of(NEUTRAL_TEXT)))
    check("空串 → 中性（不是平淡）", mood.energy_of("") == mood.ENERGY_NEUTRAL,
          str(mood.energy_of("")))
    check("不接路由也认得出（自己现算一遍）",
          mood.energy_of(LOW_TEXT, None) == mood.ENERGY_LOW, "")
    check("接了路由就听路由的（模型给的情绪优先于字数）",
          mood.energy_of("嗯嗯", DialogRoute(INTENT_VENT, "愤怒")) == mood.ENERGY_ANGRY, "")
    check("五种能量齐了、没有第六种",
          set(mood.ENERGY_ORDER) == {mood.ENERGY_BRIGHT, mood.ENERGY_FLAT,
                                     mood.ENERGY_LOW, mood.ENERGY_ANGRY,
                                     mood.ENERGY_NEUTRAL},
          str(mood.ENERGY_ORDER))

    # ---------- ② 字数预算跟着对方走 ----------

    section("② 字数预算：积极 > 中性 > 平淡")

    flat, neutral, bright = (
        mood.budget_for_energy(mood.ENERGY_FLAT),
        mood.budget_for_energy(mood.ENERGY_NEUTRAL),
        mood.budget_for_energy(mood.ENERGY_BRIGHT),
    )
    check(f"平淡 {flat} < 中性 {neutral}", flat < neutral, "")
    check(f"中性 {neutral} < 积极 {bright}", neutral < bright, "")
    check("中性就是原来那个数（认不出来时一个字不改）", neutral == 300, str(neutral))
    check("认不出来的能量回落到中性",
          mood.budget_for_energy("不存在") == neutral, "")

    # ---------- ③ 提示词：该热络热络、该平实平实 ----------

    section("③ 提示词按能量给不同的分寸，且不硬凑情绪")

    flat_hint = mood.mirror_hint("嗯")
    bright_hint = mood.mirror_hint(LONG_BRIGHT)
    low_hint = mood.mirror_hint(LOW_TEXT)
    angry_hint = mood.mirror_hint(ANGRY_TEXT)

    check("平淡那条点明「对方这一句是「平淡」」",
          mood.ENERGY_FLAT in flat_hint, flat_hint)
    check("平淡那条要求：不硬凑情绪、不强行关心、不追问",
          all(word in flat_hint for word in ("硬凑情绪", "强行关心", "追问")), flat_hint)
    check("平淡那条要求短回一句", "短回一句" in flat_hint, "")
    check("积极那条点明「积极」，并允许多说两句、有来有回",
          mood.ENERGY_BRIGHT in bright_hint
          and "多接两句" in bright_hint and "有来有回" in bright_hint, bright_hint)
    check("低落那条要求放软、别轻快、别讲道理",
          all(word in low_hint for word in ("放软", "别轻快", "别讲道理")), low_hint)
    check("愤怒那条要求顺着情绪、别火上浇油",
          all(word in angry_hint for word in ("顺着", "别争对错", "火上浇油")), angry_hint)
    check("四条提示词互不相同（回复情绪确实随对方起伏）",
          len({flat_hint, bright_hint, low_hint, angry_hint}) == 4, "")
    check("每一条都带总纲：不必死守「每次只说 1~2 句」",
          all(mood.MIRROR_TAIL in h for h in (flat_hint, bright_hint, low_hint, angry_hint)), "")
    check("中性也给提示（不是空串，否则恒定萌系又回来了）",
          mood.mirror_hint(NEUTRAL_TEXT).strip() != ""
          and mood.ENERGY_NEUTRAL in mood.mirror_hint(NEUTRAL_TEXT), "")

    # ---------- ④ 真跑一遍回话路：提示词进 system、字数预算进生成参数 ----------

    box, stub, app = make_env()
    try:
        svc = app.psych_service
        route = DialogRoute(INTENT_CHAT, "平静")

        section("④ 真跑闲聊路：平淡 → 短提示 + 小预算")

        stub.messages.clear()
        stub.params.clear()
        svc._compose("嗯", 0.0, route)
        sys_text = _system_of(stub.messages[-1])
        check("平淡那一轮的 system 里有「跟上对方的节奏｜对方这一句是「平淡」」",
              "跟上对方的节奏" in sys_text and mood.ENERGY_FLAT in sys_text, "")
        check("平淡那一轮不给长预算：max_tokens 就是平淡那个数",
              stub.params[-1]["max_tokens"] == flat, str(stub.params[-1]["max_tokens"]))

        section("④ 真跑闲聊路：积极 → 长提示 + 大预算")

        stub.messages.clear()
        stub.params.clear()
        svc._compose(LONG_BRIGHT, 0.0, route)
        sys_text = _system_of(stub.messages[-1])
        check("积极那一轮的 system 里有「跟上对方的节奏｜对方这一句是「积极」」",
              "跟上对方的节奏" in sys_text and mood.ENERGY_BRIGHT in sys_text, "")
        check("积极那一轮给长预算",
              stub.params[-1]["max_tokens"] == bright, str(stub.params[-1]["max_tokens"]))
        check("积极那轮的预算确实比平淡那轮大",
              stub.params[-1]["max_tokens"] > flat, "")

        section("④ 中性长句那轮照旧；中性短句现在走短句顶（v2-chat-mirror 字数跟随补强）")

        # 中性**长**句还是能量的那个数，一个字不改。
        stub.messages.clear()
        stub.params.clear()
        svc._compose(LONG_NEUTRAL, 0.0, route)
        check("中性长句那轮的 max_tokens 还是 300（能量那档没动）",
              stub.params[-1]["max_tokens"] == 300, str(stub.params[-1]["max_tokens"]))

        # NEUTRAL_TEXT 是 6 个字的**短句**：按补强的字数规则压到短句那档，
        # 不再放开 300——他随口一句，回话不该抻成一大段。
        stub.messages.clear()
        stub.params.clear()
        svc._compose(NEUTRAL_TEXT, 0.0, route)
        n_neutral = len(NEUTRAL_TEXT)
        check(f"中性短句「{NEUTRAL_TEXT}」（{n_neutral} 字）压到短句顶 {mood.SHORT_MAX_TOKENS}",
              stub.params[-1]["max_tokens"] == mood.SHORT_MAX_TOKENS,
              str(stub.params[-1]["max_tokens"]))
        check("短句顶确实比能量那档（300）小",
              stub.params[-1]["max_tokens"] < 300, "")

        section("④ 求助/倾诉那条专业路也带上节奏（两条路共用）")

        stub.messages.clear()
        svc._compose("我最近好累，撑不住了", 0.7, DialogRoute(INTENT_VENT, "低落"))
        pro_sys = _system_of(stub.messages[-1])
        check("专业框架还在（「回应准则」没被冲掉）", "回应准则" in pro_sys, "")
        check("专业路也带了「跟上对方的节奏」", "跟上对方的节奏" in pro_sys, "")
        check("低落那轮点明「低落」并要安抚",
              mood.ENERGY_LOW in pro_sys and "放软" in pro_sys, "")

        # ---------- ⑤ 人设里那条死规矩降成常态 ----------

        section("⑤ 去除「恒定无波动」：人设那条 1~2 句不再是铁律")

        rules = resolve_voice(app.config.pet).style_rules()
        short_rule = next((r for r in rules if "1~2 句" in r), "")
        check("人设里那条短句规则还在（常态不变）", "1~2 句" in short_rule, short_rule)
        check("但它不再是铁律：写明对方起劲可以多说两句",
              "多说两句" in short_rule, short_rule)
        check("原来自称 1~2 句的写法已改掉（不再是「每次只说」）",
              "每次只说 1~2 句" not in short_rule, short_rule)

        # ---------- ⑥ 字数跟随的数值策略（v2-chat-mirror 补强） ----------

        section("⑥ 字数跟随：回多长说成具体几个字，短句更紧")

        lo6, hi6 = mood.reply_char_span(NEUTRAL_TEXT)
        check(f"短句「{NEUTRAL_TEXT}」（{len(NEUTRAL_TEXT)} 字）区间是 [{len(NEUTRAL_TEXT)},{hi6}]",
              lo6 == len(NEUTRAL_TEXT), f"{lo6},{hi6}")
        check("短句上界 ≤ 字数+8（任务书那条硬顶）",
              hi6 <= len(NEUTRAL_TEXT) + mood.REPLY_MAX_EXTRA, str(hi6))
        check("短句上界约 1~1.5 倍（不超过 1.5 倍）",
              hi6 <= -(-3 * len(NEUTRAL_TEXT) // 2), str(hi6))
        check("短句不比对方短（下限就是字数）", lo6 >= len(NEUTRAL_TEXT), str(lo6))

        # 长句：同样 1~1.5 倍、硬顶 +8，也就是"略长但克制"，不滚雪球。
        loL, hiL = mood.reply_char_span(LONG_BRIGHT)
        check("长句上界仍是字数+8（克制、不滚雪球）",
              hiL == len(LONG_BRIGHT) + mood.REPLY_MAX_EXTRA, str(hiL))
        check("长句上限不超过对方的 1.5 倍", hiL <= 1.5 * len(LONG_BRIGHT), str(hiL))

        check("空串不给区间（对方一个字没说）", mood.reply_char_span("") == (0, 0), "")
        check("空串也不给字数提示", mood.length_hint("") == "", repr(mood.length_hint("")))
        check("五个字以内全落在短句档",
              all(mood.reply_char_span(x)[1] <= len(x) + mood.REPLY_MAX_EXTRA for x in SHORT_FLAT),
              "")

        h_short = mood.length_hint(NEUTRAL_TEXT)
        check("短句提示里写明了具体字数区间", f"{lo6}~{hi6}" in h_short, h_short)
        check("短句提示里点出 1~1.5 倍与「最多不比他多 8 个字」",
              "1~1.5 倍" in h_short and "8 个字" in h_short, "")
        check("短句提示里不再有「尾缀不算字数」那条豁免（人设尾缀已去）",
              "尾缀" not in h_short and "不算" not in h_short, h_short)
        h_long = mood.length_hint(LONG_BRIGHT)
        check("长句提示改口径：略长一点、克制、别滚雪球",
              "略长" in h_long and "别滚雪球" in h_long, h_long)
        check("长短两档提示不一样", h_short != h_long, "")

        check("字数段并进了 mirror_hint（两条回话路共用）",
              "字数跟着他走" in mood.mirror_hint(NEUTRAL_TEXT), "")

        section("⑥ 短句只给一句的额度，长句照旧听能量的")

        check("短句顶就是 mood.SHORT_MAX_TOKENS",
              mood.budget_for_text(NEUTRAL_TEXT) == mood.SHORT_MAX_TOKENS,
              str(mood.budget_for_text(NEUTRAL_TEXT)))
        check("短句顶 ≤ 中性那档的能量预算（只压不抬）",
              mood.budget_for_text(NEUTRAL_TEXT) <= mood.budget_for_energy(mood.ENERGY_NEUTRAL), "")
        check("长句不看短句顶，照旧是能量那档",
              mood.budget_for_text(LONG_BRIGHT)
              == mood.budget_for_energy(mood.energy_of(LONG_BRIGHT)),
              str(mood.budget_for_text(LONG_BRIGHT)))
        check("短句顶只压不抬：低落长句仍是 260",
              mood.budget_for_text("今天好累啊，什么都不想干") == 260,
              str(mood.budget_for_text("今天好累啊，什么都不想干")))
        check("空串不落到短句档（不给 0 额度）",
              mood.budget_for_text("") == mood.budget_for_energy(mood.ENERGY_NEUTRAL),
              str(mood.budget_for_text("")))

        # 这两条是"补强的字数规则真的生效在真链上"的硬证据。
        stub.messages.clear()
        stub.params.clear()
        svc._compose("好", 0.0, route)
        check("真链：一个「好」也只给短句顶",
              stub.params[-1]["max_tokens"] == mood.SHORT_MAX_TOKENS,
              str(stub.params[-1]["max_tokens"]))
        check("真链：这一段 system 里带上了字数提示",
              "字数跟着他走" in _system_of(stub.messages[-1]), "")

        # ---------- 红线 ----------

        section("红线：两个库一行不多、零新依赖")

        before_utt = _rows(app.config.db_path, "utterance")
        before_frag = app.mind_store.fragment_count()
        svc._compose("嗯", 0.0, route)
        svc._compose(LONG_BRIGHT, 0.0, route)
        svc._compose(ANGRY_TEXT, 0.0, route)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == before_frag, "")

        tree = ast.parse((ROOT / "life" / "dialog" / "mood.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        check("mood.py 只依赖仓库自己那点东西（没引第三方库）",
              imported <= {"__future__", "life", "pet", "brain", "core"},
              str(sorted(imported)))

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("聊天回复情绪与字数镜像对方（v2-chat-mirror）")


if __name__ == "__main__":
    raise SystemExit(main())
