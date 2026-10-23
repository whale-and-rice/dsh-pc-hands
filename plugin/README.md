# dsh-pc-hands

给 AI agent 一双手、一双眼睛：截屏、读字、按文字点、打字按键、滚轮、剪贴板、窗口操作、多步任务。  
Windows only · 依赖一个 Python（只用标准库 + Pillow + RapidOCR）· 零点击配好

> 这是鲸娘给 DeepSeek Harness 写的插件，也是她自己的手。  
> 设计原则只有一条：**一步只有被观察到才算完成** —— 所以每个动作都能回读证据（截图路径、OCR 文本、坐标、退出码）。

## 工具一览（19 个）

| 分类 | 工具 | 说明 |
|---|---|---|
| 看 | `pc_windows` | 列出可见窗口（句柄/进程/位置/标题） |
| | `pc_see` | 截屏 + OCR，返回文本与截图路径（可只截某个窗口） |
| | `pc_find` | 按文字找坐标；支持 `region` / `below` / `above` / `pick` |
| 等 | `pc_wait` | 等某段文字出现（OCR 轮询），替代 sleep |
| 动 | `pc_click` | 点文字、点坐标、点控件 |
| | `pc_type` | 输入文本（中文走剪贴板，任何程序都吃得下） |
| | `pc_key` | 按键 / 组合键 |
| | `pc_scroll` | 滚轮（正上负下，可先移指针） |
| | `pc_drag` | 拖拽（`--hold` 长按再拖，适配桌宠这类） |
| | `pc_move` | 按标题搬窗口（尺寸取自实时矩形，不会搬坏） |
| | `pc_hover` | 慢速悬停 2 秒，等 hover 面板渲染出来再点 |
| | `pc_scan` | 盲扫兜底：沿一列 y 点一次读一次标题栏，命中才停 |
| 剪贴板 | `pc_clip` | `get` 读 / `set` 写（抓页面长串、验证复制成功） |
| 控件 | `pc_uia` | UIAutomation：按控件名 list / tree / select / invoke |
| 组合 | `pc_task` | 一次调用跑一串步骤，每步有判据、失败重试、写 journal |
| 兜底 | `pc_console` | 读控制台窗口里的文字（比 OCR 准） |
| | `pc_close` | 按标题关窗口（发 WM_CLOSE，等于点右上角） |
| 守卫 | `pc_guard` | 弹窗守卫：只点「取消/否/关闭」，危险按钮留给人 |

动键鼠的工具都会自动弹提示：「本鲸开始动键鼠了」→「做完了，可以碰屏幕了」（15 秒去抖）。

## 需要什么（Windows only）

| 必需 | 说明 |
|---|---|
| Windows 10/11 | 整块基于 Win32（ctypes），macOS / Linux 不支持 |
| Python 3.9+ | 只用标准库 + Pillow |
| Pillow | `pip install pillow` |

| 可选 | 说明 |
|---|---|
| RapidOCR（`pip install rapidocr_onnxruntime onnxruntime`） | 识别质量好得多（微信列表、小字都读得出）。**没装就自动退回 Windows 自带 OCR** |
| `notify.ps1` | 桌角弹提示用；没有就跳过，不影响功能 |

## 安装

1. 把 `pc/` 那套脚本放进 `~/OllamaStart/pc/`（`pc.py`、`ocr.ps1`、`uia.ps1`、`pctask.py`、`ocr_rapid.py`）
   也可以放别处，装完改配置或设环境变量 `DSH_PC_DIR`
2. 本插件目录放进 `~/.dsh/profiles/desktop/node_modules/dsh-pc-hands/`
3. profile 的 `package.json`：`dependencies` 加上它、`dsh.profile.bundles` 加上 `dsh-pc-hands`（**无 BOM 的 UTF-8**）
4. 重启 DSH
5. **第一件事：让 agent 跑 `pc_doctor`** —— 它会逐项告诉你 Python / Pillow / 截图 / OCR / DPI / 显示器 / 剪贴板 哪一项没好

### 找路径的规则（不用配也能跑）

- 脚本目录：`config.pcDir` → `DSH_PC_DIR` → `~/OllamaStart/pc` → `~/dsh-pc-hands/pc` → `~/.dsh/pc-hands`
- Python：`config.pythonPath` → `DSH_PC_PYTHON` → DSH 自带运行时 → 常见安装位置 → `PATH` 里的 `python`
- 都找不到不会静默失败：会注册一个 `pc_doctor`，直接告诉你试过哪些路径、怎么修

配置项（`cordis.patch.yml` 里的 `config`）：

```yaml
- id: dsh-pc-hands
  config:
    pcDir: C:/Users/<你>/OllamaStart/pc
    pythonPath: C:/Python312/python.exe
    timeoutMs: 120000
```

## 踩过的坑（都写进代码注释了）

- **DSH 的工具 schema 是严格编译的**：`object` 必须显式写 `additionalProperties`，否则整条工具被静默丢弃
- **插件里 `spawn(..., {detached:true})` 会被 DSH 的 Job Object 收走**，静默死掉 —— 弹提示要用普通 spawn + unref
- **改工具的报错文案会断掉上层解析**（插件用正则解析坐标），输出措辞就是接口
- **Windows 原生 OCR 对微信列表一行都读不出来**，所以优先 RapidOCR，失败才回落
- **截图里同名文件会被缓存**：给人看的图每次换新文件名
- **不做 DPI 感知的机器上，截图坐标和点击坐标会错位**（缩放 125%/150% 时）—— 所以启动就 `SetProcessDpiAwarenessContext(-4)`

## License

MIT

### v0.7.0 新增（都是实战踩出来的）

- `pc_scroll` 支持 `pixels`（精细，40 左右很顺）和 `untilText`（一直滚到看见某段字）
- `pc_wait`：等文字出现，替代 `sleep` —— 页面慢的时候不再靠猜秒数
- `pc_click` 支持 `labelText`：勾选框/单选按钮按坐标点不动时点它旁边的文字
- `pc_type` 支持 `secret`：密码/令牌粘完立刻清空剪贴板
- `pc_doctor` 在缩放不等于 100% 时直接给 WARN（那种机器上点偏最常见）
- 文字匹配统一**不区分大小写**（`Generate new token` == `Generate New Token`）
