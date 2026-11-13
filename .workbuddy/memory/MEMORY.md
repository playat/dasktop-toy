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

## 提交：本仓库不打 tag（2026-11-13 定）

渔夫明确说过「这个项目不用打 tag」，历史 tag 已全删（本地 + 远程）。以后提交：

```
ci.sh -m "fix: xxx"      # 不带位置参数 → TAGS 为空 → 只 add/commit/pull/push，不打 tag
```

**别**再用 `ci.sh dsh-pet -m "..."` 那种带 tag 名的写法（那是别的项目的习惯）。

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
- **跨屏尺寸钉死**（2026-11-12）：Qt6 换屏按「保持物理尺寸」补偿，但把 DPR 比值**乘了两遍**
  （1.5→1.25 应是 ×1.2，实际 ×1.44）：实测窗口瞬间变 665×374，反方向 321×181，看着"突然变大"。
  `setFixedSize` 挡不住（QWindow 层不走 min/max 约束，只有 QWidget::resize 才 clamp），
  所以在 `resizeEvent` 里以**中心为锚**复位 + `setUpdatesEnabled(False)` 吞掉中间帧，`_want` 记录目标尺寸
  （`apply_size()` 是唯一改尺寸的入口）。筐同理，但它是 240 正方形、按 `_cfg.width` 钉死。
- **动作之间停顿**（`animGap`）：`_after_oneshot` 走 `_idle_then(kind)` = **先 `_hold_idle()` 播待机循环、
  再 `_schedule_next(kind)` 起 `_gap_timer`**（顺序不能反：`_play()` 会 `_gap_timer.stop()`）。
  间隔 = `random(min,max)` + 动作后 `actionExtraMs` + 偶尔 `longPauseMs`，再 `×(1+loadInfluence×负载)`。
  drag/throw、`_on_gap_elapsed`、`set_external_lock(False)` 走裸 `_schedule_next`；status 那条不加间隔。
- **待机卡死**（2026-11-06）：`_play_idle()` 抽到的常是**正在 loop 播的那条待机**（`待机/` 只有 1 个素材），
  `player.play()` 对「同名+未 finished」返回 False，旧码不看返回值 → 不排期 → 永远待机。
  现在 `_play_idle` 失败时改走 `_idle_then("idle")` 重排；`_play_action/_play_turn` 失败也补排。
  另有 `_idle_guard`（3s 单次）兜底：`_hold_idle()` 末尾启动、`_schedule_next()`/`_play()`/`_start_move()`
  一有正经动作就 stop，只在「state==idle 且 gap 没排上」时补排一次。

## 鼠标碰撞（`physics.mouseCollision`）

Qt 只把落在窗口上的鼠标事件送进来，窗口外扫过来收不到 → 每只宠物一个 16ms `QTimer` 轮询
`QCursor.pos()` 自己算速度（实测主屏 1.5 / 副屏 1.25 下光标与窗口几何是**同一套逻辑坐标**，偏差 0）。

- 判据全在 `physics.cursor_push(body, cursor, v, min_speed=)`（纯函数）：鼠标当半径 10 的小球。
  **只认速度门槛**（`physics.mouseMinSpeed`，默认 800px/s）：慢慢移过去不弹（那是想点它/右键它），
  够快才弹，力度 `clamp(speed×1.25, 260, 2800)`。**没有「进入沿」判据**——手贴在身上再猛甩照样算撞。
  方向 = 0.7 扫向 + 0.3 离心 + 0.45 上抬。位置命中用 `body_box`（不是窗口矩形，透明边不算）。
  （历史：先按 200px/s 门槛做 → 用户说慢速也该弹 → 改成进入沿触发 → 用户又说慢慢移也弹，
  2026-11-13 定稿成「速度门槛 + 可配」，别再来回改。）
