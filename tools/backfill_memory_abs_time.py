"""
一次性回填：给老记忆补上 abs_time（相对时间的绝对锚点）。

背景：记忆里的"明早 / 今晚 / 下周三"这类相对说法，以前是原样存下来的，没有锚到
"说话那天"，所以过几天再被念出来时会说错话（说"明早"其实已经是过去的事了）。
现在新记忆入库时就会锚定，老记忆缺这一列。

基准取每条记忆的 created_at（记住的那天）：它不是"说话那一刻"的精确值，但对老数据
是最合理的近似，且 created_at 就是入库时间，跟说话时间差不了多少。

- 只填 abs_time 为空的，已有值一律不动；可重复跑，跑第二遍不会有事；
- 正文里没有相对时间说法的直接跳过（没有锚点就不猜）；
- 手动跑一次即可，程序启动时不会自动执行。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\backfill_memory_abs_time.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select

from core.config import Config
from core.reltime import anchor, rewrite
from data.db import Database
from data.models import Memory

SAMPLE_LIMIT = 8      # 抽查打印几条


def main() -> int:
    config = Config.load()
    db = Database(config.db_path)
    db.init()          # 老库会在这里补上 abs_time 列

    filled: list[Memory] = []
    scanned = skipped = 0

    with db.session_factory() as session:
        rows = session.scalars(select(Memory).where(Memory.abs_time.is_(None))).all()
        for memory in rows:
            scanned += 1
            when = anchor(memory.content, memory.created_at)[1]
            if when is None:
                skipped += 1        # 正文里没有相对时间说法，不动它
                continue
            memory.abs_time = when
            filled.append(memory)
        session.commit()

        print(f"数据库：{db.path}")
        print(f"扫到 abs_time 为空的老记忆：{scanned} 条")
        print(f"正文含相对时间说法、已回填：{len(filled)} 条")
        print(f"正文没有时间说法、跳过：{skipped} 条")

        if filled:
            now = datetime.now()
            print("\n抽查（按现在念出来的样子）：")
            for memory in filled[:SAMPLE_LIMIT]:
                print(f"  #{memory.id}  记住于 {memory.created_at.strftime('%Y-%m-%d %H:%M')}")
                print(f"       正文：{memory.content}")
                print(f"       锚点：{memory.abs_time.strftime('%Y-%m-%d %H:%M')}")
                print(f"       现在念：{rewrite(memory.content, memory.abs_time, now)}")
            if len(filled) > SAMPLE_LIMIT:
                print(f"  （另有 {len(filled) - SAMPLE_LIMIT} 条未列出）")
        else:
            print("\n没有需要回填的记忆。")

    db.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
