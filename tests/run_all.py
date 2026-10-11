"""
一键跑完所有自动测试。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tests\\run_all.py            # 离线测试（打桩模型，不联网、不花钱）
    .venv\\Scripts\\python.exe tests\\run_all.py --live     # 额外跑"真调模型测指令识别"
    .venv\\Scripts\\python.exe tests\\run_all.py --slow     # 额外跑"慢测试"（跨重启 + 性格不压框架，发版前跑）

每个测试模块都在独立子进程里跑：互不干扰，一个崩了不影响后面的。
每个模块自己造临时配置和临时数据库，不会碰你正在养的那只宠物。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent

OFFLINE = (
    "test_config.py",
    "test_data_root.py",
    "test_commands.py",
    "test_chatter.py",
    "test_reltime.py",
    "test_crisis.py",
    "test_timer.py",
    "test_activity.py",
    "test_proactive.py",
    "test_playmate.py",
    "test_screen.py",
    "test_focus_web.py",
    "test_focus_interact.py",
    "test_focus_notes.py",
    "test_focus_notes2.py",
    "test_focus_quiet.py",
    "test_chat_core.py",
    "test_chat_style.py",
    "test_chat_mirror.py",
    "test_chat_intent.py",
    "test_mood.py",
    "test_mood_proto.py",
    "test_mood_detect.py",
    "test_scene_guard.py",
    "test_scene_state.py",
    "test_scene_switch.py",
    "test_skin_guofeng.py",
    "test_ui.py",
    "test_settings_overflow.py",
    "test_carousel.py",
    "test_card_offer.py",
    "test_comfort.py",
    "test_intent.py",
    "test_mind.py",
    "test_rag.py",
    "test_kb_init.py",
    "test_game.py",
    "test_game_fix.py",
    "test_game_activate.py",
    "test_social.py",
    "test_social_auto.py",
    "test_social_closing.py",
    "test_social_ocr_isolate.py",
    "test_social_ocr_catch.py",
    "test_social_scene_boundary.py",
    "test_social_session_reset.py",
    "test_social_tail_fix.py",
    "test_social_input_draft_exclude.py",
    "test_social_await_prime.py",
    "test_social_material_boundary.py",
    "test_social_material_empty.py",
    "test_social_listview_leak.py",
    "test_social_list_loading.py",
    "test_social_account_pick_page.py",
    "test_social_await_snap.py",
    "test_social_input_edge_line.py",
    "test_social_group_fragment.py",
    "test_social_trigger_window.py",
    "test_social_on_demand_snap.py",
    "test_social_card_timeout.py",
    "test_social_card_timeout_absolute.py",
    "test_social_offer_fast_latency.py",
    "test_social_dup_ocr_login.py",
    "test_social_grab_whitelist.py",
    "test_social_orphan_fix.py",
    "test_social_offer_lifetime.py",
    "test_social_cooldown_per_window.py",
    "test_social_cooldown_by_peer.py",
    "test_social_peer_by_titlebar.py",
    "test_social_no_foreground_steal.py",
    "test_social_offer_expiry_note.py",
    "test_social_offer_key_jitter.py",
    "test_social_session_sync_fg.py",
    "test_social_continuous.py",
    "test_multimodal.py",
    "test_mood_fix.py",
    "test_mood_horror.py",
    "test_mood_screen.py",
    "test_mood_purge.py",
    "test_mood_anger.py",
    "test_mood_vote.py",
    "test_mood_window.py",
    "test_emotion_log.py",
    "test_contradict.py",
    "test_proactive_voice.py",
    "test_speech_polish.py",
    "test_card_del.py",
    "test_life_card_star.py",
    "test_stage.py",
    "test_voice.py",
    "test_asr.py",
    "test_work_card_font.py",
    "test_dialog.py",
    "test_dialog_local.py",
    "test_notes.py",
    "test_notes_path.py",
    "test_notes_cadence.py",
    "test_notes_offer_resume.py",
    "test_card_chat.py",
    "test_chat_scroll_buffer.py",
    "test_chat_scroll_smooth.py",
    "test_card_dedup.py",
    "test_card_kb.py",
    "test_card_tools.py",
    "test_card_reminders.py",
    "test_card_layout.py",
    "test_card_tool_policy.py",
    "test_life_skill.py",
    "test_search_tune.py",
    "test_reset_state.py",
    "test_offline_fallback.py",
    "test_local_llm.py",
    "test_worker_exit_emit.py",
)
LIVE = ("test_live_llm.py", "test_live_comfort.py", "test_live_intent.py")
SLOW = ("test_slow.py",)


# 模块自己打的汇总行：`……：共 N 项，通过 M，失败 K`。harness.summary() 就是这格式。
_SUMMARY_RE = re.compile(r"共\s*\d+\s*项，通过\s*\d+，失败\s*(\d+)")


def _fail_count_in_output(lines: list[str]) -> int | None:
    """从模块自己打的汇总行里读"失败几项"；没有汇总行就 None（不据此判）。"""
    for line in reversed(lines):
        match = _SUMMARY_RE.search(line)
        if match:
            return int(match.group(1))
    return None


def _emit(raw: bytes) -> None:
    """把子进程的这一行**原样**送去终端。

    走 bytes 直写，不经过父进程的编码器：本机控制台是 gbk，子进程的 utf-8 输出
    要是重新编码一遍，遇到解不出的字就抛 UnicodeEncodeError 把整轮跑挂掉
    （试过 text=True + print，正是这么炸的）。
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:                     # 被接到非字节流上（极少数情况）
        sys.stdout.write(raw.decode("utf-8", "replace"))
        sys.stdout.flush()
        return
    sys.stdout.flush()                     # 先把上面那句标题冲出去，别让两行换位
    buffer.write(raw)
    buffer.flush()