- 不参与的情况：`state in ("drag","throw")`、`QGuiApplication.mouseButtons() != NoButton`
  （点击/拖动都是主动操作，不能抢）、不可见、同一只 300ms 冷却。
  **`game_lock` 不排除**（2026-11-13 改）：球局里照样能撞飞，撞进筐同样计分。
- `_start_throw(vx, vy)`：**没有 shot 参数了**（2026-11-13 删）。甩出 / 鼠标撞飞 / 宠物互撞
  一律算「一投」，都调 `on_shot_start`；落地一律 `_end_shot`。`_throw_shot` 字段已废弃删除。
  （历史：原本鼠标撞飞 `shot=False` 不计分，渔夫要求撞进筐也要计分才统一。）
- 运行期开关：`window.set_mouse_collision(on)` 起停 timer；菜单勾选走 `PetApp.set_mouse_collision`
 （**全局**，只改内存 `cfg.physics.mouse_collision`，不写文件）；`reload_config` 按文件值复位。

## 对话输入框（`saybox.py`）

「说句话…」不再 `QInputDialog`，改成宠物**右侧**的自绘输入条（白底圆角 + 左/右侧小尾巴，投影+描边）。
挂 `window.followers` 跟随（和计分徽章同一套）；回车发送→`chat()`、Esc/失焦收；`events.close_saybox()`
在隐藏/关这只/重载时调用。**必须吃键盘所以不能加 `WindowTransparentForInput`**，开时 `activateWindow+setFocus`；
占位提示自己用浅灰画（样式表会吃掉 `QPalette.PlaceholderText`）。

## 菜单结构

根：`动作`（首项是表情包子菜单）/ 大小 / 固定原地 / **投篮游戏** / 回到初始位置 / 重载配置 / 碎碎念 /
说句话… / 刷新余额 / 再来一只 / 退出。
`build_menu(extra=, action_extra=, game_action=, quit_text=, on_quit_all=, quit_all_text=)`。
**多开时倒数第二项「关掉这只」** → `main.close_pet(win)`（最后一只才 `quit()`），
**最后一项「退出（关掉全部 N 只）」→ `main.quit()`**（2026-11-12 加：任何一只的菜单都能一键全关）；
单只时只留「退出」，`quit_all_text` 传空串就不出这一项。
events 的 key 是 `pet_id#N`，关窗口必须 `events.detach(win)`，否则事件打到已删窗口 → RuntimeError。

## 投篮游戏（`basket.py`）

一把梭全在这个文件：`BasketWindow`（筐，可拖/右键菜单/命中波纹）+ `ScorePanel`（筐顶常驻计分板 +
命中飘字，不吃鼠标）+ `BasketGame`（控制器）。判定纯函数在 `physics`：`segment_point_distance` /
`score_for_distance`（u≤perfectRatio 满分，线性到 1 分）。

- 计分板贴**筐顶**（不是宠物头顶）：`BasketWindow.on_move` 回调 → `panel.follow(basket.geometry())`，
  拖筐逐帧跟着走；筐贴屏幕顶时自动翻到筐下面。两行：`本局 N · 累计 M` / `最高 · H 中 / G 局`。

- 接入面：`window` 上的 `on_shot_start / on_throw_step / on_shot_land` 三个钩子 + `game_lock`
  + `followers`（每帧贴位）。**游戏逻辑一律不写进 window.py**，那边只负责"喂坐标"。
- **`game_lock` 只禁「走位/转身」**（2026-11-11 改）：`_roll_next` 里 `stay = fixed or game_lock`
  把 turn/move 权重归零，**原地小动作照常**。旧码是 `_hold_idle(); return`，整局只播待机 →
  玩家以为程序卡死（"一直循环待机"最像这个，先问一句是不是还开着游戏）。解锁/上锁都补一次 `_schedule_next`。
- 筐拖出所有屏幕后，`mouseReleaseEvent` 调 `_clamp_to_screens()` 推回最近屏留 48px（只在松手时校正，
  拖动中不动，否则跟鼠标错位）。看不见的筐点不到右键 = 关不掉的游戏。
