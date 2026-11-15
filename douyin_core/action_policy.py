"""账号写操作的准入策略（与浏览器内核分离的「政策层」）。

为什么单独一层
--------------
点赞 / 评论 / 转发 / 私信 / 投稿都会**以用户本人的账号**留下痕迹，而且公众可见。
所以判定不能散落在每个指令里，必须集中、可审计、默认可关：

1. ``writes_enabled`` 总闸（默认关）
2. ``write_actions`` 动作白名单（默认全不选）
3. ``write_daily_quota`` 每个动作每日上限（0 = 不发）
4. ``dm_allow_targets`` 收件人白名单（对 dm / share 生效）
5. ``write_dry_run`` 演练开关（默认开：只返回 will-do，不碰真账号）
6. ``require_confirm``：模型调用必须显式 ``confirm=true``；人打的指令视为已确认

判定结果只有三种：``blocked`` / ``dry_run`` / ``live``。
任何一条不满足都返回 ``blocked`` 并带可执行的原因，不做「静默降级」。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

WRITE_ACTIONS: tuple[str, ...] = ("like", "comment", "share", "dm", "publish")
TARGETED_ACTIONS: tuple[str, ...] = ("dm", "share")

ACTION_LABELS = {
    "like": "点赞 / 取消点赞",
    "comment": "发评论 / 回复评论",
    "share": "转发视频给好友",
    "dm": "发私信",
    "publish": "投稿视频",
}


def day_start_ts(now: float | None = None) -> float:
    """本地时区当天 00:00 的时间戳（额度按自然日重置）。"""
    current = time.localtime(now or time.time())
    return time.mktime((current.tm_year, current.tm_mon, current.tm_mday, 0, 0, 0, 0, 0, -1))


@dataclass
class Decision:
    action: str
    mode: str                      # blocked | dry_run | live
    reason: str
    target: str = ""
    quota: int = 0
    used: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.mode == "live"

    @property
    def preview(self) -> bool:
        return self.mode == "dry_run"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "mode": self.mode,
            "allowed": self.allowed,
            "reason": self.reason,
            "target": self.target,
            "quota": self.quota,
            "used_today": self.used,
        }

    def message(self) -> str:
        if self.mode == "live":
            return "已通过写操作闸门"
        if self.mode == "dry_run":
            return f"演练模式（未真正执行）：{self.reason}"
        return f"已拒绝：{self.reason}"


class ActionPolicy:
    def __init__(
        self,
        *,
        writes_enabled: bool = False,
        dry_run: bool = True,
        actions: tuple[str, ...] = (),
        quotas: dict[str, int] | None = None,
        targets: tuple[str, ...] = (),
        publish_dir: str = "",
        require_confirm: bool = True,
        store: Any = None,
    ):
        self.writes_enabled = bool(writes_enabled)
        self.dry_run = bool(dry_run)
        self.actions = tuple(a for a in actions if a in WRITE_ACTIONS)
        self.quotas = {a: max(0, int((quotas or {}).get(a, 0))) for a in WRITE_ACTIONS}
        self.targets = tuple(t.strip() for t in targets if t and t.strip())
        self.publish_dir = str(publish_dir or "").strip()
        self.require_confirm = bool(require_confirm)
        self.store = store

    # ------------------------------------------------------------- 判定入口 --
    def check(self, action: str, target: str = "", *, confirm: bool = False) -> Decision:
        action = str(action or "").strip()
        target = str(target or "").strip()

        if action not in WRITE_ACTIONS:
            return Decision(action, "blocked", f"未知写动作 {action!r}")

        quota = self.quotas.get(action, 0)
        used = self.used_today(action)

        if action not in self.actions:
            return Decision(action, "blocked", f"动作未在白名单里（write_actions 未包含 {action}）", target, quota, used)
        if not self.writes_enabled:
            return Decision(action, "blocked", "写操作总闸 writes_enabled 是关的", target, quota, used)
        if quota <= 0:
            return Decision(action, "blocked", f"{ACTION_LABELS[action]} 的每日额度是 0（= 不发）", target, quota, used)
        if used >= quota:
            return Decision(action, "blocked", f"今天已经做过 {used} 次，上限 {quota} 次/天", target, quota, used)
        if self.targets and action in TARGETED_ACTIONS and target not in self.targets:
            return Decision(action, "blocked", "收件人不在 dm_allow_targets 白名单里", target, quota, used)
        if self.dry_run:
            return Decision(action, "dry_run", "write_dry_run=true，只回放将要执行的动作", target, quota, used)
        if self.require_confirm and not confirm:
            return Decision(action, "dry_run", "需要明确确认（模型调用须传 confirm=true）", target, quota, used)
        return Decision(action, "live", "通过", target, quota, used)

    def check_publish_path(self, path: str) -> tuple[bool, str]:
        """投稿文件必须落在允许目录内，避免模型把任意文件传上去。"""
        if not self.publish_dir:
            return False, "未配置 publish_allow_dir，投稿被拒（出于安全默认不允许任意路径）"
        try:
            from pathlib import Path

            root = Path(self.publish_dir).expanduser().resolve()
            candidate = Path(str(path or "")).expanduser().resolve()
        except Exception as exc:
            return False, f"路径无法解析：{type(exc).__name__}"
        if not candidate.is_file():
            return False, f"文件不存在：{candidate}"
        try:
            candidate.relative_to(root)
        except ValueError:
            return False, f"文件不在允许目录 {root} 内"
        return True, "ok"

    # --------------------------------------------------------------- 记账 ----
    def used_today(self, action: str) -> int:
        if self.store is None:
            return 0
        try:
            return int(self.store.count_actions_today(action, day_start_ts()))
        except Exception:
            return 0

    def record(self, decision: Decision, *, ok: bool, detail: str = "") -> None:
        if self.store is None:
            return
        try:
            self.store.log_action(
                action=decision.action,
                target=decision.target,
                mode=decision.mode,
                ok=bool(ok),
                detail=detail[:500],
            )
        except Exception:
            pass

    # --------------------------------------------------------------- 说明 ----
    def describe(self) -> dict[str, Any]:
        return {
            "writes_enabled": self.writes_enabled,
            "dry_run": self.dry_run,
            "require_confirm": self.require_confirm,
            "actions": {a: (a in self.actions) for a in WRITE_ACTIONS},
            "quotas": dict(self.quotas),
            "used_today": {a: self.used_today(a) for a in WRITE_ACTIONS},
            "targets": list(self.targets),
            "publish_dir": self.publish_dir,
        }

    def summary_lines(self) -> list[str]:
        info = self.describe()
        lines = [
            f"总闸 writes_enabled={'开' if info['writes_enabled'] else '关'}；"
            f"演练 write_dry_run={'开' if info['dry_run'] else '关'}；"
            f"模型需确认={'是' if info['require_confirm'] else '否'}",
        ]
        for action in WRITE_ACTIONS:
            enabled = "✓" if info["actions"][action] else "✗"
            lines.append(
                f"{enabled} {ACTION_LABELS[action]}："
                f"今日 {info['used_today'][action]}/{info['quotas'][action]}"
            )
        if info["targets"]:
            lines.append("收件人白名单：" + "、".join(info["targets"]))
        else:
            lines.append("收件人白名单：未设置（dm / share 不限制；建议设置）")
        lines.append(
            "投稿目录：" + (info["publish_dir"] or "未设置（投稿会被拒绝）")
        )
        return lines
