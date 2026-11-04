# dsh-pet 工作区笔记

PySide6 桌宠（零 Electron / 零 Node），重构自 PC2005-cloud/dsh-pet。工作区平铺，一份 `.venv/`。
**踩坑放 `GOTCHAS.md`，改代码前先扫一眼。**

## 目录分工

- `assets/` = **项目文件**（进 git）：素材源，工具写这里，源码模式读这里。
- `dist/` = **构建产物**（不进 git）：平铺 exe + `_internal/` + `config.jsonc` + `assets/`，删了能重建。

分层见 `README.md`。

## 命令（根目录）

`run-pet.cmd` 是**唯一批处理**：自检 .venv / python 可用 / 四个依赖（PySide6/av/numpy/zstandard）缺哪个报哪个。
其余一律 python：`build.py`（只编译）、`make_pet_webm.py`（`video/src` → `assets/webm`）、`session.py --all`、`balance.py`。

## 素材链抠像（2026-10-28 收紧）

上游 `chroma_step02.py` 是「色相 70~170 + sat≥0.15」，照抄留灰边 + 水印位灰白点。`key_frame` 两处改动：
- `SAT_MIN` **0.15→0.07** 且要求 **`G ≥ max(R,B)+2`**。白/浅灰角色同样低饱和，但绿不是最大通道，不会被啃；
  两条配合才能既吃灰边又不掏白裙子。
- `_strip_watermark()`：豆包左上角另打一串白字，mask 只盖底部。按「亮 val>0.70 + 无彩 sat<0.13 + 角色框外 +
  扁长(宽≥2.5×高、高≤12%) + 面积<2%」逐帧认出，判 alpha=0。
- 上游只做到「清除 99.6%」，后来**全量 PR 手工抠像**，别指望自动方案完美。
- 改判据后要 `--force` 重跑（覆盖同名）。
- ffmpeg：`colorkey`/`chromakey` 用不了（低饱和灰绿 vs 白角色只差 0.154）；`geq` 能 100% 复刻但慢一倍，
  水印填充无等价滤镜。详见 `2026-10-29.md`。

## 运行时要点

- 右键「动作」菜单**不读配置**：`AnimationSet.scan()` 每次开菜单都 rglob 扫 `ASSETS/webm`，键=相对路径去后缀，
  单层 >30 自动分页。`path_of()` 先精确匹配再按 stem 兜底。`ASSETS` 探 `HERE/assets` → `HERE/dist/assets`。
- 窗口 = `pets[0].size`（462）→ 462×260；状态机 idle/action/turn/move/drag/throw/click；
  每只独立解码线程 + fixedEnabled；「大小」不写回配置。落地挤压锚点 = 水平中心 + 脚底。
- **动作之间停顿**（`animGap`）：`_after_oneshot` 走 `_idle_then(kind)` = **先 `_hold_idle()` 播待机循环、
  再 `_schedule_next(kind)` 起 `_gap_timer`**（顺序不能反：`_play()` 会 `_gap_timer.stop()`）。
  间隔 = `random(min,max)` + 动作后 `actionExtraMs` + 偶尔 `longPauseMs`，再 `×(1+loadInfluence×负载)`。
  drag/throw、`_on_gap_elapsed`、`set_external_lock(False)` 走裸 `_schedule_next`；status 那条不加间隔。

## 对话输入框（`saybox.py`）

「说句话…」不再 `QInputDialog`，改成宠物**右侧**的自绘输入条（白底圆角 + 左/右侧小尾巴，投影+描边）。
挂 `window.followers` 跟随（和计分徽章同一套）；回车发送→`chat()`、Esc/失焦收；`events.close_saybox()`
在隐藏/关这只/重载时调用。**必须吃键盘所以不能加 `WindowTransparentForInput`**，开时 `activateWindow+setFocus`；
占位提示自己用浅灰画（样式表会吃掉 `QPalette.PlaceholderText`）。

## 菜单结构

根：`动作`（首项是表情包子菜单）/ 大小 / 固定原地 / **投篮游戏** / 回到初始位置 / 重载配置 / 碎碎念 /
说句话… / 刷新余额 / 再来一只 / 退出。`build_menu(extra=, action_extra=, game_action=, quit_text=)`。
**多开时末项改「关掉这只」** → `main.close_pet(win)`（最后一只才 `quit()`）；
events 的 key 是 `pet_id#N`，关窗口必须 `events.detach(win)`，否则事件打到已删窗口 → RuntimeError。

## 投篮游戏（`basket.py`）

一把梭全在这个文件：`BasketWindow`（筐，可拖/右键菜单/命中波纹）+ `ScorePanel`（筐顶常驻计分板 +
命中飘字，不吃鼠标）+ `BasketGame`（控制器）。判定纯函数在 `physics`：`segment_point_distance` /
`score_for_distance`（u≤perfectRatio 满分，线性到 1 分）。

- 计分板贴**筐顶**（不是宠物头顶）：`BasketWindow.on_move` 回调 → `panel.follow(basket.geometry())`，
  拖筐逐帧跟着走；筐贴屏幕顶时自动翻到筐下面。两行：`本局 N · 累计 M` / `最高 · H 中 / G 局`。

- 接入面：`window` 上的 `on_shot_start / on_throw_step / on_shot_land` 三个钩子 + `game_lock`
  + `followers`（每帧贴位）。**游戏逻辑一律不写进 window.py**，那边只负责"喂坐标"。