- 计分取这一投离筐心的**最近距离**（进圈后继续记 min，出圈或落地才结算），不是入口那一点。
- 成绩记录 `basket-score.json` 落在 `HERE`（exe 同级/工程根），已进 `.gitignore`。
  格式 `{best, total, hits, games}`（最高单局/累计总分/命中数/局数），开局 + 每次命中都写盘；
  `read_record()` 对老格式 `{"best":N}`、坏文件、负值全部容错。`BasketGame.best` 是 `rec.best` 的只读 property。
  **离屏测试要先把 `main.SCORE_FILE` 指到临时路径**，否则读写真文件。
- 命中回位：`_settle()` 里给这只宠物排回位定时器（**不等落地**，撞墙弹跳能耗几秒），回位时
  `win.abort_throw()`（**别调 `_end_shot`**，会重复报一投结束 → 庆祝播两遍）；`state=="drag"` 不抢鼠标。
  配置 returnHome / returnDelayMs；`_on_shot_land` 里该宠物定时器 `isActive()` 就跳过庆祝。
- **庆祝只播在命中的那只**：`_celebrate_now(win)` 读 `_shots[win]`，多开不串台（探针实测非开局那只也播）。
  它正被拎/在飞时 `play_effect` 直接 return（**不是排队**），所以这里加了退避重试
  `CELEBRATE_RETRY_MS=180 × CELEBRATE_RETRY_MAX=12`（`_Shot.retry/tries`），`_go_home` 遇到
  `state=="drag"` 也不许再清 `celebrate` 标志（以前清了就永久丢）。`stop()`/`detach()` 要连 retry 一起停。
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
- **代理**（2026-11-12）：`platform.deepseek.com` **国内直连就通**（解析到国内 WAF，443 秒连），
  所以 `dsh.balanceProxy` **默认 `direct`**，完全无视系统代理。绕代理反而出问题：
  `urllib` 默认跟随系统代理（环境变量 > 注册表 `Internet Settings`），代理软件退出后系统代理常残留
  `127.0.0.1:7890`，端口没人听 → WinError 10061「积极拒绝」，看着像 DeepSeek 挂了。
  可选 `auto`（跟随系统 + 回环代理 10061 时自动直连重试）/ 具体 URL。
- **坑**：重试必须 `_build_request()` 新建 Request —— 走代理时 `Request.set_proxy()` 会改写 host，
  复用同一个对象重试还是连那个死代理（改了 direct 依然 10061）。
- 实测（`env https_proxy=http://127.0.0.1:1`）：默认 direct 直接打穿拿到 40003；
  真实凭据下报 `充值 ¥0 赠金 ¥0`；auto 打一行「改直连重试」后也通；活代理下不误降级。

## 会话状态（日志观察）

- `session.py`（原 pet_state.py）：读 `$DSH_HOME/sessions/*/session-*/session.*.jsonl*`，
  zstd 多帧**增量**解码 → idle/thinking/tool/waiting_approval/waiting_answer + notices。
`SessionWatcher`（线程+Signal）+ `ReactionPolicy.plan()`；**六条触发规则在文件头，改行为先读**。
- 多会话：glob 按 mtime 取近 72h 前 8 个（扫描缓存 3s），每 path 一个 `SessionFold` 各记 offset（**只读新增字节**）；
  primary = 非 idle + 最近事件；focus 按 3/2/1 挑且**同档粘住**。notice 不分主次，`_PRIO` 出错>庆祝>等你。

## 打包

- onedir → `--distpath build/pkg`，再 `flatten()` 搬 exe+`_internal` 到 `dist/` 根；素材/配置**外置**。
- `sync_assets()` → `_mirror()`：大小+mtime 一致跳过，多余删掉；`--clean` 不清 `dist/assets`。
