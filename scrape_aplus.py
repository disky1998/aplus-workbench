#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon A+ 内容抓取器（普通 A+ / 高级 A+ Premium A+，桌面端 + 移动端）

默认只抓普通 A+ 与高级 A+，不含品牌故事；需要品牌故事时加 --include-brand-story。

用法示例：
    python scrape_aplus.py --asin B0BHR2PJ6H --domain ae --both
    python scrape_aplus.py --asin-file asins.txt --domain sa --both --download-images
    python scrape_aplus.py --asin B0XXXX --headed --user-data-dir ./profile   # 复用登录态，降验证码率
    python scrape_aplus.py --html-file saved.html --asin B0XXXX               # 只解析本地 HTML，不发网络请求

输出目录：out/<ASIN>/<desktop|mobile>/
    aplus.html      A+ 区域完整 HTML（可直接浏览器打开还原）
    content.json    结构化数据：模块列表、文本、图片 URL、是否为高级 A+
    images/         下载的图片（--download-images）
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import zipfile
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install -r requirements.txt")

try:
    import requests
except ImportError:
    requests = None

import product as prod

# ---------------------------------------------------------------- 选择器配置

# 亚马逊各时期 / 各站点的 A+ 容器 ID，按顺序尝试
APLUS_CONTAINER_IDS = [
    "aplus",                # 现行主容器（普通 A+ 与高级 A+ 都在里面）
    "aplus_feature_div",    # 老版主容器
    "aplus3p_feature_div",  # 第三方 / 供应商内容
]
# 品牌故事默认不抓，需要时加 --include-brand-story
BRAND_STORY_IDS = ["aplusBrandStory_feature_div"]

# 兜底：任何 id / class 里带 aplus 的容器
APLUS_FALLBACK_SELECTORS = [
    '[id*="aplus"]',
    '[id*="a-plus"]',
    '[class*="aplus-module"]',
    '[data-cel-widget*="aplus"]',
]
# 兜底命中的容器里，这些关键词一律排除
EXCLUDE_KEYWORDS = [
    "brandstory", "brand_story",   # 品牌故事（默认排除）
    "sustainabilitystory",         # 可持续故事（也不是 A+）
    "relatedcontent",              # 关联推荐位
    "btffeaturedcontent",          # 底部推荐内容
]
# class 里出现这些也按排除处理（品牌故事的模块 class 带 apm-brand / brand-story）
EXCLUDE_CLASS_RE = re.compile(r"brand[-_]?story|apm-brand|sustainability[-_]?story", re.I)


def active_container_ids(include_brand_story: bool = False) -> list:
    ids = list(APLUS_CONTAINER_IDS)
    if include_brand_story:
        ids += BRAND_STORY_IDS
    return ids


def is_excluded(node_id: str, include_brand_story: bool) -> bool:
    """判断兜底命中的容器是否应排除"""
    low = (node_id or "").lower().replace("-", "").replace("_", "")
    for kw in EXCLUDE_KEYWORDS:
        if kw.replace("_", "") == "brandstory" and include_brand_story:
            continue
        if kw in low:
            return True
    return False
def node_is_excluded(node, include_brand_story: bool = False) -> bool:
    """节点自身**或任一祖先**命中排除关键词 → 排除。

    ⚠️ 必须查祖先链：亚马逊的品牌故事块内部会套一个 <div id="aplus">，
    直接按 id 取 #aplus 会取到品牌故事本身（实测 B08TSSDWFL）。
    """
    cur, depth = node, 0
    while cur is not None and depth < 12:
        if is_excluded(cur.get("id") or "", include_brand_story):
            return True
        cls = " ".join(cur.get("class") or [])
        if not include_brand_story and EXCLUDE_CLASS_RE.search(cls):
            return True
        cur = cur.parent
        depth += 1
    return False


def is_brand_module(m, include_brand_story: bool = False) -> bool:
    """品牌故事模块即使在合法容器里也要剔掉"""
    if include_brand_story:
        return False
    txt = (m.get("id") or "") + " " + " ".join(m.get("class") or [])
    return bool(EXCLUDE_CLASS_RE.search(txt))


def brand_image_set(soup) -> set:
    """页面里所有品牌故事 / 可持续故事的图片 URL（用于兜底过滤）"""
    urls = set()
    for n in soup.select('[id*="brandstory" i], [class*="brandstory" i], '
                         '[id*="brand-story" i], [class*="brand-story" i], '
                         '[id*="sustainabilitystory" i]'):
        for u in extract_images(n):
            urls.add(u)
    return urls


def extract_slides(module) -> list:
    """轮播模块的每一屏（图 + 文案），按 -slide-N 的数字序号排序。

    原页面的轮播是 JS 驱动的横向轨道（viewport overflow:hidden + 固定宽度轨道），
    只克隆 DOM 的话第二屏之后全被裁掉，所以这里显式把每一屏都取出来。
    """
    out = []
    for s in module.select('[id*="-slide-"]'):
        m = re.search(r"-slide-(\d+)\s*$", s.get("id") or "")
        imgs = extract_images(s)
        txt = clean_text(s.get_text(" ", strip=True))
        if not imgs and not txt:
            continue
        out.append({"index": int(m.group(1)) if m else len(out),
                    "images": imgs, "text": txt})
    out.sort(key=lambda x: x["index"])
    if not out:                                  # 没有 -slide-N 命名时退化为卡片
        for i, s in enumerate(module.select(".a-carousel-card")):
            imgs = extract_images(s)
            txt = clean_text(s.get_text(" ", strip=True))
            if imgs or txt:
                out.append({"index": i, "images": imgs, "text": txt})
    return out


# A+ 内部模块（高级 A+ 每个模块是一个 cel_widget）
MODULE_SELECTORS = [
    '[data-cel-widget^="aplus"]',
    ".aplus-module",
    '[class*="aplus-module"]',
]

# 标准 A+ 模块图宽通常 970 / 300；高级 A+（Premium）模块图宽 1464
PREMIUM_WIDTH_HINT = 1464   # 高级 A+（Premium A+）模块设计宽度
STANDARD_WIDTH_MAX = 970     # 普通 A+ 内容区最大宽度，超过即说明是高级 A+
# 高级 A+ 的辅助证据：模块 class 里常带 premium 字样
PREMIUM_CLASS_HINTS = ("premium", "aplus-premium", "premium-aplus")
# 普通 A+ 的类名证据
STANDARD_CLASS_HINTS = ("aplus-standard", "3p-module")
# ⚠️ 判定高级 A+ 的图宽阈值必须 ≥1400：普通 A+ 的模块图宽实测能到 1000px，
#    用 970 当阈值会把普通 A+ 误判成高级 A+（B08TSSDWFL 就是这么错的）
PREMIUM_WIDTH_STRICT = 1400

# 抓取模式：
#   auto     —— 自动判定（普通 A+ 只抓桌面端；高级 A+ 抓桌面端 + 移动端）
#   standard —— 强制按普通 A+ 处理（只抓桌面端）
#   premium  —— 强制按高级 A+ 处理（桌面端 + 移动端都抓）
APLUS_MODES = ("auto", "standard", "premium")


def plan_viewports(mode: str, is_premium: bool) -> list:
    """根据模式与判定结果得出该抓哪些端"""
    if mode == "standard":
        return ["desktop"]
    if mode == "premium":
        return ["desktop", "mobile"]
    return ["desktop", "mobile"] if is_premium else ["desktop"]

# 注意：实测 Windows 版 Chrome UA 会被亚马逊直接拦下（返回 0 张图的空壳页），
# 换成 macOS 版就正常。这里放一个 UA 池，每次随机取一个，降低被识别的概率。
UA_DESKTOP_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/131.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/130.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/129.0.0.0 Safari/537.36"),
]
UA_DESKTOP = UA_DESKTOP_POOL[0]

