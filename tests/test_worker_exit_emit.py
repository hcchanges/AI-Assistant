"""后台任务收尾那一声：退出时在途 worker 回来 emit，不许把退出搅成 CRITICAL。

真机那条账（data/logs/pet.log）：

    [CRITICAL] main: 未捕获的异常
    Traceback (most recent call last):
      File "core/worker.py", line 48, in run
        self.signals.done.emit(result)
    RuntimeError: Signal source has been deleted

一共 9 次，**9/9 都紧跟在 `=== 电子宠物 退出 ===` 之后**（同一份日志里一次都没落在
会话中途；那两天其余几条 CRITICAL 都是 2026-09-29/10-01 的老账，跟这条不是一回事）。
它不是"监听哑掉"，是退出收尾那一档：

    `run_async` 把 task 的唯一强引用放在模块级 `_pending_tasks` 里（模块头那段
    "task 必须存活到回调执行完毕"讲的就是它）。关程序时解释器收尾会清模块全局，
    这个集合一没，task 连同它的 `_TaskSignals` 一起被回收；而**还在途**的活儿
    （抓屏认字、提炼、模型往返）这才回来，emit 的源已经不在。这句话从
    `QRunnable::run()` 里逃出去谁也接不住，PySide 只能交给 sys.excepthook。

根因在收尾这一档，不在"谁先谁后"——本机拿最小复刻分三格验过（脚本不进仓库）：
事件循环退出后**不收尾**、哪怕 QApplication 已经销毁而 worker 才回来，都不炸；
一让 `sys.exit` 收尾，同一段代码就报上面那一句。复刻脚本另存于
`%TEMP%/p18/probe_exit.py`、`probe_exit2.py`。

这里钉的是修法（v2-worker-exit-emit-guard）：`_CallableTask._notify` 发之前先看源
还在不在，发的时候也不让 `RuntimeError` 从 `run()` 里逃出去——程序都在关了，这一声
"我干完了"没人接得着，记一行 debug 就走。① 钉"源还在时照旧送得到"，②③④ 钉收尾
那一档不炸也不冒 CRITICAL。

红线：零新依赖（`shiboken6` 随 PySide6 一起来）；这一份不建环境、不连库、不联网。
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, ensure_app, note, section, summary, wait_for  # noqa: E402

import shiboken6  # noqa: E402

import core.worker as worker_mod  # noqa: E402
from core.worker import _CallableTask, _TaskSignals, _still_alive, run_async  # noqa: E402


class _Catcher(logging.Handler):
    """把 ERROR 及以上的日志收下来——真机上用户看得见的那一条 CRITICAL 就是它。"""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _bare_task(fn) -> _CallableTask:
    """造一个没人持有引用的 task：跟 `run_async` 起的那个同构，但不进 `_pending_tasks`。"""
    task = _CallableTask(fn, (), {})
    task.setAutoDelete(False)
    return task


def _run_catching(task: _CallableTask) -> str | None:
    """跑一趟，返回"逃出来的异常"长什么样；没逃出来就是 None（= 修好了）。"""
    try:
        task.run()
    except BaseException as exc:  # noqa: BLE001 - 这儿就是要看它会不会逃
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> int:
    ensure_app()
    note("这一份不建环境（不调 make_env）、不连库、不联网：收尾那一档跟数据无关")

    catcher = _Catcher()
    logging.getLogger().addHandler(catcher)
    quiet = logging.getLogger("core.worker")
    quiet_level = quiet.level

    try:
        # ---------- ① 源还在：照旧送得到（别把正常那条腿修坏） ----------

        section("① 源还在时：done / failed 照旧送到回调")

        spoken: list[tuple[str, object]] = []
        run_async(
            lambda: "结果",
            on_done=lambda r: spoken.append(("done", r)),
            on_failed=lambda e: spoken.append(("failed", e)),
        )
        check("跑完的活儿，回调照旧收到结果",
              wait_for(lambda: spoken == [("done", "结果")]), repr(spoken))

        quiet.setLevel(logging.CRITICAL)   # 下面故意让它失败一次，别把 traceback 打进测试输出
        try:
            spoken.clear()

            def _boom() -> None:
                raise ValueError("这一趟炸了")

            run_async(
                _boom,
                on_done=lambda r: spoken.append(("done", r)),
                on_failed=lambda e: spoken.append(("failed", type(e).__name__)),
            )
            check("炸了的活儿，回调照旧收到那一声（失败路一个字节没动）",
                  wait_for(lambda: spoken == [("failed", "ValueError")]), repr(spoken))
        finally:
            quiet.setLevel(quiet_level)

        before_release = len(worker_mod._pending_tasks)
        run_async(lambda: 1, on_done=lambda r: None)
        check("回调派发完，task 照旧从 _pending_tasks 里放掉（生命周期那条没动）",
              wait_for(lambda: len(worker_mod._pending_tasks) == before_release),
              f"{before_release} → {len(worker_mod._pending_tasks)}")

        # ---------- ② 源没了：这一声安静落空 ----------

        section("② 收尾后源已销毁：emit 不炸、也不冒 CRITICAL")

        task = _bare_task(lambda: "老账")
        shiboken6.delete(task.signals)      # 复刻"收尾把 _TaskSignals 收掉"那一刻

        raw: str | None = None
        try:
            task.signals.done.emit("x")
        except RuntimeError as exc:
            raw = str(exc)
        check("前提：源已销毁后再 emit，抛的就是真机那一类（*deleted*）",
              raw is not None and "deleted" in raw, raw or "（竟然没抛，前提不成立）")

        seen_before = len(catcher.records)
        crashed = _run_catching(task)
        check("★ 修后的 run() 不抛（这一声安静落空）", crashed is None, crashed or "没抛")
        fresh = catcher.records[seen_before:]
        check("★ 也没有往外冒 ERROR/CRITICAL（真机那条 CRITICAL 就是它）",
              not fresh, "；".join(f"{r.levelname} {r.getMessage()}" for r in fresh) or "干净")

        quiet.setLevel(logging.CRITICAL)
        try:
            task_failed = _bare_task(lambda: (_ for _ in ()).throw(ValueError("干不成")))
            shiboken6.delete(task_failed.signals)
            crashed = _run_catching(task_failed)
        finally:
            quiet.setLevel(quiet_level)
        check("★ 失败那一半同理：活儿炸了、源也没了，run() 还是不抛",
              crashed is None, crashed or "没抛")

        # ---------- ③ 存活判定 ----------

        section("③ 存活判定：新对象算在、销毁过的算不在、认不出的不炸")

        alive = _TaskSignals()
        check("刚造出来的对象：算在", _still_alive(alive) is True, str(_still_alive(alive)))
        shiboken6.delete(alive)
        check("销毁过的对象：算不在（就是这一眼拦住收尾那一档）",
              _still_alive(alive) is False, str(_still_alive(alive)))

        try:
            weird = _still_alive(None)          # 收尾期问出什么都不稀奇
            check("认不出的东西也不炸（判定让路给 emit 那圈兜底）", True, f"回的是 {weird!r}")
        except BaseException as exc:            # noqa: BLE001
            check("认不出的东西也不炸（判定让路给 emit 那圈兜底）", False,
                  f"{type(exc).__name__}: {exc}")

        # ---------- ④ 判定万一撒谎 ----------

        section("④ 兜底：存活判定说「还在」（撒谎），try/except 也得兜住")

        task3 = _bare_task(lambda: "老账")
        shiboken6.delete(task3.signals)
        original = worker_mod._still_alive
        worker_mod._still_alive = lambda signals: True      # 撒谎：永远说还在
        try:
            crashed = _run_catching(task3)
        finally:
            worker_mod._still_alive = original
        check("★ 判定撒谎时，run() 照样不抛（那圈 try/except 是真兜底）",
              crashed is None, crashed or "没抛")

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：零新依赖 / 模块头把为什么写在原地")

        import PySide6  # noqa: PLC0415

        check("shiboken6 与 PySide6 同一份 site-packages（不是新装的依赖）",
              Path(shiboken6.__file__).parent.parent == Path(PySide6.__file__).parent.parent,
              f"{Path(shiboken6.__file__).parent} / {Path(PySide6.__file__).parent}")
        src = inspect.getsource(worker_mod)
        check("改动处留了「为什么」（v2-worker-exit-emit-guard 与真机那 9 次）",
              "v2-worker-exit-emit-guard" in src and "Signal source has been deleted" in src,
              "")
        check("run() 里不再有裸 emit（两个都收进 _notify 了）",
              "self.signals.failed.emit" not in src and "self.signals.done.emit" not in src, "")
    finally:
        logging.getLogger().removeHandler(catcher)

    return summary("后台任务收尾 emit（v2-worker-exit-emit-guard）")


if __name__ == "__main__":
    raise SystemExit(main())
