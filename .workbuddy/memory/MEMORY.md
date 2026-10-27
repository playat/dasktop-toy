# dsh-pet 工作区笔记

## 是什么（2026-10-23 两次重构后）

**一个平铺的工作区**（没有 `pet_py/` 这层了，用户要求环境统一在根）：
1. **`make_pet_webm.py`** —— AI 绿幕视频 → 透明 webm 的素材工具（单遍管线，不需要 ffmpeg.exe）
2. **根目录那 8 个 .py** —— PySide6 桌宠运行时（零 Electron / 零 Node）

上游 Electron 插件（原 `dsh-pet/`，292MB）、`scripts/`、`tools/`、`docs/` 等**已按用户要求整体删除**。
⚠️ 2026-10-23 用户自己 `git init` 并提交过；2026-10-26 起 `assets/` 已被 git 跟踪（124 个 webm 在版本里）。

**两个目录的分工（2026-10-26 用户明确定档，别再搞混）**：

- `assets/` = **项目文件**（进 git）：素材的源，生成工具写这里，源码模式运行时读这里。
- `dist/` = **构建产物**（不进 git）：`build.py` 打包前把 `assets/` 整个镜像过去，
  所以「改了代码/素材 + 跑一次 build」就得到一版可移植的 dist，删了也能重建。

```
dsh-pet/
├── .venv/                  工作区统一环境（运行时 + 素材工具共用，唯一一份）
├── requirements.txt
├── setup-pet.cmd / run-pet.cmd
├── main.py window.py player.py video.py config.py physics.py menu.py tray.py
├── config.jsonc
├── assets/                 ★ 项目文件：素材的源（webm 108 / memes 27 / pic 8 / sound / fonts）
│   └── webm/               生成工具写这里
├── dist/                   ★ 构建产物，平铺（没有 DshPet/ 这层），可整包搬走
│   ├── DshPet.exe  _internal/  config.jsonc
│   └── assets/             按根 assets/ 镜像出来的一份
├── make_pet_webm.py / make-webm.cmd
├── build.py / build.cmd
├── prompts/
├── video/        watermark_mask_v5.mkv + src/（原始 mp4）
└── .workbuddy/    项目记忆
```

## 命令（全部在根目录）

```bat
setup-pet.cmd                                  :: 建 .venv（腾讯云源，优先 PATH 的 python）
run-pet.cmd                                    :: 启动
make-webm.cmd                                  :: 双击 = 处理 video/src，增量，已生成的跳过
.venv\Scripts\python.exe make_pet_webm.py            :: 等价的命令行写法
```


`--src` 不传 → 默认 `video/src`；**成品写工程根 `assets/webm`**（项目文件、进 git）。
增量判断递归扫 `assets/webm` 整棵树（stem 匹配），命中子目录里的成品也算已生成，
重做时原地覆盖、不在根目录造重复。
`dist/assets` 已存在时（打过包）生成完顺手 `copy2` 同步一份过去（`sync_to_dist()`，
`--no-sync` 可关），这样开着的打包版宠物也能立刻看到新动作——但 dist 只是产物，
真正对齐靠 `build.py` 的镜像。
`make_pet_webm.py` 的 `ROOT = Path(__file__).parent`（在根目录，别再加 parent）。
输出几何：640×360、站立 266.7px、脚底 y=330（= 运行时 `FEET_Y_RATIO=330/360`）。

## 不可动摇的实测事实

- **PyAV 必须显式 `av.CodecContext.create('libvpx-vp9','r')` + `pix_fmt='yuva420p'` + 手动 extradata**。
  用 `container.decode()` 会得到 yuv420p、alpha 全 255 的假结果（看着像素材没 alpha，其实是解码器错）。
- **背景 alpha 是 1~15 噪声不是 0**：82% 像素落 1~15。解码后必须 `alpha[alpha<16]=0`，
  否则整窗挡住鼠标（Windows 分层窗口按 per-pixel alpha 命中）。
