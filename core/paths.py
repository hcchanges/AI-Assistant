"""运行时路径：开发态用脚本目录，打包态用 PyInstaller 的 _MEIPASS。

只依赖标准库，任何 core/life/ui 模块都可以 import 它，不会成环。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是不是 PyInstaller 打包出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """只读资源根：打包态是 _MEIPASS（onedir 下的 _internal），开发态是项目根。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", "")
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """可写根：打包态是 exe 所在目录，开发态是项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return bundle_dir()


def resource_path(*parts: str) -> Path:
    return bundle_dir().joinpath(*parts)


def app_path(*parts: str) -> Path:
    return app_dir().joinpath(*parts)


def scene_skin_dir() -> Path:
    """国风神兽整图目录：随仓库走（pet/assets/scenes），打包态在 _MEIPASS 下。

    以前开发态读项目上一级的数据目录，那份图不进 git，clone 下来必缺；
    现在四张换皮图入库，源码运行和 exe 用同一套路径。
    """
    return bundle_dir() / "pet" / "assets" / "scenes"


def ensure_app_file(name: str, template: str | None = None) -> tuple[Path, bool]:
    """保证 exe 旁边有某个用户文件；缺了就从内置模板复制。

    返回 (路径, 这次是否新建)。模板不存在或写不进去时原样返回目标路径，
    交给调用方按既有逻辑报错（不要在这里把启动打断）。
    """
    target = app_path(name)
    if target.exists():
        return target, False
    source = resource_path(template or name)
    if source.is_file():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return target, True
        except OSError:
            pass
    return target, False
