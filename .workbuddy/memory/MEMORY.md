# dsh-pet 工作区笔记

## 是什么（2026-10-23 两次重构后）

**一个平铺的工作区**（没有 `pet_py/` 这层了，用户要求环境统一在根）：
1. **`make_pet_webm.py`** —— AI 绿幕视频 → 透明 webm 的素材工具（单遍管线，不需要 ffmpeg.exe）
2. **根目录那 8 个 .py** —— PySide6 桌宠运行时（零 Electron / 零 Node）

上游 Electron 插件（原 `dsh-pet/`，292MB）、`scripts/`、`tools/`、`docs/` 等**已按用户要求整体删除**。
⚠️ 用户在 2026-10-23 自己 `git init` 并提交过几次；**git 里 0 个 webm**（素材被 .gitignore 排除），
所以素材没有版本历史可回滚——唯一的副本是 `dist/DshPet/assets/webm/`（打包时拷贝的外置素材）。

```
dsh-pet/
├── .venv/                  工作区统一环境（运行时 + 素材工具共用，唯一一份）
├── requirements.txt
├── setup-pet.cmd / run-pet.cmd
├── main.py window.py player.py video.py config.py physics.py menu.py tray.py
├── config.jsonc   assets/webm/（运行时素材 106 个）
├── make_pet_webm.py / make-webm.cmd
├── build.py / build.cmd    dist/DshPet/
├── prompts/
├── video/        watermark_mask_v5.mkv + src/（原始 mp4）+ webm/（产物）
└── .workbuddy/    项目记忆
```

## 命令（全部在根目录）

```bat
setup-pet.cmd                                  :: 建 .venv（腾讯云源，优先 PATH 的 python）
run-pet.cmd                                    :: 启动
make-webm.cmd                                  :: 双击 = 处理 video/src，增量，已生成的跳过
.venv\Scripts\python.exe make_pet_webm.py            :: 等价的命令行写法
```


`--src` 不传 → 默认 `video/src`；**成品直接写 `assets/webm`**（就是运行时素材目录，
没有 deploy/复制这一步）。增量判断递归扫 `assets/webm` 整棵树（stem 匹配），
命中子目录里的成品也算已生成，重做时原地覆盖、不在根目录造重复。
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
  只能用 `_check_only` 这类不可能撞名的名字。素材没有 git 历史，只能从 `dist/DshPet/assets/webm/` 找回。
- **`PIL.ImageGrab` 抓不到 `WS_EX_LAYERED` 窗口**。曾据此误判"没显示"，实际用户看得清楚。
  验证透明窗口只能靠 `widget.grab()` 或让用户看。
- pip 走本机代理极慢，**装大包一律腾讯云源**（24~36MB/s）。
- **venv 绝不能 mv 搬**：`activate` / `activate.bat` 里的 `VIRTUAL_ENV` 和 `Scripts/*.exe` launcher
  都内嵌绝对路径，搬完全废（pip.exe 静默 rc=1）。搬过就删了重建。
  `python.exe` 本身不受影响（靠 pyvenv.cfg 的 home），所以「能跑但进不了环境」是这个症状。
- 非 tty 下 Python stdout 块缓冲，调试要 `-u` 或 `flush=True`。

## 运行时要点

- **右键「动作」菜单不读配置**：`AnimationSet.scan()` 用 `rglob` 递归扫 `assets/webm`，
  键 = 相对路径去后缀（`小动作/悠闲哼歌`）；`menu.py:_add_actions` 递归把目录变子菜单。
  **只有根层**再用配置的池/分类分组（配置没有的进「未分类」），单层 >30 条自动分页。
  `build_menu()` 每次调用都 `scan()`，丢文件进去不用重启也不用重载。
- `config.jsonc` 的 `animations.*` 现在只决定**自动行为**（自动待机/点击/走位/随机动作），不决定菜单内容。
- `path_of()` 先精确匹配键、再按 stem 兜底 → 配置里写名字、素材挪进子目录也能找到（同名取第一个）。
- `main.py`：`ASSETS = HERE/"assets"`，`AnimationSet(ASSETS/"webm")` —— **只认 `assets/webm`**。
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
