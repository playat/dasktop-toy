# dsh-pet 工作区笔记

PySide6 桌宠（零 Electron / 零 Node），重构自插件 PC2005-cloud/dsh-pet。工作区平铺，环境一份 `.venv/`。
**踩过的坑单独放 `GOTCHAS.md`，动手改代码前先扫一眼。**

## 目录分工（2026-10-26 定档）

- `assets/` = **项目文件**（进 git）：素材的源，生成工具写这里，源码模式读这里。
- `dist/` = **构建产物**（不进 git）：build 前把 assets 镜像过去，删了能重建。平铺：exe + `_internal/` + `config.jsonc` + `assets/`。

代码分层见 `README.md` 的目录表（运行时 / 事件 / 素材工具三层）。

## 命令（根目录）

`setup-pet.cmd` / `run-pet.cmd` / `make-webm.cmd` / `build.cmd`；
`.venv\Scripts\python.exe session.py --all`（会话一览）、`balance.py`（余额 CLI 全参数保留）。
`make_pet_webm.py`：`--src` 默认 `video/src` → 写 `assets/webm`（递归扫 stem 判增量）。

## 运行时要点

- **右键「动作」菜单不读配置**：`AnimationSet.scan()` 用 rglob 扫 `ASSETS/webm`，键=相对路径去后缀；`_add_actions` 递归成子菜单，每次开菜单都 scan，单层 >30 自动分页。`animations.*` 只管自动行为。
- `path_of()`：先精确匹配键，再按 stem 兜底（同名取第一）。
- `main.py:ASSETS = _assets_dir()`：探 `HERE/assets`（有 webm 才算）→ `HERE/dist/assets` 兜底。
- 窗口 = `pets[0].size`（462，16:9）→ 462×260；状态机 idle/action/turn/move/drag/throw/click。
  每只宠物独立解码线程 + 独立 fixedEnabled；菜单「大小」不写回配置（重启回 462）。
- 落地挤压锚点 = **水平中心 + 脚底**；petCollision = `collidePet` 直译。

## 菜单结构

根：`动作`（第一项是**表情包子菜单**，`events.meme_menu()` 注入）/ `大小` / `固定原地` / `回到初始位置` / `重载配置` /
`碎碎念` / `说句话…` / `刷新余额` / `再来一只` / `退出`。
`build_menu(..., extra=, action_extra=)`：extra 平铺根，action_extra 塞「动作」子菜单。**已删 `DSH：未连接…` 与 `会话：…` 两行灰字**。

## 余额（唯一通道：直连，不看 DSH）

- `events.refresh_balance()` 只调 `balance.py`，无回退；DshBridge 的 balance 信号/方法/分发已删；
  `balance_ready = available(cfg)`（只看凭据在不在）。查询在工作线程，`Signal(object)` 回主线程，`_balance_busy` 防重入。
- 读 `$DSH_HOME/.credentials.yaml` 的 `records."deepseek-account-platform/default".payload.token`，
  带 `x-dsh-auth-token` 请求 `{issuer}/api/v0/users/get_user_summary`。**凭据等同密码**：只读不打印。
- 结果包 `{ok, shape:"money", data:{wallets:[{currency,total,fullBalance}]}, text}`；`total`=充值+赠金，
  `fullBalance`=`dsh.balanceFull`（默认 100，0=不折算），复用 `balance_percent → balance_event_index` 六档。
## 会话状态（日志观察）

- `session.py` 第一部分（原 pet_state.py）：读 `$DSH_HOME/sessions/*/session-*/session.*.jsonl*`，
  zstd 多帧**增量**解码 → `idle/thinking/tool/waiting_approval/waiting_answer` + notices。
- 第二部分：`SessionWatcher`（线程+Signal）+ `ReactionPolicy`（纯逻辑 `plan(snapshot)`）。
  **触发规则六条写在文件头，改行为先读那段**：只认 `(会话,status)` 跃迁；`thinking↔tool` 稳 `minHoldMs` 才认；
  首次/换会话只对齐动画不弹气泡；notice 只播一次；催促有上限且忙碌内部一切换就清零。
- 多会话怎么选：glob 按 mtime 取近 72h 前 `maxSessions`（8）个，目录扫描缓存 3s；每个 path 一个 `SessionFold`
  各自记 offset（**只读新增字节**）；primary = 非 idle + 最近事件；Policy 再按 3(等审批/回答)/2(忙)/1(闲)
  挑焦点，**同档粘住当前会话**，只有更高档才切，避免两只抢动画。notice 不分主次，动画按 `_PRIO` 打架（出错>庆祝>等你）。
- `events.on_work_status()` 在 `session_ready` 时直接 return（日志优先，DSH workStatus 让位）。
  窗口 `set_status()/play_effect()`：状态动画播完自动重播，拖拽/飞行/占用时挂起。
- 依赖 `zstandard`（requirements + build `--collect-all zstandard`）。

## 打包

- onedir → `--distpath build/pkg`，再 `flatten()` 搬 exe+`_internal` 到 `dist/` 根；素材/配置**外置**在 exe 同级。
- `sync_assets()` → `_mirror()`：大小+mtime 一致跳过，多余删掉；`--clean` 不清 `dist/assets`；
  老布局 `drop_old_layout()` 由 `_covers()` 确认后才删。