- 点击穿透**不需要 setMask**：分层窗口天然按 alpha 让位，比原版固定矩形 HIT_BOX 更贴形。
- **窗口定位只用 Qt 逻辑像素**（`availableGeometry()` + `move()`）。混用 Win32 物理像素在 150% 下会推屏外
  （win 存在、visible=1，人眼看不见）。主屏 2560×1466 逻辑 @1.5x。
- **Ctrl+C 必须配空转 QTimer(200ms)**：Qt 循环不跑字节码，光 `signal.signal(SIGINT)` 没反应。
- **右键菜单不能在 mousePress 里 exec()**（撞鼠标 grab 会瞬间关闭）：press 记坐标，release 里
  `QTimer.singleShot(0, popup)`。
- **托盘图标必须 `alpha[alpha<16]=0` 再算 bbox**，否则 1~15 噪声让裁切框退化成全图。
- 多屏：甩飞/走位前 `screenAt()` 同步 bounds，否则副屏宠物被主屏 edge 钳回来。

### 验证陷阱

- **`.cmd` 里绝不能出现中文**（哪怕只是 `rem` 注释）：cmd.exe 按 cp936 解析 UTF-8 文件，
  中文行会被当命令执行、后面的参数也会被拆坏（实测报 `'㈢殑鍙傛暟缁х画閫忎紶' 不是内部或外部命令`）。
  `chcp 65001` 救不了（解析早于执行）。→ 批处理一律纯 ASCII，中文提示交给 Python 打印。
- **venv 不限制任何盘符访问**，C 盘目录报"不存在"是路径字符串问题（多余引号 / 末尾反斜杠），
  不要往"权限"上猜。
- **测试素材别用真实动画名**：曾用 `悠闲哼歌` 当测试源，`--deploy` 把 584KB 原素材覆盖成 3KB 再被删掉。
  只能用 `_sync_probe` 这类不可能撞名的名字。现在素材在 `assets/` 且已进 git，
  误删可以 `git checkout -- assets/webm/` 找回（`dist/assets` 是镜像出来的，别当备份）。
- **本机 `Path.rmdir()` 在 `F:` 盘上是「递归删除」**：实测对非空目录也不报错、直接把里面的文件删光
  （`os.rmdir` 正常情况下该抛 WinError 145）。踩过一次：镜像后清理空目录那一步把刚落好的
  162 个素材全删了，返回值还是 0。→ **任何 rmdir 前先 `if any(d.iterdir()): continue`**；
  同理别用 `rmdir` 当「清空目录」用，要删就 `shutil.rmtree`（它本来就是递归的，语义明确）。
- **`PIL.ImageGrab` 抓不到 `WS_EX_LAYERED` 窗口**。曾据此误判"没显示"，实际用户看得清楚。
  验证透明窗口只能靠 `widget.grab()` 或让用户看。
- pip 走本机代理极慢，**装大包一律腾讯云源**（24~36MB/s）。
- **venv 绝不能 mv 搬**：`activate` / `activate.bat` 里的 `VIRTUAL_ENV` 和 `Scripts/*.exe` launcher
  都内嵌绝对路径，搬完全废（pip.exe 静默 rc=1）。搬过就删了重建。
  `python.exe` 本身不受影响（靠 pyvenv.cfg 的 home），所以「能跑但进不了环境」是这个症状。
- 非 tty 下 Python stdout 块缓冲，调试要 `-u` 或 `flush=True`。

## 运行时要点

- **右键「动作」菜单不读配置**：`AnimationSet.scan()` 用 `rglob` 递归扫 `ASSETS/webm`，
  键 = 相对路径去后缀（`小动作/悠闲哼歌`）；`menu.py:_add_actions` 递归把目录变子菜单。
  **只有根层**再用配置的池/分类分组（配置没有的进「未分类」），单层 >30 条自动分页。
  `build_menu()` 每次调用都 `scan()`，丢文件进去不用重启也不用重载。