# 反自动化检测：抹掉 Playwright/ChromeDriver 的明显指纹（有头无头都生效）
STEALTH_JS = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    window.chrome = window.chrome || { runtime: {} };
    const q = window.navigator.permissions && window.navigator.permissions.query;
    if (q) {
      window.navigator.permissions.query = (p) =>
        (p && p.name === 'notifications')
          ? Promise.resolve({ state: 'denied', onchange: null })
          : q.call(window.navigator.permissions, p);
    }
  } catch (e) {}
})();
"""

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Mobile/15E148 Safari/604.1"
)


# ---------------------------------------------------------------- 解析逻辑

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def raw_img_url(img) -> str:
    """取未经高分辨率重写的原始 URL —— src / data-src 才带真实渲染宽度（SX970 / SX1464）。
    data-old-hires 恒为 SL1500，用它判断模块宽度会失真。"""
    u = img.get("src") or img.get("data-src") or img.get("data-old-hires") or ""
    if u.startswith("//"):
        u = "https:" + u
    return u


def img_size_from_url(url: str):
    """从 Amazon 图片 URL 中解析宽度。

    两种格式：
      1) 老图床：.../71abc._AC_SX1464_.jpg            -> 1464
      2) 新 A+ 图床：.../xxx.__CR0,0,1464,600_PT0_SX970_.jpg
         __CR<x>,<y>,<w>,<h> 是裁剪矩形，w 即模块原始设计宽度（1464=高级A+，970=普通A+）
    """
    if not url:
        return None, None
    # 2) 新格式优先：裁剪矩形里的宽高才是模块真实设计尺寸
    m3 = re.search(r"__CR(\d+),(\d+),(\d+),(\d+)[,_]", url)
    if m3:
        x, _y, w, h = (int(g) for g in m3.groups())
        # 返回裁剪右边界：x=0 时即真实源宽；x>0（移动端居中裁剪）时是源宽下界
        return x + w, h
    # 1) 老格式
    m = re.search(r"_AC_(?:S[XY]|US|SL|SS|SY|SR)(\d+)[,_]", url)
    if m:
        return int(m.group(1)), None
    m2 = re.search(r"_AC_SR(\d+),(\d+)_", url)
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    return None, None


def extract_images(node, high_res: bool = True):
    """提取图片 URL。优先取 data-old-hires（原图），其次 data-src（懒加载），最后 src。"""
    urls = []
    for img in node.find_all("img"):
        u = None
        if high_res:
            u = img.get("data-old-hires") or img.get("data-a-dynamic-image")
        if not u:
            u = img.get("data-src") or img.get("src")
        if u and u.startswith("//"):
            u = "https:" + u
        if u and u.startswith("http"):
            u = re.sub(r"\._AC_[A-Z]{2}\d+(?:,\d+)?_\.jpg$", "._AC_SL1500_.jpg", u)
            if u not in urls:
                urls.append(u)
    return urls


def extract_video(node):
    vids = []
    for v in node.find_all(["video", "source"]):
        u = v.get("src") or v.get("data-src")
        if u:
            if u.startswith("//"):
                u = "https:" + u
            vids.append(u)
    return vids


def guess_module_type(module) -> str:
    """粗略判断模块类型：纯图 / 图文 / 对比表 / 纯文本 / 视频"""
    imgs = module.find_all("img")
    vids = module.find_all("video")
    tables = module.find_all("table")
    text = clean_text(module.get_text(" ", strip=True))
    if vids:
        return "video"
    if tables:
        return "comparison-table"
    if imgs and len(text) > 20:
        return "image-text"
    if imgs:
        return "image-only"
    if text:
        return "text-only"
    return "unknown"


def parse_aplus(html: str, asin: str = "", viewport: str = "desktop",
                include_brand_story: bool = False, mode: str = "auto"):
    """解析详情页 HTML，返回 A+ 结构化数据

    mode: auto / standard / premium —— 见 APLUS_MODES，会覆盖自动判定结果
    """
    soup = BeautifulSoup(html, "lxml")

    # 1) 收集所有 A+ 容器
    #    用 find_all 而不是 find：同一页面里可能有多处 id="aplus"
    #    （品牌故事块内部就套了一个），只取第一个会取到品牌故事。
    containers = []
    for cid in active_container_ids(include_brand_story):
        for n in soup.find_all(id=cid):
            if node_is_excluded(n, include_brand_story):
                continue
            containers.append((cid, n))
    if not containers:
        for sel in APLUS_FALLBACK_SELECTORS:
            for n in soup.select(sel):
                if node_is_excluded(n, include_brand_story):
                    continue
                containers.append((sel, n))

    # 去重：如果 aplus 里嵌套了 aplus_feature_div，只保留最外层
    filtered = []
    for name, node in containers:
        if any(node in other.descendants for _, other in containers if other is not node):
            continue
        filtered.append((name, node))
    containers = filtered or containers

    # 注意：这里以前会在「#aplus 有内容」时丢掉 aplus_feature_div —— 实测是错的。
    # 很多页面真正的 A+ 就放在 aplus_feature_div 里，而 #aplus 反而是品牌故事块
    # 内部那个假容器（B08TSSDWFL）。现在品牌故事已按祖先链排除、同名 id 也全遍历，
    # 所以只需要上面的嵌套去重，不要再按名字丢容器。

    # 丢掉空容器（有 A+ 时亚马逊仍会输出空的品牌故事占位 div）
    containers = [
        (name, n) for name, n in containers
        if n.find_all("img") or len(clean_text(n.get_text(" ", strip=True))) >= 20
    ]
    if not containers:
        return {
            "asin": asin, "viewport": viewport, "has_aplus": False,
            "is_premium_candidate": False, "premium_evidence": [],
            "aplus_kind": "none", "mobile_supported": False,
            "aplus_mode": mode if mode in APLUS_MODES else "auto",
            "containers": [], "modules": [],
            "module_count": 0, "image_count": 0, "video_count": 0,
            "max_image_width": None, "note": "A+ 容器存在但全为空，该 ASIN 没有 A+ 内容",
            "images": [], "videos": [], "text": "", "html": "",
        }

    if not containers:
        return {
            "asin": asin, "viewport": viewport, "has_aplus": False,
            "is_premium_candidate": False, "premium_evidence": [],
            "aplus_kind": "none", "mobile_supported": False,
            "aplus_mode": mode if mode in APLUS_MODES else "auto",
            "containers": [], "modules": [],
            "module_count": 0, "image_count": 0, "video_count": 0,
            "max_image_width": None, "note": "页面上没有找到任何 A+ 容器",
            "images": [], "videos": [], "text": "", "html": "",
        }

    html_parts, all_modules, all_images, all_videos, text_parts = [], [], [], [], []
    premium_class_hits: list = []
    standard_class_hits: list = []
    _brand_imgs = set() if include_brand_story else brand_image_set(soup)

    for name, node in containers:
        html_parts.append(str(node))

        mods = []
        for msel in MODULE_SELECTORS:
            found = node.select(msel)
            if found:
                mods = found
                break
        if not mods:
            # 没有标准模块时，按直接子节点切分
            mods = [c for c in node.find_all("div", recursive=False)] or [node]

        for i, m in enumerate(mods):
            # 品牌故事模块即使在合法容器里也要剔掉
            if is_brand_module(m, include_brand_story):
                continue
            slides = extract_slides(m)
            if slides:
                # 轮播：按 slide 顺序取图（DOM 里 slide-2 可能排在 slide-0 前面）
                imgs = [u for s in slides for u in s["images"]]
            else:
                imgs = extract_images(m)
            t = clean_text(m.get_text(" ", strip=True))
            if not imgs and len(t) < 20:          # aplus-mantle 这类空壳不计入
                continue
            # 模块宽度取「模块内所有图的最大宽度」——只看第一张会被小图标带偏
            w = None
            for _img in m.find_all("img"):
                _bw, _ = img_size_from_url(raw_img_url(_img))
                if _bw and (w is None or _bw > w):
                    w = _bw
            # 高级 A+ / 普通 A+ 的类名证据
            cls = " ".join(m.get("class") or []).lower()
            if any(h in cls for h in PREMIUM_CLASS_HINTS) and cls not in premium_class_hits:
                premium_class_hits.append(cls[:60])
            if any(h in cls for h in STANDARD_CLASS_HINTS) and cls not in standard_class_hits:
                standard_class_hits.append(cls[:60])
            # 页面里存在重复 id（同一个 #aplus 出现两次），不去重会把图片数翻倍
            for _u in imgs:
                if _u not in all_images:
                    all_images.append(_u)
            all_videos.extend(extract_video(m))
            text_parts.append(t)
            all_modules.append({
                "index": i,
                "container": name,
                "type": "carousel" if slides else guess_module_type(m),
                "text": t,
                "images": imgs,
                "slides": slides,
                "slide_count": len(slides),
                "videos": extract_video(m),
                "image_width_hint": w,
            })

        # 兜底：把模块切分没覆盖到的图片也收进来（品牌故事的图排除掉）
        for u in extract_images(node):
            if u in _brand_imgs or u in all_images:
                continue
            all_images.append(u)

    # 2) 是否高级 A+：**类名证据优先**，图宽只作兜底
    #    （普通 A+ 的模块图宽实测能到 1000px，按图宽判会误判）
    widths = [m["image_width_hint"] for m in all_modules if m["image_width_hint"]]
    mx = max(widths) if widths else 0
    evidence: list = []
    if premium_class_hits:
        evidence.append(f"高级 A+ 类名：{premium_class_hits[0]}")
    elif mx >= PREMIUM_WIDTH_STRICT:
        evidence.append(f"模块图宽 {mx}px ≥ {PREMIUM_WIDTH_STRICT}px（高级 A+ 设计宽度）")

    mode = mode if mode in APLUS_MODES else "auto"
    if mode == "premium":
        is_premium = True
        evidence = ["手动指定：高级 A+"]
    elif mode == "standard":
        is_premium = False
        evidence = ["手动指定：普通 A+"]
    else:
        is_premium = bool(evidence)
        if not evidence:
            kind_hint = f"普通 A+ 类名：{standard_class_hits[0]}" if standard_class_hits \
                else "无 premium 类名"
            evidence = [f"{kind_hint}，最大图宽 {mx or '-'}px"
                        f"（未达 {PREMIUM_WIDTH_STRICT}px）"]

    # 2b) 移动端会把 1464 的图居中裁剪成 x=332,w=800，此时取 x+w（裁剪右边界）
    #     作为源图宽度下界即可判定：>970 就不可能是普通 A+
    # 3) 空壳判定：容器存在但里面没图没字，说明该 ASIN 页面根本没渲染 A+
    #    （亚马逊会先输出 data-csa-c-slot-id 占位 div，有 A+ 时才异步填充）
    joined_text = clean_text(" ".join(text_parts))
    is_shell = not all_images and len(joined_text) < 20
    note = None
    if is_shell:
        note = ("A+ 容器是空壳，该 ASIN 页面上没有渲染出 A+ 内容。若商品只做了品牌故事，"
                "请加 --include-brand-story 重试")

    return {
        "asin": asin,
        "viewport": viewport,
        "has_aplus": not is_shell,
        "note": note,
        "is_premium_candidate": is_premium,
        "premium_evidence": evidence,
        "aplus_kind": ("none" if is_shell else ("premium" if is_premium else "standard")),
        "mobile_supported": (not is_shell) and is_premium,
        "aplus_mode": mode,
        "max_image_width": max(widths) if widths else None,
        "module_count": len(all_modules),
        "image_count": len(all_images),
        "video_count": len(all_videos),
        "containers": [n for n, _ in containers],
        "modules": all_modules,
        "images": all_images,
        "videos": all_videos,
        "text": joined_text,
        "html": "\n".join(html_parts),
    }


# ---------------------------------------------------------------- 图片下载与打包

IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|bmp)(?:\?|$)", re.I)


def ext_from(url: str = "", content_type: str = "") -> str:
    """先看响应 Content-Type，再退回 URL 后缀，最后默认 .jpg"""
    ct = (content_type or "").lower()
    for key, ext in (("png", ".png"), ("webp", ".webp"), ("gif", ".gif"),
                     ("bmp", ".bmp"), ("jpeg", ".jpg"), ("jpg", ".jpg")):
        if key in ct:
            return ext
    m = IMG_EXT_RE.search(url or "")
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg")
    return ".jpg"


def image_url_for_download(url: str, hires: int = 0) -> str:
    """把 URL 里的 _SX970_ 提升到指定宽度（保留 __CR 裁剪参数，构图不变）"""
    if not url or not hires:
        return url
    return re.sub(r"_SX\d+_", f"_SX{int(hires)}_", url)


def find_local_image(imgdir: Path, idx: int):
    """找 images/ 下第 idx 张本地文件（命名形如 00.jpg）"""
    if not imgdir.is_dir():
        return None
    for p in sorted(imgdir.iterdir()):
        if p.is_file() and p.name.startswith(f"{idx:02d}."):
            return p
    return None


def _download_one(ctx, url: str, dest_noext: Path, timeout: int = 30000):
    """下载单张图片。优先走浏览器上下文的请求（自动带 cookie / UA / 代理），
    失败再退回 requests / urllib。返回 (Path|None, 字节数, 错误信息)"""
    # 1) Playwright 上下文请求
    if ctx is not None:
        try:
            resp = ctx.request.get(url, timeout=timeout)
            if resp.ok:
                body = resp.body()
                if body:
                    p = Path(str(dest_noext) + ext_from(url, (resp.headers or {}).get("content-type", "")))
                    p.write_bytes(body)
                    return p, len(body), None
            else:
                last = f"HTTP {resp.status}"
        except Exception as e:
            last = f"context.request: {e}"
    else:
        last = "无浏览器上下文"

    # 2) requests
    if requests is not None:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA_DESKTOP})
            if r.ok and r.content:
                p = Path(str(dest_noext) + ext_from(url, r.headers.get("content-type", "")))
                p.write_bytes(r.content)
                return p, len(r.content), None
            last = f"requests HTTP {r.status_code}"
        except Exception as e:
            last = f"requests: {e}"

    # 3) urllib 兜底
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA_DESKTOP})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        if body:
            p = Path(str(dest_noext) + ext_from(url, r.headers.get("content-type", "")))
            p.write_bytes(body)
            return p, len(body), None
    except Exception as e:
        last = f"urllib: {e}"
    return None, 0, last


def save_images(ctx, urls, imgdir: Path, hires: int = 0,
                skip_existing: bool = False) -> tuple:
    """下载图片清单到 imgdir，命名 00.jpg / 01.png ...

    返回 (成功张数, 失败张数)
    """
    imgdir.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for i, u in enumerate(urls or []):
        if skip_existing and find_local_image(imgdir, i):
            ok += 1
            continue
        target = image_url_for_download(u, hires)
        p, _n, err = _download_one(ctx, target, imgdir / f"{i:02d}")
        if p:
            ok += 1
        else:
            fail += 1
            print(f"    [warn] 图片下载失败 {u[:70]} -> {err}")
    return ok, fail


def meta_for(out_root: Path, sku: str, vp: str) -> dict:
    p = out_root / sku / vp / "content.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _url_module_info(meta: dict) -> dict:
    """建立 图片URL -> {type, width} 映射，供 manifest 标注模块类型"""
    info = {}
    for m in meta.get("modules") or []:
        for u in m.get("images") or []:
            info.setdefault(u, {"type": m.get("type") or "",
                                "width": m.get("image_width_hint") or ""})
    return info


def package_images(out_root: Path, items, zip_path: Path, ctx=None,
                   download_missing: bool = False, hires: int = 0) -> dict:
    """按 content.json 的图片清单打包 A+ 图片，并附 manifest.csv

    items: [(sku, asin, domain), ...]
    download_missing: 本地缺图时用浏览器上下文补下载（True 时需要 ctx）

    返回 {zip, count, downloaded, missing, skus}
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    rows, added, downloaded, missing = [], 0, 0, 0
    skus = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for sku, asin, dom in items:
            hit_any = False
            for vp in ("desktop", "mobile"):
                meta = meta_for(out_root, sku, vp)
                if not meta:
                    continue
                urls = meta.get("images") or []
                imgdir = out_root / sku / vp / "images"
                uinfo = _url_module_info(meta)
                for i, u in enumerate(urls):
                    local = find_local_image(imgdir, i)
                    if local is None and download_missing:
                        local, _n, _err = _download_one(
                            ctx, image_url_for_download(u, hires), imgdir / f"{i:02d}")
                        if local:
                            downloaded += 1
                    if local:
                        z.write(local, f"{sku}/A+/{vp}/{local.name}")
                        added += 1
                        hit_any = True
                        name = local.name
                    else:
                        missing += 1
                        name = ""
                    mi = uinfo.get(u) or {}
                    rows.append({
                        "SKU": sku, "ASIN": asin, "站点": dom, "视图": vp,
                        "序号": i, "文件名": name,
                        "模块类型": mi.get("type", ""),
                        "图片宽度": mi.get("width", ""),
                        "原始URL": u,
                    })
            if hit_any or meta_for(out_root, sku, "desktop"):
                skus.append(sku)

        if rows:
            import io as _io
            buf = _io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
            z.writestr("manifest.csv", buf.getvalue().encode("utf-8-sig"))
            z.writestr("README.txt",
                       ("Amazon A+ 图片包\n"
                        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"SKU 数：{len(set(r['SKU'] for r in rows))}\n"
                        f"图片文件：{added}\n\n"
                        "目录结构：<SKU>/A+/<desktop|mobile>/<序号>.<扩展名>\n"
                        "manifest.csv 记录了每张图的来源 URL、所属模块类型与设计宽度。\n"
                        "普通 A+ 只有 desktop（无独立移动端版本）；高级 A+ 含 desktop + mobile。\n"
                        ).encode("utf-8"))

    if added == 0:
        zip_path.unlink(missing_ok=True)
        return {"zip": None, "count": 0, "downloaded": downloaded,
                "missing": missing, "skus": skus, "empty": True}
    return {"zip": str(zip_path), "count": added, "downloaded": downloaded,
            "missing": missing, "skus": skus, "empty": False}


