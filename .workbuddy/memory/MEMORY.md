# dsh-pet 工作区笔记

## 是什么（2026-10-23 两次重构后）

**一个平铺的工作区**（没有 `pet_py/` 这层了，用户要求环境统一在根）：
1. **`make_pet_webm.py`** —— AI 绿幕视频 → 透明 webm 的素材工具（单遍管线，不需要 ffmpeg.exe）
2. **根目录那 8 个 .py** —— PySide6 桌宠运行时（零 Electron / 零 Node）

上游 Electron 插件（原 `dsh-pet/`，292MB）、`scripts/`、`tools/`、`docs/` 等**已按用户要求整体删除**。
⚠️ **用户在 2026-10-23 17:06 自己 `git init` 并提交了两次**（106 个 webm 也一起入库了），
此前删掉的东西不在历史里。

```
dsh-pet/
├── .venv/                  工作区统一环境（运行时 + 素材工具共用，唯一一份）
├── requirements.txt
├── setup-pet.cmd / run-pet.cmd
├── main.py window.py player.py video.py config.py physics.py menu.py tray.py
├── config.jsonc   assets/webm/（运行时素材 106 个）
├── make_pet_webm.py / make-webm.cmd
├── prompts/       video/(mask + webm/ 产物)
└── .workbuddy/    项目记忆
```

## 命令（全部在根目录）

```bat
setup-pet.cmd                                  :: 建 .venv（腾讯云源，优先 PATH 的 python）
run-pet.cmd                                    :: 启动
.venv\Scripts\python.exe make_pet_webm.py --src D:\AI视频 --deploy
```


`--deploy` = 成品从 `video/webm` 同步到 `pet_py/assets/webm`。
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

- **`PIL.ImageGrab` 抓不到 `WS_EX_LAYERED` 窗口**。曾据此误判"没显示"，实际用户看得清楚。
  验证透明窗口只能靠 `widget.grab()` 或让用户看。
- pip 走本机代理极慢，**装大包一律腾讯云源**（24~36MB/s）。
- **venv 绝不能 mv 搬**：`activate` / `activate.bat` 里的 `VIRTUAL_ENV` 和 `Scripts/*.exe` launcher
  都内嵌绝对路径，搬完全废（pip.exe 静默 rc=1）。搬过就删了重建。
  `python.exe` 本身不受影响（靠 pyvenv.cfg 的 home），所以「能跑但进不了环境」是这个症状。
- 非 tty 下 Python stdout 块缓冲，调试要 `-u` 或 `flush=True`。

## 运行时要点

- `main.py`：`ASSETS = HERE/"assets"`，`AnimationSet(ASSETS/"webm")` —— **只认 `pet_py/assets/webm`**。
- 窗口尺寸 = `pets[0].size`（462，16:9），窗口 462×260。
- 状态机 idle/action/turn/move/drag/throw/click，权重 idle10/turn5/move5 + 分类加权。
- 松手：轨迹 <500px/s 死区 → 原地站住；超过才抛掷/反弹/落地挤压。
- 落地挤压锚点是**水平中心 + 脚底**（不是左边缘），`SQ_MAX_SQUASH=0.72`、宽度耦合 0.12。
- petCollision 已实现（`collidePet` 直译，等质量 e≈1 交换动量），配置默认 true。
- 每只宠物独立解码线程 + 独立 `fixedEnabled`。
- 菜单「大小」不写回配置（重启回 462）。
- `config.jsonc` 是从原插件整份拷来的，含大量 DSH 时代注释，只有部分字段 pet_py 会读。

## 打包（build.py / build.cmd）

- PyInstaller onedir → `dist/DshPet/`，136MB。`--onefile` 也可（启动慢 2~3s）。
- **素材/配置外置**在 exe 同级，不走 `--add-data`。`main.py:_app_dir()`：
  frozen 时 `Path(sys.executable).parent`，`HERE`/`CONFIG_FILE`/`ASSETS` 全部据此解析。
- **`--windowed` 时 `sys.stdout is None`** → 满屏 `print` 会炸；main.py 顶部重定向到 `dsh-pet.log`。
- **别给 PyInstaller 传 `--clean`**：它清 `%LOCALAPPDATA%\pyinstaller` 上百个文件，
  在受限环境触发批量删除拦截，构建直接失败。清 build/dist 用 `build.py --clean`。
- 必须 `--collect-all av`（否则 libvpx dll 缺失）；图标从素材首帧抠，必须先 `alpha[alpha<16]=0`。
