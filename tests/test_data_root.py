"""
运行时数据归置（v2-data-root）的自动测试。

覆盖：
1. app.data_dir 的解析——配置了按配置、空则退回项目内 data/；
2. data_path 的新子目录落点（db / vector / logs / models）+ 绝对路径原样返回；
3. 旧配置（data/ 前缀、无 data_dir）剥前缀后仍落老位置；
4. prepare_data_root：可写不动、不可写退回默认并清空 data_dir；
5. migrate_runtime_data：把旧 data/ 里那几样运行期数据**补齐**到 data_dir，原文件保留——
   只补缺、不覆盖已有，跑完落一个一次性哨兵（v2-migrate-guard）。

全部用临时目录，不碰用户真实的 E 盘或项目 data/。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

import yaml

import core.config as config_mod
from core.config import Config


def _write_config(path: Path, data_dir: str) -> dict:
    """写一份能过 validate 的最小配置，data_dir 由调用方定。"""
    raw = {
        "app": {"data_dir": data_dir},
        "llm": {
            "active": "deepseek",
            "providers": {
                "deepseek": {
                    "protocol": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key_ref": "llm_deepseek",
                }
            },
        },
        "vision": {"provider": "deepseek", "model": "qwen-vl-max"},
        "work": {"provider": "deepseek", "top_n": 5},
        "card": {"categories": {"餐饮": ["外卖"]}},
    }
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pet_data_root_") as tmp:
        tmp = Path(tmp)

        section("data_dir 解析")
        cfg_empty = Config.load(_write_config(tmp / "c1.yaml", ""))
        check(
            "data_dir 空 -> 退回项目内 data/",
            cfg_empty.data_dir == config_mod.ROOT_DIR / "data",
            str(cfg_empty.data_dir),
        )

        ext = tmp / "ext_root"
        cfg = Config.load(_write_config(tmp / "c2.yaml", str(ext)))
        check("data_dir 配置了 -> 按配置", cfg.data_dir == ext, str(cfg.data_dir))
        check("db_path -> <根>/db/pet.db", cfg.db_path == ext / "db" / "pet.db", str(cfg.db_path))
        check("mind_db_path -> <根>/db/mind.db", cfg.mind_db_path == ext / "db" / "mind.db", str(cfg.mind_db_path))
        check("vector_path -> <根>/vector", cfg.vector_path == ext / "vector", str(cfg.vector_path))
        check("log_dir -> <根>/logs", cfg.log_dir == ext / "logs", str(cfg.log_dir))
        check("model_dir -> <根>/models/bge-small-zh-v1.5", cfg.model_dir == ext / "models" / "bge-small-zh-v1.5", str(cfg.model_dir))
        check("local_models_dir -> <根>/models", cfg.local_models_dir == ext / "models", str(cfg.local_models_dir))

        section("data_path 细节")
        check(
            "绝对路径原样返回",
            cfg.data_path(str(tmp / "abs.db")) == tmp / "abs.db",
            str(cfg.data_path(str(tmp / "abs.db"))),
        )
        # 旧配置：data/ 前缀 + 无 data_dir -> 剥前缀后仍落项目内 data/ 的老位置
        c3 = _write_config(tmp / "c3.yaml", "")
        raw = yaml.safe_load(c3.read_text(encoding="utf-8"))
        raw["memory"] = {
            "db_path": "data/pet.db",
            "vector_path": "data/chroma",
            "model_path": "data/models/bge-small-zh-v1.5",
        }
        c3.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        cfg_legacy = Config.load(c3)
        check(
            "旧 data/pet.db 剥前缀 -> 老 data/pet.db",
            cfg_legacy.db_path == config_mod.ROOT_DIR / "data" / "pet.db",
            str(cfg_legacy.db_path),
        )
        check(
            "旧 data/models/... 剥前缀 -> 老 data/models/bge...",
            cfg_legacy.model_dir == config_mod.ROOT_DIR / "data" / "models" / "bge-small-zh-v1.5",
            str(cfg_legacy.model_dir),
        )

        section("prepare_data_root 不可写退默认")
        c = Config.load(_write_config(tmp / "c4.yaml", ""))
        check("data_dir 空 -> 返回 None", c.prepare_data_root() is None, "?")
        ok_root = tmp / "ok_root"
        c_ok = Config.load(_write_config(tmp / "c5.yaml", str(ok_root)))
        check("可写 -> 返回 None", c_ok.prepare_data_root() is None, "?")
        check("可写 -> data_dir 保持不变", c_ok.app.data_dir == str(ok_root), c_ok.app.data_dir)
        # 父级是个文件，子目录建不出来 -> 模拟"盘没挂 / 没权限"
        blocker = tmp / "afile"
        blocker.write_text("x", encoding="utf-8")
        bad = str(blocker / "sub")
        c_bad = Config.load(_write_config(tmp / "c6.yaml", bad))
        got = c_bad.prepare_data_root()
        check("不可写 -> 返回被退掉的路径", got == bad, str(got))
        check("不可写 -> data_dir 被清空", c_bad.app.data_dir == "", repr(c_bad.app.data_dir))

        section("migrate_runtime_data：只补缺，不覆盖已有的")

        old_data = tmp / "old" / "data"
        new_data = tmp / "new"
        sentinel = new_data / config_mod._MIGRATION_SENTINEL
        old_data.mkdir(parents=True)
        (old_data / "pet.db").write_text("pet", encoding="utf-8")
        (old_data / "mind.db").write_text("mind", encoding="utf-8")
        (old_data / "chroma").mkdir()
        (old_data / "chroma" / "x.sqlite3").write_text("vec", encoding="utf-8")
        # 旧 data/ 里那份日志正是真机上"每启动一次抹掉当天日志"的元凶
        (old_data / "logs").mkdir()
        (old_data / "logs" / "pet.log").write_text("旧日志-10月4日那份", encoding="utf-8")
        (old_data / "models").mkdir()
        (old_data / "models" / "m.bin").write_text("model", encoding="utf-8")
        (old_data / "card_chat").mkdir()
        (old_data / "card_chat" / "a.json").write_text("{}", encoding="utf-8")
        (old_data / "pomodoro_state.json").write_text("{}", encoding="utf-8")
        # 一份没主的旁挂文件：老目录里只剩 -wal、主库早没了
        (old_data / "pet.db-wal").write_text("旧wal", encoding="utf-8")

        prev_root = config_mod.ROOT_DIR
        config_mod.ROOT_DIR = tmp / "old"

        def run(root: Path) -> None:
            Config.load(_write_config(tmp / "c7.yaml", str(root))).migrate_runtime_data()

        try:
            # ---- 第一趟：目标全缺 -> 该补的全补上 ----
            run(new_data)

            check("pet.db 补齐到 <根>/db/",
                  (new_data / "db" / "pet.db").read_text(encoding="utf-8") == "pet", "?")
            check("mind.db 补齐到 <根>/db/", (new_data / "db" / "mind.db").exists(), "?")
            check("chroma -> vector", (new_data / "vector" / "x.sqlite3").exists(), "?")
            check("models -> models", (new_data / "models" / "m.bin").exists(), "?")
            check("card_chat -> cache/card_chat",
                  (new_data / "cache" / "card_chat" / "a.json").exists(), "?")
            check("pomodoro_state.json -> cache/",
                  (new_data / "cache" / "pomodoro_state.json").exists(), "?")
            check("旧 data/ 原样保留（只复制不删）", (old_data / "pet.db").exists(), "?")
            check("第一趟跑完落下一次性哨兵（下次启动直接返回）",
                  sentinel.exists(), str(sentinel))
            check("**logs 不在搬家名单里**（日志由 data_dir 自己生成，旧那份搬不过来）",
                  not (new_data / "logs" / "pet.log").exists()
                  and "logs" not in [d for _, d in config_mod._RUNTIME_MIGRATIONS],
                  repr([d for _, d in config_mod._RUNTIME_MIGRATIONS]))

            # ---- 第二趟：哨兵在 -> 连走都不走 ----
            (new_data / "db" / "pet.db").write_text("运行期新内容", encoding="utf-8")
            (old_data / "notes_state.json").write_text("旧笔记", encoding="utf-8")
            run(new_data)
            check("哨兵在 -> 一个字节都不动（连缺的 notes_state.json 也不补）",
                  (new_data / "db" / "pet.db").read_text(encoding="utf-8") == "运行期新内容"
                  and not (new_data / "cache" / "notes_state.json").exists(),
                  "?")

            # ---- 第三趟：把哨兵摘掉，逼它重走一遍"只补缺" ----（A 那条闸单独验）
            sentinel.unlink()
            (new_data / "cache" / "card_chat" / "a.json").unlink()
            # 第一趟主库是缺的，旁挂文件当时**合法地**跟过来了一份；这里先清掉，
            # 好单独看"主库这一趟没搬"时它会不会又跟过来
            (new_data / "db" / "pet.db-wal").unlink()
            run(new_data)

            check("★ 目标已存在且更新 -> 迁移后内容不变（修前这里会被旧 pet.db 盖回去）",
                  (new_data / "db" / "pet.db").read_text(encoding="utf-8") == "运行期新内容",
                  repr((new_data / "db" / "pet.db").read_text(encoding="utf-8")))
            check("★ 真缺的照样补齐（card_chat 被删了，这一趟补回来）",
                  (new_data / "cache" / "card_chat" / "a.json").exists(), "?")
            check("★ 没主的旁挂文件不跟过来（主库没搬，旧 pet.db-wal 就是别人的页）",
                  not (new_data / "db" / "pet.db-wal").exists(), "?")
            check("这一趟也落了哨兵", sentinel.exists(), str(sentinel))

            # ---- 第四趟：主库真缺 -> 主库和它的旁挂文件一起过去 ----
            sentinel.unlink()
            (new_data / "db" / "pet.db").unlink()
            run(new_data)
            check("主库缺 -> 补回来，并且旁挂文件跟着走",
                  (new_data / "db" / "pet.db").exists()
                  and (new_data / "db" / "pet.db-wal").read_text(encoding="utf-8") == "旧wal",
                  "?")

            section("幂等 / 边界：同根、无旧目录，都直接返回")
            # data_dir 与旧 data/ 是同一个 -> 头一道闸直接返回：不报错、不落哨兵、不写文件
            before = sorted(p.name for p in old_data.iterdir())
            Config.load(_write_config(tmp / "c8.yaml", str(old_data))).migrate_runtime_data()
            check("data_dir == 旧 data/ -> 直接返回（没报错，也没往里写哨兵）",
                  sorted(p.name for p in old_data.iterdir()) == before
                  and not (old_data / config_mod._MIGRATION_SENTINEL).exists(),
                  repr(sorted(p.name for p in old_data.iterdir())))

            # 旧 data/ 整个不存在 -> 同样直接返回
            nowhere = tmp / "nowhere"
            config_mod.ROOT_DIR = tmp / "does_not_exist"
            Config.load(_write_config(tmp / "c9.yaml", str(nowhere))).migrate_runtime_data()
            check("旧 data/ 不存在 -> 直接返回，一个新目录都不建",
                  not nowhere.exists(), str(nowhere))
        finally:
            config_mod.ROOT_DIR = prev_root

    return summary("运行时数据归置（v2-data-root）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
