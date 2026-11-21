# -*- coding: utf-8 -*-
"""浏览器预热池 —— 一次性起一个有头 Chromium + N 个标签页，抓取时 N 路并行复用。

为什么这么做
------------
* 亚马逊对「无头 / 全新访客」最狠。用真实有头浏览器 + 固定 profile，人工过掉一次
  验证码，登录态就固化在 profile 里，之后所有标签页共享同一份 cookie。
* 多线程抓取要求每个线程持有**自己的** Playwright 连接（sync API 不能跨线程使用），
  所以每个 worker 线程各自 `connect_over_cdp` 连到同一个浏览器，各用各的标签页。

⚠️ 一个必须绕开的坑（实测踩过）
-------------------------------
**全新 profile 首次启动时，Chrome 的浏览器级 CDP WebSocket 能连上，但握手会卡死**
（表现为 `connect_over_cdp: Timeout 20000ms exceeded`，日志里是 `<ws connected>` 之后不动），
因为浏览器还在做 profile 首次初始化。实测：profile 已存在 → 10s 就绪；全新 profile → 卡 150s+。

解法：预热前先用同一 profile 跑一次**静默初始化启动**（headless + about:blank），
把 profile 建出来再关掉，正式启动时就不会卡。

对外三个动作：preheat() / status() / close()，全部可被 Web 层轮询。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

DEFAULT_SIZE = 6
# 移动端标签页数。UA / 视口 / 触摸是**逐标签页**用 CDP Emulation 覆盖的
# （见 scrape_aplus.apply_mobile_emulation），所以它们仍是持久 context 里的
# 普通标签页 —— 预热时能在浏览器窗口里看到，且和桌面端共享同一份登录态。
DEFAULT_MOBILE_SIZE = 3

# 沙箱兼容参数：部分机器（安全软件 / AppLocker / WDAC 拦截「用户可写目录」下的进程）
# 下 Chrome 的沙箱无法初始化，表现为浏览器进程启动即死、调试端口永远不监听
# （profile 目录里只剩半成品 .tmp，Crashpad 记录 variations_crash_streak 暴涨）。
# 实测：Program Files 里的 Chrome 带沙箱正常，用户目录下的 Playwright Chromium /
# Chrome for Testing 全部失败；加上这些参数后立即可用。
# 代价是失去渲染进程的沙箱隔离 —— 采集公开商品页场景可接受。
SANDBOX_FALLBACK_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def chromium_path() -> str:
    """Playwright 自带 Chromium 的可执行文件路径"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def spawn_kwargs() -> dict:
    """Windows 下别弹出黑框；其它平台原样返回"""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


