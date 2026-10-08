"""
后台任务：LLM 调用等耗时操作放到线程池，避免卡住界面。

回调函数在主线程执行（靠 Qt 的跨线程信号队列），所以回调里可以直接改界面。

关键约束：task 必须存活到回调执行完毕。
原因——run() 在 worker 线程 emit 信号后，回调是排队到主线程异步执行的；
若 task 在 emit 后被回收（局部变量离开作用域 + QRunnable 默认 autoDelete），
signals 对象随之销毁，Qt 会丢弃尚未派发的排队事件，回调永远不会触发。
因此这里用一个集合持有 task 引用，并关闭 autoDelete，回调执行后再释放。

另一头也得管：**退出程序**时解释器收尾会把这个集合（模块全局）也清掉，在途的
活儿回来 emit 时信号源已经销毁——那一声由 `_CallableTask._notify` 安静放过（见那里）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)

# 持有正在等待回调的 task，防止在回调执行前被回收
_pending_tasks: set[_CallableTask] = set()
_pending_lock = threading.Lock()


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _CallableTask(QRunnable):
    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # 交给调用方统一处理
            logger.exception("后台任务执行失败: %s", self.fn)
            self._notify("failed", exc)
        else:
            self._notify("done", result)

    def _notify(self, which: str, payload: Any) -> None:
        """把这一声发出去；**发不出去也不许炸**（v2-worker-exit-emit-guard）。

        关程序时解释器收尾会清模块全局，本模块里那唯一的强引用（`_pending_tasks`）
        一没，task 连同它的 `_TaskSignals` 就被回收；而此时**还在途**的后台活儿
        （抓屏认字、提炼、模型往返）回来照样要 emit —— 源已经不在，PySide 抛
        `RuntimeError: Signal source has been deleted`。这句异常从
        `QRunnable::run()` 里逃出去谁也接不住，PySide 只能交给 sys.excepthook，用户
        看到的是 `[CRITICAL] main: 未捕获的异常`（真机 pet.log 里 9 次全是它，9/9 都
        落在 `=== 电子宠物 退出 ===` 之后）。程序都在关了，这一声"我干完了"没人接
        得着，记一行 debug 就走——不拿它把退出搅成一条 CRITICAL。

        先看一眼源还在不在再发：那一眼图的是让收尾这一档压根不进 emit；收尾期连
        `isValid` 都可能问不出来，真正兜底的是下面那圈 try/except。
        """
        if not _still_alive(self.signals):
            logger.debug("后台任务回来时程序已在收尾（信号源已销毁），%s 这一声不发了", which)
            return
        try:
            getattr(self.signals, which).emit(payload)
        except RuntimeError as exc:
            # 上面那一眼与 emit 之间被收掉（收尾是别的线程在跑）：同一种情况，不炸
            logger.debug("后台任务 %s 这一声发出时信号源已销毁，不发了（%s）", which, exc)


def _still_alive(signals: QObject) -> bool:
    """这个 QObject 的底层对象还在不在（解释器收尾时会被一起销毁）。

    `shiboken6` 是 PySide6 自带的，不是新依赖；万一问不出来就当"还在"——
    真兜底的是 `_notify` 那圈 try/except。
    """
    try:
        from shiboken6 import isValid

        return bool(isValid(signals))
    except Exception:  # noqa: BLE001 - 收尾期什么怪事都可能有，别在这儿再抛一个
        return True


def run_async(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_failed: Callable[[Exception], None] | None = None,
    **kwargs: Any,
) -> None:
    """在线程池里跑 fn，结果通过回调返回主线程。"""
    task = _CallableTask(fn, args, kwargs)
    task.setAutoDelete(False)  # 生命周期交给 Python 侧管理，避免被 C++ 提前删除

    if on_done is not None:
        task.signals.done.connect(on_done)
    if on_failed is not None:
        task.signals.failed.connect(on_failed)
    else:
        task.signals.failed.connect(lambda exc: logger.error("后台任务未处理异常: %s", exc))

    def _release(*_args: Any) -> None:
        # 回调执行后释放引用，允许 task 被回收
        with _pending_lock:
            _pending_tasks.discard(task)

    task.signals.done.connect(_release)
    task.signals.failed.connect(_release)

    # 持有引用，保证 task 与 signals 存活到回调派发完成
    with _pending_lock:
        _pending_tasks.add(task)

    QThreadPool.globalInstance().start(task)
