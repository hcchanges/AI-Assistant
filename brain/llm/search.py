"""
联网检索（指令 16 第 4、5 节）：两条工作卡片要"大模型联网、不爬虫"。

为什么单开一条通道，而不是继续用 OpenAI 兼容模式（实测记录）：
1. 兼容模式也认 enable_search——检索结果确实会注入上下文（同一句话，
   提问 token 从 28 涨到 5678，模型也终于知道今天是几号）；
2. 但它**不回带结构化来源**：正文里的链接是模型自己"写"出来的，
   实测它会明说"该 URL 是我按同源报道的格式推断的"。这不能摆到界面上当"数据来源"。
3. 通义原生接口加 search_options 之后，返回里带 output.search_info.search_results：
   每条有 index / title / url / site_name，实测一次给 10 条，正文里的 [1][2] 角标正好对得上。
   其中 forced_search 必须打开——只写 enable_search 时模型会自己判断要不要搜，
   实测它会跳过检索、直接编数据（详见 ask() 里的注释）。

所以这里走原生 HTTP 接口（只用标准库 urllib，不引新依赖）。
要接别家联网时在这里加分支，不动上面那套兼容协议。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from collections.abc import Sequence

from brain.llm.base import LLMError

logger = logging.getLogger(__name__)

# 原生接口路径（兼容模式是 .../compatible-mode/v1）
NATIVE_PATH = "/api/v1/services/aigc/text-generation/generation"

# 联网检索要等搜索引擎，比普通对话慢，给久一点
SEARCH_TIMEOUT_SEC = 90


@dataclass(frozen=True)
class SearchSource:
    """一条真实来源（来自厂商的检索结果，不是模型写的链接）。"""

    index: int
    title: str
    url: str
    site: str


@dataclass(frozen=True)
class SearchAnswer:
    """一次联网检索的结果：正文 + 结构化来源。"""

    text: str
    sources: tuple[SearchSource, ...]


# ---------- 搜索调优（v2-search-tune，零新依赖） ----------
# 三条都是纯函数 / 薄壳：query 构造（build_query）、多源聚合（merge_answers +
# multi_ask）、结果校验（filter_sources）。不引新 SDK、不加 Key，只把既有这条
# 通义原生检索通道用得更讲究。

# 明显的广告/营销来源：域名或站名里带这些字样就不摆（保守起见只收不会误伤的）。
_AD_MARKERS = ("doubleclick", "adclick", "sponsored", "/ad/", ".ads.", "广告", "推广")


def _is_ad(url: str, site: str) -> bool:
    text = f"{url} {site}".lower()
    return any(marker in text for marker in _AD_MARKERS)


def build_query(*parts: str) -> str:
    """把场景实体与限定词拼成一条消歧过的搜索词（query 构造优化）。

    空串 / 纯空白 / None 的部分都丢掉，剩下的用空格连起来。谁带什么限定词是
    调用方的事：攻略那带「PC」、出行那带「攻略/穿搭」、工作卡那带规格品牌——这里
    只管拼，不猜。
    """
    bits = [str(p).strip() for p in parts if p is not None and str(p).strip()]
    return " ".join(bits)


def filter_sources(sources: Sequence[SearchSource]) -> tuple[SearchSource, ...]:
    """结果校验：去掉空链接、广告页、重复来源，其余**保持原编号**。

    不重新编号是刻意的——正文里的 [1][2] 角标照厂商给的编号走，一旦重排就对不上；
    被丢掉的编号宁可让界面留空（工作卡那条链本来就「读不到就当没标依据」），也
    不能把别人的链接顶到那个编号上。
    """
    kept: list[SearchSource] = []
    seen: set[str] = set()
    for source in sources:
        url = (source.url or "").strip()
        if not url or _is_ad(url, source.site):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        kept.append(source)
    return tuple(kept)


def merge_answers(answers: Sequence[SearchAnswer]) -> SearchAnswer:
    """多源聚合：多个角度的检索结果去重融合成一份。

    正文取**第一个非空**的那份（几个角度答的是同一道题，正文不硬拼——拼了会把
    不同角标的 [n] 搅乱）；来源做并集：主答案的来源排前面（正文角标真正引用的、
    优先可信），其余角度里没见过的链接按序补在后面、编号续在最大编号之后，当
    额外的参考来源摆。
    """
    primary = next((a for a in answers if (a.text or "").strip()), None)
    if primary is None:
        primary = answers[0] if answers else SearchAnswer("", ())
    sources = list(filter_sources(primary.sources))
    seen = {s.url.rstrip("/") for s in sources}
    extra_index = max((s.index for s in sources), default=0) + 1
    for answer in answers:
        if answer is primary:
            continue
        for source in filter_sources(answer.sources):
            key = source.url.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                SearchSource(
                    index=extra_index,
                    title=source.title,
                    url=source.url,
                    site=source.site,
                )
            )
            extra_index += 1
    return SearchAnswer(text=primary.text, sources=tuple(sources))


def multi_ask(search, prompt: str, system: str, queries: Sequence[str]) -> SearchAnswer:
    """从多个角度各检索一次再聚合（多源聚合的入口）。

    queries 为空就退回单次；给两个就真搜两遍，其中一次失败不影响另一次——全都
    失败才把最后一次的错抛上去（宁可如实报错，也不拿空结果充数）。
    """
    if not queries:
        return search.ask(prompt, system)
    answers: list[SearchAnswer] = []
    last_error: BaseException | None = None
    for query in queries:
        try:
            answers.append(search.ask(prompt, system, search_query=query))
        except LLMError as exc:      # noqa: BLE001 —— 单次失败别带崩整条
            last_error = exc
            logger.warning("多角度检索其中一次失败（%s）：%s", query, exc)
    if not answers:
        raise (last_error if last_error is not None else LLMError("多角度检索一次都没成功"))
    return merge_answers(answers)


def native_endpoint(base_url: str) -> str:
    """把兼容模式的地址换算成原生生成接口地址。

    配置里写的是 https://dashscope.aliyuncs.com/compatible-mode/v1，
    原生接口在同一台机器的 /api/v1/... 上：去掉 compatible-mode 那段再拼。
    """
    root = base_url.split("/compatible-mode/")[0].rstrip("/")
    return root + NATIVE_PATH


class DashScopeSearch:
    """通义原生接口的联网检索客户端。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self._api_key = api_key
        self._endpoint = native_endpoint(base_url)
        self.model_name = model
        self.name = "dashscope-search"

    def ask(self, prompt: str, system: str = "", search_query: str = "") -> SearchAnswer:
        """问一句并检索。返回正文与来源；来源可能为空（模型没检索到东西）。

        search_query 是消歧过的搜索关键词（query 构造优化）：给了就顶在用户消息
        最前面，让检索引擎照这几个词去搜，而不是拿整段提示词去猜。
        """
        if search_query:
            prompt = f"联网检索关键词：{search_query}\n\n" + prompt
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model_name,
            "input": {"messages": messages},
            "parameters": {
                "enable_search": True,
                "result_format": "message",
                "search_options": {
                    # enable_source 才会在返回里带上 search_results，正文里也才有 [1][2] 角标
                    "enable_source": True,
                    "enable_citation": True,
                    "citation_format": "[<number>]",
                    # forced_search 是必须的：只写 enable_search 时模型会自己判断"要不要搜"，
                    # 实测同一段提示词不加它 → 一条来源都没有（提问 token 只有 308），
                    # 模型直接编出"热度 12400"这种数字；加上它 → 真检索、10 条来源、
                    # 提问 token 涨到 7690。宁可多花点检索费，也不能让界面摆着编的数据。
                    "forced_search": True,
                },
            },
        }
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SEC) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise LLMError(f"{self.model_name} 联网检索失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"{self.model_name} 联网检索连不上网：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.model_name} 联网检索返回的不是 JSON：{exc}") from exc

        # 原生接口出错时也是 HTTP 200，靠 code/message 字段报错
        if data.get("code"):
            raise LLMError(f"联网检索出错：{data.get('message') or data.get('code')}")

        output = data.get("output") or {}
        choices = output.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMError("联网检索没有返回内容，换个说法再试一次")

        sources: list[SearchSource] = []
        for item in (output.get("search_info") or {}).get("search_results") or []:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            sources.append(
                SearchSource(
                    index=int(item.get("index") or 0),
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    site=str(item.get("site_name") or "").strip(),
                )
            )
        logger.info("联网检索完成：正文 %d 字，来源 %d 条", len(text), len(sources))
        return SearchAnswer(text=text, sources=filter_sources(sources))

    def ask_multi(self, prompt: str, system: str = "", queries: Sequence[str] = ()) -> SearchAnswer:
        """多角度检索再聚合（多源聚合的入口，逻辑见 multi_ask）。"""
        return multi_ask(self, prompt, system, queries)