# ---------------------------------------------------------------- 浏览器抓取

CAPTCHA_TEXT = [
    "enter the characters you see below",
    "type the characters you see in this image",
    "sorry, we just need to make sure you're not a robot",
]


def is_captcha(page) -> bool:
    try:
        if page.locator("#captchacharacters").count() > 0:
            return True
        if page.locator('form[action*="validateCaptcha"]').count() > 0:
            return True
        title = (page.title() or "").lower()
        if "robot check" in title or "captcha" in title:
            return True
        body = page.evaluate("() => (document.body && document.body.innerText) || ''") or ""
        low = body.lower()
        if any(t in low for t in CAPTCHA_TEXT):
            return True
        return False
    except Exception:
        return False


def looks_blocked(page) -> bool:
    """滚动完之后再判断一次：页面上既没有商品标题也没有图片，基本就是被拦了。
    亚马逊的验证码页不一定带 captcha 字样（可能只是个空壳 div），
    只靠 title 判断会漏掉，导致静默产出空结果。"""
    try:
        has_title = page.locator("#productTitle").count() > 0
        n_img = page.evaluate("() => document.querySelectorAll('img').length") or 0
        return (not has_title) and n_img == 0
    except Exception:
        return False


def navigate(page, url: str, scroll: bool = True, timeout: int = 45000,
             include_brand_story: bool = False, wait_aplus: bool = True):
    """在**已有页面**上打开 URL 并等 A+ 渲染完。

    与 prepare_page 的区别：这里不创建、也不关闭页面 —— 池化模式复用的正是
    预热好的标签页，页面所有权归调用方。
    """
    page.set_default_timeout(timeout)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    if is_captcha(page):
        raise RuntimeError("触发亚马逊验证码（Robot Check）。建议：勾选「有头模式」并先点"
                           "「预热浏览器」，在弹出窗口里过掉验证码（会话会被记住），"
                           "或加代理、调大间隔")

    # 滚动整页，触发 A+ 图片懒加载
    if scroll:
        for _ in range(14):
            page.mouse.wheel(0, random.randint(700, 1200))
            page.wait_for_timeout(random.randint(300, 600))
        page.wait_for_timeout(1200)

    if not wait_aplus:
        return page

    # 等 A+ 容器出现，并且要等里面的内容真正被 JS 填进来。
    # 亚马逊的 aplus_feature_div 常先渲染成一个空壳（data-csa-c-slot-id 占位），
    # 滚动到视口后才由异步请求填充，只等元素出现会拿到空 div。
    for cid in active_container_ids(include_brand_story):
        try:
            page.wait_for_selector(f"#{cid}", timeout=5000)
        except Exception:
            continue
        try:
            page.locator(f"#{cid}").first.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        if wait_aplus_filled(page, cid, timeout=12000):
            break
        # 空壳：再滚一轮把它顶进视口，重试一次
        for _ in range(6):
            page.mouse.wheel(0, random.randint(600, 900))
            page.wait_for_timeout(random.randint(300, 500))
        page.wait_for_timeout(1500)
        if wait_aplus_filled(page, cid, timeout=12000):
            break
    return page


