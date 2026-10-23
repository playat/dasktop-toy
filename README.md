# 桌宠工作区

两件事：**把 AI 生成的绿幕视频加工成透明 webm**，以及**把这些 webm 当成桌宠跑起来**。

```
dsh-pet/
├── make_pet_webm.py             素材生成工具（一条命令，单遍管线）
├── prompts/                     AI 生成提示词配方（106 个动作的契约）
├── video/
│   ├── watermark_mask_v5.mkv    水印遮罩（1280×720）
│   └── webm/                    成品输出目录
└── pet_py/                      桌宠运行时（PySide6 + PyAV，零 Electron）
    └── assets/webm/             运行时读取的素材
```

数据流是一条直线：

```
prompts/  →  AI 生成  →  video/*.mp4  →  make_pet_webm.py  →  video/webm/*.webm
                                                                    │ --deploy
                                                                    ▼
                                                        pet_py/assets/webm/*.webm
                                                                    │
                                                                    ▼
                                                             pet_py/run-pet.cmd
```

---

## 1. 跑起来

```bat
pet_py\setup-pet.cmd     :: 首次：建 .venv + 装依赖（PySide6 / av / numpy / pillow）
pet_py\run-pet.cmd       :: 启动桌宠
```

- 右键宠物：动作点播、大小、固定原地、回到初始位置、重载、再来一只、退出
- 拖拽：松手站住；**甩出去**才会飞、撞墙反弹、落地压一下
- 托盘图标：显示/隐藏、加一只、全部回位、重载、退出
- 终端里 `Ctrl+C` 退出

配置在 `pet_py/config.jsonc`（JSONC，带注释），改完右键「重载配置」即可，不用重启。

## 2. 生成素材

```bat
pet_py\.venv\Scripts\python.exe make_pet_webm.py --src D:\ai视频 --deploy
```

嫌命令长就把文件夹**直接拖到 `make-webm.cmd` 上**，效果一样。

| 参数 | 说明 |
| --- | --- |
| `--src` | AI 视频目录（mp4 / mov / webm / mkv） |
| `--out` | 输出目录，默认 `video/webm` |
| `--deploy` | 顺手同步到 `pet_py/assets/webm`，运行时直接能播 |
| `--mask` | 水印遮罩，默认 `video/watermark_mask_v5.mkv`（必须和源视频同分辨率） |
| `--no-watermark` | 素材本身没水印时跳过 |
| `--jobs` | 并行进程数，默认 4 |
| `--only` | 只处理文件名含该子串的视频 |

**输出文件名 = 动画名**，必须和 `config.jsonc` 里写的一字不差（不带 `.webm`）。

工具做四件事，全在一条管线里，不落中间文件、不需要装 ffmpeg：

1. **去水印** — 按 mask 找到水印区域，逐圈膨胀从周围取色填掉
2. **绿幕抠像** — 只认「色相 70~170° + 饱和度/明度 ≥0.15」为背景，蓝、白、红不会被误抠
3. **归一化** — 用首尾各 5 帧量站立高度/水平中心/脚底，统一缩放到 640×360，站立 266.7px、脚底 y=330
4. **转码** — VP9 / yuva420p / CRF 40 / 24fps

## 3. 做一只自己的桌宠

1. 用 `prompts/桌面宠物 10 秒动作提示词.md` 里的配方生成绿幕视频 —— **「通用前缀」那一段不能改**，绿幕纯色、角色居中、双脚锚定、首尾是标准站立，全靠它保证下游能对齐
2. 全部丢进一个目录，跑上面的命令
3. 在 `pet_py/config.jsonc` 的 `animations` 里把动画名填进对应池子：
   - `idle` / `turn` / `move` / `drag` / `clicks` / `actions`（动作点播的三级菜单）
   - 最小可跑：`idle` 一个就够；体验完整建议 idle + drag + turn + clicks + moves 约 6 个
4. 右键「重载配置」

配比看 `animationWeights`（idle / turn / move 的掷骰权重），`fixedEnabled: true` 的宠物不会自己走动。

## 依赖

- Python 3.10+
- `pet_py/requirements.txt`：PySide6-Essentials / av / numpy / pillow
- 素材编码：PyAV 自带的 libvpx-vp9，**不需要外部 ffmpeg**

`LICENSE` 为上游客体项目（dsh-pet）的 MIT 许可，`prompts/` 与素材源自该项目。
