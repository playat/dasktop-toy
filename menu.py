"""右键菜单（M3）：原生 QMenu，层级与 shared/menu.ts 一致。

    动作 ┬ 待机      ┬ 动画名…
         ├ 转向      │
         ├ 拖拽      │
         ├ 点击回应  │
         ├ 移动      │
         ├ <分类>    │
         └ <事件档位>┘
    大小 ┬ 大 / 中 / 小 / 迷你
    固定原地（勾选）
    回到初始位置
    重载配置
    退出

点播不受 fixedEnabled 影响——这是用户主动触发，和 Electron 版语义一致。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QWidget

from config import PetConfig
from player import AnimationSet

# 与项目自带菜单同一套观感（白底、13px、圆角高亮）
MENU_QSS = """
QMenu {
    background: #ffffff;
    border: 1px solid #d8d8d8;
    border-radius: 8px;
    padding: 5px;
    color: #2b2b2b;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}
QMenu::item {
    padding: 5px 26px 5px 12px;
    border-radius: 5px;
    min-width: 118px;
}
QMenu::item:selected { background: #e8f1ff; color: #101010; }
QMenu::item:disabled { color: #9a9a9a; }
QMenu::separator { height: 1px; background: #ececec; margin: 5px 7px; }
QMenu::indicator { width: 14px; height: 14px; left: 6px; }
"""

EVENT_LABELS = {"balance": "余额档位", "whisper": "碎碎念", "workStatus": "工作状态"}

SIZE_PRESETS: list[tuple[str, int]] = [
    ("大", 540),
    ("中", 462),
    ("小", 380),
    ("迷你", 320),
]


def _fill(menu: QMenu, names: list[str], anims: AnimationSet, on_play: Callable[[str], None]) -> int:
    """往菜单里塞一批动画项，返回真正可用的条数（缺素材的也列出来，只是禁用）。"""
    usable = 0
    for name in names:
        action = QAction(name, menu)
        if anims.path_of(name) is None:
            action.setEnabled(False)
            action.setText(f"{name}（缺素材）")
        else:
            usable += 1
            action.triggered.connect(lambda _checked=False, n=name: on_play(n))
        menu.addAction(action)
    return usable


def build_menu(
    parent: QWidget | None,
    cfg: PetConfig,
    anims: AnimationSet,
    *,
    size: int,
    fixed: bool,
    on_play: Callable[[str], None],
    on_size: Callable[[int], None],
    on_fixed: Callable[[bool], None],
    on_home: Callable[[], None],
    on_reload: Callable[[], None],
    on_quit: Callable[[], None],
    extra: list[QAction] | None = None,
) -> QMenu:
    menu = QMenu(parent)
    menu.setStyleSheet(MENU_QSS)

    # ---- 动作（二级分类 / 三级动画名）----
    action_menu = menu.addMenu("动作")
    action_menu.setStyleSheet(MENU_QSS)
    pools = cfg.pools
    groups: list[tuple[str, list[str]]] = [
        ("待机", pools.idle),
        ("转向", pools.turn),
        ("拖拽", pools.drag),
        ("点击回应", pools.clicks),
        ("移动", [m.name for m in pools.moves]),
    ]
    for label, names in groups:
        if not names:
            continue
        sub = action_menu.addMenu(label)
        sub.setStyleSheet(MENU_QSS)
        _fill(sub, names, anims, on_play)

    for cat in pools.categories:
        actions = [str(x) for x in (cat.get("actions") or [])]
        if not actions:
            continue
        sub = action_menu.addMenu(str(cat.get("id") or cat.get("label") or "分类"))
        sub.setStyleSheet(MENU_QSS)
        _fill(sub, actions, anims, on_play)

    for key, names in pools.events.items():
        sub = action_menu.addMenu(EVENT_LABELS.get(key, key))
        sub.setStyleSheet(MENU_QSS)
        _fill(sub, names, anims, on_play)

    if action_menu.isEmpty():
        action_menu.setEnabled(False)

    # ---- 大小 ----
    size_menu = menu.addMenu("大小")
    size_menu.setStyleSheet(MENU_QSS)
    size_group = []
    for label, value in SIZE_PRESETS:
        act = QAction(f"{label}（{value}px）", size_menu)
        act.setCheckable(True)
        act.setChecked(abs(value - size) <= 12)
        act.triggered.connect(lambda _checked=False, v=value: on_size(v))
        size_menu.addAction(act)
        size_group.append(act)

    # ---- 固定原地 ----
    fixed_act = QAction("固定原地（不自己走/转身）", menu)
    fixed_act.setCheckable(True)
    fixed_act.setChecked(fixed)
    fixed_act.triggered.connect(lambda checked: on_fixed(checked))
    menu.addAction(fixed_act)

    menu.addSeparator()

    home_act = QAction("回到初始位置", menu)
    home_act.triggered.connect(lambda _checked=False: on_home())
    menu.addAction(home_act)

    reload_act = QAction("重载配置", menu)
    reload_act.triggered.connect(lambda _checked=False: on_reload())
    menu.addAction(reload_act)

    for act in extra or []:
        act.setParent(menu)
        menu.addAction(act)

    menu.addSeparator()
    quit_act = QAction("退出", menu)
    quit_act.triggered.connect(lambda _checked=False: on_quit())
    menu.addAction(quit_act)

    return menu
