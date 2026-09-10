# -*- coding: utf-8 -*-
"""
Amazon A+ 抓取工作台 —— Web 版
启动：python webapp.py        然后浏览器打开 http://127.0.0.1:8788
"""
import io
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import requests
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 打包成 exe 时复用系统里已下载的 Playwright 内核（必须在 playwright 被导入前设置）
if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    _local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(_local) / "ms-playwright")

import scrape_aplus as sa  # noqa: E402
import exporter as exp  # noqa: E402
import product as prodmod  # noqa: E402
import pool as poolmod  # noqa: E402
import updater as UPD  # noqa: E402
import version as V  # noqa: E402

PORT = int(os.environ.get("APLUS_PORT", "8788"))
POOL_SIZE = int(os.environ.get("APLUS_POOL", "6"))

# HERE 在 exe 里指向解包临时目录（只读），可写数据一律放到 exe 旁边
BASE = UPD.app_dir()
WEB_OUT = BASE / "web_out"
WEB_OUT.mkdir(parents=True, exist_ok=True)
STATE_FILE = BASE / "profile_storage_state.json"
WEB_DIR = HERE / "web"

app = FastAPI(title=V.APP_NAME)
UPDATER = UPD.Updater()

# 预热浏览器池：6 个标签页共享一份登录态，抓取时 6 路并行
POOL = poolmod.BrowserPool(BASE, stealth_js=sa.STEALTH_JS,
                           stealth_args=list(sa.STEALTH_ARGS))

# ---------------------------------------------------------------- 任务管理

TASKS: dict = {}
TASKS_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()          # 并行抓取时 6 个线程会同时写日志


class Task:
    def __init__(self, tid, asins, domain, opts):
        self.id = tid
        self.asins = asins                 # [(sku, asin, domain_or_None), ...]
        self.domain = domain
        self.opts = opts
        self.logs: list = []
        self.status = "running"            # running / done / error / stopped
        self.progress = {"total": 0, "done": 0, "current": ""}
        self.results: list = []
        self.combined = None
        self.zipfile = None
        self.outdir: Path = WEB_OUT / tid
        self.started = time.time()
        self.stop_flag = False
        self.workers = 1
        self._q = queue.Queue()

    def log(self, msg=""):
        line = str(msg).rstrip()
        with _LOG_LOCK:
            self.logs.append(line)
            if len(self.logs) > 4000:
                del self.logs[:1000]
        self._q.put(line)

    def since(self, n):
        with _LOG_LOCK:
            return {"logs": list(self.logs[n:]), "next": len(self.logs)}


class LogWriter(io.TextIOBase):
    """把 scraper 内部的 print 也接进任务日志（并行模式下多线程会同时写）"""

    def __init__(self, task):
        self.task = task
        self.buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self.buf += s
            lines = []
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                lines.append(line)
        for line in lines:
            self.task.log(line)
        return len(s)

    def flush(self):
        with self._lock:
            rest, self.buf = self.buf, ""
        if rest.strip():
            self.task.log(rest)


# ---------------------------------------------------------------- 入参解析

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)
URL_RE = re.compile(
    r"amazon\.([a-z]{2,3}(?:\.[a-z]{2,3})?)/(?:[^/]*/)?"
    r"(?:dp|gp/product|product|dp/product)/([A-Z0-9]{10})", re.I)


def parse_inputs(text: str):
    """支持三种行格式（可混贴）：
      SKU<tab|空格|逗号>链接      -> SKU 记号（如 EK1499）
      链接                        -> 用 ASIN 当 SKU
      裸 ASIN
    返回 [(sku, asin, domain_or_None), ...], [无法识别的行]
    """
    items, bad = [], []
    for raw in text.strip().splitlines():
        line = raw.strip().strip('"\'')
        if not line:
            continue
        # 提取 URL / ASIN
        mu = URL_RE.search(line)
        if mu:
            domain, asin = mu.group(1).lower(), mu.group(2).upper()
        else:
            ma = None
            for tok in re.split(r"[\s,;]+", line):
                if ASIN_RE.match(tok):
                    ma = tok
                    break
            if not ma:
                bad.append(line)
                continue
            asin, domain = ma.upper(), None
        # SKU = 行内去掉 URL/ASIN 后剩下的第一个 token
        rest = URL_RE.sub(" ", line)
        rest = re.sub(rf"\b{re.escape(asin)}\b", " ", rest, flags=re.I)
        tokens = [t for t in re.split(r"[\s,;]+", rest) if t]
        sku = None
        if tokens:
            cand = tokens[0]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,23}", cand) and not ASIN_RE.match(cand):
                sku = cand
        items.append((sku or asin, asin, domain))
    # 去重保序（按 ASIN）
    seen, out = set(), []
    for sku, a, d in items:
        if a in seen:
            continue
        seen.add(a)
        out.append((sku, a, d))
    return out, bad