def prepare_page(context, url: str, scroll: bool = True, timeout: int = 45000,
                 include_brand_story: bool = False, wait_aplus: bool = True):
    """新建页面并打开（非池化模式用）—— 失败时自己收尾"""
    page = context.new_page()
    try:
        return navigate(page, url, scroll=scroll, timeout=timeout,
                        include_brand_story=include_brand_story,
                        wait_aplus=wait_aplus)
    except Exception:
        try:
            page.close()
        except Exception:
            pass
        raise


def wait_aplus_filled(page, cid: str, timeout: int = 12000) -> bool:
    """等待指定容器内出现实质内容（图片或文字），返回是否等到"""
    js = """
    (cid) => {
        const el = document.getElementById(cid);
        if (!el) return false;
        const imgs = el.querySelectorAll('img');
        const txt = (el.innerText || '').trim();
        return imgs.length > 0 || txt.length > 50;
    }
    """
    try:
        page.wait_for_function(js, arg=cid, timeout=timeout)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 排版还原

def wait_images_loaded(page, ids, timeout: int = 20000) -> bool:
    """等待 A+ 容器内所有 <img> 真正下载完成（complete 且 naturalWidth>0）。

    不等这一步就克隆排版，会把"未加载时的 0~1px 高度"冻结进内联样式，
    导致合并页里图片全部塌成一条细线（TS0400 的 970x600 大 PNG 尤其容易触发）。
    """
    js = """
    (ids) => {
      const imgs = [];
      ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.querySelectorAll('img').forEach(im => imgs.push(im));
      });
      if (!imgs.length) return true;
      return imgs.every(im => im.complete &&
        (im.naturalWidth > 0 || !im.getAttribute('src')));
    }
    """
    try:
        page.wait_for_function(js, arg=list(ids), timeout=timeout)
        return True
    except Exception:
        return False