- `config.jsonc` 的 `animations.*` 现在只决定**自动行为**（自动待机/点击/走位/随机动作），不决定菜单内容。
- `path_of()` 先精确匹配键、再按 stem 兜底 → 配置里写名字、素材挪进子目录也能找到（同名取第一个）。
- `main.py`：`ASSETS = _assets_dir()` —— 依次探 `HERE/assets`（有 webm 才算）→ `HERE/dist/assets`（兜底），
  两边都用同一句判断：源码模式命中根 `assets/`，打包后 `HERE=dist/` 就命中 `dist/assets/`。
- 窗口尺寸 = `pets[0].size`（462，16:9），窗口 462×260。
- 状态机 idle/action/turn/move/drag/throw/click，权重 idle10/turn5/move5 + 分类加权。
- 松手：轨迹 <500px/s 死区 → 原地站住；超过才抛掷/反弹/落地挤压。
- 落地挤压锚点是**水平中心 + 脚底**（不是左边缘），`SQ_MAX_SQUASH=0.72`、宽度耦合 0.12。
- petCollision 已实现（`collidePet` 直译，等质量 e≈1 交换动量），配置默认 true。
- 每只宠物独立解码线程 + 独立 `fixedEnabled`。
- 菜单「大小」不写回配置（重启回 462）。
- `config.jsonc` 是从原插件整份拷来的，含大量 DSH 时代注释，只有部分字段 pet_py 会读。

## 余额（唯一通道：直连，2026-10-27 用户定档）

- **不看 DSH 是否启动**（用户 2026-10-27 明确要求）：`events.refresh_balance()` 只调
  `balance.py` 直连，没有回退分支；`DshBridge` 的 `balance` 信号 /
  `refresh_balance()` / `elif key == "balance"` 分发**已删**，`state` 的 balance 叶子不订阅。
- `EventDirector.balance_ready` = `balance_direct.available(cfg)`（只看凭据文件在不在）。
- 配置里**没有** `balanceDirect` 了（只有 `events.balanceEnabled` + 凭据在不在两个开关）。
- 直连做法：读 `$DSH_HOME/.credentials.yaml` 里
  `records."deepseek-account-platform/default".payload.token`，带 `x-dsh-auth-token` 请求
  `{issuer}/api/v0/users/get_user_summary`（**2026-10-27 已把 `balance_watch.py` 整个并入
  `balance.py`**，不再有第二个文件；`python balance.py` 就是原来的命令行，`--watch` / `--json` / `--threshold` / `--token` / `--home` / `--issuer` / `--timeout` 全保留）。
- `balance.py` 把结果包成 `{ok, shape:"money", data:{wallets:[{currency,total,fullBalance}]}, text}`，
  `total` = 充值+赠金，`fullBalance` = 配置 `dsh.balanceFull`（默认 100，0 = 不折算）；
  这样能直接复用现成的 `balance_percent` → `balance_event_index` 六档。
- 配置：`dsh.balanceFull / balanceHome / balanceTimeout`（仍在 dsh 段，只是取数不走 dsh）。
- **凭据等同密码**：只读 token 用于请求，不打印不进日志。token 失效是 HTTP 401 或 `code=40003`。
- 直连查询在工作线程跑，结果靠 `_Signals.balance`（`Signal(object)`）回主线程；`_balance_busy` 防重入。

## 会话状态（日志观察，2026-10-27）

- **读日志引擎现在在 `session.py` 第一部分**（原 `pet_state.py` 于 2026-10-27 并入）：读 `$DSH_HOME/sessions/*/session-*/session.*.jsonl*`，
  zstd 多帧增量解码 → `idle/thinking/tool/waiting_approval/waiting_answer` + notices
  （完成/出错/审批/后台任务/交付）。仍能单独跑：`python session.py`（一行）/`--json`/`--all`/`--watch 2`。
