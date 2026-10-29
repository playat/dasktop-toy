"""右键菜单（M3）：原生 QMenu。

动作列表**完全由 `assets/webm` 的目录结构决定**，既不看配置里的池/分类，也不按文件名猜语义：

    动作 ┬ 说明：xxx.md 的内容     ← 根目录放了说明文件时显示（不可点）
         ├ 动画A                    ← 根目录直接放的素材，平铺
         ├ 小动作 ┬ 说明：…        ← 子目录 = 子菜单，目录里的 .md 变成说明条目
         │        ├ 悠闲哼歌
         │        └ 深层 ─ 超大伸懒腰（任意深度）
         └ 情绪 ─ 生气

也就是说：想怎么分类就怎么建目录。往 `assets/webm` 丢文件、建子目录，右键菜单立刻跟着变，
不用改配置。文件名 = 菜单条目；播放键是「子目录/名字」，同名文件放在不同目录也能区分。

**特殊分类的告知文件**：在目录里放一个 `.md`（推荐命名 `说明.md`，也认 `README.md` / 任意 `.md`、`.txt`），
它的第一行当标题、全文当悬停提示，显示成该子菜单顶部一条灰色不可点条目。
`.md` 不会被当成素材，也不影响播放。

配置里的 `animations` 池子仍然管**自动行为**（待机播什么、点一下播什么、随机走位），
但跟菜单内容彻底无关了。

    动作 ┬ 表情包 ┬ 随机一张 / 各张图    ← events.EventDirector 注入，没有图就不出现
         └ 动画目录…
    大小 ┬ 大 / 中 / 小 / 迷你
    固定原地（勾选）
    投篮游戏                              ← main 注入（`game_action`），开局后变「结束投篮游戏（N 分）」
    回到初始位置
    重载配置
    碎碎念 / 说句话… / 刷新余额          ← events.EventDirector 注入（`extra`）
    退出                                 ← 开了多只时改叫「关掉这只」（`quit_text`）

「动作」里的注入项走 `action_extra`（可以是 QAction，也可以是整块 QMenu）。

点播不受 fixedEnabled 影响——这是用户主动触发，和 Electron 版语义一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QWidget

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

SIZE_PRESETS: list[tuple[str, int]] = [
    ("大", 540),
    ("中", 462),
    ("小", 380),
    ("迷你", 320),
]


# 单层最多摆这么多条，超了就按序号分页（目录里堆上百个文件时不至于滚到怀疑人生）
MAX_ITEMS_PER_MENU = 30

NOTE_LABEL_MAX = 30  # 说明标题太长会撑爆菜单宽度


def _submenu(parent: QMenu, title: str) -> QMenu:
    """建子菜单：统一外观 + 打开悬停提示（子目录的说明靠它露全文）。"""
    sub = parent.addMenu(title)
    sub.setStyleSheet(MENU_QSS)
    sub.setToolTipsVisible(True)
    return sub


def _note_action(menu: QMenu, note: tuple[str, str]) -> None:
    """把目录里的 .md 显示成一条灰色不可点条目（标题外露，全文进悬停提示）。"""
    title, body = note
    label = title if len(title) <= NOTE_LABEL_MAX else title[: NOTE_LABEL_MAX - 1] + "…"
    act = QAction(f"说明 · {label}", menu)
    act.setEnabled(False)
    act.setToolTip(body)
    menu.addAction(act)


def _file_actions(
    menu: QMenu, files: list[tuple[str, str, Path]], on_play: Callable[[str], None]
) -> int:
    """一层的文件条目。files: [(显示名, 播放键, 路径)]，返回条数。"""
    if len(files) <= MAX_ITEMS_PER_MENU:
        for label, key, _path in files:
            act = QAction(label, menu)
            act.triggered.connect(lambda _checked=False, k=key: on_play(k))
            menu.addAction(act)
        return len(files)

    count = 0
    for start in range(0, len(files), MAX_ITEMS_PER_MENU):
        chunk = files[start : start + MAX_ITEMS_PER_MENU]
        count += _file_actions(_submenu(menu, f"{start + 1} - {start + len(chunk)}"), chunk, on_play)
    return count


def _add_actions(menu: QMenu, node: dict, on_play: Callable[[str], None]) -> int:
    """递归铺菜单：本层说明 → 子目录（子菜单）→ 本层文件；返回条目总数。

    不读任何配置：目录结构就是唯一的分类依据，根目录的文件直接平铺。
    """
    count = 0

    note = node.get("note")
    if note:
        _note_action(menu, note)
        count += 1

    for name in sorted(node["dirs"]):
        child = node["dirs"][name]
        sub = _submenu(menu, name)
        got = _add_actions(sub, child, on_play)
        if got:
            count += got
            if child.get("note"):
                sub.menuAction().setToolTip(child["note"][1])
        else:
            sub.menuAction().setVisible(False)  # 没有 webm 也没有说明的空目录不占位置

    count += _file_actions(menu, node["files"], on_play)
    return count


def build_menu(
    parent: QWidget | None,
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
    quit_text: str = "退出",
    extra: list[QAction] | None = None,
    action_extra: list[QAction | QMenu] | None = None,
    game_action: QAction | None = None,
) -> QMenu:
    menu = QMenu(parent)
    menu.setStyleSheet(MENU_QSS)
    menu.setToolTipsVisible(True)

    # ---- 动作（每次开菜单都重扫 assets/webm，目录即分类）----
    anims.scan()
    action_menu = _submenu(menu, "动作")
    # 别的模块注入到「动作」里的条目（目前是表情包子菜单），排在动画目录前面
    for item in action_extra or []:
        if isinstance(item, QMenu):
            action_menu.addMenu(item)
        else:
            action_menu.addAction(item)
    if action_extra:
        action_menu.addSeparator()

    if _add_actions(action_menu, anims.tree(), on_play) == 0 and not action_extra:
        hint = QAction("（assets/webm 里没有素材）", action_menu)
        hint.setEnabled(False)
        action_menu.addAction(hint)

    # ---- 大小 ----
    size_menu = _submenu(menu, "大小")
    for label, value in SIZE_PRESETS:
        act = QAction(f"{label}（{value}px）", size_menu)
        act.setCheckable(True)
        act.setChecked(abs(value - size) <= 12)
        act.triggered.connect(lambda _checked=False, v=value: on_size(v))
        size_menu.addAction(act)

    # ---- 固定原地 ----
    fixed_act = QAction("固定原地（不自己走/转身）", menu)
    fixed_act.setCheckable(True)
    fixed_act.setChecked(fixed)
    fixed_act.triggered.connect(lambda checked: on_fixed(checked))
    menu.addAction(fixed_act)

    # ---- 投篮游戏（开局 / 结束同一项，文案由调用方决定，带当前分数）----
    if game_action is not None:
        game_action.setParent(menu)
        menu.addAction(game_action)

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
    quit_act = QAction(quit_text, menu)
    quit_act.triggered.connect(lambda _checked=False: on_quit())
    menu.addAction(quit_act)

    return menu
