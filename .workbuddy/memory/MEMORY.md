# dsh-pet 工作区笔记

PySide6 桌宠（零 Electron / 零 Node），重构自插件 PC2005-cloud/dsh-pet。工作区平铺，环境一份 `.venv/`。
**踩过的坑单独放 `GOTCHAS.md`，动手改代码前先扫一眼。**

## 目录分工

- `assets/` = **项目文件**（进 git）：素材的源，生成工具写这里，源码模式读这里。
- `dist/` = **构建产物**（不进 git）：平铺 exe + `_internal/` + `config.jsonc` + `assets/`，删了能重建。

分层见 `README.md` 目录表。

## 命令（根目录）

`run-pet.cmd` 是**唯一批处理**：依次自检 .venv 是否存在 → venv 里 python 能否跑 → 四个依赖
（PySide6/av/numpy/zstandard）缺哪个，缺就打印补装命令退出。其余**一律 python 命令**：
`build.py`（只编译，不自动装 PyInstaller）、`make_pet_webm.py`、`session.py --all`、`balance.py`。
`setup-pet.cmd` / `make-webm.cmd` / `build.cmd` 已删。`make_pet_webm.py` 默认 `video/src` → `assets/webm`。

## 素材链抠像（2026-10-28 收紧）

上游 `chroma_step02.py` 是「色相 70~170 + sat≥0.15」，照抄会留一圈灰边 + 水印位灰白点。`key_frame` 两处改动：
- `SAT_MIN` **0.15→0.07** 且要求 **`G ≥ max(R,B)+2`**。白/浅灰角色同样低饱和，但绿不是最大通道，不会被啃；
  两条配合才能既吃灰边又不掏白裙子。
- `_strip_watermark()`：豆包在左上角另打一串白字，mask 只盖了底部。按「亮 val>0.70 + 无彩 sat<0.13 +
  角色外接框外 + 单块扁长(宽≥2.5×高、高≤12%画面) + 面积<2%」逐帧认出，直接判 alpha=0。
- 上游只做到「清除 99.6%」，后来**全量改用 PR 手工抠像**，别指望自动方案完美。
- 改判据后要 `--force` 重跑才生效（覆盖 `assets/webm` 同名文件）。

## 运行时要点

- 右键「动作」菜单**不读配置**：`AnimationSet.scan()` 每次开菜单都 rglob 扫 `ASSETS/webm`，键=相对路径去后缀，
  单层 >30 自动分页。`path_of()` 先精确匹配再按 stem 兜底。`ASSETS` 探 `HERE/assets` → `HERE/dist/assets`。
- 窗口 = `pets[0].size`（462）→ 462×260；状态机 idle/action/turn/move/drag/throw/click；
  每只独立解码线程 + fixedEnabled；「大小」不写回配置。落地挤压锚点 = 水平中心 + 脚底。
- **动作之间停顿**（`animGap`）：`_after_oneshot` 走 `_idle_then(kind)` = **先 `_hold_idle()` 播待机循环、
  再 `_schedule_next(kind)` 起 `_gap_timer`**（顺序不能反：`_play()` 会 `_gap_timer.stop()`）。
  间隔 = `random(min,max)` + 动作后 `actionExtraMs` + 偶尔 `longPauseMs`，再 `×(1+loadInfluence×负载)`。
  drag/throw 分支与 `_on_gap_elapsed`/`set_external_lock(False)` 仍走裸 `_schedule_next`；status 那条不加间隔。

## 菜单结构

根：`动作`（首项是表情包子菜单，`events.meme_menu()`）/ 大小 / 固定原地 / 回到初始位置 / 重载配置 /
碎碎念 / 说句话… / 刷新余额 / 再来一只 / 退出。`build_menu(extra=, action_extra=)`。

## 余额（只走直连，不看 DSH）

- `events.refresh_balance()` 只调 `balance.py`；DshBridge 的 balance 信号/方法/分发已删；
  `balance_ready = available(cfg)`。工作线程 + `Signal` 回主线程，`_balance_busy` 防重入。
- 读 `$DSH_HOME/.credentials.yaml` 的 `deepseek-account-platform/default` token，带 `x-dsh-auth-token`
  请求 `{issuer}/api/v0/users/get_user_summary`。**凭据等同密码：只读不打印**。
- 结果 `{ok, shape:"money", data:{wallets:[{currency,total,fullBalance}]}}`；
  `fullBalance`=`dsh.balanceFull`（默认 100，0=不折算），复用六档表情。

## 会话状态（日志观察）

- `session.py` 第一部分（原 pet_state.py）：读 `$DSH_HOME/sessions/*/session-*/session.*.jsonl*`，
  zstd 多帧**增量**解码 → idle/thinking/tool/waiting_approval/waiting_answer + notices。
`SessionWatcher`（线程+Signal）+ `ReactionPolicy.plan()`；**六条触发规则在文件头，改行为先读**。
- 多会话：glob 按 mtime 取近 72h 前 8 个（目录扫描缓存 3s），每个 path 一个 `SessionFold` 各记 offset
  （**只读新增字节**）；primary = 非 idle + 最近事件；focus 按 3/2/1 挑且**同档粘住**，避免两只抢动画。
  notice 不分主次，`_PRIO` 出错>庆祝>等你。依赖 `zstandard`（build 要 `--collect-all zstandard`）。

## 打包

- onedir → `--distpath build/pkg`，再 `flatten()` 搬 exe+`_internal` 到 `dist/` 根；素材/配置**外置**。
- `sync_assets()` → `_mirror()`：大小+mtime 一致跳过，多余删掉；`--clean` 不清 `dist/assets`。