# 在页面内执行：克隆 A+ 容器，把每个元素的「计算样式」内联进去。
# 这样产出的 HTML 不依赖亚马逊的 CSS 文件，单独打开也是原排版。
JS_RENDER = r"""
(cfg) => {
  const PROPS = [
    "display","position","top","left","right","bottom","float","clear","z-index","opacity",
    "width","height","min-width","min-height","max-width","max-height","box-sizing",
    "margin-top","margin-right","margin-bottom","margin-left",
    "padding-top","padding-right","padding-bottom","padding-left",
    "border-top-width","border-right-width","border-bottom-width","border-left-width",
    "border-top-style","border-right-style","border-bottom-style","border-left-style",
    "border-top-color","border-right-color","border-bottom-color","border-left-color",
    "border-top-left-radius","border-top-right-radius",
    "border-bottom-left-radius","border-bottom-right-radius",
    "overflow","overflow-x","overflow-y","box-shadow","text-shadow",
    "background-color","background-image","background-size","background-position","background-repeat",
    "color","font-family","font-size","font-weight","font-style","line-height","letter-spacing",
    "text-align","text-decoration","text-transform","white-space","word-break","vertical-align",
    "flex-direction","flex-wrap","justify-content","align-items","align-self",
    "flex-grow","flex-shrink","flex-basis","order",
    "grid-template-columns","grid-template-rows","gap","column-gap","row-gap",
    "object-fit","object-position","transform","transform-origin",
    "list-style-type","list-style-position","table-layout","border-collapse"
  ];

  const hasContent = (el) => !!el && (
      el.querySelectorAll("img").length > 0 ||
      (el.innerText || "").trim().length >= 20);

  // 品牌故事 / 关联推荐：自身或任一祖先命中关键词就排除
  // （亚马逊会把 id="aplus" 套在品牌故事块内部，只按 id 取会取到品牌故事）
  const EXCL_CLS = /brand[-_]?story|apm-brand|sustainability[-_]?story/i;
  const isExcluded = (el) => {
    let cur = el, d = 0;
    while (cur && d < 12) {
      const id = (cur.id || "").toLowerCase().replace(/[-_]/g, "");
      for (const kw of (cfg.exclude || [])) if (id.indexOf(kw) > -1) return true;
      const cls = (cur.getAttribute && cur.getAttribute("class")) || "";
      if (!cfg.brandStory && EXCL_CLS.test(cls)) return true;
      cur = cur.parentElement; d++;
    }
    return false;
  };

  // 轮播离线化：原页面用 JS 驱动横向轨道（viewport overflow:hidden + 轨道固定宽度，
  // 实测 3510px），只克隆 DOM 的话第二屏之后全被裁掉、看不到。
  // 这里改成「横向可滚动的静态排布」，每一屏的图和版式都完整保留。
  const normalizeCarousel = (root) => {
    const set = (el, prop, val) => el.style.setProperty(prop, val, "important");
    root.querySelectorAll(".a-carousel-viewport, .a-carousel-container").forEach(vp => {
      set(vp, "overflow", "visible");
      set(vp, "overflow-x", "auto");
      set(vp, "height", "auto");
      set(vp, "max-height", "none");
      set(vp, "width", "100%");
      const tracks = vp.querySelectorAll("ol.a-carousel, ul.a-carousel");
      const fixTrack = (tr) => {
        set(tr, "display", "flex");
        set(tr, "flex-wrap", "nowrap");
        set(tr, "gap", "10px");
        set(tr, "width", "auto");
        set(tr, "max-width", "none");
        set(tr, "margin", "0");
        set(tr, "padding", "0");
        set(tr, "transform", "none");
        set(tr, "left", "auto");
        set(tr, "position", "static");
        set(tr, "list-style", "none");
      };
      if (tracks.length) tracks.forEach(fixTrack); else fixTrack(vp);
      vp.querySelectorAll(".a-carousel-card, li").forEach(li => {
        set(li, "flex", "0 0 auto");
        set(li, "width", "auto");
        set(li, "max-width", "100%");
        set(li, "margin", "0");
        set(li, "position", "static");
        set(li, "transform", "none");
        set(li, "visibility", "visible");
        set(li, "left", "auto");
      });
    });
    // 分页圆点 / 首屏索引这类 JS 控件在离线页里没有意义
    root.querySelectorAll(
      "input.a-carousel-firstvisibleitem, .a-carousel-firstvisibleitem, .aplus-pagination-dots"
    ).forEach(n => n.remove());
  };

  // 1) 找到有内容的容器（与 Python 侧解析逻辑保持一致）
  let roots = [];
  for (const id of cfg.ids) {
    // 同一 id 可能出现多次（品牌故事内部也套了一个 id="aplus"），必须全取
    document.querySelectorAll('[id="' + id + '"]').forEach(el => {
      if (isExcluded(el) || !hasContent(el)) return;
      roots.push({ id: id, el: el });
    });
  }
  // 嵌套去重：#aplus 常常嵌在 #aplus_feature_div 里，两个都收会抓两遍
  roots = roots.filter(r => !roots.some(o => o !== r && o.el.contains(r.el)));
  if (!roots.length) return null;

  // 2) 清掉会干扰离线渲染的节点（原页面与克隆体同步删除，保证两棵树结构一致）
  const JUNK = "script,iframe,noscript,link,input,button,select,textarea";
  roots.forEach(r => r.el.querySelectorAll(JUNK).forEach(n => n.remove()));

  // 3) 逐个克隆 + 内联计算样式
  const frag = document.createDocumentFragment();
  let img_total = 0, img_unloaded = 0, img_squashed = 0;
  for (const r of roots) {
    const root = r.el;
    const clone = root.cloneNode(true);
    const ow = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    const cw = document.createTreeWalker(clone, NodeFilter.SHOW_ELEMENT);
    for (;;) {
      const o = ow.nextNode(), c = cw.nextNode();
      if (!o || !c) break;
      const cs = getComputedStyle(o);
      let css = "";
      for (const p of PROPS) {
        const v = cs.getPropertyValue(p);
        if (!v) continue;
        css += p + ":" + v + ";";
      }
      // 图片还没下载完时，计算出的 width/height 是占位的 0~1px，
      // 内联进去会永久锁死尺寸 —— 直接剔除这两项，交给 CSS height:auto 撑开
      if (c.tagName === "IMG" && (!o.complete || o.naturalWidth === 0)) {
        css = css.replace(/(^|;)(width|height):[^;]*;/g, "$1");
      }
      c.setAttribute("style", css);

      if (c.tagName === "IMG") {
        // 体检：统计"没下载完"和"已下载但被压扁"的图片，供 Python 侧决定是否重抓
        img_total++;
        const rect = o.getBoundingClientRect();
        if (!o.complete || o.naturalWidth === 0) {
          img_unloaded++;
        } else if (rect.height < 20 && o.naturalHeight > 20) {
          img_squashed++;
        }
        let src = c.getAttribute("src") || c.getAttribute("data-src") || "";
        // 占位用的 base64 灰图直接换掉
        if (!src || src.indexOf("data:image") === 0) {
          src = c.getAttribute("data-src") || c.getAttribute("data-old-hires") || "";
        }
        if (src && src.indexOf("data:image") === 0) src = "";
        // 提高清晰度：只放大 URL 里的 SX 尺寸，保留 __CR 裁剪参数以免改变构图
        if (src && cfg.hires) src = src.replace(/_SX\d+_/, "_SX" + cfg.hires + "_");
        if (src.indexOf("//") === 0) src = "https:" + src;
        if (src) c.setAttribute("src", src);
        c.removeAttribute("srcset");
        c.removeAttribute("data-a-dynamic-image");
        c.removeAttribute("data-old-hires");
        c.removeAttribute("onload");
        c.setAttribute("style", (c.getAttribute("style") || "") + "max-width:100%;");
      }
    }
    normalizeCarousel(clone);
    frag.appendChild(clone);
  }

  const box = document.createElement("div");
  box.appendChild(frag);
  // 用容器实际渲染宽度做外层包裹，才能 1:1 还原（高级 A+ 桌面端约 1404，移动端约 362）
  let width = 0;
  roots.forEach(r => {
    const w = r.el.offsetWidth || Math.round(r.el.getBoundingClientRect().width);
    if (w > width) width = w;
  });
  return {
    html: box.innerHTML,
    width: Math.round(width),
    stats: { img_total, img_unloaded, img_squashed }
  };
}
"""


def build_render_html(page, asin: str, viewport: str, wrap_width,
                      include_brand_story: bool = False, hires: int = 0,
                      max_retry: int = 3):
    """在浏览器里把 A+ 区域克隆出来并内联计算样式，产出可独立打开的还原页。

    带质量自检：若克隆时仍有图片没下载完 / 被压扁，就再等一轮重新克隆，
    最多 max_retry 次，取体检最好的一次（避免大图慢加载把尺寸冻成 1px）。
    返回 (html, stats)
    """
    ids = active_container_ids(include_brand_story)
    best, best_score = None, -1

    for i in range(max_retry):
        if i:
            time.sleep(1.5)
            page.mouse.wheel(0, 300)          # 再顶一下，催懒加载
            page.wait_for_timeout(800)
        wait_images_loaded(page, ids, timeout=15000 if i == 0 else 20000)
        try:
            res = page.evaluate(JS_RENDER, {
                "ids": ids,
                "hires": int(hires or 0),
                "brandStory": bool(include_brand_story),
                "exclude": [k.replace("_", "") for k in EXCLUDE_KEYWORDS],
            })
        except Exception:
            res = None
        if not res or not res.get("html"):
            continue
        st = res.get("stats") or {}
        bad = int(st.get("img_unloaded", 0)) + int(st.get("img_squashed", 0))
        score = -bad
        if bad == 0:
            best, best_score = res, score
            break
        if score > best_score:
            best, best_score = res, score

    if not best:
        return "", {}

    stats = best.get("stats") or {}
    body = best["html"]
    # 优先用容器实际渲染宽度，抓不到再退回解析出的图宽
    w = best.get("width") or wrap_width or 970
    if w < 300:
        w = wrap_width or 970
    vw = "width=device-width, initial-scale=1" if viewport == "mobile" else f"width={w}"
    doc = (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta name="viewport" content="{vw}">\n'
        f"<title>{asin} · A+ ({viewport})</title>\n"
        "<style>\n"
        "html,body{margin:0;padding:0;background:#fff;}\n"
        "body{font-family:'Amazon Ember',Arial,Helvetica,sans-serif;}\n"
        f".aplus-page{{width:{w}px;max-width:100%;margin:0 auto;background:#fff;}}\n"
        ".aplus-page a{pointer-events:none;text-decoration:none;color:inherit;}\n"
        "img{max-width:100%;height:auto !important;}\n"
        "</style></head>\n<body>\n"
        f'<div class="aplus-page">\n{body}\n</div>\n</body></html>\n'
    )
    return doc, stats