def run_one(filename: str) -> bool:
    print(f"\n{'#' * 64}\n# {filename}\n{'#' * 64}")
    # 边跑边把输出透出来（跟以前一样），同时留一份尾巴用来读汇总行。
    proc = subprocess.Popen(
        [sys.executable, "-u", str(TESTS_DIR / filename)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # 管道不是终端，子进程默认按本机区域写 gbk——读汇总行时就得跟着猜编码。
        # 钉成 utf-8，读的那一侧才有个确定的码。终端是走控制台 API 的，不受这条影响。
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        _emit(raw)
        tail.append(raw.decode("utf-8", "replace"))
        if len(tail) > 60:
            tail.pop(0)
    code = proc.wait()
    # 两重判定：以退出码为准，再用模块自己打的汇总行兜一道。有的模块（test_reset_state
    # 就漏过）只调了 summary() 没把失败数带成退出码，光看退出码会把红的记成"通过"。
    # 读不到汇总行就退回只看退出码，绝不凭空判红。
    seen = _fail_count_in_output(tail)
    if seen is None:
        return code == 0
    return code == 0 and seen == 0


def main() -> int:
    wanted = list(OFFLINE)
    if "--live" in sys.argv:
        wanted += list(LIVE)
    if "--slow" in sys.argv:
        wanted += list(SLOW)

    results: list[tuple[str, bool]] = []
    for filename in wanted:
        results.append((filename, run_one(filename)))

    print(f"\n{'=' * 64}")
    print("总览")
    print("-" * 64)
    for filename, ok in results:
        print(f"  {'通过' if ok else '失败'}   {filename}")
    failed = [name for name, ok in results if not ok]
    print(f"{'=' * 64}")
    if failed:
        print(f"有 {len(failed)} 个模块没通过：{'、'.join(failed)}")
    else:
        print(f"全部 {len(results)} 个模块通过")
    tips = []
    if "--live" not in sys.argv:
        tips.append("要跑真调模型的指令识别，加 --live")
    if "--slow" not in sys.argv:
        tips.append("要跑跨重启 / 性格不压框架的慢测试，加 --slow")
    for tip in tips:
        print(f"（{tip}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
