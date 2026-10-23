# dsh-pet 工作区笔记

## 是什么（2026-10-23 重构后）

**两个自包含部分**，已经不是 DSH 插件仓库：
1. **`make_pet_webm.py`** —— AI 绿幕视频 → 透明 webm 的素材工具（单遍管线，不需要 ffmpeg.exe）
2. **`pet_py/`** —— PySide6 桌宠运行时（零 Electron / 零 Node）

上游 Electron 插件（原 `dsh-pet/`，292MB）、`scripts/`、`tools/`、`docs/` 等**已按用户要求整体删除**。
工作区没有 git，删了不可恢复。

```
dsh-pet/
├── make_pet_webm.py / make-webm.cmd    素材工具（根目录，不再在 scripts/）
├── prompts/                            106 动作生成契约（保留，「通用前缀」不可改）
├── video/  watermark_mask_v5.mkv + webm/（默认输出）
└── pet_py/ main/config/physics/player/video/window/menu/tray + assets/webm/ + .venv
```

## 命令

```bat
pet_py\setup-pet.cmd                                  :: 建 venv（腾讯云源）
pet_py\run-pet.cmd                                    :: 启动
pet_py\.venv\Scripts\python.exe make_pet_webm.py --src D:\AI视频 --deploy
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
