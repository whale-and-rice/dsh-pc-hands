# dsh-pc-hands

给 AI agent 一双手、一双眼睛：截屏读字、按文字点、打字按键、滚轮、剪贴板、窗口操作、多步任务。  
**Windows only · 一个 Python 就够 · 装完先跑 `pc_doctor` 自检**

> 这是 [DeepSeek Harness](https://github.com/deepseek-ai) 的插件，也是鲸娘自己的手。
> 设计原则只有一条：**一步只有被观察到才算完成** —— 每个动作都能回读证据（截图路径、OCR 文本、坐标、退出码）。

---

## 装的三种方式

### ① 下载解压（最稳，推荐）

从 [Releases](https://github.com/whale-and-rice/dsh-pc-hands/releases) 下 `dsh-pc-hands-*.zip`，解压后双击：

```bat
setup.cmd                 :: 装脚本 + Pillow（不需要管理员）
setup.cmd -WithOCR        :: 顺便装 RapidOCR（识别质量明显更好，约 100MB）
setup.cmd -InstallPlugin  :: 顺便注册进 DSH profile
```

### ② npm（等发布后可用）

```bash
npm i dsh-pc-hands
```

包内自带 `pc/` 脚本；插件会优先用包里的那份，没有才回落到 `~/OllamaStart/pc`。
装完仍需把插件注册进 DSH profile（或手动 `-InstallPlugin`）。

### ③ 直接从 CDN 取单文件（不装任何东西）

```bash
# 例如把最新版脚本拉到本地
curl -L https://cdn.jsdelivr.net/gh/whale-and-rice/dsh-pc-hands@v0.6.1/pc/pc.py -o pc.py
```

装完 **重启 DSH**，然后让 agent 跑一次 `pc_doctor`：

```
pc-doctor
  OK   System      Windows 11
  OK   Python      3.12.14  ...\python.exe
  OK   DPI         mode=per-monitor-v2  system_dpi=96  scale=100%
  OK   Screenshot  Pillow 12.3.0  grab=1440x1080
  OK   OCR         rapidocr=有  ocr.ps1=有
  OK   Clipboard   可读
  ----  0 FAIL  1 WARN  ----
```

哪一项 `FAIL`，就照它上面那行提示修 —— 不用猜。

## 需要什么

| 必需 | 说明 |
|---|---|
| Windows 10 / 11 | 整块基于 Win32（ctypes），macOS / Linux 不支持 |
| Python 3.9+ | 只用标准库 + Pillow |
| Pillow | `pip install pillow`（setup 会替你装） |

| 可选 | 说明 |
|---|---|
| `rapidocr_onnxruntime` + `onnxruntime` | 识别质量好得多（小字、列表行、聊天正文都读得出）。没装自动退回 Windows 自带 OCR |
| `notify.ps1` | 桌角弹提示；没有就跳过 |

**找路径的规则**（不用配也能跑）：脚本目录 `config.pcDir` → `DSH_PC_DIR` → `~/OllamaStart/pc` → `~/dsh-pc-hands/pc` → `~/.dsh/pc-hands`；Python `config.pythonPath` → `DSH_PC_PYTHON` → DSH 自带运行时 → 常见安装位置 → `PATH`。都找不到时不会静默失败，而是注册一个会解释原因的 `pc_doctor`。

## 18 个工具

| 分类 | 工具 | 说明 |
|---|---|---|
| 看 | `pc_windows` | 列出可见窗口（句柄/进程/位置/标题） |
| | `pc_see` | 截屏 + OCR，返回文本与截图路径 |
| | `pc_find` | 按文字找坐标（支持 `region` / `below` / `above` / `pick`） |
| 动 | `pc_click` | 点文字、点坐标、点控件 |
| | `pc_type` | 输入文本（中文走剪贴板，任何程序都吃得下） |
| | `pc_key` | 按键 / 组合键 |
| | `pc_scroll` | 滚轮（正上负下，可先移指针） |
| | `pc_drag` | 拖拽（`hold` 长按再拖，适配桌宠这类） |
| | `pc_move` | 按标题搬窗口（尺寸取自实时矩形，不会搬坏） |
| | `pc_hover` | 慢速悬停 2 秒，等 hover 面板渲染出来再点 |
| | `pc_scan` | 盲扫兜底：沿一列 y 点一次读一次标题栏，命中才停 |
| 剪贴板 | `pc_clip` | `get` 读 / `set` 写 |
| 控件 | `pc_uia` | UIAutomation：按控件名 list / tree / select / invoke |
| 组合 | `pc_task` | 一次调用跑一串步骤，每步有判据、失败重试、写 journal |
| 兜底 | `pc_console` | 读控制台窗口文字（比 OCR 准） |
| | `pc_close` | 按标题关窗口（发 WM_CLOSE，等于点右上角） |
| 守卫 | `pc_guard` | 弹窗守卫：只点「取消/否/关闭」，危险按钮留给人 |
| 自检 | `pc_doctor` | 一屏报告环境是否就绪 |

动键鼠的工具会自动弹提示：「本鲸开始动键鼠了」→「做完了，可以碰屏幕了」（15 秒去抖）。

## 出问题了怎么办

| 现象 | 先看 | 常见原因 |
|---|---|---|
| 工具根本没出现 | `plugin-loaded.json` 的 `count` / `failures` | 脚本目录不对（`pc_doctor` 会说试过哪些路径） |
| `点了没反应` / 点偏 | `pc_doctor` 的 `DPI` 行 | 老版本没做 DPI 感知；现在启动就 per-monitor-v2 |
| OCR 全是空的 | `pc_doctor` 的 `OCR` 行 | 没装语言包 / 引擎没装 |
| 截图是黑的 | `Screenshot` 行 | 安全桌面（UAC 弹窗）不能被普通进程截图 |
| 中文变乱码 | — | `pc.py` 已强制 UTF-8；若是自己写的调用脚本，记得也设 UTF-8 |

## 已知限制（诚实清单）

- **只能 Windows**：全部基于 Win32 API。
- **UAC / 安全桌面**截不到、点不到（系统不允许）。
- **管理员权限的窗口**：标准权限进程发的键鼠消息会被 UIPI 拦掉；必要时以管理员身份跑 DSH。
- **OCR 依赖语言包**：Windows OCR 只认已安装的语言；装 RapidOCR 可以绕开这个限制。
- **多显示器**按虚拟屏坐标；移动过显示器排列后，旧坐标会失效，重新截图即可。
- 这是「能操作整台电脑」的能力，**权限应当收紧**：只给信得过的 agent、只在需要的会话里开。

## 设计上的几条硬规矩

1. **每个动作都回读证据**：截图路径、OCR 文本、坐标、退出码 —— 不靠「我以为点到了」。
2. **危险按钮不自动点**：弹窗守卫只点取消类，确定/删除/允许一律留给人。
3. **找路径有顺序、有兜底、有解释**：装错了要能自己修，而不是静默失败。
4. **不做「猜」**：OCR 读不出就说读不出，不用视觉模型硬猜坐标。

## License

MIT

---

# dsh-pc-hands (English)

A DeepSeek Harness plugin that gives an agent hands and eyes on Windows: screenshot + OCR, click by text, type, scroll, clipboard, window management, popups guard, and a verified multi-step task runner — 18 tools total.

```bat
setup.cmd -WithOCR -InstallPlugin     :: install scripts, RapidOCR, and register the plugin
```

Requires Windows 10/11 + Python 3.9+ + Pillow. RapidOCR is optional (falls back to the built-in Windows OCR). After installing, restart DSH and run `pc_doctor`; it reports Python, Pillow, screenshot, OCR engine, DPI scaling, monitors, clipboard and notification script, one line each.

Design rule: **an action only counts once it has been observed** — every step returns evidence (screenshot path, OCR text, coordinates, exit code). Dangerous dialog buttons are never clicked automatically.
