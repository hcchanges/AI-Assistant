"""画面情绪（CV 腿）：零-shot 中文 CLIP 判两态（恐怖 / 笑容）+ 样例原型判三态（愤怒/笑容/恐惧）；模型不在就优雅降级回 OCR。

**为什么是零-shot 而不是微调**：微调一个情绪分类器要自己标数据、训练、再导出，
而且换一版权重就得重来。中文 CLIP 本来就会算"一张图跟一句话像不像"——把各态写
成一句中文（"一张恐怖惊悚的画面"…）当候选，让它比一比谁最像，这就是它的本职。
不用训练、不用标数据，改判据只是改几个字。

**愤怒/恐惧怎么回 CV**（v2-mood-fear，接 v2-mood-detect）：v2-mood-detect 曾把愤怒
从 CV 整档移走（愤怒共情只认字幕），因为写实游戏里压抑/紧张/打斗的气氛曾被**文本
零-shot**的"一张愤怒争吵的画面"误判成用户"愤怒"。现在用户补了愤怒样例（愤怒2）与
恐惧样例，就改走**样例原型**这条更稳的路：原型锚在真实的愤怒脸/恐惧脸上，不像泛化
文本句那样会跟压抑的游戏氛围撞车。所以愤怒/恐惧只加回**原型**（PROTO_EMOTIONS/
PROTO_DIRS），**文本零-shot 里仍没有愤怒**（prompt_table 还是 恐怖/笑容 + 中立）。
幽默（SCENE_FUNNY）仍删。

**悲伤整态已删**（v2-mood-tri，用户决定）：`SCENE_CRY` 连同它的**文本零-shot 提示词**
一并移出这条腿。原因是四态在 CLIP 嵌入空间里相互挤——悲伤夹在愤怒与恐惧正中间
（怒↔悲 0.0638、悲↔恐 0.0716 是六对里最近的两对），它自己单帧弃权率最高（42.65%），
同时又是把愤怒/恐惧往下拽的那块。砍掉它只要三态：愤怒/笑容/恐惧。样例图不删。

**为什么是 ONNX 而不是 PyTorch**：本机没装 torch（装了要 2.5GB 上下），仓库有
"零新依赖"这条线。ModelScope 上 `Xenova/chinese-clip-vit-base-patch16` 有现成的
ONNX 量化导出，跑在已经在依赖里的 onnxruntime 上，一个字的新依赖都不用加。

**体积与算力**（真机实测，不是估的）：

    onnx/model_q4f16.onnx      131.79 MB      ← 取这个
    onnx/model_quantized.onnx  190.80 MB
    onnx/model.onnx (fp32)     753.70 MB
    一次 224×224 前向（CPU）     约 75~150 ms

**判据是真的验过的**（不是"跑通了就算"）：拿 chinese-clip 官方仓库里那张真实节庆照片
（`festival.jpg`，450×300）喂进去，喜庆那一档拿到 **0.990**（neutral 0.010，其余三档
加起来 0.0002）；换成一张全黑图，最高的那档是恐怖 **0.399**，中立档把它压过去了。
完整的一小张标定表在下文 `DEFAULT_MIN_SCORE` 上面。

**判不准就不说话**：零-shot 的绝对值不是标定过的概率，任何一档都没超过
``mood_min_score`` 就直接返回 None——宁可不判，也别硬凑一个场景出来吓人。

隐私：喂进去的只有**当前画面这一帧**，在内存里过一遍就丢，不落盘、不落库、不发网络。
模型权重放 `data/models/` 下，那个目录在 .gitignore 里，仓库不带任何权重文件。
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR

logger = logging.getLogger(__name__)

# ---------- 模型 ----------

MODEL_DIRNAME = "chinese-clip-vit-base-patch16-onnx"
MODEL_REPO = "Xenova/chinese-clip-vit-base-patch16"

# 一个图的三个输入：文字 id / 注意力掩码 / 图像张量；四个输出里只用 logits
ONNX_REL = "onnx/model_q4f16.onnx"
_WEIGHT_BYTES = 131_790_000        # 只是给"下没下完"一个下限参考，不做校验用

# 缺一个都跑不起来；ensure_model 按这张表逐个补
MODEL_FILES: tuple[str, ...] = (
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    ONNX_REL,
)

# ModelScope 先试（本机实测 HTTP 200、18 秒拉完），HF 当异地备用（本机 HF 超时）
_MS_ENDPOINT = "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={path}"
_HF_ENDPOINT = "https://huggingface.co/{repo}/resolve/main/{path}"

DOWNLOAD_TIMEOUT = 180

# ---------- 前处理（照 preprocessor_config.json 的约定，别自己改） ----------

IMAGE_SIZE = 224
MAX_TOKENS = 52                    # 中文 CLIP 的文本长度上限
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)

# ---------- 判据 ----------

# 每一态给两句。多给一句是为了别把结果押在一种说法上——"喜庆"写成"节日庆典红火"
# 和写成"一张喜庆热闹的画面"，模型给的分数不完全一样，取高的那个更稳。
_PROMPT_HORROR = (
    "一张恐怖惊悚的画面",
    "恐怖片场景，阴森吓人",
    "血腥的尸体画面，血肉模糊",
    "割喉喷血的恐怖场面",
)
# v2-mood-screen ④：这一档从"喜庆（场景红调）"改判"人脸笑容"——喜庆靠红色画面不稳，
# 影视里的开心反而是人脸微笑/大笑最可识别。标识符仍叫 SCENE_FESTIVE，语义换成笑容。
#
# v2-mood-detect：**每档从两句加到四句**。跟恐怖那一档补血腥锚是同一个道理——
# 零-shot 的分数押在"这一句说法跟画面像不像"上，多说几种说法、取最高的那句，召回
# 就稳一截（用户报的"笑容识别不了"里，有一半是"这张脸跟这一句说法不投缘"）。
# **阈值一个数没动**（见下面 DEFAULT_MIN_SCORE 那一段的诚实标注）。
_PROMPT_FESTIVE = (
    "一张人脸上带着微笑的画面",
    "人们开怀大笑的场面",
    "一个人笑得很开心的特写",
    "面带笑容、神情愉悦的人物",
)
# v2-mood-tri：哭泣（`_PROMPT_CRY` 那四句 + `SCENE_CRY` 那一档）已整态删除，
# 这条零-shot 腿只剩下面两态。删掉的不只是提示词，还有 `_SCENE_PROMPTS` 里那一格。
_SCENE_PROMPTS: dict[str, tuple[str, ...]] = {
    SCENE_HORROR: _PROMPT_HORROR,
    SCENE_FESTIVE: _PROMPT_FESTIVE,
}

# 中立档不是第四个场景，是个"锚"：哪一态都没比"平淡画面"更像，就不该报场景。
# 没有这个锚，softmax 逼着二选一，再平淡的画面也会被推一个场景出来。
_NEUTRAL_PROMPTS = ("一张平淡普通的画面", "日常平静的画面，没什么特别")

NEUTRAL_KEY = "neutral"

# 判到什么分数才肯报场景。**这个数是量出来的，不是拍的**——本机实测：
#
#     真实节庆照片（festival.jpg，450×300）   festive 0.990   ← 该报
#     ────────────────────── 空档 ──────────────────────
#     纯红画面                                festive 0.658   ← 合成色块，不该报
#     白噪声                                  horror  0.506   ← 同上
#     纯灰画面                                horror  0.417   ← 同上
#     全黑画面                                angry   0.399   ← 同上（angry 档 v2-mood-detect 已从 CV 移走）
#
# 真实画面 0.99，合成噪声最高 0.66，中间有一段空档。取 0.70 落在空档里——
# 不是贴着 0.66 取，是往真实那侧留出余量，让"不该报的"一个都进不来。
# 偏保守是**故意**的：宁可这一拍不开口，也别对着看不出内容的画面乱猜一个情绪。
# 而且 monitor 那边还要过一道一分钟多数票（见 life/screen/vote.py）——噪声每拍给的
# 档都不一样，窗口里凑不出多数，这是第二道闸。
#
# 代价也说清楚：真画面若是**含蓄**的那类（CLIP 只给 0.4~0.6），这档就不会开口。
# 那是"少说话"，不是"说错话"，功能上可接受。
#
# v2-mood-screen ④ 的注意：festive 这一档提示词从「喜庆场景」改成了「人脸微笑/大笑」，
# 上面那张「真实节庆照片 festive 0.990」是**旧提示词**的标定值，换提示词之后分数会变。
# 笑容档的阈值需要拿真机的人脸笑容画面重新标定；0.70 暂时沿用旧值（保守 + monitor
# 的一分钟多数票第二道闸，先照旧跑，跑出问题改 `screen.mood_min_score` 一个数即可）。
#
# 诚实的边界：这一段的真实样本只有 **1 张真照片**，其余全是合成图。真机跑起来若
# 发现该报的没报（或反过来），改 `screen.mood_min_score` 一个数就能挪，不用动代码。
#
# v2-mood-detect 的诚实标注（这条**没有**解决）：上面那张标定表的分数全是**旧提示词**
# 下量的。笑容/哭泣两档换过说法、又各加到四句，它们的分数就跟着变了，而这个 0.70
# 仍是按旧提示词定的。本轮**没有**重标——手上没有真机的"人脸笑容"画面，拿合成图量
# 出来的数只能说明"噪声不冒头"，说明不了"真笑容够不够 0.70"。所以这一档采取的是
# **稳的那一侧**：宁可漏（少说一句），不改阈值（改错就是把噪声认成笑容）。
# 真机上若确认"真笑容报不出来"，改 `screen.mood_min_score` 一个数即可，代码不用动。
DEFAULT_MIN_SCORE = 0.70

# ---------- 样例原型（few-shot 情绪识别，v2-mood-proto） ----------
#
# 零-shot 的毛病在**人脸情绪**：文本提示词（"一张人脸上带着微笑的画面"这类）对人脸表情
# 不稳——v2-mood-proto 实测 18 张愤怒样例 0 张判对，哭泣/笑容的 argmax 也就 79.5%/76%，
# 而且三态的文本 softmax 都过不了 0.70 的 `mood_min_score`，真机上基本不开口。
#
# 用户手上有真的表情样例（E:\\电子宠物\\样例\\表情样例 下的 悲伤/笑容，恐惧还空着），
# 所以给这两态换成**图像原型**：把样例图过一遍 CLIP 取 `image_embeds`，每态取质心归一，
# 新帧比余弦相似度。这是"few-shot 原型分类"，不是微调——质心是 numpy 平均，一个字的新
# 依赖都不加，换样例重跑 `tools/build_mood_prototypes.py` 就能更新。
#
# v2-mood-fear（接 v2-mood-detect）：用户补了愤怒样例（愤怒2）与恐惧样例，愤怒/恐惧
# 加回原型——愤怒从"只认字幕"回到"字幕 + 原型画面"，恐惧从"纯文本零-shot"升级为
# "文本零-shot + 原型画面"。文本零-shot 里仍没有愤怒（愤怒的文本句对写实游戏气氛会误报，
# 见本文件开头 v2-mood-detect 的说明）。**v2-mood-tri 之后原型是三态**（愤怒/笑容/恐惧），
# 悲伤已整态删除。`DEFAULT_MIN_SCORE` 判据**一个数没动**。
#
# 阈值是量出来的，不是拍的（下面这一组是**四态时期**的本机实测，留一法，四态共 176 张：
# 悲伤 39 / 愤怒 38（愤怒+
# 愤怒2 合并）/ 笑容 67 / 恐惧 32）：
#
#     最近质心准确率（留一）              151/176 = 85.80%
#     真图自身余弦  中位 0.842 / p10 0.777
#     合成平面图（灰/黑/白/噪声）最高 0.739（白→悲伤）
#
# PROTO_CONF 取 0.76：压在平面图 0.739 之上、真图中位 0.842 之下，把"不该报的"挡在
# 外面。PROTO_MARGIN 再要求领先第二名一截，把"悲伤/愤怒/恐惧长得近、贴边"的那类挡掉。
# 四态比两态更挤（悲伤↔愤怒↔恐惧 都在"痛苦的表情"这一块），所以留一法从两态的 96.77%
# 掉到 85.80%——但 0.76/0.03 这套门限**一个数没动**就接着用：平面图最高还是 0.739 被
# 0.76 挡在门外；留一法里贴边的那 4 张（margin 0.025~0.030）被 0.03 挡成 None；只剩
# 1 张愤怒→恐惧（margin 0.054）会开口报错，而它过不了 monitor 那边"连着同态两拍"的
# 第二道闸。代价也说清楚：四态下悲伤/愤怒/恐惧的召回都掉一截（各自 49%/61%/59%，见
# 收尾报告第七十九节的逐态表），那是"少说话"，不是"说错话"，跟 `DEFAULT_MIN_SCORE`
# 同一套取舍。
#
# 为什么不再加"文本中立闸"：实测平面图的文本中立档 softmax 只有 0.08~0.28（不高），
# 挡不住它；真正把平面图挡在门外的是 PROTO_CONF 这道余弦下限，加上 monitor 那边"连着
# 同态两拍"的第二道闸。
#
# v2-mood-tri 之后上面这段的**结论只会更稳**：平面图那个最高分 0.739 恰恰是**对悲伤
# 质心**量的（白→悲伤），悲伤整态删掉之后，"合成平面图最高分"只可能变低（剩下三态里
# 最像的那个都不会比 0.739 更高），0.76 这道下限的余量只会变大。逐态召回那组数字
# （49%/61%/59%）则是四态时期的记录，留作历史。三态下的弃权率/质心见收尾报告第九十六节。
PROTO_FILENAME = "mood_prototypes.npz"
# v2-mood-tri：**三态**（愤怒/笑容/恐惧）。悲伤（`SCENE_CRY`）已整态删除，不在这一集里，
# 它的样例目录也不再参与编码——但**图没删**，`样例\表情样例\悲伤\` 原样在盘上。
# `PROTO_DIRS` 里也没有它：这个 dict 扫的就是「哪些目录进编码」，删掉即彻底不参与。
PROTO_EMOTIONS = (SCENE_FESTIVE, SCENE_ANGRY, SCENE_HORROR)
# 场景 → 样例目录（可多个：愤怒 = 愤怒 + 愤怒2 合并成同一档）。build_prototypes 会把
# 同一档的多个目录并起来算一个质心。
PROTO_DIRS = {
    SCENE_FESTIVE: ("笑容",),
    SCENE_ANGRY: ("愤怒", "愤怒2"),
    SCENE_HORROR: ("恐惧",),
}
PROTO_CONF = 0.76          # 余弦相似度绝对下限（高于四态下平面图最高 0.739；三态只会更低，一个数没动）
PROTO_MARGIN = 0.03        # 领先第二名的默认下限（没单独列出的态走这个数）
PROTO_TEMP = 20.0          # 原型余弦 → 归一分数的锐化温度（把 0.03 的余弦差拉成清晰分数差）

# v2-mood-face-crop·按态分 margin：上面那个 0.03 对四态一刀切，但四态离得多远差很多——
# 笑容最近的一态在 0.0858 外，0.03 的门对它根本够不着边。把门按**冠军态**分别定
# （判定时不知道真值，只能按"这一帧最像谁"取门），实测值是 273 张留一法扫描出来的：
#
#   | 态   | margin | 弃权率 45.59/22.31/36.36/27.50 → | 判对 181 → 196 | 代价 |
#   | 悲伤 | 0.03   | 42.65（−2.94）                  |                | 悲→笑 1→3（+2 张） |
#   | 笑容 | 0.015  | **12.40（−9.91）**              |                | 无 |
#   | 愤怒 | 0.025  | **29.55（−6.81）**              |                | 无 |
#   | 恐惧 | 0.03   | 27.50（±0）                     |                | 无 |
#
# 三对近质心双向互混仍是 1 / 2 / 3（愤怒↔悲伤 1、愤怒↔恐惧 2、悲伤↔恐惧 3），一条没动。
# 指令里原拟的「悲 0.02 / 恐 0.04」实测**否掉**：悲 0.02 把愤怒↔悲伤从 1 顶到 3、
# 悲伤↔恐惧从 3 顶到 4；恐 0.04 把恐惧弃权率从 27.50% 顶到 42.50%（恐惧弃权帧的
# 冠军领先普遍落在 0.03~0.04 之间，抬门就是抬自己）。
# v2-mood-tri：三态沿用下面这组分 margin 值（笑容 0.015 / 愤怒 0.025 / 恐惧 0.03），
# **一个数没动**——它们本来就是各态自己量出来的，与悲伤在不在无关。悲伤那一行（0.03）
# 随它整态删掉，不是"删了它让谁变松"，是它不再有冠军的机会。
PROTO_MARGIN_BY_STATE: dict[str, float] = {
    SCENE_FESTIVE: 0.015,
    SCENE_ANGRY: 0.025,
    SCENE_HORROR: 0.03,
}


def margin_for(scene: str) -> float:
    """这一态当冠军时的 margin 门。没列出来的态回落到 PROTO_MARGIN。"""
    return PROTO_MARGIN_BY_STATE.get(scene, PROTO_MARGIN)


class CvUnavailable(RuntimeError):
    """这台机器现在跑不了 CV 腿（权重没下全 / 没装 onnxruntime / 推理炸了）——退回 OCR。"""


def prompt_table() -> tuple[list[str], list[str]]:
    """把提示词摊平成一个批次：返回 (全部提示词, 每条属于哪一档)。

    摊平是为了**一次前向算完**——十句话分十次算要多花九次图编码的时间，
    而图编码正是这里最贵的一段。
    """
    prompts: list[str] = []
    keys: list[str] = []
    for scene in (SCENE_HORROR, SCENE_FESTIVE):
        for text in _SCENE_PROMPTS[scene]:
            prompts.append(text)
            keys.append(scene)
    for text in _NEUTRAL_PROMPTS:
        prompts.append(text)
        keys.append(NEUTRAL_KEY)
    return prompts, keys


# ---------- 样例原型（few-shot） ----------


def proto_path(models_dir: Path | str) -> Path:
    """样例原型该落在哪：跟 CLIP 权重同一个 models 目录下的一个 npz。"""
    return Path(models_dir) / PROTO_FILENAME


def load_prototypes(models_dir: Path | str) -> dict[str, np.ndarray] | None:
    """读预建的原型向量。缺文件 / 坏了返回 None（退化回纯文本零-shot，不炸）。"""
    import numpy as np

    path = proto_path(models_dir)
    try:
        data = np.load(path, allow_pickle=False)
        protos: dict[str, np.ndarray] = {}
        for scene in PROTO_EMOTIONS:
            vec = data[scene]
            protos[scene] = vec / (np.linalg.norm(vec) + 1e-9)
        return protos
    except Exception as exc:      # noqa: BLE001 —— 没有就是没有，当纯文本零-shot 跑
        logger.info("画面情绪：样例原型没读上（%s），这趟只用文本零-shot", exc)
        return None


def build_prototypes(
    ref_dir: Path | str,
    model_dir_path: Path | str,
    out_proto_path: Path | str,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, np.ndarray]:
    """从样例图建原型：每态 image_embeds 取质心再归一，写成一个 npz。

    读 `model_dir_path` 下的 CLIP 权重（`model_dir(model_dir_path)`），结果写到
    `out_proto_path`。样例目录结构：`ref_dir/<悲伤|愤怒|愤怒2|笑容|恐惧>/*.png`
    （jpg/jpeg 也收）；`PROTO_DIRS` 里同一档的多个目录（愤怒/愤怒2）会并成一个质心。
    缺图的那一态跳过；三态都缺就返回空 dict 且不落盘。
    """
    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    from tokenizers import Tokenizer

    root = model_dir(model_dir_path)
    if not (root / ONNX_REL).is_file():
        raise CvUnavailable(f"CLIP 权重不在（{root}）")
    session = ort.InferenceSession(str(root / ONNX_REL), providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
    prompts, _ = prompt_table()
    rows, masks = [], []
    for text in prompts:
        enc = tokenizer.encode(text).ids[:MAX_TOKENS]
        pad = MAX_TOKENS - len(enc)
        rows.append(enc + [0] * pad)
        masks.append([1] * len(enc) + [0] * pad)
    text_ids = np.array(rows, dtype=np.int64)
    text_mask = np.array(masks, dtype=np.int64)

    protos: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    for scene, dirnames in PROTO_DIRS.items():
        files: list[Path] = []
        for dirname in dirnames:
            d = Path(ref_dir) / dirname
            files += sorted(d.glob("*.png")) + sorted(d.glob("*.jpg")) + sorted(d.glob("*.jpeg"))
        if not files:
            logger.warning("画面情绪：样例目录 %s 里没图，这一态跳过", dirnames)
            continue
        vecs = []
        for i, f in enumerate(files):
            arr = np.asarray(Image.open(f).convert("RGB"), dtype=np.uint8)
            outs = session.run(
                None,
                {
                    "input_ids": text_ids,
                    "attention_mask": text_mask,
                    "pixel_values": _preprocess(arr).astype(np.float32),
                },
            )
            emb = {o.name: v for o, v in zip(session.get_outputs(), outs)}["image_embeds"][0]
            vecs.append(emb / (np.linalg.norm(emb) + 1e-9))
            if progress is not None:
                progress(scene, i + 1, len(files))
        centroid = np.mean(vecs, axis=0)
        protos[scene] = centroid / (np.linalg.norm(centroid) + 1e-9)
        counts[scene] = len(files)

    if not protos:
        return protos
    out = Path(out_proto_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        out,
        **protos,
        counts=np.array([counts.get(s, 0) for s in PROTO_EMOTIONS], dtype=np.int64),
    )
    logger.info("画面情绪：原型已建成（%s）→ %s", counts, out)
    return protos


# ---------- 权重 ----------


def model_dir(models_dir: Path | str) -> Path:
    """权重该落在哪。"""
    return Path(models_dir) / MODEL_DIRNAME


def model_ready(models_dir: Path | str) -> bool:
    """权重齐不齐。**只查文件在不在，不建会话**——这个方法会在界面线程被问到。"""
    root = model_dir(models_dir)
    for rel in MODEL_FILES:
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def ensure_model(models_dir: Path | str, progress: Callable[[str, int], None] | None = None) -> Path:
    """缺哪个补哪个，返回权重目录。补不下来抛 `CvUnavailable`。

    一次只下缺的那些，所以下到一半断了再跑会接着下、不会从头再来。
    先问 ModelScope，不行再问 HF——本机实测 HF 连不上，但换个网络它就能当后手。
    """
    root = model_dir(models_dir)
    root.mkdir(parents=True, exist_ok=True)
    for rel in MODEL_FILES:
        target = root / rel
        if target.is_file() and target.stat().st_size > 0:
            continue
        _fetch_one(rel, target, progress)
    return root


def _fetch_one(rel: str, target: Path, progress: Callable[[str, int], None] | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    context = ssl.create_default_context()
    quoted = urllib.parse.quote(rel)
    errors: list[str] = []
    for endpoint in (_MS_ENDPOINT, _HF_ENDPOINT):
        url = endpoint.format(repo=MODEL_REPO, path=quoted)
        try:
            logger.info("画面情绪：拉权重 %s（%s）", rel, url.split("/")[2])
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT, context=context) as resp:
                temp = target.with_suffix(target.suffix + ".part")
                got = 0
                with open(temp, "wb") as handle:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)
                        got += len(chunk)
                        if progress is not None:
                            progress(rel, got)
                temp.replace(target)
            return
        except Exception as exc:      # noqa: BLE001 —— 这个源不行就换下一个
            errors.append(f"{url.split('/')[2]}: {type(exc).__name__} {exc}")
            logger.info("画面情绪：这个源没成（%s）", errors[-1])
    raise CvUnavailable("权重拉不下来——" + "；".join(errors))


@dataclass(frozen=True)
class MoodVerdict:
    """这一帧的判读结果。scene=None 表示**没把握**（三态谁都没比中立档更像）。"""

    scene: str | None
    score: float
    scores: dict[str, float]      # 三态 + neutral 的归一分数，供融合与调试看

    @property
    def confident(self) -> bool:
        return self.scene is not None


class MoodCvReader:
    """零-shot 判读器（恐怖 / 笑容）+ 三态原型（愤怒 / 笑容 / 恐惧）。会话懒建、建失败就记一笔、以后不再试。"""

    def __init__(
        self,
        models_dir: Path | str,
        min_score: float = DEFAULT_MIN_SCORE,
        auto_download: bool = True,
    ) -> None:
        self._models_dir = Path(models_dir)
        self._min_score = float(min_score)
        self._auto_download = auto_download
        self._session: Any = None
        self._tokenizer: Any = None
        self._text_ids: Any = None
        self._text_mask: Any = None
        self._protos: dict[str, Any] | None = None
        self._failed = False
        self._download_lock = threading.Lock()
        self._downloading = False

    @property
    def min_score(self) -> float:
        """判到什么分数才肯报场景。"""
        return self._min_score

    @min_score.setter
    def min_score(self, value: float) -> None:
        """设置页改阈值时就地生效——不用把 132MB 的会话重建一遍。"""
        self._min_score = float(value)

    # ---------- 能力 ----------

    @property
    def available(self) -> bool:
        """现在能不能判。**便宜**：只查文件，不建会话、不碰网络。

        界面线程每拍都要问一次这个，所以它不能把 131MB 的图建起来。
        """
        return not self._failed and model_ready(self._models_dir)

    @property
    def downloading(self) -> bool:
        return self._downloading

    @property
    def loaded(self) -> bool:
        """会话建起来了没有（测试与设置页提示用）。"""
        return self._session is not None

    def download_async(
        self,
        on_done: Callable[[], None] | None = None,
        on_failed: Callable[[Exception], None] | None = None,
    ) -> bool:
        """后台补权重。已经在补 / 已经齐了都返回 False（没起新活儿）。

        放后台是因为这是 131MB——摆界面线程上就是十几秒的卡死。
        """
        with self._download_lock:
            if self._downloading or model_ready(self._models_dir):
                return False
            self._downloading = True

        def _work() -> None:
            try:
                ensure_model(self._models_dir)
            except Exception as exc:      # noqa: BLE001 —— 下不下来就是"本地档没有"，回落 OCR
                logger.info("画面情绪：权重没下来，本地档先按'没有'算（%s）", exc)
                if on_failed is not None:
                    on_failed(exc)
            else:
                if on_done is not None:
                    on_done()
            finally:
                self._downloading = False

        threading.Thread(target=_work, name="mood-cv-download", daemon=True).start()
        return True

    # ---------- 判读 ----------

    def classify_array(self, array) -> MoodVerdict | None:
        """一帧画面 → 三态判读。跑不起来（缺依赖 / 没权重 / 推理炸了）返回 None。

        **这个方法在后台线程跑**（一次前向几十到上百毫秒），界面线程不叫它。
        """
        if array is None:
            return None
        try:
            session, ids, mask = self._ensure()
        except CvUnavailable as exc:
            logger.debug("画面情绪：本机这趟没用上（%s）", exc)
            return None
        try:
            return self._infer(session, ids, mask, array)
        except Exception as exc:      # noqa: BLE001 —— 判读炸了不该把采样链带崩
            logger.debug("画面情绪：这一帧判读失败（%s）", exc)
            return None

    # ---------- 内部 ----------

    def _ensure(self):
        """把会话、分词器、文字张量准备好（一次）。建不起来抛 `CvUnavailable`。"""
        if self._session is not None:
            return self._session, self._text_ids, self._text_mask
        if self._failed:
            raise CvUnavailable("这台机器之前就没建起来")
        if not model_ready(self._models_dir):
            raise CvUnavailable(f"权重不在（{model_dir(self._models_dir)}）")
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            self._failed = True
            raise CvUnavailable(f"缺依赖：{exc}") from exc
        try:
            root = model_dir(self._models_dir)
            session = ort.InferenceSession(
                str(root / ONNX_REL), providers=["CPUExecutionProvider"]
            )
            tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
            prompts, _keys = prompt_table()
            rows, masks = [], []
            for text in prompts:
                encoded = tokenizer.encode(text).ids[:MAX_TOKENS]
                pad = MAX_TOKENS - len(encoded)
                rows.append(encoded + [0] * pad)
                masks.append([1] * len(encoded) + [0] * pad)
        except Exception as exc:      # noqa: BLE001 —— 建不起来就退回 OCR，不是崩溃
            self._failed = True
            raise CvUnavailable(f"建会话失败：{exc}") from exc
        self._session = session
        self._tokenizer = tokenizer
        self._text_ids = np.array(rows, dtype=np.int64)
        self._text_mask = np.array(masks, dtype=np.int64)
        self._protos = load_prototypes(self._models_dir)
        logger.info(
            "画面情绪：中文 CLIP 起来了（%d 条判据，样例原型 %s）",
            len(prompts),
            "在" if self._protos else "不在（纯文本零-shot）",
        )
        if self._protos:
            # v2-mood-detect 再审·终：把「愤怒↔笑容质心距离」打出来，真机上要是愤怒
            # 老被投成笑容，一眼就能看出是不是质心贴太近（该重挑样例，不动阈值）。
            logger.info(
                "画面情绪：愤怒↔笑容质心距离 %.3f（越大越不易互投）",
                proto_separation(self._protos),
            )
        return session, self._text_ids, self._text_mask

    def _infer(self, session, ids, mask, array) -> MoodVerdict:
        import numpy as np

        pixel = _preprocess(array)
        logits = session.run(
            None,
            {
                "input_ids": ids,
                "attention_mask": mask,
                "pixel_values": pixel.astype(np.float32),
            },
        )
        # 输出顺序照图上的名字取，别按下标——不同的导出顺序会变
        names = [out.name for out in session.get_outputs()]
        by_name = dict(zip(names, logits))
        per_prompt = by_name["logits_per_image"][0]

        _prompts, keys = prompt_table()
        # 同一档的多句取最高的那句：多给一句话是为了托底，不是为了拉平均
        best: dict[str, float] = {}
        for key, value in zip(keys, per_prompt.tolist()):
            if key not in best or value > best[key]:
                best[key] = value
        text_verdict = _verdict_from(best, self._min_score)
        if self._protos:
            emb = by_name["image_embeds"][0]
            proto = proto_verdict(emb, self._protos)
            if proto is not None:
                return proto
            # 原型在但没开口：若这一帧"长得像某张脸却拿不准是哪一张"（最像的那态过了
            # PROTO_CONF、只是没领先），文本零-shot 在人脸表情上不可信（v2-mood-proto 已证），
            # 别再让它猜——直接"没把握"，这一拍退回 OCR 或干脆不说话。
            sims = _proto_sims(emb, self._protos)
            if max(sims.values()) >= PROTO_CONF:
                scores = _proto_scores(sims)
                top = max(sims, key=sims.get)
                return MoodVerdict(None, scores[top], scores)
        return text_verdict


def _verdict_from(best: dict[str, float], min_score: float = DEFAULT_MIN_SCORE) -> MoodVerdict:
    """四档原始 logit（三态 + neutral）→ 归一分数 → 最高的那一档；没把握就 scene=None。

    两道关都要过：**先得比中立档更像**（不然再平淡的画面也能被逼出一个场景），
    **再得过 min_score**（零-shot 的绝对值不标定，低分区间不可信）。
    """
    import math

    top = max(best.values())
    exps = {k: math.exp(v - top) for k, v in best.items()}
    total = sum(exps.values()) or 1.0
    scores = {k: v / total for k, v in exps.items()}

    scenes = {k: v for k, v in scores.items() if k != NEUTRAL_KEY}
    scene = max(scenes, key=lambda k: scenes[k])
    score = scores[scene]
    neutral = scores.get(NEUTRAL_KEY, 0.0)
    if score <= neutral or score < min_score:
        return MoodVerdict(None, score, scores)
    return MoodVerdict(scene, score, scores)


def _proto_sims(image_emb, protos: dict[str, np.ndarray]) -> dict[str, float]:
    """一帧对三态质心的归一化余弦（L2 归一后点积）。只算 `PROTO_EMOTIONS` 里的态。"""
    import numpy as np

    v = np.asarray(image_emb, dtype=np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)
    return {scene: float(np.dot(v, protos[scene])) for scene in PROTO_EMOTIONS}


def _proto_scores(sims: dict[str, float]) -> dict[str, float]:
    """原型余弦 → 归一分数的锐化温度 softmax（分数只用于上报，不参与判据）。"""
    import math

    exps = {k: math.exp(sims[k] * PROTO_TEMP) for k in PROTO_EMOTIONS}
    total = sum(exps.values()) or 1.0
    scores = {
        SCENE_HORROR: 0.0, SCENE_ANGRY: 0.0, SCENE_FESTIVE: 0.0, NEUTRAL_KEY: 0.0,
    }
    for k in PROTO_EMOTIONS:
        scores[k] = exps[k] / total
    return scores


def proto_verdict(image_emb, protos: dict[str, np.ndarray]) -> MoodVerdict | None:
    """few-shot 原型判读：三态**（愤怒/笑容/恐惧）里最像的那一态够不够格顶掉文本零-shot。

    v2-mood-tri 之后只剩这三态（悲伤整态删除，见 `PROTO_EMOTIONS`）：候选集少一个，
    "第二名"也跟着变——原先被悲伤抢走的领先量，现在要么归给真冠军、要么归给真亚军，
    贴边帧因此更容易过门（弃权率下降），而跨态互混只在剩下三态之间结算。

    **一条没走的路，记在这儿免得有人再走一遍**（v2-mood-recall-fix 实测，四态时期）：为了降弃权，
    试过给"最像的那态余弦 ≥ 0.85 且领先 ≥ 0.015、且不是悲伤"开一条窄路放宽 `PROTO_MARGIN`。
    本机 217 张留一法实测结果——笑容弃权 18.18% → 15.15%（只降 3 个点）、**愤怒 34.04%
    一点没降**（愤怒弃权帧的 top 余弦中位只有 0.8059，够不到 0.85），而**跨态误判从
    2.76% 翻到 5.07%**（悲伤被判成别人从 3 张涨到 7 张）。按"误判率不得上升"的回退标准
    这条窄路不成立，因此**没有接进代码**。降弃权真正的路子是补样（让最像的那态与
    第二名的差距本身拉开），不是松闸——松闸对谁都松，贴边的悲伤首当其冲。

    返回 None 表示原型这次不开口，分两种（`_infer` 据此决定要不要退回文本零-shot）：

    1. 最像的那态余弦没过 `PROTO_CONF`——这张不像三态里任何一张脸（平面图/中性画面/
       纯场景），退回文本零-shot 去认场景；
    2. 最像的那态够像、但领先第二名不足**该态自己的** margin——长得像脸却拿不准是哪一张
       （愤怒/恐惧贴边），这种帧文本零-shot 在人脸表情上更不可信，直接"没把握"。

    margin 按冠军态取（`margin_for`，v2-mood-face-crop），不是一刀切：脸的"像不像
    自己人"各态松紧本就不同，笑容最近的一态在 0.0858 外、0.03 的门对它形同虚设。
    判定时不知道真值，所以只能按"这一帧最像谁"取门。

    `image_emb` 是这一帧的 CLIP 图像向量（未归一也行），`protos` 是 `load_prototypes`
    读回的归一化质心。**不拿文本零-shot 当门槛**：文本零-shot 在这几态上比分压得低，
    拿它把关会把原型好不容易接住的帧又堵回去。
    """
    sims = _proto_sims(image_emb, protos)
    top = max(sims, key=sims.get)
    runner = max(s for k, s in sims.items() if k != top)
    if sims[top] < PROTO_CONF or (sims[top] - runner) < margin_for(top):
        return None
    scores = _proto_scores(sims)
    return MoodVerdict(top, scores[top], scores)


def proto_separation(
    protos: dict[str, Any] | None,
    scene_a: str = SCENE_ANGRY,
    scene_b: str = SCENE_FESTIVE,
) -> float:
    """两态质心的余弦距离（1 − 归一化点积）。越大越分得开、越不易互相投。

    v2-mood-detect 再审·终的「愤怒↔笑容校准」：愤怒画面会不会投到笑容，取决于这两个
    质心离多远。距离太小（贴在一起）说明样例里愤怒与笑容长得太近，`proto_verdict` 的
    `PROTO_MARGIN` 会兜不住——该重拍 / 重挑样例，而不是去动阈值（阈值一个数没动）。
    缺任一态返回 -1.0（没法比，当「没量到」）。
    """
    if protos is None:
        return -1.0
    a = protos.get(scene_a)
    b = protos.get(scene_b)
    if a is None or b is None:
        return -1.0
    import numpy as np

    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    a = a / (np.linalg.norm(a) + 1e-9)
    b = b / (np.linalg.norm(b) + 1e-9)
    return float(1.0 - np.dot(a, b))


def _preprocess(array):
    """numpy 图（H×W×3，uint8）→ CLIP 要的张量（1×3×224×224，float32）。

    照 `preprocessor_config.json` 的约定来：缩到 224×224（`do_center_crop` 是 false，
    所以是直接缩放不是中心裁剪）、除以 255、按 CLIP 的均值方差标准化、HWC→CHW。
    """
    import numpy as np
    from PIL import Image

    image = Image.fromarray(np.asarray(array, dtype=np.uint8)).convert("RGB")
    image = image.resize((IMAGE_SIZE, IMAGE_SIZE), Image.BICUBIC)
    data = np.asarray(image, dtype=np.float32) / 255.0
    data = (data - np.array(CLIP_MEAN, dtype=np.float32)) / np.array(CLIP_STD, dtype=np.float32)
    return data.transpose(2, 0, 1)[None, ...]


__all__ = [
    "CvUnavailable",
    "DEFAULT_MIN_SCORE",
    "MODEL_DIRNAME",
    "MODEL_FILES",
    "MODEL_REPO",
    "MoodCvReader",
    "MoodVerdict",
    "NEUTRAL_KEY",
    "PROTO_CONF",
    "PROTO_DIRS",
    "PROTO_EMOTIONS",
    "PROTO_FILENAME",
    "PROTO_MARGIN",
    "PROTO_MARGIN_BY_STATE",
    "PROTO_TEMP",
    "build_prototypes",
    "ensure_model",
    "load_prototypes",
    "margin_for",
    "model_dir",
    "model_ready",
    "prompt_table",
    "proto_path",
    "proto_separation",
    "proto_verdict",
]
