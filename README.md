# 亚马逊采集工作台

一个 exe 搞定两件事：**商品链接信息** + **A+ 内容**。

| 抓什么 | 字段 |
|---|---|
| **链接信息** | 标题 · 7.27 商品亮点（小标题）· 类目路径 · 最小类目节点 ID · 五点描述 · 主图（还原原始 master 大图）· 品牌 · 价格 · 评分 · 评论数 · 卖家 |
| **A+ 内容** | 普通 A+ / 高级 A+（Premium A+），桌面端 + 移动端；排版还原单文件 HTML、按 SKU 分节合集、A+ 图片包 |

两者**共用同一个已打开的页面** —— 只多跑一段 JS，不额外开浏览器、不额外加载页面。

行为说明：

- 站点默认阿联酋 / 沙特；粘贴完整链接时以链接自带站点为准（美站、英站都能直接抓）
- 品牌故事（Brand Story）默认不抓，需要时勾「含品牌故事」。底部关联推荐位（`aplusRelatedContent` / `aplusDetailPageBtfFeaturedContent`）不算 A+，已排除
- **普通 A+ 没有独立移动端版本**（移动端就是同一套 970 模块等比缩放），默认自动跳过；判定依据会写在结果表里

---

## 下载即用（推荐）

到 [Releases](https://github.com/disky1998/aplus-workbench/releases/latest) 下载 **`AmazonWorkbench.exe`**，双击即可：

- 自动起本地服务并打开前台页面
- **右下角系统托盘**常驻，右键菜单：打开前台 / 获取更新 / 打开输出目录 / 关于 / 退出
- 启动后**自动检查更新**，有新版本会弹气泡提醒，页面右上角出现红点
- 点「获取更新」→「下载并更新」会**自动替换 exe 并重启**，不用手动覆盖

要求：Windows 10 / 11 64 位。首次使用请在「环境」面板点一次「安装 / 更新内核」（约 150 MB，只需一次）。

> **抓取建议**：默认已勾选「有头模式」。第一次跑一个任务，在弹出窗口里过掉验证码，会话会被记住，之后就不容易再被拦。页面标题旁出现「登录态就绪」就说明记住了。

---

## 为什么自己写

GitHub 上没有成规模的开源 A+ 爬虫（2026-09 检索结果）：

| 项目 | 星 | 状态 | 说明 |
|------|-----|------|------|
| `amz-crawler-mcp`（npm） | - | 0.1.0 / 停止 | 唯一明确做 A+ 的开源工具，但抓的是 **Seller Central 编辑器里的模板预览**，不是竞品页面 |
| `Pangolin-spg/amazon-walmart-shopify-scrape-api` | 59★ | 活跃 | Go 解析库，只解析**本地 HTML**，不含网络请求 / 代理 / 验证码处理 |
| `benhorvath/apify-amazon-aplus` | 0★ | 2025-09 | Apify actor，总结产品页设计元素，而不是结构化 A+ 模块 |
| 各类通用 scraper | 几十~几千★ | 混杂 | A+ 通常只有一个 `has_aplus` 布尔值或纯文本，不出模块结构 |

**关键限制（务必知道）**：官方 **SP-API A+ Content API（aplusContent 2020-11-01）只能读写你自己的 A+ 内容**，`searchContentDocuments` / `getContentDocument` 拿不到任何竞品的 A+。所以竞品 A+ 只有爬这一条路。

## 安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Web 工作台

```bat
双击  启动工作台.bat          :: 源码模式
或
双击  dist\APlusWorkbench.exe :: 打包后的 exe（带托盘）
```

或命令行：`python webapp.py` → 自动打开 **http://127.0.0.1:8788**

功能：

- **输入框直接粘贴**商品链接或 ASIN（混贴、换行、逗号分隔都行，自动识别站点与去重，边打字边显示识别结果）
- 🔥 **浏览器预热 + 多路并行**：一键启动 6 个标签页（有头浏览器，共享同一份登录态 —— 任意一个里登录 / 过验证码即可），之后 6 个线程各用一个标签页同时抓，实测 3 路并行 34 秒跑完 3 个 SKU（单抓一个商品本身要 18-19 秒）。没预热时「开始抓取」置灰；也可勾选「跳过预热」走原来的单线程
- **站点**：阿联酋 `ae` / 沙特 `sa`；粘贴完整链接时以链接自带站点为准（美站、英站等都能直接抓）
- **抓取内容三选一**：全部（商品信息 + A+，共用同一个页面一次抓完）/ 商品信息（不解析 A+，快很多）/ A+ 内容
- **A+ 抓取模式三选一**：自动判定（推荐）/ 强制普通 A+（仅桌面端）/ 强制高级 A+（桌面端 + 移动端），判定依据直接写在结果表里
- **一键安装 / 更新 Playwright 内核**（抓取真正依赖的浏览器，不需要 chromedriver）
- **获取更新**：启动自动检查 + 手动检查，页面内查看版本对比与更新说明，一键下载（exe 版自动替换重启）
- 实时日志、进度条、结果表（SKU/ASIN · 标题 · 关键数据 · 状态，超过 20 条自动分页）
- **两张导出表**：`商品信息表`（标题/亮点/类目/NodeID/五点/主图/品牌/价格/评分/评论/卖家）与`批量改写导入表`（前 8 列严格对齐「批量文案重构」的导入要求，可直接上传）
- `combined.html`（按 SKU 分节的合集）与 `aplus_images.zip`（A+ 图片包 + `manifest.csv`）在每次抓取结束时自动生成，历史产出区随时可下载
- 浅色 / 深色双主题

| 接口 | 说明 |
|---|---|
| `GET /api/env` | Chrome 版本、驱动链接、Playwright 内核状态、是否已有登录态 |
| `GET /api/version` | 当前版本、仓库、是否 exe 版 |
| `POST /api/pool/preheat` | 预热浏览器（body: size / site） |
| `GET /api/pool/status` | 预热进度（state / ready / steps） |
| `POST /api/pool/close` | 关闭预热浏览器 |
| `GET /api/update/check?force=1` | 检查更新（走 GitHub Release，回退 version.json） |
| `POST /api/update/download` | 下载新版 exe（exe 版会自替换重启） |
| `GET /api/update/status` | 下载进度 |
| `GET /api/driver/link?version=` | 指定版本的驱动下载候选 |
| `POST /api/run` | 提交任务（body: text/domain/both/aplusMode/workers/…） |
| `GET /api/task/{id}?since=n` | 增量日志 + 进度 + 结果 |
| `POST /api/task/{id}/stop` | 停止任务 |
| `GET /api/history` | 扫描 `web_out/*`，列出所有已有产出及其类型/视图/产物状态 |
| `POST /api/history/{id}/rebuild` | 对已有产出重建合集 / 重新打包图片 |
| `GET /api/history/{id}/download/combined` | 下载合集 HTML |
| `GET /api/history/{id}/download/zip` | 下载 A+ 图片压缩包 |
| `GET /out/<task>/combined.html` | 合集页面（静态直出，可直接打开） |

端口改环境变量 `APLUS_PORT` 即可。

## 打包与发布

```bat
build_exe.bat          :: 装依赖 + 生成图标 + PyInstaller 打包 -> dist\APlusWorkbench.exe
python make_release.py :: 由 dist 里的 exe 生成 version.json（含 sha256）
python publish.py      :: 提交源码 + 建 GitHub Release 并上传 exe 与 version.json
```

更新机制：

1. 客户端读 `https://api.github.com/repos/<owner>/<repo>/releases/latest`
2. 接口不可用（限流/离线）时回退读仓库根目录的 `version.json`
3. 比较语义化版本号，有新版则提示；下载后比对大小，exe 版写一个 `cmd` 脚本等待主进程退出 → 覆盖 → 重启

**发新版本只需要三步**：改 `version.py` 里的 `__version__` → 更新 `RELEASE_NOTES.md` → `build_exe.bat` + `python publish.py`。

## 用法

```bash
# 单 ASIN，中东站，桌面 + 移动
python scrape_aplus.py --asin B0BHR2PJ6H --domain ae --both

# 批量 + 下载图片 + 排版还原页 + 整块截图
python scrape_aplus.py --asin-file asins.txt --domain sa --both \
    --download-images --render --screenshot --hires 1464

# 复用登录态（大幅降低验证码概率，推荐）
python scrape_aplus.py --asin B0XXXX --headed --user-data-dir ./profile

# 走代理
python scrape_aplus.py --asin B0XXXX --proxy http://user:pass@host:port

# 只解析已保存的 HTML（不发网络请求，适合调试选择器）
python scrape_aplus.py --html-file page.html --asin B0XXXX

# 连品牌故事一起抓（默认不抓）
python scrape_aplus.py --asin B0XXXX --both --include-brand-story

# 多个 ASIN 一次抓完，自动排版并合并成按 SKU 分节的 combined.html
python scrape_aplus.py --asin B0AAA B0BBB B0CCC --domain com \
    --both --render --screenshot --hires 1464

# 强制按高级 A+ 处理（桌面 + 移动端都抓），并打包图片
python scrape_aplus.py --asin B0XXXX --domain com --aplus-mode premium --package-images

# 强制按普通 A+ 处理（只抓桌面端，快一倍）
python scrape_aplus.py --asin-file asins.txt --domain ae --aplus-mode standard --render
```

| 参数 | 说明 |
|---|---|
| `--domain` | 站点后缀：ae / sa / com / co.uk / de … |
| `--delay` | ASIN 间隔秒数（默认 6，加随机抖动） |
| `--aplus-mode` | `auto`（默认，自动判定）/ `standard`（只抓桌面端）/ `premium`（桌面端 + 移动端） |
| `--package-images` | 抓完打成 `aplus_images.zip`（自动隐含 `--download-images`） |
| `--combined-name` | 合集文件名，默认 `combined.html` |
| `--render` | 额外产出 `render.html`：**按原网页排版还原**，可独立打开 |
| `--screenshot` | 额外产出 A+ 区域整块截图 `aplus_*.png` |
| `--hires N` | render / 下载的图片把 URL 里的 SX 尺寸提到 N（如 1464），保留 `__CR` 裁剪参数 |
| `--force-mobile` | `auto` 模式下，普通 A+ 也强制抓移动端 |
| `--retry N` | 单个 ASIN 失败重试次数（默认 2） |

## 排版还原（--render）

`--render` 会在页面还活着的时候，把 A+ 容器克隆出来并把**每个元素的计算样式内联**成 `style="..."`，
产出的 `render.html` **不依赖亚马逊的 CSS 文件**，双击就能按原排版打开（图片走亚马逊 CDN）。

要点：

- **容器去重**：页面里 `#aplus` 常嵌在 `#aplus_feature_div` 里且存在重复 id，
  两个都收会把内容抓两遍（实测页面高度直接翻倍）。已做嵌套去重 + `#aplus` 有内容时丢弃 `aplus_feature_div`。
- **包裹宽度取容器实际渲染宽度**（`offsetWidth`），所以高级 A+ 桌面端约 1404px、移动端约 362px，1:1 还原。
- **`--hires` 只改 URL 里的 `_SX数字_`**，不动 `__CR` 裁剪参数，否则构图会变。
- 清掉了 `script / iframe / noscript / link / input / button` 等离线无意义节点。

## 按 SKU 分节的合集（combined.html）

只要带了 `--render`，抓取结束后会自动在输出根目录生成 **`combined.html`** ——
所有 SKU 合在一个文件里，**按 ASIN 分节**，每节一条分隔线：

```
┌──────────────────────────────────────────────┐
│ 顶部：汇总表（ASIN / 类型 / 模块 / 图片 / 图宽 / 移动端）│
│      + 吸顶导航，点一下跳到对应 SKU              │
├──────────────────────────────────────────────┤
│ ───────── B0F64WJ33G  [高级 A+]  ─────────   │  ← SKU 分界线
│    桌面端 · 1404px                            │
│    （原排版 A+ 内容）                          │
│    移动端 · 362px                             │
│    （原排版 A+ 内容）                          │
├──────────────────────────────────────────────┤
│ ───────── B0F3VQCPXZ  [普通 A+]  ─────────   │
│    桌面端 · 1404px                            │
│    （原排版 A+ 内容）                          │
│    ⚠ 普通 A+ 无独立移动端版本（已跳过）          │
└──────────────────────────────────────────────┘
```

**为什么每节都用 Shadow DOM**：A+ 内容自带 3~5 个 `<style>` 块（字体、间距、动画），
直接拼进同一个文档会互相污染，而且各家页面都有 `id="aplus"` 这类重复 id。
所以每节的内容放进独立的 shadow root —— 样式天然隔离，id 也不会打架，同时保持单文件。

## 普通 A+ 与移动端

**普通 A+（标准 A+）没有独立的移动端版本** —— 移动端就是把同一套 970px 模块等比缩放，
内容完全一致（实测连图片 URL 的裁剪参数都一样）。所以默认（`--aplus-mode auto`）逻辑是：

- 桌面端判定为**普通 A+** → 自动跳过移动端抓取，省一半时间，并在 `content.json` 里写 `mobile_skipped` / `mobile_note`
- 判定为**高级 A+** → 移动端有独立排版（`aplus-m-premium-*` 模块），照常抓

判定是启发式的，**判错时可以直接覆盖**，不用改代码：

| 模式 | 行为 | 何时用 |
|---|---|---|
| `auto`（默认） | 按证据自动判定 | 常规批量 |
| `standard` | 一律当普通 A+，只抓桌面端 | 明确知道没做高级 A+，想省时间；或自动判定误判为高级 A+ |
| `premium` | 一律当高级 A+，桌面 + 移动端都抓 | 自动判定漏判了移动端版本 |

Web 工作台里对应「A+ 抓取模式」下拉；判定依据会打印到日志、写进结果表与合集页。

## A+ 图片打包与合集下载

两个产物**互相独立**，也可随时补生成：

```bash
# 抓取时直接产出
python scrape_aplus.py --asin B0XXXX --domain com --render --package-images

# 已有输出目录，事后补生成（Web 工作台点「重新打包图片」等价）
```

图片包结构：

```
aplus_images.zip
├── manifest.csv          SKU, ASIN, 站点, 视图, 序号, 文件名, 模块类型, 图片宽度, 原始URL
├── README.txt
└── <SKU>/A+/<desktop|mobile>/00.jpg  01.png ...
```

- 打包以 `content.json` 的图片清单为准，**本地缺图会自动补下载**（Web 端走浏览器上下文请求，自带 cookie / UA / 代理）
- 扩展名按响应 `Content-Type` 决定（jpg / png / webp / gif），不再靠 URL 猜
- 普通 A+ 只有 `desktop`；高级 A+ 含 `desktop` + `mobile`

合集 `combined.html` 的每节会标注：类型（普通/高级 A+）、**判定依据**、每个视图用的是 `render.html` 还是 `aplus.html`，以及移动端是「已抓 / 未抓 / 无独立版本」。缺少 `render.html` 时自动退回 `aplus.html`，不会出空节。

## 反爬要点（实测踩坑）

- **桌面 UA 池必须是 macOS 版**：Windows 版 Chrome UA（126/131）实测被亚马逊直接拦下，
  返回 0 张图的空壳页；同一 IP 换 macOS 版 UA 立刻正常。已在 `UA_DESKTOP_POOL` 里放 3 个 macOS UA 随机轮换。
- **验证码页不一定带 captcha 字样**：可能只是一个 title 为 `Amazon.com`、0 张图的空壳。
  只查 title 会漏检并**静默产出空结果** —— 已加 `looks_blocked()`（无 `#productTitle` 且无 `<img>` 即判定被拦），失败显式报错并重试。
- 仍被拦时：`--headed --user-data-dir ./profile` 复用已登录浏览器，或 `--proxy` 换 IP。

## 输出

```
out/<ASIN>/desktop/
├── aplus.html      A+ 区域原始 HTML（依赖原站 CSS）
├── render.html     排版还原页（--render，可独立打开）
├── aplus_*.png     A+ 整块截图（--screenshot）
├── content.json    结构化数据
└── images/         --download-images 时下载（00.jpg / 01.png …）
out/<ASIN>/mobile/  同上（普通 A+ 会跳过）
out/combined.html   按 SKU 分节的合集（--render 时自动生成）
out/aplus_images.zip A+ 图片包 + manifest.csv（--package-images 时生成）
```

`content.json` 字段：

```jsonc
{
  "asin": "B0BHR2PJ6H",
  "viewport": "desktop",
  "has_aplus": true,
  "is_premium_candidate": true,   // 是否按高级 A+ 处理
  "aplus_kind": "premium",        // none / standard / premium
  "premium_evidence": ["模块图宽 1464px ≥ 1464px"],   // 判定依据
  "mobile_supported": true,       // 是否有独立移动端版本
  "aplus_mode": "auto",           // 本次用的模式
  "max_image_width": 1464,
  "module_count": 5,
  "image_count": 12,
  "containers": ["aplus"],
  "modules": [
    { "index": 0, "type": "image-text", "text": "...", "images": ["..."], "image_width_hint": 1464 }
  ],
  "mobile_skipped": true,         // 普通 A+ 会写这两项
  "mobile_note": "普通 A+ 无独立移动端版本：…"
}
```

## 抓哪些容器

| 容器 ID | 内容 | 默认 |
|---------|------|------|
| `#aplus` | 现行主容器，普通 A+ 与高级 A+ 都在里面 | 抓 |
| `#aplus_feature_div` | 老版主容器 | 抓 |
| `#aplus3p_feature_div` | 第三方 / 供应商内容 | 抓 |
| `#aplusBrandStory_feature_div` | 品牌故事 | 不抓（`--include-brand-story` 开启） |
| `aplusRelatedContent` / `aplusDetailPageBtfFeaturedContent` | 底部关联推荐位，不是 A+ | 排除 |

## 普通 A+ vs 高级 A+ 怎么判

两者在 DOM 上都躺在 `#aplus` 里，没有官方标记。脚本用**两类证据**判断：

1. **模块图宽**（主证据）
   - 标准 A+ 模块图宽 **970**（整宽）或 300（侧栏）
   - 高级 A+（Premium A+）模块图宽 **1464**
   - 判据：`max_image_width > 970` 即为高级 A+
2. **模块 class**：含 `premium` / `aplus-premium` 字样也算（覆盖某些宽度读不出来的页面）

两条都不命中就是普通 A+。命中任一即判为高级 A+，并把依据记进 `premium_evidence`。
`--aplus-mode standard|premium` 可强制覆盖判定。

### 宽度从哪读（两种图床格式）

现在亚马逊 A+ 图走的是 `aplus-media-library-service-media` 图床，宽度藏在**裁剪参数**里，
老的那套 `_AC_SX1464_` 已经不适用了：

```
桌面端：.../16056523-8102-....__CR0,0,1464,600_PT0_SX1464_.jpg   -> 1464
移动端：.../16056523-8102-....__CR332,0,800,600_PT0_SX800_.jpg   -> 裁剪成 800
        ^                       x=332  w=800
```

`__CR<x>,<y>,<w>,<h>` 是裁剪矩形。取 **x + w（裁剪右边界）** 作为源宽：
- x=0 时就是真实源宽（桌面端通常如此）
- x>0（移动端居中裁剪）时是源宽**下界** —— 1132 仍 > 970，照样能判出高级 A+

判定时**不要**用 `data-old-hires`（恒为 SL1500，会让所有模块都变成 1500 而全部误判为高级 A+）。

`is_premium_candidate=true` 即本轮按高级 A+ 处理（该值已被 `--aplus-mode` 覆盖过）。
这是启发式判断，重要场景请人工复核 `aplus.html`，或直接用 `--aplus-mode` 指定。

## 反爬建议

1. **首选** `--user-data-dir` 复用已登录的浏览器 profile，验证码率显著下降
2. 中东站（ae / sa）风控比美国站松，同 IP 可跑的量更大
3. `--delay` 别低于 5；批量建议 30~50 个 ASIN 一批，换 IP 再跑
4. 遇到 `Robot Check` 脚本会直接报错退出，不会静默写入脏数据

## 免责

仅用于抓取公开可访问的页面内容做竞品分析，请遵守目标站点的使用条款与当地法律。
