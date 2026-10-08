"""
配置加载/保存的自动测试（指令：配置不被重置）。

覆盖用户报告的那个严重问题——config.yaml 被程序用默认值重写：
1. 加载时只读 + 合并"缺失的"默认值，不覆盖用户已有值；
2. 保存时用「合并」而不是「整体重建」，绝不丢未知段、未知字段、用户新增的 provider；
3. "改一次 → 重启（重新 load）" 后用户的东西还在。

用一个临时 config 文件测，不碰项目里真实的 config.yaml。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

import yaml

from core.config import Config, ConfigError

# 一个"用户已经改过"的配置：model 换成了 deepseek-flash、加了一个 deepseek_pro provider、
# 有 work 段、还有程序不认识的未知段 custom_extra 和未知字段 custom_field。
USER_CONFIG = """app:
  name: 测试宠物
  autostart: false
  log_level: INFO
  log_dir: data/logs
llm:
  active: deepseek
  temperature: 0.5
  providers:
    deepseek:
      protocol: openai_compatible
      base_url: https://api.deepseek.com/v1
      model: deepseek-flash
      api_key_ref: llm_deepseek
    deepseek_pro:
      protocol: openai_compatible
      base_url: https://api.deepseek.com/v1
      model: deepseek-reasoner
      api_key_ref: llm_deepseek_pro
    tongyi:
      protocol: openai_compatible
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model: qwen-plus
      api_key_ref: llm_dashscope
  custom_field: 别删我
pet:
  name: 天渌
  persona: 软萌黏人
  position: [100, 200]
  hover_hint: true
  hover_hint_sec: 0.5
memory:
  db_path: data/pet.db
  vector_path: data/chroma
  model_path: data/models/bge-small-zh-v1.5
  top_k: 5
schedule:
  quiet_hours: ['23:00', '07:30']
  max_notify_per_hour: 3
pomodoro:
  focus_min: 25
vision:
  provider: tongyi
  model: qwen-vl-max
work:
  provider: tongyi
  top_n: 5
  pet_comment: true
card:
  categories:
    餐饮: [外卖, 奶茶]
    出行: [地铁, 公交]
  classify_confidence: 0.6
  invite_global_days: 3
  decline_silence_days: 7
  archive_idle_days: 14
life_cards:
  profile_tools_enabled: true