# ---------------------------------------------------------------- 抓取流程

def _opts_common(o: dict):
    want = (o.get("want") or "all").lower()
    if want not in ("all", "product", "aplus"):
        want = "all"
    mode = (o.get("aplusMode") or "auto").lower()
    if mode not in sa.APLUS_MODES:
        mode = "auto"
    return want, mode


def _scrape_item(task, o, out_root, sku, asin, dom, want, mode,
                 factory=None, state_file=None, page=None, browser=None, tag=""):
    """抓一个 SKU（含 desktop / mobile 决策），返回结果 entry。

    页面来源：
      page    —— 复用预热池里已开好的标签页（桌面端，池化模式）
      browser —— 池化模式下 CDP 连上的浏览器，用来临时开移动端上下文
      factory —— 非池化模式（自己新建 context）
    """
    pre = f"    {tag}" if tag else "    "
    vps = ["desktop"] if want == "product" else (
        ["desktop", "mobile"] if (o.get("both") or mode == "premium") else ["desktop"])

    entry = {"sku": sku, "asin": asin, "domain": dom, "viewports": {}, "ok": False,
             "premium": False, "kind": "", "evidence": [], "product": None, "note": ""}

    # 用下标循环：判定为普通 A+ 后 vps 会被就地移除 mobile
    i = 0
    while i < len(vps):
        if task.stop_flag:
            break
        vp = vps[i]
        data, err = None, None
        attempts = int(o.get("retry", 2)) + 1
        for attempt in range(attempts):
            if task.stop_flag:
                break
            try:
                pool_page = page if (page is not None and vp == "desktop") else None
                pool_browser = browser if pool_page is None else None
                # 池化时登录态由共享 profile 维持，不需要再落盘 storage_state
                sf = str(state_file) if (state_file and pool_page is None) else None
                data = sa.fetch_one(
                    factory, asin, dom, vp, True, out_root,
                    o.get("downloadImages", False),
                    o.get("includeBrandStory", False),
                    o.get("render", True),
                    o.get("screenshot", False),
                    int(o.get("hires", 0) or 0),
                    folder=sku, state_file=sf,
                    mode=mode, want=want,
                    page=pool_page, browser=pool_browser)
                err = None
                break
            except Exception as e:
                err = e
                if attempt < attempts - 1:
                    w = float(o.get("delay", 6)) * (attempt + 1)
                    print(f"{pre}[重试 {attempt+1}/{attempts-1}] {e} -> {w:.0f}s")
                    for _ in range(int(w)):
                        if task.stop_flag:
                            break
                        time.sleep(1)
        if data is None:
            entry["note"] = str(err)
            print(f"{pre}[失败] {err}")
            i += 1
            continue

        entry["ok"] = True
        entry["premium"] = data.get("is_premium_candidate", False)
        entry["kind"] = data.get("aplus_kind", "")
        entry["evidence"] = data.get("premium_evidence") or []

        prod = data.get("product")
        if prod:
            entry["product"] = {
                "title": (prod.get("title") or "")[:120],
                "bullets": len(prod.get("bullets") or []),
                "images": len(prod.get("images") or []),
                "highlights": bool(prod.get("highlights")),
                "category": prod.get("category") or "",
                "nodeId": prod.get("nodeId") or "",
                "brand": prod.get("brand") or "",
                "price": prod.get("price") or "",
                "rating": prod.get("rating") or "",
                "reviews": prod.get("reviews") or "",
                "bulletsSource": prod.get("bulletsSource") or "",
                "miss": prodmod.missing_fields(prod),
            }

        if data.get("product_only"):
            # 只抓商品信息：没有 A+ 结果，直接收工
            entry["kind"] = "none"
            entry["ok"] = bool(prod and not prodmod.missing_fields(prod))
            if not entry["ok"]:
                entry["note"] = "商品信息不全：" + \
                    "、".join(prodmod.missing_fields(prod or {}))
            i += 1
            continue

        entry["viewports"][vp] = {
            "has": data.get("has_aplus"),
            "modules": data.get("module_count"),
            "images": data.get("image_count"),
            "maxw": data.get("max_image_width"),
            "skipped": data.get("mobile_skipped", False),
        }
        print(f"{pre}-> {vp}: has={data.get('has_aplus')} "
              f"模块={data.get('module_count')} 图={data.get('image_count')} "
              f"{'高级A+' if data.get('is_premium_candidate') else '普通A+'} "
              f"最大宽={data.get('max_image_width')}")
        print(f"{pre}   判定依据：{'；'.join(data.get('premium_evidence') or []) or '—'}")

        # 普通 A+ 没有独立移动端版本 —— 移除 mobile 并把原因写回 content.json，
        # 否则合集里看不出「移动端为什么缺失」
        want_mobile = (mode == "premium") or (
            mode == "auto" and (data.get("is_premium_candidate") or o.get("forceMobile")))
        if vp == "desktop" and "mobile" in vps and not want_mobile:
            vps.remove("mobile")
            entry["viewports"]["mobile"] = {"skipped": True}
            note = ("普通 A+ 无独立移动端版本：移动端与桌面端内容一致"
                    "（同一套 970 模块等比缩放），已跳过。"
                    "需要时把「A+ 模式」选为「强制高级 A+（桌面+移动端）」重抓。")
            data["mobile_skipped"] = True
            data["mobile_note"] = note
            try:
                (out_root / sku / "desktop" / "content.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            print(f"{pre}[跳过移动端] {note}")
        i += 1

    return entry


def _run_task_serial(task, want, mode):
    """没预热浏览器时的串行路径（兼容 userDataDir / storage_state 等老参数）"""
    import contextlib
    from playwright.sync_api import sync_playwright

    o = task.opts
    out_root = task.outdir
    out_root.mkdir(parents=True, exist_ok=True)
    items = [(sku, a, d or task.domain) for sku, a, d in task.asins]

    lw = LogWriter(task)
    results = []
    try:
        with contextlib.redirect_stdout(lw), contextlib.redirect_stderr(lw):
            state_file = STATE_FILE
            use_state = bool(o.get("persist", True)) and state_file.exists()
            print(f"[启动] 共 {len(items)} 个 SKU · 站点 {task.domain}")
            print(f"[模式] 单线程串行（建议先点「预热浏览器」再抓，更快也不容易被拦）")
            print(f"[登录态] {'复用上次的 cookies（验证码概率更低）' if use_state else '首次运行，结束后自动记住'}")

            launch_kwargs = {
                "headless": not o.get("headed", False),
                "args": list(sa.STEALTH_ARGS),
            }
            if o.get("proxy"):
                launch_kwargs["proxy"] = {"server": o["proxy"]}

            with sync_playwright() as p:
                ctx_root = None
                browser = None
                if o.get("userDataDir"):
                    ctx_root = p.chromium.launch_persistent_context(
                        o["userDataDir"],
                        user_agent=random.choice(sa.UA_DESKTOP_POOL),
                        viewport={"width": 1440, "height": 2400},
                        **launch_kwargs)

                    def factory(vp):
                        return ctx_root
                else:
                    browser = p.chromium.launch(**launch_kwargs)

                    def factory(vp):
                        kw = {}
                        if use_state:
                            kw["storage_state"] = str(state_file)
                        if vp == "mobile":
                            kw.update(user_agent=sa.UA_MOBILE,
                                      viewport={"width": 390, "height": 844},
                                      device_scale_factor=3,
                                      is_mobile=True, has_touch=True, locale="en-US")
                        else:
                            kw.update(user_agent=random.choice(sa.UA_DESKTOP_POOL),
                                      viewport={"width": 1440, "height": 2400},
                                      locale="en-US")
                        return browser.new_context(**kw)

                try:
                    for idx, (sku, asin, dom) in enumerate(items):
                        if task.stop_flag:
                            break
                        task.progress["current"] = f"{sku}({asin})"
                        print(f"\n[{idx+1}/{len(items)}] {sku} = {asin} @ {dom}"
                              f"  [{want} · 模式 {mode}]")
                        entry = _scrape_item(
                            task, o, out_root, sku, asin, dom, want, mode,
                            factory=factory,
                            state_file=state_file if o.get("persist", True) else None)
                        results.append(entry)
                        task.results = list(results)
                        task.progress["done"] = idx + 1
                        if idx < len(items) - 1 and not task.stop_flag:
                            d = float(o.get("delay", 6)) + random.uniform(0, 3)
                            for _ in range(int(d)):
                                if task.stop_flag:
                                    break
                                time.sleep(1)
                finally:
                    try:
                        if ctx_root is not None:
                            ctx_root.close()
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass
    except Exception as e:
        task.log(f"[致命错误] {e}")
        task.log(traceback.format_exc())
        task.status = "error"
    finally:
        lw.flush()
        if task.status == "running":
            task.status = "stopped" if task.stop_flag else "done"
        task.log(f"[结束] 状态={task.status} 用时={time.time()-task.started:.0f}s")


def _run_task_pool(task, want, mode, workers):
    """预热池路径：workers 个线程各自连 CDP，各用一个预热好的标签页"""
    import contextlib
    from playwright.sync_api import sync_playwright

    o = task.opts
    out_root = task.outdir
    out_root.mkdir(parents=True, exist_ok=True)
    items = [(sku, a, d or task.domain) for sku, a, d in task.asins]
    cdp = POOL.cdp_url

    lw = LogWriter(task)
    results = []
    rlock = threading.Lock()
    job_q: "queue.Queue" = queue.Queue()
    for it in items:
        job_q.put(it)
    done_stat = {"n": 0}
    clock = threading.Lock()

    def worker(slot):
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp)
                contexts = browser.contexts
                ctx = contexts[0] if contexts else browser.new_context()
                pages = list(ctx.pages)
                page = pages[slot] if slot < len(pages) else ctx.new_page()
                print(f"[#{slot+1}] 已接入预热标签页")
                while not task.stop_flag:
                    try:
                        sku, asin, dom = job_q.get_nowait()
                    except queue.Empty:
                        break
                    with clock:
                        done_stat["n"] += 1
                        n = done_stat["n"]
                    task.progress["current"] = f"#{slot+1} {sku}"
                    print(f"\n[{n}/{len(items)}] #{slot+1} {sku} = {asin} @ {dom}"
                          f"  [{want} · 模式 {mode}]")
                    entry = _scrape_item(task, o, out_root, sku, asin, dom, want, mode,
                                         page=page, browser=browser,
                                         tag=f"#{slot+1} ")
                    with rlock:
                        results.append(entry)
                        task.results = list(results)
                        task.progress["done"] = len(results)
                    if not task.stop_flag:
                        # 6 路并行时把每条之间的等待摊薄
                        d = float(o.get("delay", 6)) / max(1, workers) + random.uniform(0, 1.2)
                        for _ in range(int(d)):
                            if task.stop_flag:
                                break
                            time.sleep(1)
        except Exception as e:
            print(f"#{slot+1} [错误] 标签页接入失败：{e}")

    try:
        with contextlib.redirect_stdout(lw), contextlib.redirect_stderr(lw):
            print(f"[启动] 共 {len(items)} 个 SKU · 站点 {task.domain}")
            print(f"[模式] {workers} 路并行（复用预热好的标签页，共享同一份登录态）")
            threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                       for i in range(workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    except Exception as e:
        task.log(f"[致命错误] {e}")
        task.log(traceback.format_exc())
        task.status = "error"


def run_task(task: Task):
    """入口：预热了就并行，没预热就串行"""
    o = task.opts
    want, mode = _opts_common(o)
    out_root = task.outdir
    out_root.mkdir(parents=True, exist_ok=True)
    items = [(sku, a, d or task.domain) for sku, a, d in task.asins]
    task.progress = {"total": len(items), "done": 0, "current": ""}

    psize = int(POOL.status().get("size") or 0)
    if POOL.is_ready() and psize:
        want_workers = int(o.get("workers") or psize)
        workers = max(1, min(psize, want_workers, len(items) or 1))
        task.workers = workers
        _run_task_pool(task, want, mode, workers)
    else:
        task.workers = 1
        _run_task_serial(task, want, mode)

    task.log(f"[用时] {time.time()-task.started:.0f}s")
    if task.status == "running":
        task.status = "stopped" if task.stop_flag else "done"

    # 抓完统一产出：合集 HTML + 图片包（A+ 模式才有）
    if not task.stop_flag and want != "product":
        lw = LogWriter(task)
        import contextlib
        with contextlib.redirect_stdout(lw), contextlib.redirect_stderr(lw):
            if o.get("buildCombined", True):
                print("\n[合并] 正在按 SKU 分节生成合集 ...")
                try:
                    cp = sa.build_combined_html(out_root, items, task.domain)
                    task.combined = cp.relative_to(WEB_OUT).as_posix()
                    print(f"[完成] 合集 -> {cp}（{cp.stat().st_size // 1024} KB）")
                except Exception as e:
                    print(f"[warn] 合并失败 -> {e}")
                    traceback.print_exc(file=lw)
            if o.get("packageImages", True):
                print("[打包] 正在压缩 A+ 图片 ...")
                try:
                    zpath = out_root / "aplus_images.zip"
                    res = sa.package_images(out_root, items, zpath,
                                            ctx=None, download_missing=False)
                    if res.get("empty"):
                        print("[打包] 没有可打包的图片（未勾选「下载图片」或无 A+）")
                    else:
                        task.zipfile = zpath.relative_to(WEB_OUT).as_posix()
                        print(f"[完成] 图片压缩包 -> {zpath}（{res['count']} 张）")
                except Exception as e:
                    print(f"[warn] 打包失败 -> {e}")
                    traceback.print_exc(file=lw)
        lw.flush()

    task.log(f"[结束] 状态={task.status} 用时={time.time()-task.started:.0f}s")


# ---------------------------------------------------------------- 环境检测

def _win_reg_version():
    try:
        import winreg
    except ImportError:
        return None, None
    paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}"),
    ]
    for root, sub in paths:
        try:
            k = winreg.OpenKey(root, sub)
            for name in ("version", "pv"):
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    if v and re.match(r"^\d+(\.\d+){2,3}$", str(v)):
                        winreg.CloseKey(k)
                        return str(v), f"注册表 {sub.split(chr(92))[0]}"
                except OSError:
                    continue
            winreg.CloseKey(k)
        except OSError:
            continue
    return None, None