def fetch_one(browser_ctx_factory, asin: str, domain: str, viewport: str,
              scroll: bool, out_root: Path, download_images: bool,
              include_brand_story: bool = False, render: bool = False,
              screenshot: bool = False, hires: int = 0,
              folder: str | None = None, state_file: str | None = None,
              mode: str = "auto", want: str = "all",
              page=None, browser=None):
    """抓一个 SKU 的一个视图。

    want: all      —— 商品链接信息 + A+ 内容
          product  —— 只要商品链接信息（不解析 A+，快很多）
          aplus    —— 只要 A+ 内容

    页面来源三选一：
      page     —— 复用预热池里已存在的标签页（桌面端，池化模式；调用方拥有，不关）
      browser  —— CDP 连上的浏览器，临时新开上下文（移动端用，UA/视口必须换）
      都为空    —— 走 browser_ctx_factory（非池化模式）
    """
    key = folder or asin
    url = f"https://www.amazon.{domain}/dp/{asin}"
    own = page is None          # 页面/上下文是否由本函数创建并负责关闭
    ctx = None
    try:
        if own:
            if browser is not None:
                if viewport == "mobile":
                    ctx = browser.new_context(
                        user_agent=UA_MOBILE,
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=3, is_mobile=True, has_touch=True,
                        locale="en-US")
                else:
                    ctx = browser.new_context(
                        user_agent=random.choice(UA_DESKTOP_POOL),
                        viewport={"width": 1440, "height": 2400},
                        locale="en-US")
            else:
                ctx = browser_ctx_factory(viewport)
            # 抹掉自动化指纹（每个 context 注入一次即可）
            try:
                ctx.add_init_script(STEALTH_JS)
            except Exception:
                pass
            page = prepare_page(ctx, url, scroll=scroll,
                                include_brand_story=include_brand_story,
                                wait_aplus=(want != "product"))
        else:
            # 复用预热标签页：只导航，不创建也不关闭
            ctx = page.context
            navigate(page, url, scroll=scroll,
                     include_brand_story=include_brand_story,
                     wait_aplus=(want != "product"))
        # 持久化上下文 / 池化标签页的 viewport 不固定，移动端要单独纠正
        if viewport == "mobile":
            try:
                page.set_viewport_size({"width": 390, "height": 844})
            except Exception:
                pass
        if is_captcha(page) or looks_blocked(page):
            raise RuntimeError("被亚马逊拦截（验证码页 / 空页面）。建议勾选「有头模式」并先"
                               "点「预热浏览器」，过掉验证码后会话会被记住；"
                               "或加代理、调大间隔")

        # ---- 商品链接信息（与 A+ 共用同一个页面，只多跑一段 JS）----
        product_data = None
        if want in ("all", "product") and viewport == "desktop":
            product_data = prod.extract_product(page)
            if product_data.get("error"):
                print(f"    [warn] 商品信息：{product_data['error']}")
                if product_data.get("blocked"):
                    raise RuntimeError(product_data["error"])
                product_data = None
            else:
                product_data.update({
                    "url": url, "asin": asin, "domain": domain,
                    "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                pdir = out_root / key
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / "product.json").write_text(
                    json.dumps(product_data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"    [商品] 标题{'✓' if product_data.get('title') else '×'} "
                      f"亮点{'✓' if product_data.get('highlights') else '×'} "
                      f"类目{'✓' if product_data.get('category') else '×'} "
                      f"NodeID{'✓' if product_data.get('nodeId') else '×'} "
                      f"五点 {len(product_data['bullets'])} 条 "
                      f"主图 {len(product_data['images'])} 张")

        if want == "product":
            return {"asin": asin, "viewport": viewport, "url": url,
                    "product": product_data, "product_only": True}

        html = page.content()
        data = parse_aplus(html, asin=asin, viewport=viewport,
                           include_brand_story=include_brand_story, mode=mode)

        # 亚马逊偶尔会返回「没有 A+ 容器」的简化版页面（同一个链接换个时间抓就不一样，
        # 实测 B0F4X77JZW 无 th=1 时抓到过 531KB 的无 A+ 页面）。容器不在就重载再解析。
        if want != "product" and not data.get("has_aplus"):
            for _try in range(2):
                try:
                    print(f"    [重试] 未找到 A+ 容器，重新加载页面（第 {_try + 1} 次）")
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                    for _ in range(10):
                        page.mouse.wheel(0, random.randint(700, 1200))
                        page.wait_for_timeout(350)
                    page.wait_for_timeout(1500)
                    html2 = page.content()
                    d2 = parse_aplus(html2, asin=asin, viewport=viewport,
                                     include_brand_story=include_brand_story, mode=mode)
                    if d2.get("has_aplus"):
                        html, data = html2, d2
                        print("    [重试] 重新加载后已拿到 A+ 内容")
                        break
                except Exception as e:
                    print(f"    [warn] A+ 重试失败 -> {e}")
                    break
        data["url"] = url
        data["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if product_data:
            data["product"] = product_data

        d = out_root / key / viewport
        d.mkdir(parents=True, exist_ok=True)

        # 排版还原页（需在页面还活着的时候做）
        if render and data.get("has_aplus"):
            try:
                w = data.get("max_image_width") or 970
                rh, rstats = build_render_html(page, asin, viewport, w,
                                               include_brand_story, hires)
                if rh:
                    (d / "render.html").write_text(rh, encoding="utf-8")
                if rstats:
                    data["render_quality"] = rstats
                    bad = int(rstats.get("img_unloaded", 0)) + int(rstats.get("img_squashed", 0))
                    if bad:
                        data["render_warning"] = (
                            f"{bad}/{rstats.get('img_total', 0)} 张图抓取时未加载完，"
                            f"排版可能不完整，建议重跑该 SKU")
                        print(f"    [warn] 排版体检：{data['render_warning']}")
            except Exception as e:
                print(f"    [warn] 排版还原失败 -> {e}")

        if screenshot and data.get("has_aplus"):
            try:
                for cid in data.get("containers", []):
                    loc = page.locator(f"#{cid}").first
                    if loc.count():
                        loc.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(600)
                        loc.screenshot(path=str(d / f"aplus_{cid[:24]}.png"))
            except Exception as e:
                print(f"    [warn] 截图失败 -> {e}")

        (d / "aplus.html").write_text(data.pop("html") or "", encoding="utf-8")
        (d / "content.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if download_images and data.get("images"):
            got, failed = save_images(ctx, data["images"], d / "images", hires=hires)
            data["downloaded_images"] = got
            if failed:
                data["download_failed"] = failed
                print(f"    [warn] {failed} 张图片下载失败（重跑该 SKU 可补齐）")
            print(f"    [图片] 已下载 {got}/{len(data['images'])} 张 -> {d / 'images'}")
        return data
    finally:
        # 只有自己创建的页面 / 上下文才负责收尾；池化标签页要留着下一次用
        if own:
            # 关浏览器前把登录态落盘，下次任务直接复用（少挨验证码）
            if state_file and ctx is not None:
                try:
                    ctx.storage_state(path=str(state_file))
                except Exception:
                    pass
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass


# ---------------------------------------------------------------- 合并成一个 HTML

def load_render_section(asin_dir: Path, vp: str, meta: dict | None = None):
    """取出某个视图的 A+ 主体片段和包裹宽度。

    优先用 render.html（已把计算样式内联，可离线 1:1 还原排版）；
    没有时退回 aplus.html（亚马逊原始片段，样式依赖在线 CSS）。
    """
    meta = meta or {}
    fallback_w = meta.get("max_image_width") or 970

    rf = asin_dir / vp / "render.html"
    if rf.exists():
        s = rf.read_text(encoding="utf-8")
        m = re.search(r'<div class="aplus-page"[^>]*>\s*(.*?)\s*</div>\s*</body>', s, re.S)
        if not m:  # 兜底：直接取 body 内容
            m = re.search(r"<body[^>]*>\s*(.*?)\s*</body>", s, re.S)
        inner = (m.group(1) if m else "").strip()
        if inner:
            mw = re.search(r"\.aplus-page\{width:(\d+)px", s)
            return {"html": inner,
                    "width": int(mw.group(1)) if mw else fallback_w,
                    "source": "render.html"}

    af = asin_dir / vp / "aplus.html"
    if af.exists():
        inner = af.read_text(encoding="utf-8").strip()
        if inner:
            return {"html": inner, "width": fallback_w, "source": "aplus.html"}
    return None


def _meta(asin_dir: Path, vp: str) -> dict:
    p = asin_dir / vp / "content.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


CSS_COMBINED = """
*{box-sizing:border-box}
body{margin:0;background:#f2f0ec;color:#2c2c2a;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",Arial,sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:1500px;margin:0 auto;padding:0 20px 60px}
h1{font-size:22px;font-weight:600;margin:0}
.head{position:sticky;top:0;z-index:50;background:#f2f0ecf2;backdrop-filter:blur(6px);
  padding:16px 0 10px;border-bottom:1px solid #ddd8d0;margin-bottom:18px}
.head .sub{color:#888780;font-size:12px;margin-top:3px}
.nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.nav a{font-size:12px;padding:3px 10px;border:1px solid #ddd8d0;border-radius:20px;
  background:#fff;color:#444;text-decoration:none}
.nav a:hover{border-color:#888780}
table.sum{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e0dcd4;
  border-radius:10px;overflow:hidden;margin-bottom:8px}
table.sum th{text-align:left;padding:9px 11px;background:#f7f5f1;font-weight:600;font-size:12px;color:#5f5e5a}
table.sum td{padding:9px 11px;border-top:1px solid #eae6de;font-size:13px}
.tag{padding:1px 8px;border-radius:5px;font-size:12px;white-space:nowrap}
.tag.pre{background:#e1f5ee;color:#0f6e56}
.tag.std{background:#eeece6;color:#5f5e5a}
.tag.none{background:#fcebeb;color:#a32d2d}
.sku{margin:34px 0 0;border-top:2px solid #2c2c2a;padding-top:14px}
.sku h2{font-size:18px;margin:0 0 6px;font-weight:600;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sku h2 a{font-size:13px;font-weight:400;color:#666}
.sku .stats{color:#6b6a66;font-size:12.5px;margin-bottom:12px}
.vp{margin:16px 0 0}
.vp > .vplabel{font-size:13px;font-weight:600;color:#5f5e5a;margin:0 0 8px;
  display:flex;align-items:center;gap:8px}
.vp > .vplabel:before{content:"";width:3px;height:13px;background:#888780;border-radius:2px}
.host{background:#fff;border:1px solid #e0dcd4;border-radius:10px;overflow:hidden;margin:0 auto}
.skipnote{background:#fff8e6;border-left:3px solid #b8860b;padding:9px 12px;border-radius:6px;
  font-size:12.5px;color:#6b5410;margin-top:10px}
.emptybox{background:#fcebeb;border-left:3px solid #a32d2d;padding:10px 14px;border-radius:6px;color:#791f1f}
.warnbox{background:#fff4e5;border-left:3px solid #e07b00;padding:9px 12px;border-radius:6px;
  font-size:12.5px;color:#8a4b00;margin-top:10px}
.why{font-size:12px;color:#8b8a85;margin:-6px 0 12px}
.srcbadge{font-size:11px;font-weight:400;padding:1px 6px;border-radius:5px;background:#fdf3e2;color:#9a6700;margin-left:6px}
.srcbadge.ok{background:#e6f2ee;color:#0f6e56}
.mob{font-size:12px}
.mob.ok{color:#0f6e56;font-weight:600}
.mob.no{color:#a32d2d;font-weight:600}
.mob.dim{color:#8b8a85}
"""


def build_combined_html(out_root: Path, asins: list, domain: str,
                        name: str = "combined.html") -> Path:
    """把所有 ASIN 的 A+ 排版页合并成一个 HTML，按 SKU 分节。

    asins 为 (label, asin, domain_or_None) 元组列表：label 是输出目录名
    （CLI 模式下等于 ASIN，Web 模式下是用户给的 SKU 码）。

    每个 SKU 的内容放进独立的 Shadow DOM，避免各家的内嵌 <style> 互相污染、
    也避免重复 id 冲突。
    """
    secs, rows, nav = [], [], []

    for label, asin, dom in asins:
        dom = dom or domain
        adir = out_root / label
        vp_parts = []
        for vp, label_v in (("desktop", "桌面端"), ("mobile", "移动端")):
            meta = _meta(adir, vp)
            sec = load_render_section(adir, vp, meta)
            if not sec:
                continue
            vp_parts.append((vp, label_v, sec, meta))

        dm = _meta(adir, "desktop") or _meta(adir, "mobile") or {}
        title = label if label == asin else f"{label} <span style='font-weight:400;color:#8b8a85;font-size:13px'>({asin})</span>"
        if not vp_parts:
            secs.append(
                f'<div class="sku" id="sku-{label}"><h2>{title}'
                f'<a href="https://www.amazon.{dom}/dp/{asin}" target="_blank">打开商品页 ↗</a></h2>'
                f'<div class="emptybox">没有抓到 A+ 内容。{dm.get("note") or ""}</div></div>')
            rows.append(f'<tr><td>{title}</td><td colspan="5"><span class="tag none">无 A+</span></td></tr>')
            nav.append(f'<a href="#sku-{label}">{label}</a>')
            continue

        kind = dm.get("aplus_kind") or ("premium" if dm.get("is_premium_candidate") else "standard")
        tag = (f'<span class="tag pre">高级 A+ Premium</span>' if kind == "premium"
               else '<span class="tag std">普通 A+</span>')
        stats = (f'模块 {dm.get("module_count", 0)} · 图片 {dm.get("image_count", 0)} · '
                 f'最大图宽 {dm.get("max_image_width") or "-"} · '
                 f'容器 {", ".join(dm.get("containers", [])) or "-"}')
        ev = "；".join(dm.get("premium_evidence") or []) or "—"
        why = ('类型判定：' + ev + ' · 移动端：' +
               ("有独立版本（已抓取）" if kind == "premium" and any(v == "mobile" for v, _, _, _ in vp_parts)
                else ("有独立版本（本次未抓）" if kind == "premium" else "无独立版本，普通 A+ 只做桌面端")))

        blocks = []
        for vp, label_v, sec, meta in vp_parts:
            w = sec["width"]
            tid = f"tpl-{label}-{vp}"
            src = sec.get("source", "")
            srcbadge = ('<span class="srcbadge ok">render.html</span>' if src == "render.html"
                        else f'<span class="srcbadge">{src}</span>')
            blocks.append(
                f'<div class="vp"><div class="vplabel">{label_v} · {w}px {srcbadge}</div>'
                f'<div class="host" data-tpl="{tid}" data-w="{w}"></div>'
                f'<template id="{tid}">{sec["html"]}</template></div>')

        if dm.get("mobile_skipped") or (kind != "premium" and not any(v == "mobile" for v, _, _, _ in vp_parts)):
            blocks.append(
                '<div class="skipnote">移动端：' +
                (dm.get("mobile_note") or
                 "普通 A+ 无独立移动端版本，内容与桌面端一致（同一套 970 模块等比缩放），已跳过。") +
                '</div>')

        # 排版体检不合格（图片抓取时没加载完）时，在页面上直接亮黄条，别让人肉眼看
        warns = []
        for vp, label_v, _sec, meta in vp_parts:
            if meta.get("render_warning"):
                warns.append(f'{label_v}：{meta["render_warning"]}')
        if warns:
            blocks.append('<div class="warnbox">⚠ 排版可能不完整 —— ' +
                          "；".join(warns) + "。建议重跑该 SKU。</div>")

        secs.append(
            f'<div class="sku" id="sku-{label}">'
            f'<h2>{title} {tag}'
            f'<a href="https://www.amazon.{dom}/dp/{asin}" target="_blank">打开商品页 ↗</a></h2>'
            f'<div class="stats">{stats}</div>'
            f'<div class="why">{why}</div>'
            f'{"".join(blocks)}</div>')

        if any(v == "mobile" for v, _, _, _ in vp_parts):
            mob = '<span class="mob ok">已抓</span>'
        elif kind == "premium":
            mob = '<span class="mob no">未抓</span>'
        else:
            mob = '<span class="mob dim">无独立版本</span>'
        rows.append(
            f'<tr><td><a href="#sku-{label}">{title}</a></td>'
            f'<td>{tag}</td><td>{dm.get("module_count", 0)}</td>'
            f'<td>{dm.get("image_count", 0)}</td>'
            f'<td>{dm.get("max_image_width") or "-"}</td><td>{mob}</td></tr>')
        nav.append(f'<a href="#sku-{label}">{label}</a>')

    js = """
    document.querySelectorAll('.host').forEach(function(host){
      var tpl = document.getElementById(host.dataset.tpl);
      if(!tpl) return;
      var root = host.attachShadow({mode:'open'});
      var st = document.createElement('style');
      st.textContent = ':host{display:block;background:#fff} '
        + '.aplus-page{width:' + host.dataset.w + 'px;max-width:100%;margin:0 auto;background:#fff} '
        + 'img{max-width:100%;height:auto !important} a{pointer-events:none;text-decoration:none;color:inherit}';
      root.appendChild(st);
      var box = document.createElement('div');
      box.className = 'aplus-page';
      box.appendChild(tpl.content.cloneNode(true));
      root.appendChild(box);
      host.style.maxWidth = host.dataset.w + 'px';
    });
    """

    doc = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>A+ 合集 · {len(asins)} 个 SKU</title>\n"
        f"<style>{CSS_COMBINED}</style></head>\n<body>\n<div class=\"wrap\">\n"
        f"<div class=\"head\"><h1>A+ 内容合集</h1>\n"
        f"<div class=\"sub\">{len(asins)} 个 SKU · 站点 amazon.{domain} · "
        f"生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')} · 每节按原网页排版还原</div>\n"
        f"<div class=\"nav\">{''.join(nav)}</div></div>\n"
        f"<table class=\"sum\"><tr><th>ASIN</th><th>类型</th><th>模块</th><th>图片</th>"
        f"<th>最大图宽</th><th>移动端</th></tr>{''.join(rows)}</table>\n"
        f"{''.join(secs)}\n</div>\n<script>{js}</script>\n</body></html>\n")
    p = out_root / name
    p.write_text(doc, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="Amazon A+ 内容抓取器（桌面端 + 移动端）")
    ap.add_argument("--asin", nargs="*", default=[], help="一个或多个 ASIN")
    ap.add_argument("--asin-file", help="ASIN 列表文件，每行一个")
    ap.add_argument("--domain", default="ae", help="站点后缀：ae / sa / com / co.uk / de ...  默认 ae")
    ap.add_argument("--both", action="store_true", help="同时抓桌面端和移动端（默认只抓桌面端）")
    ap.add_argument("--mobile-only", action="store_true", help="只抓移动端")
    ap.add_argument("--download-images", action="store_true", help="下载 A+ 图片到本地")
    ap.add_argument("--headed", action="store_true", help="有头模式，便于手动过验证码")
    ap.add_argument("--user-data-dir", help="持久化浏览器目录，复用登录态")
    ap.add_argument("--proxy", help="代理，如 http://user:pass@host:port")
    ap.add_argument("--out", default="out", help="输出目录，默认 out")
    ap.add_argument("--html-file", help="只解析本地 HTML 文件，不发起网络请求")
    ap.add_argument("--delay", type=float, default=6.0, help="ASIN 之间随机延迟基准秒数，默认 6")
    ap.add_argument("--include-brand-story", action="store_true",
                    help="同时抓取品牌故事模块（默认不抓）")
    ap.add_argument("--render", action="store_true",
                    help="额外输出 render.html：按原网页排版还原，内联计算样式，可独立打开")
    ap.add_argument("--screenshot", action="store_true", help="额外输出 A+ 区域整块截图 png")
    ap.add_argument("--hires", type=int, default=0, metavar="N",
                    help="render.html 里把图片 URL 的 SX 尺寸提到 N（如 1464），保留裁剪参数不变")
    ap.add_argument("--force-mobile", action="store_true",
                    help="普通 A+ 也强制抓移动端（默认：普通 A+ 无独立移动端版本，自动跳过）")
    ap.add_argument("--aplus-mode", choices=list(APLUS_MODES), default="auto",
                    help="A+ 类型策略：auto=自动判定（默认）；standard=按普通 A+ 处理（只抓桌面端）；"
                         "premium=按高级 A+ 处理（桌面端 + 移动端）")
    ap.add_argument("--package-images", action="store_true",
                    help="抓取结束后把所有 A+ 图片打成 aplus_images.zip（含 manifest.csv）")
    ap.add_argument("--combined-name", default="combined.html",
                    help="合集 HTML 文件名，默认 combined.html")
    ap.add_argument("--retry", type=int, default=2,
                    help="单个 ASIN 失败（被拦截/空结果）时的重试次数，默认 2")
    args = ap.parse_args()
    if args.package_images:
        args.download_images = True     # 打包需要本地有图

    asins = list(args.asin)
    if args.asin_file:
        asins += [l.strip() for l in Path(args.asin_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not asins:
        ap.error("请提供 --asin 或 --asin-file")

    out_root = Path(args.out)
    out_root.mkdir(exist_ok=True)

    # 纯解析模式
    if args.html_file:
        html = Path(args.html_file).read_text(encoding="utf-8", errors="ignore")
        data = parse_aplus(html, asin=asins[0], viewport=args.domain,
                           include_brand_story=args.include_brand_story)
        d = out_root / asins[0] / "parsed"
        d.mkdir(parents=True, exist_ok=True)
        (d / "aplus.html").write_text(data.pop("html") or "", encoding="utf-8")
        (d / "content.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] 解析完成 -> {d/'content.json'}")
        print(f"     has_aplus={data['has_aplus']} 模块={data['module_count']} 图={data['image_count']} "
              f"疑似高级A+={data['is_premium_candidate']} 最大图宽={data['max_image_width']}")
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("缺少 playwright，请先执行：pip install -r requirements.txt && playwright install chromium")

    viewports = ["mobile"] if args.mobile_only else (["desktop", "mobile"] if args.both else ["desktop"])

    with sync_playwright() as p:
        launch_kwargs = {"headless": not args.headed, "args": list(STEALTH_ARGS)}
        if args.proxy:
            launch_kwargs["proxy"] = {"server": args.proxy}
        if args.user_data_dir:
            ctx_root = p.chromium.launch_persistent_context(
                args.user_data_dir,
                user_agent=random.choice(UA_DESKTOP_POOL),
                viewport={"width": 1440, "height": 2400},
                **launch_kwargs,
            )

            def factory(vp):
                return ctx_root  # 持久上下文不可关闭，外层统一处理
        else:
            browser = p.chromium.launch(**launch_kwargs)

            def factory(vp):
                if vp == "mobile":
                    return browser.new_context(
                        user_agent=UA_MOBILE,
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=3,
                        is_mobile=True,
                        has_touch=True,
                        locale="en-US",
                    )
                return browser.new_context(
                    user_agent=random.choice(UA_DESKTOP_POOL),
                    viewport={"width": 1440, "height": 2400},
                    locale="en-US",
                )

        for idx, asin in enumerate(asins):
            vps = list(viewports)
            for vp in vps:
                print(f"[{idx+1}/{len(asins)}] {asin} [{vp}] ...")
                data, err = None, None
                for attempt in range(args.retry + 1):
                    try:
                        data = fetch_one(factory, asin, args.domain, vp, True, out_root,
                                         args.download_images, args.include_brand_story,
                                         args.render, args.screenshot, args.hires,
                                         mode=args.aplus_mode)
                        err = None
                        break
                    except Exception as e:
                        err = e
                        if attempt < args.retry:
                            wait = args.delay * (attempt + 2) + random.uniform(0, 3)
                            print(f"    [重试 {attempt+1}/{args.retry}] {e} -> {wait:.0f}s 后重试")
                            time.sleep(wait)
                if data is not None:
                    print(f"    -> has_aplus={data['has_aplus']} 类型={data.get('aplus_kind')} "
                          f"模块={data['module_count']} 图={data['image_count']} "
                          f"视频={data['video_count']} 最大图宽={data['max_image_width']}")
                    print(f"       判定依据：{'；'.join(data.get('premium_evidence') or []) or '—'}")
                else:
                    print(f"    [失败] {err}")

                # 普通 A+ 没有独立的移动端版本：移动端只是把同一套 970 模块等比缩放，
                # 内容完全一致（实测图片 URL 连裁剪参数都一样），没必要再抓一遍。
                if (vp == "desktop" and "mobile" in vps and not args.force_mobile
                        and data and data.get("has_aplus")
                        and not (args.aplus_mode == "premium"
                                 or (args.aplus_mode == "auto" and data.get("is_premium_candidate")))):
                    vps.remove("mobile")
                    note = ("普通 A+ 无独立移动端版本：移动端与桌面端内容一致（同一套 970 模块等比缩放），"
                            "已跳过移动端抓取。需要时用 --force-mobile 或 --aplus-mode premium 强制抓取。")
                    data["mobile_skipped"] = True
                    data["mobile_note"] = note
                    (out_root / asin / "desktop" / "content.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"    [跳过移动端] {note}")
            if idx < len(asins) - 1:
                time.sleep(args.delay + random.uniform(0, args.delay))

        if args.user_data_dir:
            ctx_root.close()
        else:
            browser.close()

    if args.render:
        try:
            p = build_combined_html(out_root, [(a, a, args.domain) for a in asins], args.domain,
                                    name=args.combined_name)
            print(f"\n[合集] 已按 SKU 分节合并 -> {p.resolve()}")
        except Exception as e:
            print(f"\n[warn] 合并 HTML 失败 -> {e}")

    if args.package_images:
        try:
            items = [(a, a, args.domain) for a in asins]
            zp = out_root / "aplus_images.zip"
            res = package_images(out_root, items, zp, ctx=None, download_missing=False)
            if res.get("empty"):
                print("\n[打包] 没有可打包的图片 —— 请确认抓取时已下载图片（--download-images）")
            else:
                print(f"\n[打包] {res['count']} 张图片 -> {zp.resolve()}")
                if res.get("missing"):
                    print(f"       本地缺 {res['missing']} 张（可重跑该 SKU 补齐）")
        except Exception as e:
            print(f"\n[warn] 打包失败 -> {e}")

    print(f"\n完成，结果在：{out_root.resolve()}")


if __name__ == "__main__":
    main()