custom_extra:
  hello: world
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _read_raw(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pet_config_") as tmp:
        path = Path(tmp) / "config.yaml"

        section("加载：读用户值 + 合并缺失默认值")
        _write(path, USER_CONFIG)
        config = Config.load(path)
        check("用户改的 model 读进来了", config.llm.provider("deepseek").model == "deepseek-flash", config.llm.provider("deepseek").model)
        check("用户加的 provider 读进来了", "deepseek_pro" in config.llm.providers, str(list(config.llm.providers)))
        check("work 段读进来了", config.work.pet_comment is True and config.work.top_n == 5, str(config.work))
        check("身份（调试用）缺省就是管理员", config.work.role == "admin", config.work.role)
        check("温度读的是用户值 0.5", config.llm.temperature == 0.5, str(config.llm.temperature))
        check(
            "card 的品类词表读进来了",
            config.card.categories.get("出行") == ["地铁", "公交"],
            str(config.card.categories),
        )
        check(
            "card 的判类置信度读的是用户值 0.6",
            config.card.classify_confidence == 0.6,
            str(config.card.classify_confidence),
        )
        check(
            "life_cards 的两半卡开关读的是用户值 true",
            config.life_cards.profile_tools_enabled is True,
            str(config.life_cards.profile_tools_enabled),
        )

        # 缺失字段用默认值补：把 activity 段删掉，load 后 activity.enabled 应是默认 True
        _write(
            path,
            USER_CONFIG.replace("activity:\n  enabled: true\n  sample_sec: 3\n  idle_min: 5\n  retention_days: 14\n  blacklist: []\n  sedentary_min: 30\n  sedentary_repeat_min: 30\n  distract_switches: 14\n  distract_window_sec: 60\n  distract_cooldown_min: 5\n  remind: false\n", ""),
        )
        defaulted = Config.load(path)
        check("缺失的 activity 段用默认值补上", defaulted.activity.enabled is True, str(defaulted.activity.enabled))

        # 新加的段老配置里没有：缺失时两半卡工具默认关
        _write(path, USER_CONFIG.replace("life_cards:\n  profile_tools_enabled: true\n", ""))
        no_life_cards = Config.load(path)
        check(
            "老配置里没有 life_cards 段 → 两半卡工具默认关",
            no_life_cards.life_cards.profile_tools_enabled is False,
            str(no_life_cards.life_cards.profile_tools_enabled),
        )

        section("保存：合并而不是重建（绝不丢东西）")
        _write(path, USER_CONFIG)
        config = Config.load(path)
        config.pet.position = [999, 888]   # 模拟"拖宠物 → 保存位置"那次运行时 save
        config.save()

        raw = _read_raw(path)
        check("保存后用户 model 没被改回默认", raw["llm"]["providers"]["deepseek"]["model"] == "deepseek-flash", raw["llm"]["providers"]["deepseek"]["model"])
        check("保存后用户加的 provider 还在", "deepseek_pro" in raw["llm"]["providers"], str(list(raw["llm"]["providers"])))
        check("保存后 work 段还在", "work" in raw and raw["work"]["top_n"] == 5, str(raw.get("work")))
        check(
            "保存后 card 词表还在",
            raw.get("card", {}).get("categories", {}).get("餐饮") == ["外卖", "奶茶"],
            str(raw.get("card")),
        )
        check(
            "保存后 life_cards 开关还在（重启后状态保持）",
            raw.get("life_cards", {}).get("profile_tools_enabled") is True,
            str(raw.get("life_cards")),
        )
        check("保存后未知段 custom_extra 还在", raw.get("custom_extra", {}).get("hello") == "world", str(raw.get("custom_extra")))
        check("保存后未知字段 custom_field 还在", raw["llm"].get("custom_field") == "别删我", str(raw["llm"].get("custom_field")))
        check("保存后位置写回了新值", raw["pet"]["position"] == [999, 888], str(raw["pet"]["position"]))

        section("重启（重新 load）后一切照旧")
        reloaded = Config.load(path)
        check("重启后 model 仍是 flash", reloaded.llm.provider("deepseek").model == "deepseek-flash", reloaded.llm.provider("deepseek").model)
        check("重启后 deepseek_pro 还在", "deepseek_pro" in reloaded.llm.providers, str(list(reloaded.llm.providers)))
        check("重启后 work 段还在", reloaded.work.top_n == 5 and reloaded.work.pet_comment is True, str(reloaded.work))
        check("重启后位置是保存后的值", reloaded.pet.position == [999, 888], str(reloaded.pet.position))

        section("保存：程序跑着的时候，外面改的配置不会被吞")
        # 宠物跑着（内存里是启动那一份），管理员在外面直接编辑 config.yaml
        _write(path, USER_CONFIG)
        running = Config.load(path)          # 这一份是"启动时的快照"
        edited = _read_raw(path)
        edited["card"]["categories"]["出行"] = ["飞机", "高铁", "机场"]   # 外面加的出行词
        edited["llm"]["custom_field"] = "外面改的"                        # 外面改的未知字段
        _write(path, yaml.safe_dump(edited, allow_unicode=True, sort_keys=False))

        running.pet.position = [123, 456]    # 程序自己只改了位置（比如拖了一下宠物）
        running.save()

        merged = _read_raw(path)
        check(
            "外面加的出行词没被这次保存吞掉",
            merged["card"]["categories"]["出行"] == ["飞机", "高铁", "机场"],
            str(merged["card"]["categories"].get("出行")),
        )
        check(
            "外面改的未知字段没被改回去",
            merged["llm"].get("custom_field") == "外面改的",
            str(merged["llm"].get("custom_field")),
        )
        check("程序自己改的位置照样写回了", merged["pet"]["position"] == [123, 456], str(merged["pet"]["position"]))

        section("坏配置：缺 model / 未知协议仍要报错")
        _write(path, USER_CONFIG.replace("model: deepseek-flash", "model: ''"))
        try:
            Config.load(path)
            check("provider 缺 model → 报错", False, "居然没报错")
        except ConfigError as exc:
            check("provider 缺 model → 报错", "deepseek" in str(exc), str(exc))

    return summary("配置保存/加载")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
