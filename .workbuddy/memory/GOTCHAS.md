# dsh-pet 实测坑清单

> 每一条都是踩过的，别凭直觉推翻。细节和现场见 `.workbuddy/memory/2026-10-*.md`。

## PyAV / 素材

- **VP9 带 alpha 必须显式解码**：`av.CodecContext.create('libvpx-vp9','r')` + `pix_fmt='yuva420p'` + 手动 extradata。
  用 `container.decode()` 得到的是 yuv420p、alpha 全 255 的假结果（看着像素材没 alpha，其实是解码器错）。
- **背景 alpha 是 1~15 噪声不是 0**（82% 像素）：解码后必须 `alpha[alpha<16]=0`，否则整窗挡住鼠标。
- 输出几何：640×360、站立 266.7px、脚底 y=330（= 运行时 `FEET_Y_RATIO=330/360`）。
- **测试素材别用真实动画名**：曾用 `悠闲哼歌` 当测试源，`--deploy` 把 584KB 原素材覆盖成 3KB 再被删。
  只能用 `_sync_probe` 这类不可能撞名的名字。误删用 `git checkout -- assets/webm/`（dist/assets 是镜像，不是备份）。

## 窗口 / Qt

- 点击穿透**不需要 setMask**：分层窗口天然按 per-pixel alpha 让位。
- **窗口定位只用 Qt 逻辑像素**（`availableGeometry()` + `move()`）。混用 Win32 物理像素在 150% 缩放下会推屏外
  （win 存在、visible=1，人眼看不见）。主屏 2560×1466 逻辑 @1.5x。
- 多屏：甩飞/走位前 `screenAt()` 同步 bounds，否则副屏宠物被主屏 edge 钳回来。
- **托盘图标必须 `alpha[alpha<16]=0` 再算 bbox**，否则噪声让裁切框退化成全图。
- **右键菜单不能在 mousePress 里 exec()**（撞鼠标 grab 会瞬间关闭）：press 记坐标，release 里 `QTimer.singleShot(0, popup)`。
- **Ctrl+C 必须配空转 QTimer(200ms)**：Qt 循环不跑字节码，光 `signal.signal(SIGINT)` 没反应。
- **`PIL.ImageGrab` 抓不到 `WS_EX_LAYERED` 窗口**，曾据此误判"没显示"。验证只能 `widget.grab()` 或让用户看。
- **`QMenu.exec` 不能 monkeypatch**（PySide 走 C++ 原方法，离屏会卡住）。要截获菜单树就替换 `main.build_menu`。
  ⚠ 真踩过：`QMenu.exec = fake_exec` 的离屏脚本卡在 `menu.exec()` 里，**跑了 9 小时**，
  输出全是宠物的 `[anim]` 随机日志（菜单树一行没打）。这类一次性探针必须自带硬超时，
  或者干脆不 exec —— 直接调 `main.build_menu(...)` 拿返回的 QMenu 去 walk。
- **离屏下视频解码远慢于实时**（10s 素材实测播了 24~27s，`offscreen` + 无真实绘制）。
  别用「两次 `_play` 的时间差」量节奏，要分开记 `_after_oneshot`（播完）和 `_play`（下一段开始），
  只看这一段静止时长，才不受解码快慢干扰。

## 投篮游戏（`basket.py`）

- **计分不能按「进圈那一点」算**：第一版一进判定圈就用入口距离结算，实测一条正穿筐心
  （最近 3.7px）的球被判成 **4 分**。必须进圈后一路记 `min(dist)`，等它**飞出圈或落地**
  再按最近距离结算 —— 于是 `+N` 飘字比"进筐"晚 100~300ms，这是对的，别当 bug 改回去。
- **判定点用 `physics.body_box` 的中心**，不是窗口中心：窗口脚下有透明留白，
  两者 y 差 `(192.5/360 - 0.5) × H ≈ 9px`（H=260），占判定半径 18%。
- **篮筐窗口要拖，不能 `WindowTransparentForInput`**：改用 `setMask(alpha)` —— 只有画出来的
  像素吃鼠标，透明区穿透（宠物停在筐后面还能抓起来）。反过来**计分徽章必须
  `WindowTransparentForInput`**，否则会挡住宠物。代价：换成真图后图片的透明区点不到。
- **`ScoreBadge.follow()` 必须在 `show()` 之后**：它对隐藏窗口只记 anchor 不挪位置，
  顺序反了徽章会先闪在屏幕左上角 (0,0)。
- **游戏期间要给窗口上 `game_lock`**：`_roll_next` 只播待机。不加的话投完落地宠物按随机链
  自己走开/转身，玩家每投一次都得先追上去。

## CPU 负载（`_LoadMeter`）