- 计分取这一投离筐心的**最近距离**（进圈后继续记 min，出圈或落地才结算），不是入口那一点。
- 成绩记录 `basket-score.json` 落在 `HERE`（exe 同级/工程根），已进 `.gitignore`。
  格式 `{best, total, hits, games}`（最高单局/累计总分/命中数/局数），开局 + 每次命中都写盘；
  `read_record()` 对老格式 `{"best":N}`、坏文件、负值全部容错。`BasketGame.best` 是 `rec.best` 的只读 property。
  **离屏测试要先把 `main.SCORE_FILE` 指到临时路径**，否则读写真文件。
- 命中回位：`_settle()` 里给这只宠物排回位定时器（**不等落地**，撞墙弹跳能耗几秒），回位时
  `win.abort_throw()`（**别调 `_end_shot`**，会重复报一投结束 → 庆祝播两遍）；`state=="drag"` 不抢鼠标。
  配置 returnHome / returnDelayMs；`_on_shot_land` 里该宠物定时器 `isActive()` 就跳过庆祝。
- **多开共用一个筐**（2026-11-04 改）：`BasketGame.wins` 是宠物列表，`anchor` 只是开局那只
  （决定筐放哪边、计分板字号）。`attach(win)` / `detach(win)` 负责接线+上锁；每只一份 `_Shot`
  dataclass（live/scored/celebrate/best_d/home/**自己的 QTimer**），回调一律带 `win` 参数。
  `main.toggle_game` 是**全局开关**（在玩就结束，不管右键哪只）；`close_pet` 走 `detach`，
  一只不剩才 `stop_game()`；`spawn` 时游戏开着就自动 `attach`。分数是整局共用，不按宠物分账。
- 配置段 `basketball`：image / width / mouthRatio / mouthX / mouthY / hitRatio / perfectRatio /
  badgeOffset / celebrateAnim / showRings / returnHome / returnDelayMs。几何全用比例 → 换筐图不用改代码。
  **窗口恒为正方形，边长=width（没有 height 字段）**，图也要求正方形；非方图 `fit_square()` 等比缩进方框居中。
  真图默认 `width=240`、`mouthX=0.615`、`mouthY=0.522`、`mouthRatio=0.47`、`hitRatio=1.3`（R≈73px）。
  **hitRatio 允许 >1**（判定圈比筐口大一圈 = 把宠物体积算进去），解析时上界夹到 3.0。
- **朝向镜像**：图默认板在左、篮口朝右；窗口中心落在屏幕中线右侧就 `_flipped`（`_pix_flip` 预生成），
  判定用 `mouth_rel_x()` = `1-mouthX`。`showEvent`/`moveEvent` 里 `_auto_flip()`（拖动中逐帧）；
  拖动期间 mask 用两向并集 `_mask_drag`，不然 Windows 上中途改区域会断鼠标抓取。
- **跨屏 DPI（2026-11-04 修）**：本机主屏 T3252U @1.5、副屏 @1.25。跨屏时 Qt 会先给窗口一个
  按比例硬算的临时尺寸（**实测 240→346→241**），尺寸一歪 `mouth_center/hit_radius/mask` 全跟着歪
  → 画面碎成块、白边乱飞。三件套修法：①`resizeEvent` 里**钉死** `cfg.width`（`_pinning` 挡重入）；
  ②`_build_pix(dpr)` 按目标屏 DPR 从**原图**重缩（150%→360px、125%→300px），`setDevicePixelRatio`，
  跨屏不再糊；③`_sync_dpr()` 换屏时 `_mask_now=None` + 重 `setMask`（旧区域是按旧 DPR 下发的）。
  mask 永远按**逻辑像素**（240）算，交给 Qt 换算，别自己乘 DPR。
  `ScorePanel._place()` 里也钉 `_want` 尺寸（跨屏临时尺寸会裁字）。
- 收尾不能忘：`close_pet` / `reload` / `quit` / `set_hidden` 都要过 `stop_game()` / `set_visible()`。

## 余额（只走直连，不看 DSH）

- `events.refresh_balance()` 只调 `balance.py`；DshBridge 的 balance 相关已删；
  `balance_ready = available(cfg)`。工作线程 + `Signal` 回主线程，`_balance_busy` 防重入。
- 读 `$DSH_HOME/.credentials.yaml` 的 `deepseek-account-platform/default` token，带 `x-dsh-auth-token`
  请求 `{issuer}/api/v0/users/get_user_summary`。**凭据等同密码：只读不打印**。
- 结果 `{ok, shape:"money", data:{wallets:[{currency,total,fullBalance}]}}`；`fullBalance`=配置值（0=不折算）。

## 会话状态（日志观察）

- `session.py`（原 pet_state.py）：读 `$DSH_HOME/sessions/*/session-*/session.*.jsonl*`，
  zstd 多帧**增量**解码 → idle/thinking/tool/waiting_approval/waiting_answer + notices。
`SessionWatcher`（线程+Signal）+ `ReactionPolicy.plan()`；**六条触发规则在文件头，改行为先读**。
- 多会话：glob 按 mtime 取近 72h 前 8 个（扫描缓存 3s），每 path 一个 `SessionFold` 各记 offset（**只读新增字节**）；
  primary = 非 idle + 最近事件；focus 按 3/2/1 挑且**同档粘住**。notice 不分主次，`_PRIO` 出错>庆祝>等你。

## 打包

- onedir → `--distpath build/pkg`，再 `flatten()` 搬 exe+`_internal` 到 `dist/` 根；素材/配置**外置**。
- `sync_assets()` → `_mirror()`：大小+mtime 一致跳过，多余删掉；`--clean` 不清 `dist/assets`。
