"""场景判定的 60 秒滑动窗口多数票（v2-mood-detect 再审·终）。

原来判"现在在演什么"靠**连击**：连着 `scene_stable_samples` 拍都是同一态才算数。
这条规则有个天生的毛病——单帧噪点（这一拍 0.71、下一拍 0.68）就把连击自己断了，
于是贴阈值线的笑容永远等不到"连着两拍"，愤怒/笑容还容易因为一两个噪点互相投。

改成**一分钟多数票**：维护一个 60 秒的滑动窗口，每拍（一次采样 + 一次推理）看一眼结论；
窗口满了之后，取**出现次数最多**的那一态当最终判定。规则就三条：

1. **不满窗口不定案**——从这一窗第一拍到现在还不到 60 秒，证据没攒够，先维持上一次的判定；
2. **平票不定案**——两种情绪票数一样多，谁也说不过谁，同样维持上一次的判定（不秒退）；
3. **弃权帧不投票**——这一拍没把握（`None`）就**什么都不投**，只作废这一拍的时间。

第 3 条是 v2-mood-recall-fix 改的，也是这一轮的要害。原先"没把握"是**投一票空票**，
空票照样进多数票——于是真机上"四态弃权率 31%"直接变成"窗口里三成票是噪音"，
而**弃权票只稀释、不给信息**：四态一开口就几乎不会说错（留一法实测，判成别的情绪
合计 0.01%），却因为空票太多，凑不出多数、迟迟定不了案。真视频帧比样张更难，
弃权更多，一段两分钟的戏就这么一路"没人过半"到底。

所以现在**只有有定论的票进窗口**，`decide()` 也只剩**两种**出口：

- 返回场景名：定案，这是这会儿的场景；
- 返回 `None`：**没定案**（不满窗口 / 平票 / 一票都没有）——这一拍什么都不该动，
  既不该报新场景，也不该把已经在显示的那个状态词撤掉。

**散场不再靠"空票占多数"来判**，改由 monitor 的**硬闸**触发（前台不是视频、
换了前台、非视频硬闸命中、空白屏、自己的窗口）——那些是"确实没在放片"的硬证据，
比"猜不出来"可靠得多。这一层只负责降噪，**阈值一个数没动**。

纯内存、不落盘、不写库。时钟可注入（默认 `time.monotonic`），单测塞一个假钟就能
把"一分钟"压成毫秒跑完。

**v2-mood-window 又加了两道闸**（详情见下面那一段常量注释）：首弹门槛（票数 ≥ 8
且最多那一态 ≥ 60% 才第一次开口；**v2-mood-window-tune 起愤怒/恐惧降到 6**，见
`FIRST_MIN_VOTES_BY_STATE`）与切换去抖（已弹的那一态要被换掉，新态得连续当
≥10 秒的唯一最多票）。窗口宽度与 `mood_cv` 的 PROTO_* 三个阈值**一个数没动**
——用户要修的是"开口太轻、改口太容易"，不是窗口太长。
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Callable

from life.screen.scenes import SCENE_ANGRY, SCENE_HORROR

# ---------- v2-mood-window：首弹门槛 + 切换去抖 ----------
#
# 窗口宽度（45~60 秒）与 `mood_cv` 的 PROTO_* 三个阈值**一个数没动**。真机上要修的不是
# "窗口太长"，是这条窗口规则缺两道闸：
#
# 用户实测：看一段**愤怒**的戏，开局先飘「我怕怕…」，过一会儿又改成「太气人」。
# 查下来是两个毛病叠在一起——
#
# 1. **首弹太轻**：窗口一满、只要有一态是"唯一最多票"就立刻开口，**没有票数下限、
#    没有占比下限**。而真机一拍是"5 秒采样 + 一次识别约 4 秒"，60 秒窗口里只装得下
#    十来拍、有定论的更少（四态实测弃权率 31%，真视频更高）——于是**3 票就能定案**，
#    开局那几帧偏恐怖，第一句就飘错了。
# 2. **翻案太容易**：定案之后，下一拍窗口里的唯一最多票一变就立刻改口，**没有
#    "新态得持续多久"的门槛**。于是偶发帧一来，已说出口的状态词就被翻掉。
#
# 两道闸（取值都有实测依据，见收尾报告第九十九节）：
#
# · **首弹门槛**：窗口满之外，还要**有定论的票 ≥ 8** 且 **最多那一态占比 ≥ 60%**。
#   60% 而不是"最多就行"：最多只保证超过别人，不保证过半；≥8 票是因为弃权率高处
#   一个窗口只有六七张有定论的票，门槛要卡在"一个窗口攒不够"的位置，逼它多攒一趟。
#   仿真 600 轮（按阶段一实测逐帧分布，一戏 120 秒）：首弹弹错率 9.2% → 2.3%，
#   干净戏仍有 95% 弹得出来、只是晚 5~7 秒。**10 票更准但太钝**——干净戏只剩 58%
#   弹得出来，弃权一高就干脆不开口了，所以取 8。
#   **v2-mood-window-tune**：这 8 票后来按态放宽了——真实视频里愤怒/恐惧的弃权率比
#   样张更高，一个窗口攒不够 8 张，直接沉默；它们降到 6，笑容维持 8。见下面
#   `FIRST_MIN_VOTES_BY_STATE` 那段。
# · **切换去抖**：已经弹出去的那一态要被换掉，新态得**连续**当窗口里的唯一最多票
#   **≥10 秒**（10 秒 ≈ 真机两拍）。"连续"意味着中间只要被平票或别的态打断，计时
#   就从头来。
#
#   **"窗口占多数"这一半没有单独判一次**，因为它本来就不可能不成立：`decide()` 只在
#   有**唯一最多票**时才往下走，而窗口里最多同时有**三个**态（v2-mood-tri 之后就是
#   笑容 / 愤怒 / 恐怖三态，`_count_scene` 还会把不认识的态丢在门外）。设某一态是唯一
#   最多票、票数为 t，则另外两态各最多 t-1，总数最多 3t-2，于是它的占比
#   t/(3t-2) ≥ 2/4 = 50%（t=2 时取等号，t 越大越高）。既然唯一最多票**必然过半**，
#   再写一个"过半吗"的判断就是一行永远为真的死代码——不写，把这条推理记在这里。
FIRST_MIN_VOTES = 8          # 首弹门槛：窗口里至少要有几张**有定论**的票（没单列的态走这个）
FIRST_MIN_SHARE = 0.60       # 首弹门槛：最多那一态要占多少（0.60 = 过半且有余量）
SWITCH_HOLD_SEC = 10.0       # 切换去抖：新态要连续当多久的唯一最多票才准翻案

# v2-mood-window-tune·按态分首弹票数：上面那个 8 是按"干净戏"定的，但真实视频里
# 愤怒 / 恐惧的单帧弃权率高（阶段一怒 34% / 恐 40%，真视频更高），一个 60 秒窗口
# （约 12 拍）只能攒出 ~7 张有定论的票，够不到 8——这两个态在真机上就直接沉默
# （用户报：愤怒汇集一分半、恐惧一分多都没认出来）。把它们的首弹票数降到 6：窗口
# 里攒够 6 张有定论票、且最多那一态仍占 ≥60% 才开口。**占比门槛一个数没动**——
# 它才是保准的那道闸；票数这道只管"攒够没有"，降到 6 不影响"最多那一态要过半"。
# 仿真（阶段一逐帧分布，600 轮，见收尾报告第一百零八节）：干净戏弹得出来率
# 恐惧 8 票 85.8% → 6 票 99.3%、愤怒 93.7% → 99.8%；首弹弹错率两种口径下都是 0
# （交叉源只有愤怒→恐惧 6.38%，凑不到 60%）。笑容弃权低（18%），维持 8 票不动。
#
# v2-mood-fear-tune：恐惧那 6 票后来**抬回 8**。降到 6 让恐惧更好认，代价是把剧情里的
# 「紧张」也认成「恐惧」——三态下怒↔恐质心 0.0844 是最近的一对（比怒↔笑 0.0858 还近），
# 紧张/愤怒脸跟恐惧脸贴得近，6 票就攒够、开口就飘「我怕怕…」（用户报的正是这个）。
# 恐惧是误判重灾区，宁可让它少开口、只认「真恐」，也别让紧张漏进来。愤怒维持 6 不动
# （它没这个误判），笑容照旧回落 8（SCENE_FESTIVE 不列 = 回落 FIRST_MIN_VOTES）。
FIRST_MIN_VOTES_BY_STATE: dict[str, int] = {
    SCENE_ANGRY: 6,
    SCENE_HORROR: 8,
}


def first_min_votes_for(scene: str) -> int:
    """这一态当冠军时的首弹票数下限。没单列的态回落 `FIRST_MIN_VOTES`（笑容走这里）。"""
    return FIRST_MIN_VOTES_BY_STATE.get(scene, FIRST_MIN_VOTES)


class SceneVoter:
    """一分钟多数票：吃进每拍的场景，吐出一个"这会儿算不算成立、是哪一态"。

    `window_sec` 是窗口宽度（默认 60）；`now` 是取时间的函数（测试塞假钟）。
    窗口宽度做成可改的——monitor 每拍从配置现读一遍，测试也能随手设成 0 秒
    （0 秒 = 一拍即定，等价于旧的 `scene_stable_samples=1` 那样单拍成立）。

    `note(None)`（这一拍没把握）**只推进观察窗、不投票**：窗口"够不够满"看的是
    **观察时长**（从这一窗第一拍算起），不是"攒了几张票"——否则弃权越多、
    越难凑满窗口，等于把"没把握"又罚了一遍（v2-mood-recall-fix）。
    """

    def __init__(self, window_sec: float = 60.0, now: Callable[[], float] = time.monotonic,
                 first_min_votes: int = FIRST_MIN_VOTES,
                 first_min_share: float = FIRST_MIN_SHARE,
                 switch_hold_sec: float = SWITCH_HOLD_SEC,
                 first_min_votes_by_state: dict[str, int] | None = None):
        self.window = float(window_sec)
        self._now = now
        self._votes: list[tuple[float, str]] = []    # 只存有定论的票：(时刻, 场景)
        self._opened_at: float | None = None         # 这一窗第一拍的观察时刻
        # 两道闸的取值（v2-mood-window）。做成参数是给测试用的：验"多数票本身"的用例
        # 传 0 / 0.0 就把闸摘掉，验闸本身的用例传真值。生产链路上用模块级默认值。
        self.first_min_votes = int(first_min_votes)
        self.first_min_share = float(first_min_share)
        self.switch_hold_sec = float(switch_hold_sec)
        # 按态分首弹票数（v2-mood-window-tune）：生产默认走模块级的 FIRST_MIN_VOTES_BY_STATE
        # （愤怒/恐惧降到 6），但**只有没显式改 base 的时候才套**——测试把 first_min_votes
        # 传 0 / 1 摘闸、要的是"整道闸别挡道"，此时按态分票会把它又顶回 6，反而不对，
        # 所以 base 一旦不是默认值就当"手动接管"，按态表清空。
        if first_min_votes_by_state is not None:
            self.first_min_votes_by_state = dict(first_min_votes_by_state)
        elif first_min_votes == FIRST_MIN_VOTES:
            self.first_min_votes_by_state = dict(FIRST_MIN_VOTES_BY_STATE)
        else:
            self.first_min_votes_by_state = {}
        # 已经**弹出去**的那一态（`None` = 这一场还没弹过），以及正在等门槛的挑战者
        # （态, 它从哪一刻起连续当唯一最多票）。
        self._held: str | None = None
        self._pending: tuple[str, float] | None = None

    def note(self, scene: str | None) -> str | None:
        """过一拍：`scene` 是这一拍的结论（`None` = 没把握，不投票）。返回窗口判定。"""
        t = self._now()
        if self._opened_at is None:
            self._opened_at = t       # 窗口从这一拍开始算，弃权的那一拍也算在看
        if scene:
            self._votes.append((t, scene))
        self._prune(t)
        return self.decide()

    def _prune(self, t: float) -> None:
        """丢掉窗口之外的老票（早于 t - window 的）。"""
        cutoff = t - self.window
        self._votes = [(tt, s) for tt, s in self._votes if tt >= cutoff]

    def decide(self) -> str | None:
        """按当前窗口里的**有定论票**算结论。三条纪律见模块 docstring。

        返回 `None` = **没定案**（不满窗口 / 平票 / 一票都没有，含"整窗全弃权"）——
        这一拍什么都不动，维持上一次判定，不散场：散场交给 monitor 的硬闸。
        v2-mood-window 之后，"没定案"还多两种情形：**首弹门槛没够**（票太少 / 占比不够）、
        **挑战者还在等去抖那 10 秒**。两者同样是"什么都不动"。
        """
        if not self._votes:
            return None
        # 不满窗口：从这一窗第一拍到现在还不够一个窗口宽，证据没攒够。
        # **看的是观察时长，不是票数**——弃权的那几拍同样在证明"这会儿在放这段"。
        if self._opened_at is None or (self._now() - self._opened_at) < self.window:
            return None
        counts = Counter(s for _t, s in self._votes)
        top = max(counts.values())
        winners = [s for s, c in counts.items() if c == top]
        if len(winners) != 1:
            # 平票：不定案。**挑战者的计时也在这里断**——"连续是唯一最多票"里那个
            # "连续"，平票的那一拍不算数，下一拍重新起算。
            self._pending = None
            return None
        if self.window <= 0:
            # 「一拍即定」那一档（旧 scene_stable_samples=1）：窗口只装一拍，
            # 首弹门槛与切换去抖都没有意义，照旧给最朴素的结论。
            self._held, self._pending = winners[0], None
            return winners[0]
        return self._settle(winners[0], top / len(self._votes))

    def _settle(self, winner: str, share: float) -> str | None:
        """两道闸的判定（v2-mood-window）。`winner` 已经是**唯一最多票**那一态。

        - 这一场**还没弹过**：票数够（**按态**，v2-mood-window-tune）、占比够才首次开口，
          否则继续攒。
        - 已经弹过、`winner` 就是它：什么都不变（返回它自己，monitor 那边值没变就不广播）。
        - 已经弹过、`winner` 是别的态：**去抖**——它得连续当够 `switch_hold_sec` 秒的
          唯一最多票才准翻案；否则什么都不动（返回 `None`，
          已经飘在头上的那一态照旧留着）。
        """
        if self._held is None:
            need = self.first_min_votes_by_state.get(winner, self.first_min_votes)
            if len(self._votes) < need or share < self.first_min_share:
                return None                    # 证据不够，第一句先不说
            self._held, self._pending = winner, None
            return winner
        if winner == self._held:
            self._pending = None               # 真态还在，挑战者作废
            return self._held
        if self.switch_hold_sec <= 0:
            # 去抖摘掉那一档（测试把门槛取值都传 0）：照旧立刻改口。
            self._held, self._pending = winner, None
            return winner
        now = self._now()
        if self._pending is None or self._pending[0] != winner:
            self._pending = (winner, now)      # 挑战者刚冒头 / 换了一个挑战者，重新计时
            return None
        if now - self._pending[1] < self.switch_hold_sec:
            return None                        # 还不够久，再等等
        self._held, self._pending = winner, None
        return winner

    def reset(self) -> None:
        """换了一场（换前台 / 空白屏 / 自己的窗口）就把旧票与观察窗一起作废。

        **已经弹过的那一态也一起清掉**：新的一场要重新走首弹门槛，而不是接着上一场
        的账继续算（否则换台之后第一帧就能直接改口，等于没有门槛）。
        """
        self._votes.clear()
        self._opened_at = None
        self._held = None
        self._pending = None

    @property
    def held(self) -> str | None:
        """这一场已经弹出去的那一态（`None` = 还没弹过）。日志与测试用。"""
        return self._held

    def __len__(self) -> int:
        """窗口里有几票**有定论的**票（测试与日志用；弃权的不算）。"""
        return len(self._votes)


__all__ = [
    "FIRST_MIN_SHARE",
    "FIRST_MIN_VOTES",
    "FIRST_MIN_VOTES_BY_STATE",
    "SWITCH_HOLD_SEC",
    "SceneVoter",
    "first_min_votes_for",
]