- **算「机器忙不忙」必须减基线**：桌宠自己解码十几路视频就占掉一截 CPU，直接拿
  `GetSystemTimes` 的占用率当负载，它永远觉得自己在拖满机器 → 间隔被一路放大
  （实测 2~5s 的配置跑出 29s 静止）。→ 第一次采样当 baseline，之后只报「比基线多忙了多少」，
  且 `self._base = min(self._base, busy)` 只在更闲时下调。
- `GetSystemTimes` 的 kernel 时间**已包含 idle**：`busy = (kernel+user) - idle`，别再减一次。

## 排查卡住的进程（这机器没有 wmic，也没装 psutil）

`tasklist` 只能看名字/PID/内存，PowerShell 工具在部分会话里 stdout 拿不回来。可用的组合是：

- `tasklist /M /FI "PID eq <pid>"` 看**加载了哪些 DLL** —— 有 `Qt6Widgets.dll` 的 python 进程
  就是 PySide 程序（离屏 harness 一眼能认出来）。
- 命令行靠 `ctypes` 读 PEB：`NtQueryInformationProcess` 拿 PEB → `+0x20` 取
  `ProcessParameters` → `+0x70` 是 `UNICODE_STRING.Length`、`+0x78` 是 `Buffer`，
  再 `ReadProcessMemory`（需要 `PROCESS_QUERY_INFORMATION|VM_READ`）。
  启动时间用 `GetProcessTimes` 的 CreationDate（FILETIME，1601 起算）。
- `tasklist /FO CSV` 输出是 **GBK**，`subprocess` 别用 `text=True`，要 `.stdout.decode('gbk','ignore')`。
- 杀：`taskkill /PID <pid> /F`。杀之前先确认不是 `DshPet.exe` 和 WorkBuddy 自己的 node MCP 进程。


## 文件 / 环境

- **本机 `Path.rmdir()` 在 F: 盘是「递归删除」**：对非空目录不报错、直接把里面文件删光，返回值还是 0。
  踩过：镜像后清理空目录那步把刚落好的 162 个素材全删了。→ rmdir 前 `if any(d.iterdir()): continue`；
  删树一律 `shutil.rmtree`（它本来就是递归的，语义明确）。
- **venv 绝不能 mv 搬**：`activate` / `Scripts/*.exe` launcher 内嵌绝对路径，搬完全废（pip.exe 静默 rc=1）。
  `python.exe` 本身不受影响（靠 pyvenv.cfg 的 home），所以症状是「能跑但进不了环境」。搬过就删了重建。
- venv 不限制盘符访问：C 盘目录报"不存在"是路径字符串问题（多余引号 / 末尾反斜杠），别往权限上猜。
- pip 走本机代理极慢，装大包一律腾讯云源（24~36MB/s）。非 tty 下 stdout 块缓冲，调试要 `-u`。

## 批处理 / 打包

- **`.cmd` 里绝不能出现中文**（哪怕只是 `rem` 注释）：cmd.exe 按 cp936 解析 UTF-8 文件，中文行会被当命令执行、
  后面的参数也被拆坏（实测报 `'㈢殑鍙傛暟缁х画閫忎紶' 不是内部或外部命令`）。`chcp 65001` 救不了（解析早于执行）。
  ⚠ **2026-10-28 又踩一次，症状更隐蔽**：`if (...)` 块里的中文被按 cp936 解码后字节里含 `)`，
  **提前闭合了 if 块**，于是块里本该「只打印给用户看」的 `pip install -r requirements.txt` 被真的执行了
  （真的去下载 82MB 的 PySide6）。→ 现在 `run-pet.cmd` **全文 ASCII**，中文提示只放 README；
  改完 .cmd 一定要跑一遍 `any(ord(c) > 127 for c in line)` 自检。
- 双分支批处理写成 `:label` + `goto` 比 `if (...) else (...)` 稳，块里的 `echo` 太多容易被中文/特殊字符拆坏。
- **`--windowed` 时 `sys.stdout is None`** → main 顶部重定向到 `dsh-pet.log`。
- **别直接给 PyInstaller 传 `--clean`**（清 %LOCALAPPDATA% 下上百文件，触发拦截），用 `build.py --clean`。
- 必须 `--collect-all av`；`shutil.rmtree` 的 `onerror` 从 3.12 起叫 `onexc`（`_rmtree()` 两种都试）。
- 桌宠开着打包会占用文件 → `_busy_hint()` 给人话「先从托盘退出」。

## 会话日志

- **zstd 是流式帧**（帧头没有 content size）：`zstandard.decompress()` 报
  `could not determine content size in frame header` 并被 `_decode_frames` 吞掉 → **0 个事件**（像「日志是空的」）。
  必须 `ZstdDecompressor().decompress(blob, max_output_size=64<<20)`，已做三级降级。
- `first_read_limit=4MB` 会跳过文件头 → 大日志的 `session_id/cwd` 可能为空，`_sid_of()` 用 `path` 兜底。