def _scan_chrome_exe():
    """扫描常见安装路径，从目录名/文件版本取版本号"""
    cands = []
    env = os.environ
    bases = [env.get("ProgramFiles"), env.get("ProgramFiles(x86)"),
             env.get("LOCALAPPDATA"), env.get("ProgramW6432"),
             r"C:\Program Files", r"C:\Program Files (x86)",
             str(Path.home() / "AppData" / "Local")]
    for base in bases:
        if not base:
            continue
        cands += [
            Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(base) / "Google" / "Chrome Beta" / "Application" / "chrome.exe",
            Path(base) / "Google" / "Chrome Dev" / "Application" / "chrome.exe",
        ]
    for exe in cands:
        if exe.exists():
            # 同目录下形如 131.0.6778.86 的版本子目录
            vers = [p.name for p in exe.parent.iterdir()
                    if p.is_dir() and re.match(r"^\d+(\.\d+){2,3}$", p.name)]
            ver = sorted(vers, key=lambda s: [int(x) for x in s.split(".")])[-1] if vers else None
            if not ver:
                ver = _pe_version(exe)
            return str(exe), ver
    return None, None


def _pe_version(exe: Path):
    """PowerShell 读文件版本（兜底）"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=15)
        v = (out.stdout or "").strip()
        if re.match(r"^\d+(\.\d+){2,3}$", v):
            return v
    except Exception:
        pass
    return None


def detect_chrome():
    ver, src = _win_reg_version()
    path = None
    if not ver:
        path, ver = _scan_chrome_exe()
        src = "安装目录扫描" if ver else None
    if ver and not path:
        p, _ = _scan_chrome_exe()
        path = str(p) if p else None
    return {"version": ver, "path": path, "source": src,
            "major": int(ver.split(".")[0]) if ver else None}


def playwright_browsers():
    """检查 Playwright 自带的 chromium 是否已安装"""
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                               Path.home() / "AppData" / "Local" / "ms-playwright"))
    try:
        dirs = [p.name for p in base.iterdir()
                if p.is_dir() and p.name.startswith("chromium")]
    except Exception:
        dirs = []
    return {"root": str(base), "installed": dirs, "ok": bool(dirs)}


# ---------------------------------------------------------------- 驱动下载

CFT_API = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
NPM_MIRROR = "https://registry.npmmirror.com/-/binary/chrome-for-testing/"


def driver_links(version: str):
    """返回该 Chrome 版本可用的 chromedriver 下载候选"""
    out = []
    major = int(version.split(".")[0]) if version else None

    # 1) Chrome for Testing（Chrome >= 115）
    if major and major >= 115:
        try:
            r = requests.get(CFT_API, timeout=12)
            data = r.json()
            hit = None
            for v in reversed(data.get("versions", [])):
                if v.get("version", "").startswith(f"{version.split('.')[0]}."):
                    if v["version"] == version:
                        hit = v
                        break
                    if hit is None:
                        hit = v  # 同 major 的最新一个
            if hit:
                dl = hit.get("downloads", {}).get("chromedriver", [])
                for item in dl:
                    if item.get("platform") in ("win64", "win32"):
                        out.append({
                            "name": f"Chrome for Testing · chromedriver {hit['version']} · {item['platform']}",
                            "url": item["url"], "tag": "官方精确匹配", "recommend": True})
        except Exception as e:
            out.append({"name": "官方接口查询失败（可能网络不通）", "url": "", "tag": f"{e}"})

    # 2) npmmirror 国内镜像
    if major:
        out.append({
            "name": f"npmmirror 国内镜像 · chromedriver {major}.x 目录",
            "url": f"https://registry.npmmirror.com/-/binary/chromedriver/",
            "tag": "国内加速", "recommend": major >= 115 and not any(x.get("recommend") for x in out)})

    # 3) 老版本
    if major and major < 115:
        try:
            r = requests.get(
                f"https://chromedriver.storage.googleapis.com/LATEST_RELEASE_{major}", timeout=8)
            v = (r.text or "").strip()
            if v:
                out.append({"name": f"旧版仓库 · chromedriver {v} (win32)",
                            "url": f"https://chromedriver.storage.googleapis.com/{v}/chromedriver_win32.zip",
                            "tag": "官方", "recommend": True})
        except Exception:
            pass

    # 4) 官方下载页
    out.append({"name": "Chrome 官方驱动下载页（含最新版）",
                "url": "https://developer.chrome.google.cn/docs/chromedriver/downloads",
                "tag": "官方入口"})
    out.append({"name": "Chrome for Testing 总下载页",
                "url": "https://googlechromelabs.github.io/chrome-for-testing/",
                "tag": "官方入口"})
    return out


# ---------------------------------------------------------------- API

class RunRequest(BaseModel):
    text: str
    domain: str = "ae"
    want: str = "all"                # all / product / aplus
    both: bool = True
    forceMobile: bool = False
    aplusMode: str = "auto"          # auto / standard / premium
    downloadImages: bool = False
    includeBrandStory: bool = False
    screenshot: bool = False
    hires: int = 0
    delay: float = 6
    retry: int = 2
    headed: bool = False
    proxy: str = ""
    userDataDir: str = ""
    persist: bool = True
    buildCombined: bool = True        # 抓完生成合集 HTML
    packageImages: bool = True        # 抓完打包 A+ 图片
    workers: int = 0                  # 并行路数；0 = 用预热池的规模


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/env")
def api_env():
    ch = detect_chrome()
    pw = playwright_browsers()
    ch["drivers"] = driver_links(ch["version"]) if ch["version"] else []
    return {"chrome": ch, "playwright": pw,
            "python": sys.version.split()[0],
            "version": V.__version__,
            "app": V.APP_NAME,
            "frozen": UPD.is_frozen(),
            "has_state": STATE_FILE.exists(),
            "outdir": str(WEB_OUT)}


# ---------------------------------------------------------------- 版本与更新

def _watch_and_exit():
    """自替换脚本已接管 —— 等它准备好就退出主进程"""
    for _ in range(600):
        if UPDATER.should_exit():
            time.sleep(1.8)
            os._exit(0)
        time.sleep(0.5)


@app.get("/api/version")
def api_version():
    return {"version": V.__version__, "app": V.APP_NAME, "repo": V.GITHUB_REPO,
            "frozen": UPD.is_frozen(), "has_token": UPDATER.has_token(),
            "releases_page": V.RELEASES_PAGE}


@app.get("/api/update/check")
def api_update_check(force: int = 0):
    return UPDATER.check(force=bool(force))


@app.post("/api/update/download")
def api_update_download(req: dict = Body(default={})):
    req = req or {}
    url, name = req.get("url") or "", req.get("name") or V.ASSET_NAME
    asset_id = req.get("asset_id")
    if not url:
        info = UPDATER.check()
        url = info.get("download_url") or ""
        asset_id = info.get("download_asset_id")
        exe = next((a for a in (info.get("assets") or []) if a.get("is_exe")), None)
        if exe:
            name = exe.get("name") or name
            asset_id = asset_id or exe.get("id")
    if not url:
        raise HTTPException(400, "这个版本没有可下载的 exe")
    if not UPDATER.start_download(url, name, asset_id):
        raise HTTPException(409, "已有下载任务在进行中")
    if UPD.is_frozen():
        threading.Thread(target=_watch_and_exit, daemon=True).start()
    return {"ok": True, "name": name}


@app.get("/api/update/status")
def api_update_status():
    return UPDATER.status()


@app.get("/api/driver/link")
def api_driver_link(version: str = ""):
    ch = detect_chrome()
    v = version or ch.get("version") or ""
    return {"version": v, "links": driver_links(v)}


def playwright_cli_cmd(*args):
    """Playwright CLI 调用命令（源码模式走 python -m，exe 模式走内置 node）"""
    if UPD.is_frozen():
        try:
            import playwright as _pw
            drv = Path(_pw.__file__).resolve().parent / "driver"
            node = drv / ("node.exe" if os.name == "nt" else "node")
            cli = drv / "package" / "cli.js"
            if node.exists() and cli.exists():
                return [str(node), str(cli), *args]
        except Exception:
            pass
    return [sys.executable, "-m", "playwright", *args]


@app.post("/api/env/install-playwright")
def api_install_playwright():
    """一键安装 Playwright 自带的 chromium"""
    tid = f"env-{int(time.time())}"
    t = Task(tid, [], "", {})
    t.status = "running"
    with TASKS_LOCK:
        TASKS[tid] = t

    def _work():
        lw = LogWriter(t)
        try:
            bp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "(默认用户目录)"
            t.log(f"内核安装目录：{bp}")
            cmds = [playwright_cli_cmd("install", "chromium")]
            if sys.platform.startswith("linux"):
                cmds.append(playwright_cli_cmd("install-deps"))
            for cmd in cmds:
                t.log(f"$ {' '.join(cmd)}")
                pr = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True,
                                      encoding="utf-8", errors="replace",
                                      cwd=str(HERE))
                for line in pr.stdout:
                    t.log(line.rstrip())
                pr.wait()
                t.log(f"[exit] {pr.returncode}")
        except Exception as e:
            t.log(f"[错误] {e}")
        finally:
            lw.flush()
            t.status = "done"
            t.log("[结束]")

    threading.Thread(target=_work, daemon=True).start()
    return {"task_id": tid}


@app.post("/api/run")
def api_run(req: RunRequest):
    items, bad = parse_inputs(req.text)
    if not items:
        raise HTTPException(400, "没有识别到有效的 ASIN 或链接")
    tid = time.strftime("%Y%m%d-%H%M%S")
    t = Task(tid, items, req.domain, req.dict())
    with TASKS_LOCK:
        TASKS[tid] = t
    threading.Thread(target=run_task, args=(t,), daemon=True).start()
    return {"task_id": tid, "count": len(items), "bad": bad,
            "asins": [{"sku": s, "asin": a, "domain": d or req.domain}
                      for s, a, d in items]}


@app.get("/api/task/{tid}")
def api_task(tid: str, since: int = 0):
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    d = t.since(since)
    d.update({"id": t.id, "status": t.status, "progress": t.progress,
              "results": t.results, "combined": t.combined, "workers": t.workers,
              "zipfile": t.zipfile, "outdir": str(t.outdir)})
    return d


@app.post("/api/task/{tid}/stop")
def api_stop(tid: str):
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    t.stop_flag = True
    t.log("[停止] 收到停止请求，当前步骤完成后退出")
    return {"ok": True}


@app.get("/api/tasks")
def api_tasks():
    return [{"id": t.id, "status": t.status, "progress": t.progress,
             "combined": t.combined, "started": t.started}
            for t in sorted(TASKS.values(), key=lambda x: -x.started)]


# ---------------------------------------------------------------- 浏览器预热池

class PreheatRequest(BaseModel):
    size: int = POOL_SIZE
    site: str = "ae"


@app.post("/api/pool/preheat")
def api_pool_preheat(req: PreheatRequest):
    size = max(1, min(int(req.size or POOL_SIZE), 12))
    if not POOL.preheat_async(size, req.site):
        raise HTTPException(409, "正在预热中，请稍候")
    return {"ok": True, "size": size}


@app.get("/api/pool/status")
def api_pool_status():
    st = POOL.status()
    st["default_size"] = POOL_SIZE
    return st


@app.post("/api/pool/close")
def api_pool_close():
    POOL.close()
    return {"ok": True}


# ---------------------------------------------------------------- 已有产物的再加工
#   抓取时没勾「下载图片」/ 抓完才想起要合集 —— 这里让已有目录随时能补


def scan_history():
    """扫描 web_out/* 目录，把已完成的产物还原成可操作条目"""
    out = []
    try:
        dirs = [d for d in WEB_OUT.iterdir() if d.is_dir() and not d.name.startswith("_")]
    except Exception:
        return out
    for d in sorted(dirs, key=lambda p: -p.stat().st_mtime):
        skus = []
        for sub in sorted(d.iterdir()):
            if not sub.is_dir():
                continue
            vps = {}
            for vp in ("desktop", "mobile"):
                cj = sub / vp / "content.json"
                if not cj.exists():
                    continue
                try:
                    meta = json.loads(cj.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
                vps[vp] = {
                    "has": meta.get("has_aplus"),
                    "modules": meta.get("module_count"),
                    "images": meta.get("image_count"),
                    "maxw": meta.get("max_image_width"),
                    "kind": meta.get("aplus_kind") or
                            ("premium" if meta.get("is_premium_candidate") else "standard"),
                    "skipped": bool(meta.get("mobile_skipped")),
                    "evidence": meta.get("premium_evidence") or [],
                }

            # 商品链接信息
            product = None
            pdata = {}
            pj = sub / "product.json"
            if pj.exists():
                try:
                    pdata = json.loads(pj.read_text(encoding="utf-8"))
                except Exception:
                    pdata = {}
                if pdata:
                    product = {
                        "title": (pdata.get("title") or "")[:80],
                        "bullets": len(pdata.get("bullets") or []),
                        "images": len(pdata.get("images") or []),
                        "highlights": bool(pdata.get("highlights")),
                        "brand": pdata.get("brand") or "",
                        "price": pdata.get("price") or "",
                    }
            if not vps and not product:
                continue

            dm = {}
            for vp in ("desktop", "mobile"):
                cj = sub / vp / "content.json"
                if cj.exists():
                    try:
                        dm = json.loads(cj.read_text(encoding="utf-8"))
                    except Exception:
                        dm = {}
                    if dm:
                        break
            skus.append({
                "sku": sub.name,
                "asin": dm.get("asin") or pdata.get("asin") or "",
                "viewports": vps,
                "product": product,
                "kind": dm.get("aplus_kind") or
                        ("premium" if dm.get("is_premium_candidate") else ("standard" if dm else "")),
                "evidence": dm.get("premium_evidence") or [],
            })

        zp = d / "aplus_images.zip"
        cp = d / "combined.html"
        has_product = any(s.get("product") for s in skus)
        out.append({
            "id": d.name,
            "dir": d.relative_to(WEB_OUT).as_posix(),
            "skus": skus,
            "sku_count": len(skus),
            "has_product": has_product,
            "product_count": sum(1 for s in skus if s.get("product")),
            "has_zip": zp.exists(),
            "zip_bytes": zp.stat().st_size if zp.exists() else 0,
            "has_combined": cp.exists(),
            "combined_bytes": cp.stat().st_size if cp.exists() else 0,
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
        })
    return out


@app.get("/api/history")
def api_history():
    return {"items": scan_history()}


@app.get("/api/history/{tid}/download/zip")
def api_history_zip(tid: str):
    p = WEB_OUT / tid / "aplus_images.zip"
    if not p.exists():
        raise HTTPException(404, "还没有图片压缩包，请先点「重新打包」")
    return FileResponse(str(p), media_type="application/zip",
                        filename=f"Aplus_images_{tid}.zip")


@app.get("/api/history/{tid}/download/combined")
def api_history_combined(tid: str):
    p = WEB_OUT / tid / "combined.html"
    if not p.exists():
        raise HTTPException(404, "还没有合集 HTML，请先点「重建合集」")
    return FileResponse(str(p), media_type="text/html",
                        filename=f"Aplus_combined_{tid}.html")


# ---------------------------------------------------------------- 商品信息导出

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _cd(filename: str) -> str:
    """HTTP 头只能带 latin-1，中文文件名走 RFC 5987"""
    from urllib.parse import quote
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _resolve_dir(tid: str) -> Path:
    """tid 为空时取最近一次产出"""
    if tid:
        d = WEB_OUT / tid
        if not d.is_dir():
            raise HTTPException(404, "输出目录不存在")
        return d
    dirs = sorted((p for p in WEB_OUT.iterdir()
                   if p.is_dir() and not p.name.startswith("_")),
                  key=lambda p: -p.stat().st_mtime)
    if not dirs:
        raise HTTPException(404, "还没有任何产出")
    return dirs[0]


@app.get("/api/export/product.xlsx")
def api_export_product_xlsx(dir: str = ""):
    d = _resolve_dir(dir)
    rows = exp.collect(d)
    if not rows:
        raise HTTPException(404, "该次任务里没有商品信息（未勾选「商品信息」或全部失败）")
    name = f"商品信息_{d.name}.xlsx"
    return Response(content=exp.build_xlsx(rows), media_type=XLSX_MIME,
                    headers={"Content-Disposition": _cd(name)})


@app.get("/api/export/tool.xlsx")
def api_export_tool_xlsx(dir: str = ""):
    """可直接上传到「批量文案重构」的导入表（前 8 列严格对齐其导入要求）"""
    d = _resolve_dir(dir)
    rows = exp.collect(d)
    if not rows:
        raise HTTPException(404, "该次任务里没有商品信息，无法生成改写导入表")
    name = f"批量改写导入_{d.name}.xlsx"
    return Response(content=exp.build_tool_xlsx(rows), media_type=XLSX_MIME,
                    headers={"Content-Disposition": _cd(name)})


@app.get("/api/history/{tid}/export/product.xlsx")
def api_history_product_xlsx(tid: str):
    return api_export_product_xlsx(tid)


@app.get("/api/history/{tid}/export/tool.xlsx")
def api_history_tool_xlsx(tid: str):
    return api_export_tool_xlsx(tid)


@app.get("/api/open")
def api_open(path: str = ""):
    """在资源管理器 / 浏览器中打开目录或文件"""
    try:
        target = (BASE / path) if path else WEB_OUT
        target = target.resolve()
        os.startfile(str(target))  # noqa: S606
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


app.mount("/out", StaticFiles(directory=str(WEB_OUT)), name="out")

if __name__ == "__main__":
    import uvicorn
    # 无控制台环境（pythonw / 打包）下 sys.stdout / sys.stderr 为 None，
    # uvicorn 的彩色格式化器会调 isatty() 崩掉，先补兜底流
    if sys.stdout is None or sys.stderr is None:
        _sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _sink
        if sys.stderr is None:
            sys.stderr = _sink
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  {V.APP_NAME} 已启动 -> {url}\n")
    threading.Timer(1.2, lambda: (os.startfile(url) if sys.platform == "win32" else None)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