- ⚠ **解码坑**：dsh 写的是**流式帧**（帧头没有 content size），`zstandard.decompress()`
  直接报 `could not determine content size in frame header`，`_decode_frames` 把它吞掉 →
  **0 个事件**（看着像「日志是空的」）。必须
  `ZstdDecompressor().decompress(blob, max_output_size=64<<20)`。已改成三级降级。
- `first_read_limit=4MB` 会跳过文件头 → 大日志的 `session_id/cwd` 可能为空，
  `session._sid_of()` 用 `path` 兜底。
- **触发决策在 `session.py` 第二部分**：`SessionWatcher`（线程 + `Signal(object)`）+
  `ReactionPolicy`（纯逻辑，`plan(snapshot)` 出 `("hold"|"play"|"say", …)`）。
  **触发规则六条写在文件头**，要改行为先读那段。
- 触发要点：只认 `(会话, status)` 跃迁；`thinking↔tool` 要稳 `minHoldMs` 才认；
  首次/换会话只对齐动画**不弹气泡**；notice 天然只播一次；催促有上限，计时从
  「我们确认状态」起（忙碌内部一切换就清零 = 在动就别催）。
- `events.on_work_status()` 在 `session_ready` 时直接 return —— 日志观察优先，
  DSH 的 workStatus 让位（两个来源会抢同一个动画）。
- 窗口层 `PetWindow.set_status()/_apply_status()/play_effect()`：状态动画播完**自动重播**，
  拖拽/飞行/被占用时挂起，`_roll_next()` 里补。
- 依赖 `zstandard`（requirements.txt + build 的 `--collect-all zstandard`）。

## 打包（build.py / build.cmd）

- PyInstaller onedir → `dist/`，**平铺，没有 `dist/DshPet/` 这层**（2026-10-26 改的用户要求）。
  做法：`--distpath build/pkg`，构建完 `flatten()` 把 exe + `_internal` 搬到 `dist/` 根，再删 `build/pkg`。
  `--onefile` 同样落 `dist/`。
- **素材/配置外置**在 exe 同级，不走 `--add-data`。`main.py:_app_dir()`：
  frozen 时 `Path(sys.executable).parent`，`HERE`/`CONFIG_FILE`/`ASSETS` 全部据此解析。
- **素材是「镜像」进 dist 的**（2026-10-26 用户定档：`assets/` 是项目文件、`dist/` 是产物）。
  `sync_assets()` → `_mirror(SRC_ASSETS, dist/assets)`：缺的/变了的 `copy2` 过去
  （大小 + mtime 一致就跳过，所以只改代码时不会重拷 60MB），`dist/assets` 里多出来的删掉。
  `--clean` 不清 `dist/assets`（下一步会被重建，删了白拷一遍）；要干净就删整个 `dist/`。
- **老布局 `dist/DshPet/`** 由 `drop_old_layout()` 收尾：里面的素材在根 `assets/` 里都有
  （`_covers()` 逐个相对路径比对）就整层删，有缺的就保留 + 提示，不替人删。
- **桌宠开着时打包会被占用**（exe 在 `dist/` 里跑，`_internal` 和 assets 都被锁）：`_busy_hint()` 抓 OSError 给人话，
  提示「先从托盘退出」。
- **`--windowed` 时 `sys.stdout is None`** → 满屏 `print` 会炸；main.py 顶部重定向到 `dsh-pet.log`。
- **别给 PyInstaller 传 `--clean`**：它清 `%LOCALAPPDATA%\pyinstaller` 上百个文件，
  在受限环境触发批量删除拦截，构建直接失败。清构建件用 `build.py --clean`。
- 必须 `--collect-all av`（否则 libvpx dll 缺失）；图标从素材首帧抠，必须先 `alpha[alpha<16]=0`。
- **`shutil.rmtree` 的 `onerror` 从 3.12 起叫 `onexc`**：`_rmtree()` 里两种都试（TypeError 兜底）；
  retry 里不要再吞异常，否则「删不掉」会静默留下旧目录（违反「不要 DshPet 那一层」）。
