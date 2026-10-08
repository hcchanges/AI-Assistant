"""画像存储（指令 26-C C1 S1）：会话级内存，不接 DB。

红线：data 运行数据不入库。画像里抽出来的原始用户信息（部门 / 目的地 / 偏好…）
只在会话内存里，重启即清空，绝不写进 pet.db。
"""

from __future__ import annotations

from dataclasses import dataclass, field

WORK = "work"
TRAVEL = "travel"
DIET = "diet"


@dataclass
class ProfileStore:
    """三块画像：工作 / 出行 / 饮食。字段都是「原文里提到过」的字眼，认不出就缺省。"""

    work: dict[str, str] = field(default_factory=dict)
    travel: dict[str, str] = field(default_factory=dict)
    diet: dict[str, str] = field(default_factory=dict)

    def _bucket(self, kind: str) -> dict[str, str]:
        if kind == WORK:
            return self.work
        if kind == DIET:
            return self.diet
        return self.travel

    def update(self, kind: str, fields: dict[str, str]) -> None:
        """把抽到的字段并进来；空值跳过，不拿空串覆盖已有信息。"""
        bucket = self._bucket(kind)
        for key, value in fields.items():
            if value:
                bucket[key] = value

    def get(self, kind: str) -> dict[str, str]:
        """取一份副本，别让调用方顺手改到内部状态。"""
        return dict(self._bucket(kind))

    def merge(self, kind: str, other: dict[str, str]) -> None:
        self.update(kind, other)
