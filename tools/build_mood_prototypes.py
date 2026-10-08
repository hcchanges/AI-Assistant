"""
用表情样例图建"情绪原型"（few-shot 情绪识别用的质心向量）。

为什么需要它：mood_cv 那条 CV 腿原本是零-shot（画面 vs 中文句子），对人脸情绪尤其
不稳——文本提示词"一张人脸上带着微笑的画面"这类说法在人脸表情上不开口。所以给有样例图
的三态（愤怒/笑容/恐惧）换成**图像原型**：把样例图过一遍中文 CLIP 取 image_embeds、
每态取质心、归一，写成一个 npz。运行时 mood_cv 读它，跟新帧比余弦相似度，比零-shot
稳得多（四态时期留一法 151/176 = 85.80%；三态见收尾报告第九十六节）。

（v2-mood-fear，接 v2-mood-detect：用户补了愤怒样例（愤怒2）与恐惧样例，愤怒/恐惧
加回原型——愤怒 = 愤怒 + 愤怒2 两个目录并成一个质心；恐惧 = 恐惧目录。幽默仍删。）

（v2-mood-tri：悲伤整态删除。本脚本**只认 `mood_cv.PROTO_DIRS`**——那张表里现在是
笑容/愤怒/恐惧三格，悲伤目录 `样例\\表情样例\\悲伤\\` 照旧躺在盘上但不再进编码。
要恢复只需把那一格接回 `PROTO_DIRS` 再重跑本脚本。）

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\build_mood_prototypes.py [样例目录]

默认样例目录 E:\\电子宠物\\样例\\表情样例，里面是 悲伤/愤怒/愤怒2/笑容/恐惧 子目录
（悲伤那个目录在本轮不再被扫到）。
原型写到数据根的 models/ 下（mood_prototypes.npz）。**重跑即重训**：换样例 / 补样例之后
重跑本脚本，覆盖旧 npz。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))  # 让 tools/ 目录能 import core / life

from core.config import Config
from life.screen.mood_cv import build_prototypes, model_dir, model_ready, proto_path

DEFAULT_REF = r"E:\电子宠物\样例\表情样例"


def _models_dir() -> Path:
    """原型写到数据根的 models/（v2-data-root）。读得到 config 就按它走。"""
    try:
        return Config.load().local_models_dir
    except Exception:
        return ROOT_DIR / "data" / "models"


def main(argv: list[str]) -> int:
    ref = Path(argv[0]) if len(argv) > 1 else Path(DEFAULT_REF)
    models_dir = _models_dir()
    if not model_ready(models_dir):
        print(f"CLIP 权重不在（{model_dir(models_dir)}），请先下好权重再跑本脚本。")
        return 1
    out = proto_path(models_dir)

    def progress(scene: str, done: int, total: int) -> None:
        print(f"  {scene}: {done}/{total}", flush=True)

    print(f"样例目录：{ref}")
    print(f"原型输出：{out}")
    protos = build_prototypes(ref, models_dir, out, progress=progress)
    if not protos:
        print("一个原型都没建出来（样例目录里没有悲伤/笑容的图？），未写文件。")
        return 1
    print(f"已建 {len(protos)} 个原型：{', '.join(protos)}")
    print("完成。运行时 mood_cv 会自动加载。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