class BrowserPool:
    def __init__(self, base_dir: Path, stealth_js: str = "",
                 stealth_args: list | None = None):
        self.base = Path(base_dir)
        self.profile_dir = self.base / "pool_profile"
        self.stealth_js = stealth_js
        self.stealth_args = list(stealth_args or [])
        self._lock = threading.RLock()

        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.size = 0
        self.state = "idle"          # idle / starting / ready / error / closing
        self.steps: list[dict] = []  # [{text, ok, ts}]
        self.message = "未预热"
        self.error = ""
        self.ready_at = 0.0
        self.site = "ae"
        self.compat = False          # 是否因沙箱无法初始化而启用了 --no-sandbox
        self.mobile_size = DEFAULT_MOBILE_SIZE   # 移动端标签页数（按通道分配）
        self.mobile_ready = 0                    # 本次预热实际开出来的移动端标签页数
        # 移动端的 UA/视口覆盖绑定在预热这条 CDP 连接上：连接一断，浏览器就会
        # 退回桌面渲染。所以预热完成后要**一直持有**这条连接，用事件挂住线程。
        self._hold = threading.Event()

    # ------------------------------------------------------------- 工具
    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def step(self, text: str, ok: bool = True):
        with self._lock:
            self.steps.append({"text": text, "ok": bool(ok),
                               "ts": time.strftime("%H:%M:%S")})
            self.message = text
            if len(self.steps) > 60:
                del self.steps[:-40]

    def _alive(self) -> bool:
        p = self.proc
        return bool(p) and p.poll() is None

    def is_ready(self) -> bool:
        with self._lock:
            return self.state == "ready" and self._alive()

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "ready": self.is_ready(),
                "size": self.size,
                "port": self.port,
                "cdp": self.cdp_url if self.port else "",
                "site": self.site,
                "message": self.message,
                "error": self.error,
                "alive": self._alive(),
                "steps": self.steps[-14:],
                "profile": str(self.profile_dir),
                "compat": bool(self.compat),
                "mobile_size": self.mobile_size,
                "mobile_ready": self.mobile_ready,
            }

    # ------------------------------------------------------------- 预热
    def preheat_async(self, size: int = DEFAULT_SIZE, site: str = "ae",
                      mobile_size: int | None = None) -> bool:
        """后台预热，立刻返回；进度看 status()

        mobile_size —— 额外开的移动端标签页数，默认 3。
                       传 0 表示不预热移动端，抓取时退回「临时开无痕上下文」。
        """
        with self._lock:
            if self.state == "starting":
                return False
            self.state = "starting"
            self.steps = []
            self.error = ""
            self.compat = False
            self.size = max(1, min(int(size or DEFAULT_SIZE), 12))
            self.site = site or "ae"
            self.mobile_size = max(0, min(
                int(DEFAULT_MOBILE_SIZE if mobile_size is None else mobile_size), 6))
            self.mobile_ready = 0
            self._hold = threading.Event()
            self.message = "准备中…"
        threading.Thread(target=self._do_preheat, daemon=True).start()
        return True

    # ---------------------------------------------------------- 分步实现
    def _resolve_chromium(self) -> str:
        self.step("检测 Playwright 浏览器内核")
        try:
            exe = chromium_path()
        except Exception as e:
            raise RuntimeError(f"没有可用的浏览器内核（{e}）。"
                               f"请先在「环境」面板点一次「安装 / 更新内核」")
        if not Path(exe).exists():
            raise RuntimeError("浏览器内核文件不存在，请重新安装内核")
        self.step(f"内核就绪：{Path(exe).name}")
        return exe

    def _init_profile(self, exe: str):
        """全新 profile 必须先静默初始化一次，否则正式启动时 CDP 握手会卡死"""
        self.step("首次使用：初始化浏览器配置目录（约 10 秒）")
        args = [exe, "--headless=new", "--disable-gpu",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run", "--no-default-browser-check",
                "about:blank"]
        p = None
        try:
            p = subprocess.Popen(args, **spawn_kwargs())
            mark = self.profile_dir / "Default" / "Preferences"
            end = time.time() + 30
            while time.time() < end:
                if mark.exists() and mark.stat().st_size > 0:
                    break
                time.sleep(0.6)
            time.sleep(2.0)                      # 让首启收尾，避免留下半成品 profile
        except Exception as e:
            self.step(f"初始化启动异常（{str(e)[:60]}），继续尝试", False)
        finally:
            if p:
                try:
                    p.terminate()
                    for _ in range(20):
                        if p.poll() is not None:
                            break
                        time.sleep(0.15)
                    if p.poll() is None:
                        p.kill()
                except Exception:
                    pass
            time.sleep(1.5)                      # 等文件锁释放
        self.step("配置目录已就绪")

    def _launch(self, exe: str, compat: bool = False):
        port = _free_port()
        home = f"https://www.amazon.{self.site}/"
        args = [
            exe,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,OptimizationHints,MediaRouter",
            "--lang=en-US",              # 浏览器 UI 与 JS 语言
            "--accept-lang=en-US,en",    # 影响请求头里的 Accept-Language
            "--window-size=1280,900",
            "--window-position=60,40",
        ]
        if compat:
            args += SANDBOX_FALLBACK_ARGS
        args.append(home)
        for a in self.stealth_args:
            if a not in args:
                args.append(a)
        self.step(f"启动有头浏览器（调试端口 {port}）"
                  + ("［兼容模式 --no-sandbox］" if compat else ""))
        self.proc = subprocess.Popen(args, **spawn_kwargs())
        self.port = port
        self.step("等待浏览器就绪")
        if not self._wait_devtools(45):
            raise RuntimeError("浏览器启动超时或调试端口不可用")

    def _connect(self, p):
        """带重试的 CDP 连接"""
        self.step("启动 Playwright 驱动")
        browser = None
        last = None
        for attempt in range(3):
            try:
                browser = p.chromium.connect_over_cdp(self.cdp_url, timeout=15000)
                break
            except Exception as e:
                last = e
                self.step(f"CDP 连接第 {attempt + 1}/3 次失败（{str(e)[:70]}），3s 后重试", False)
                time.sleep(3)
        if browser is None:
            raise RuntimeError(f"连不上浏览器调试端口：{last}")
        self.step("CDP 已连接")
        return browser

    def _do_preheat(self):
        hold = self._hold      # 本次预热专属的存活信号；重新预热时会被替换
        try:
            self.step("关闭上一次的浏览器实例")
            self._kill()

            exe = self._resolve_chromium()
            self.profile_dir.mkdir(parents=True, exist_ok=True)

            # 首次初始化 profile（失败也无妨，正式启动阶段会兜底）
            if not (self.profile_dir / "Default").exists():
                self._init_profile(exe)

            # 正式启动：常规失败（沙箱无法初始化 → 启动即死/端口不监听）
            # → 自动带 --no-sandbox 重试一次
            try:
                self._launch(exe)
            except RuntimeError as e:
                self.step(f"常规启动失败（{e}），改用兼容模式（--no-sandbox）重试", False)
                self._kill()
                time.sleep(1.0)
                self._launch(exe, compat=True)
                with self._lock:
                    self.compat = True

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = self._connect(p)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                if self.stealth_js:
                    try:
                        ctx.add_init_script(self.stealth_js)
                    except Exception:
                        pass
                # 语言锁死英文：持久 profile 会把语言偏好一直记着，一旦被写成阿语
                # （ar_AE）之后每次抓取都是阿语 —— 所以每次预热都重置回英文。
                try:
                    from scrape_aplus import lang_cookie
                    cname, cval = lang_cookie(self.site)
                    ctx.add_cookies([{"name": cname, "value": cval,
                                      "domain": f".amazon.{self.site}", "path": "/"}])
                    self.step(f"语言已锁定英文（{cname}={cval}）")
                except Exception as e:
                    self.step(f"语言锁定失败（{str(e)[:60]}），不影响抓取", False)
                pages = list(ctx.pages)
                if not pages:
                    pages = [ctx.new_page()]
                self.step(f"已有 {len(pages)} 个标签页，补齐到 {self.size} 个")
                home = f"https://www.amazon.{self.site}/"
                # 第 1 个标签页已经打开首页（命令行传入），其余逐个补
                for i in range(1, self.size):
                    try:
                        pg = ctx.new_page()
                        pg.goto(home, wait_until="domcontentloaded", timeout=45000)
                        pages.append(pg)
                        self.step(f"预热标签页 {i + 1}/{self.size}")
                    except Exception as e:
                        self.step(f"标签页 {i + 1} 打开失败（{str(e)[:60]}），"
                                  f"稍后抓取时会自动补", False)
                    time.sleep(0.6)
                try:
                    pages[0].bring_to_front()
                except Exception:
                    pass

                # ---- 移动端：在同一个持久 context 里多开几个可见标签页 ----
                # 以前用 browser.new_context(is_mobile=True)：那是独立的无痕上下文，
                # 浏览器窗口里看不见、也不带 profile 登录态，预热对用户没有任何
                # 可见反馈。改为逐标签页 CDP 仿真（UA / 390×844 / 触摸），标签页仍是
                # 普通标签页：窗口里看得到、能手动登录，且与桌面端共享 cookie。
                mob_pages = []
                mobile_n = min(self.mobile_size, self.size)
                for i in range(mobile_n):
                    try:
                        pg = ctx.new_page()
                        # URL 里的 _aplus_mobile=1 用于预热后识别是哪几个标签页
                        # （window.name 跨站导航会被浏览器清掉，不可靠）
                        mp = (f"https://www.amazon.{self.site}/"
                              f"?language=en_US&_aplus_mobile=1")
                        try:
                            from scrape_aplus import apply_mobile_emulation
                            apply_mobile_emulation(pg)
                        except Exception as e:
                            self.step(f"移动端仿真设置失败（{str(e)[:50]}），"
                                      f"该标签页按桌面打开", False)
                        pg.goto(mp, wait_until="domcontentloaded", timeout=45000)
                        mob_pages.append(pg)
                        self.step(f"预热移动端标签页 {i + 1}/{mobile_n}"
                                  f"（390×844 · 共享登录态）")
                    except Exception as e:
                        self.step(f"移动端标签页 {i + 1} 打开失败（{str(e)[:60]}），"
                                  f"该路抓取时退回临时上下文", False)
                    time.sleep(0.5)
                mob_real = len(mob_pages)
                try:
                    pages[0].bring_to_front()
                except Exception:
                    pass
                real = len(ctx.pages)
            self.step(f"预热完成：{self.size} 个桌面标签页"
                      + (f" + {mob_real} 个移动端标签页" if mob_real else "")
                      + "，共享同一份登录态")

            with self._lock:
                self.state = "ready"
                self.ready_at = time.time()
                self.mobile_ready = mob_real
                self.message = (f"已就绪 · {self.size} 路并行"
                                + (f" · 移动端 {mob_real} 路" if mob_real else ""))
            self.step("可以在任意标签页里登录 / 过验证码，然后点「开始抓取」")

            # 移动端的 UA / 视口覆盖绑定在**这条连接**上，连接一断浏览器就会
            # 退回桌面渲染 —— 所以预热完成后不能退出 with，用事件挂住。
            # 重新预热 / 关闭时 self._hold 会被替换或 set，这里据此退出。
            my_hold = hold
            while not my_hold.wait(1.0):
                if my_hold is not self._hold or not self._alive():
                    break
        except Exception as e:
            # 上一次预热留下的线程（持有的是旧 event）失败是正常的：新预热已经
            # 把它连着浏览器一起替换掉了，不要覆盖新状态。
            if hold is not self._hold:
                return
            with self._lock:
                self.state = "error"
                self.error = str(e)
            self.step(f"预热失败：{e}", False)

    def _wait_devtools(self, timeout: int = 45) -> bool:
        """等调试端口**真正可用**：/json/version 通 + 能列出 page 目标 + 再稳一下"""
        end = time.time() + timeout
        while time.time() < end:
            try:
                with urllib.request.urlopen(self.cdp_url + "/json/version", timeout=1.5) as r:
                    if r.status != 200:
                        raise RuntimeError("bad status")
            except Exception:
                time.sleep(0.5)
                continue
            try:
                with urllib.request.urlopen(self.cdp_url + "/json/list", timeout=1.5) as r:
                    targets = json.loads(r.read().decode("utf-8", "replace"))
                if any(t.get("type") == "page" for t in targets or []):
                    time.sleep(1.5)
                    return True
            except Exception:
                pass
            time.sleep(0.6)
        return False

    # ------------------------------------------------------------- 关闭
    def _kill(self):
        p = self.proc
        self.proc = None
        self.port = 0
        if not p:
            return
        try:
            p.terminate()
            for _ in range(20):
                if p.poll() is not None:
                    break
                time.sleep(0.15)
            if p.poll() is None:
                p.kill()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self.state = "closing"
            self._hold.set()          # 放行预热线程，让它退出 with 释放 Playwright
        self.step("正在关闭浏览器")
        self._kill()
        with self._lock:
            self.state = "idle"
            self.size = 0
            self.mobile_ready = 0
            self.message = "未预热"
        self.step("已关闭")
