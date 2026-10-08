"""
搜索调优（v2-search-tune，选 A 零依赖）的自动测试。

对着指令 S1 的三条（不引新 SDK、不加 Key）：

1. **query 构造**：build_query 按场景拼实体 + 限定词（游戏名 + 平台、菜名 + 做法那类），
   空串 / None 都丢掉；
2. **多源聚合**：merge_answers 取第一份非空正文、来源去重融合；multi_ask 多个角度各搜
   一次、单次失败不连坐、全失败才报错；
3. **结果校验**：filter_sources 丢掉空链接 / 广告页 / 重复来源，且**不重排编号**
   （正文 [n] 角标靠它对齐）；
4. **接线**：攻略 / 出行规划 / 赶时间走法三个调用点，真的把消歧过的搜索词交了出去
   （stub 记下 search_query）。原先还验了 BOM 替代与卖点预测两个调用点——那是工作
   模块里的两条工作卡，没随公开版发布，一并摘了。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary

from brain.chains.card_ai import CardAIChain
from brain.llm.base import LLMError
from brain.llm.search import (
    SearchAnswer,
    SearchSource,
    build_query,
    filter_sources,
    merge_answers,
    multi_ask,
)


def _src(index: int, url: str, site: str = "示例站", title: str = "示例来源") -> SearchSource:
    return SearchSource(index=index, title=title, url=url, site=site)


def _answer(text: str, *sources: SearchSource) -> SearchAnswer:
    return SearchAnswer(text=text, sources=tuple(sources))


# ---------- ① query 构造 ----------


def test_build_query() -> None:
    section("① build_query：实体 + 限定词，空的一律丢")
    check("正常拼接", build_query("绝地求生", "PC", "攻略") == "绝地求生 PC 攻略",
          build_query("绝地求生", "PC", "攻略"))
    check("空串 / 纯空白丢掉", build_query("北京", "", "  ", "旅游攻略") == "北京 旅游攻略",
          build_query("北京", "", "  ", "旅游攻略"))
    check("None 也丢掉", build_query(None, "小红书", "趋势") == "小红书 趋势",
          build_query(None, "小红书", "趋势"))
    check("全空给空串", build_query("", None, "  ") == "", repr(build_query("", None, "  ")))
    check("首尾空白收掉", build_query(" 露营折叠椅 ", " 最近 30 天 ") == "露营折叠椅 最近 30 天",
          build_query(" 露营折叠椅 ", " 最近 30 天 "))


# ---------- ② 结果校验 ----------


def test_filter_sources() -> None:
    section("② filter_sources：丢空链接 / 广告 / 重复，编号不重排")
    sources = (
        _src(1, "https://example.com/a"),
        _src(2, ""),                                   # 空链接：丢
        _src(3, "https://spam.example/doubleclick/x"),  # 广告域名：丢
        _src(4, "https://example.com/a"),              # 与 1 重复：丢
        _src(5, "https://example.com/b"),
    )
    kept = filter_sources(sources)
    check("只留下 1、5 两条", [s.index for s in kept] == [1, 5], str([s.index for s in kept]))
    check("编号原样保留（不重排）", kept[0].index == 1 and kept[1].index == 5,
          f"{kept[0].index},{kept[1].index}")
    check("广告按站名也能认", filter_sources((_src(1, "https://x.example/1", site="推广页"),)) == (),
          "广告站名被留下来了")


# ---------- ③ 多源聚合 ----------


def test_merge_answers() -> None:
    section("③ merge_answers：正文取第一份非空，来源去重融合")
    a1 = _answer("   ", _src(1, "https://a.example/1"), _src(2, "https://a.example/2"))
    a2 = _answer("第二份正文", _src(1, "https://a.example/2"), _src(2, "https://b.example/9"))
    merged = merge_answers((a1, a2))
    check("正文取第一份**非空**的（跳过全空白）", merged.text == "第二份正文", repr(merged.text))
    check("来源并集：主答案 2 条 + 新链接 1 条", len(merged.sources) == 3,
          str([s.url for s in merged.sources]))
    check("主答案来源在前、编号不动", [s.index for s in merged.sources][:2] == [1, 2],
          str([s.index for s in merged.sources]))
    check("补进来的来源编号续在最大编号之后",
          merged.sources[2].index == 3 and merged.sources[2].url == "https://a.example/1",
          str(merged.sources[2]))

    check("全空正文退回第一份", merge_answers((a1,)).text == "   ",
          repr(merge_answers((a1,)).text))
    check("空列表给空答案",
          merge_answers(()).text == "" and merge_answers(()).sources == (), "非空")


class _FakeSearch:
    """模拟 ask(prompt, system, search_query=)：记下收到的 query，按剧本回或抛错。"""

    def __init__(self, script):
        self.script = list(script)          # 每个元素：SearchAnswer 或 Exception
        self.calls: list[str] = []

    def ask(self, prompt, system="", search_query=""):
        self.calls.append(search_query)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_multi_ask() -> None:
    section("③ multi_ask：多角度各搜一次，单次失败不连坐，全失败才报错")
    fake = _FakeSearch([
        _answer("正文A", _src(1, "https://a.example/1")),
        _answer("正文B", _src(1, "https://b.example/1")),
    ])
    merged = multi_ask(fake, "提示词", "系统", ("角度1", "角度2"))
    check("两个角度各问了一次", fake.calls == ["角度1", "角度2"], str(fake.calls))
    check("正文取第一份", merged.text == "正文A", merged.text)
    check("来源融合成两份", len(merged.sources) == 2, str([s.url for s in merged.sources]))

    # queries 为空退回单次 ask（不带 search_query）
    fake2 = _FakeSearch([_answer("单份", _src(1, "https://a.example/1"))])
    single = multi_ask(fake2, "提示词", "系统", ())
    check("queries 空 → 单次、不带关键词", fake2.calls == [""] and single.text == "单份",
          str(fake2.calls))

    # 一个失败一个成功：不连坐
    fake3 = _FakeSearch([LLMError("这次检索没回来"), _answer("成功", _src(1, "https://a.example/1"))])
    merged3 = multi_ask(fake3, "提示词", "系统", ("坏角度", "好角度"))
    check("一次失败不连坐", merged3.text == "成功" and len(merged3.sources) == 1, merged3.text)

    # 全失败：把最后一次的错抛上去
    fake4 = _FakeSearch([LLMError("第一次失败"), LLMError("第二次失败")])
    try:
        multi_ask(fake4, "提示词", "系统", ("角度1", "角度2"))
        check("全失败要抛错", False, "没抛")
    except LLMError as exc:
        check("全失败抛的是最后一次的错", "第二次失败" in str(exc), str(exc))


# ---------- ④ 五个调用点都交了消歧搜索词 ----------


def test_call_sites_pass_query() -> None:
    section("④ 攻略 / 出行 / 赶时间 三个调用点都交了消歧搜索词")
    box, stub, app = make_env()

    # 攻略：游戏名 + 平台 / 新手避坑 两个角度
    stub.search_queries.clear()
    app.game_guide._build("绝地求生")
    check("攻略带「游戏名 + PC 平台」", "绝地求生 PC 攻略" in stub.search_queries,
          str(stub.search_queries))
    check("攻略第二角度「新手避坑」", "绝地求生 新手 避坑 技巧" in stub.search_queries,
          str(stub.search_queries))

    # 出行规划：目的地 + 攻略 / 美食穿搭 两个角度
    chain = CardAIChain(app.config)
    stub.search_queries.clear()
    chain.plan_trip("北京", "2026-10-06", "（还没有记下忌口与热量目标）")
    check("出行规划带「目的地 + 旅游攻略」", "北京 旅游攻略" in stub.search_queries,
          str(stub.search_queries))
    check("出行规划第二角度「美食穿搭」", "北京 美食 穿搭 天气 防晒" in stub.search_queries,
          str(stub.search_queries))

    # 赶时间走法：原文 + 怎么走
    stub.search_queries.clear()
    chain.quick_trip("地铁又停了", [])
    check("赶时间带「原文 + 怎么走」", "地铁又停了 怎么走" in stub.search_queries,
          str(stub.search_queries))

    # 原先这里还验了 BOM 替代（规格 + 品牌 + 替代料）与卖点预测（品类 + 平台 + 时间
    # 范围）两个调用点——那是工作模块里的两条工作卡，没随公开版发布，一并摘了。

    drain_threads()
    box.cleanup()


def main() -> int:
    test_build_query()
    test_filter_sources()
    test_merge_answers()
    test_multi_ask()
    test_call_sites_pass_query()
    return summary("搜索调优（v2-search-tune）：")


if __name__ == "__main__":
    raise SystemExit(main())
