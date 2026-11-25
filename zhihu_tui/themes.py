from __future__ import annotations

import json
from pathlib import Path

from textual.theme import Theme

CONFIG = Path.home() / ".config" / "zhihu-tui" / "config.json"
DEFAULT_THEME = "zh-light"

# (id, 显示名, 说明, Theme)
THEMES: list[tuple[str, str, str, Theme]] = [
    (
        "zh-light",
        "浅色",
        "白底深字",
        Theme(
            name="zh-light",
            primary="#1f5fbf",
            secondary="#5b6b7c",
            accent="#b45309",
            warning="#b45309",
            error="#b91c1c",
            success="#15803d",
            foreground="#1c1e21",
            background="#f6f7f9",
            surface="#eceff3",
            panel="#e2e6ec",
            dark=False,
        ),
    ),
    (
        "zh-dark",
        "深色",
        "深底浅字",
        Theme(
            name="zh-dark",
            primary="#6cb2ff",
            secondary="#8a94a6",
            accent="#ffb454",
            warning="#ffb454",
            error="#ff7b72",
            success="#7ee787",
            foreground="#d7dae0",
            background="#15171c",
            surface="#1c1f26",
            panel="#262a33",
            dark=True,
        ),
    ),
    (
        "zh-cb-light",
        "色盲友好 浅色",
        "蓝橙配色,不依赖红绿区分",
        Theme(
            name="zh-cb-light",
            primary="#0072b2",
            secondary="#5b6b7c",
            accent="#d55e00",
            warning="#e69f00",
            error="#d55e00",
            success="#0072b2",
            foreground="#1c1e21",
            background="#f6f7f9",
            surface="#eceff3",
            panel="#e2e6ec",
            dark=False,
        ),
    ),
    (
        "zh-cb-dark",
        "色盲友好 深色",
        "蓝橙配色,不依赖红绿区分",
        Theme(
            name="zh-cb-dark",
            primary="#56b4e9",
            secondary="#8a94a6",
            accent="#e69f00",
            warning="#f0e442",
            error="#e69f00",
            success="#56b4e9",
            foreground="#d7dae0",
            background="#15171c",
            surface="#1c1f26",
            panel="#262a33",
            dark=True,
        ),
    ),
    (
        "zh-contrast",
        "高对比",
        "黑底白字,黄色强调",
        Theme(
            name="zh-contrast",
            primary="#ffff00",
            secondary="#ffffff",
            accent="#00ffff",
            warning="#ffff00",
            error="#ff6b6b",
            success="#00ff00",
            foreground="#ffffff",
            background="#000000",
            surface="#000000",
            panel="#1a1a1a",
            dark=True,
        ),
    ),
]


def load_theme() -> str:
    try:
        name = json.loads(CONFIG.read_text()).get("theme", DEFAULT_THEME)
    except (OSError, ValueError):
        return DEFAULT_THEME
    return name if any(t[0] == name for t in THEMES) else DEFAULT_THEME


def save_theme(name: str) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        data = {}
    data["theme"] = name
    CONFIG.write_text(json.dumps(data, ensure_ascii=False))
