# -*- coding: utf-8 -*-
"""浏览器预热池 —— 一次性起一个有头 Chromium + N 个标签页，抓取时 N 路并行复用。

为什么这么做
------------
* 亚马逊对「无头 / 全新访客」最狠。用真实有头浏览器 + 固定 profile，人工过掉一次
  验证码，登录态就固化在 profile 里，之后所有标签页共享同一份 cookie。
* 多线程抓取要求每个线程持有**自己的** Playwright 连接（sync API 不能跨线程使用），
  所以每个 worker 线程各自 `connect_over_cdp` 连到同一个浏览器，各用各的标签页。

稳定性设计（v1.4.8 重写）
------------------------
现场反馈「各种设备上都提示：浏览器启动超时或调试端口不可用」。逐条对应的处理：

1. **内核三级兜底**：系统 Chrome → 系统 Edge → Playwright Chromium。
   前两个装在 Program Files、有正规签名，不会被 WDAC / 杀软拦；Playwright 内核在
   用户目录，是最容易被拦的那个，所以排最后。
2. **端口不再自己挑**：改用 `--remote-debugging-port=0`，让浏览器自己分配，再从
   配置目录里的 `DevToolsActivePort` 读回来。彻底消除「bind 后被别人抢走」的竞态。
3. **配置目录不再被残留进程占着**：启动前删 Singleton 锁；如果浏览器秒退（退出码 0，
   典型的「目录已被占用，把请求转交给老进程」），自动按命令行查出占用该目录的残留
   浏览器进程并结束掉，然后重试一次。
4. **端口探测不走代理**：显式用 `ProxyHandler({})`，避免系统代理把 127.0.0.1 的探测
   请求劫走（Clash / 公司代理环境下 Chrome 起来了却判成「端口不可用」）。
5. **浏览器秒退立刻失败并带上原因**：接管 stderr，进程一死就读退出码和错误末几行，
   不再空等超时；错误信息直接告诉用户是「目录被占用」「被杀软拦截」还是「调试被策略禁用」。
6. **配置目录放在纯 ASCII 路径**：优先 `%LOCALAPPDATA%\\AplusWorkbench`，若用户名是
   中文则退到 `C:\\ProgramData\\AplusWorkbench`，避免中文路径引发的启动异常。

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
import shutil
import socket
import subprocess
import sys
import tempfile
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

# 崩溃类退出码（Windows NTSTATUS 转成有符号整数后的值）
CRASH_CODES = {
    -1073741819,   # 0xC0000005 访问冲突
    -1073740791,   # 0xC0000409 stack buffer overrun
    -2147483645,   # 0x80000003 断点（WDAC / 杀软常见）
    -1073741515,   # 0xC0000135 DLL 缺失
    -1073741701,   # 0xC000007B 架构/依赖错误
}

_PROFILE_ROOT = None
_ROOT_LOCK = threading.Lock()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _no_proxy_opener():
    """明确不使用系统代理：否则 127.0.0.1 的探测会被 Clash / 公司代理劫走"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def profile_root() -> Path:
    """配置目录根：优先纯 ASCII 且可写的位置

    为什么不用 exe 旁边：exe 可能放在中文目录、OneDrive 同步目录、网络盘或只读盘，
    这些都会让 Chrome 启动异常。也不直接用 %LOCALAPPDATA%，因为 Windows 用户名
    可能是中文。
    """
    global _PROFILE_ROOT
    with _ROOT_LOCK:
        if _PROFILE_ROOT is not None:
            return _PROFILE_ROOT
        cands = []
        la = os.environ.get("LOCALAPPDATA")
        if la:
            cands.append(Path(la) / "AplusWorkbench")
        if sys.platform == "win32":
            cands.append(Path(os.environ.get("ProgramData", r"C:\ProgramData"))
                         / "AplusWorkbench")
            cands.append(Path("C:/AplusWorkbench"))
        cands.append(Path.home() / ".aplus_workbench")
        first_ok = None
        for c in cands:
            try:
                c.mkdir(parents=True, exist_ok=True)
                probe = c / "._write_test"
                probe.write_text("1", encoding="utf-8")
                probe.unlink()
            except Exception:
                continue
            if first_ok is None:
                first_ok = c
            if str(c).isascii():
                _PROFILE_ROOT = c
                return c
        _PROFILE_ROOT = first_ok or (Path.cwd() / ".aplus_workbench")
        try:
            _PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return _PROFILE_ROOT


def _exe_version(path: str) -> str:
    """读 exe 的文件版本号（失败返回空串，不阻塞启动）"""
    if sys.platform != "win32":
        return ""
    try:
        ps = ("(Get-Item -LiteralPath '" + str(path).replace("'", "''") +
              "').VersionInfo.ProductVersion")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        v = (out.stdout or "").strip()
        return v if v and v[0].isdigit() else ""
    except Exception:
        return ""


def candidate_browsers() -> list[tuple[str, str]]:
    """按稳定性排序的可用内核：系统 Chrome → 系统 Edge → Playwright Chromium

    系统浏览器装在 Program Files、带正规签名，是最稳的一档；Playwright 的 Chromium
    落在用户可写目录，最容易被 WDAC / 杀软拦，所以只作最后兜底。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, p: Path | str):
        try:
            p = Path(p)
            if not p.exists() or not p.is_file():
                return
            key = str(p).lower()
            if key in seen:
                return
            seen.add(key)
            out.append((label, str(p)))
        except Exception:
            pass

    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        la = os.environ.get("LOCALAPPDATA", "")
        for base in (pf, pf86):
            add("Chrome", Path(base) / "Google/Chrome/Application/chrome.exe")
        if la:
            add("Chrome", Path(la) / "Google/Chrome/Application/chrome.exe")
        for base in (pf86, pf):
            add("Edge", Path(base) / "Microsoft/Edge/Application/msedge.exe")
    else:
        for p in ("/usr/bin/google-chrome", "/usr/bin/chromium",
                  "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge"):
            add("Chrome" if "chrome" in p else "Edge", p)

    # 最后兜底：Playwright 自带内核
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        add("Playwright", exe)
    except Exception:
        pass
    return out


def _procs_using(profile_dir: Path) -> list[int]:
    """查出命令行里带这个配置目录的浏览器进程（只用于清理残留，不误杀用户日常浏览器）"""
    key = str(profile_dir)
    if sys.platform == "win32":
        ps = (
            "$k='" + key.replace("'", "''") + "';"
            "Get-CimInstance Win32_Process -Filter "
            "\"Name='chrome.exe' OR Name='msedge.exe' OR Name='chromium.exe'\" "
            "-ErrorAction SilentlyContinue |"
            "Where-Object { $_.CommandLine -and "
            "$_.CommandLine.ToLower().Contains($k.ToLower()) } |"
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            pids = []
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
            return pids
        except Exception:
            return []
    try:
        out = subprocess.run(["pgrep", "-f", key], capture_output=True,
                             text=True, timeout=10)
        return [int(x) for x in (out.stdout or "").split() if x.isdigit()]
    except Exception:
        return []


def clear_profile_locks(profile_dir: Path) -> int:
    """删掉配置目录的 Singleton 锁，避免「目录已在使用」被误判"""
    n = 0
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try:
            p = profile_dir / name
            if p.exists() or p.is_symlink():
                p.unlink()
                n += 1
        except Exception:
            pass
    return n


def kill_profile_holders(profile_dir: Path) -> int:
    pids = _procs_using(profile_dir)
    killed = 0
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 9)
            killed += 1
        except Exception:
            pass
    return killed


def _read_tail(path: Path | None, limit: int = 900) -> str:
    if not path:
        return ""
    try:
        if not path.exists():
            return ""
        data = path.read_bytes()
        txt = data[-limit * 4:].decode("utf-8", "replace")
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        return " | ".join(lines[-6:])[:limit]
    except Exception:
        return ""


def spawn_kwargs() -> dict:
    """Windows 下别弹出黑框；其它平台原样返回"""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


class LaunchError(RuntimeError):
    """带可读原因的启动失败"""

    def __init__(self, kind: str, detail: str = "", rc=None, label: str = ""):
        self.kind = kind            # exited / timeout / not_listening / policy
        self.detail = detail or ""
        self.rc = rc
        self.label = label
        super().__init__(self.reason())

    @property
    def short(self) -> str:
        r = self.reason()
        return r if len(r) <= 150 else r[:147] + "…"

    def reason(self) -> str:
        rc = self.rc
        tail = self.detail
        if self.kind == "exited":
            if rc == 0:
                base = ("浏览器启动后立刻退出（退出码 0）—— 配置目录已被另一个浏览器实例占用，"
                        "Chrome 把启动请求转交给它后自己退出")
            elif rc in CRASH_CODES:
                base = (f"浏览器启动即崩溃（退出码 0x{rc & 0xFFFFFFFF:X}）—— "
                        f"通常是杀毒软件 / 应用控制策略拦截了这个内核")
            else:
                base = f"浏览器启动后立刻退出（退出码 {rc}）"
            return base + (f"；系统提示：{tail}" if tail else "")
        if self.kind == "policy":
            return ("浏览器已启动但没有开放调试端口 —— 可能是企业策略禁用了远程调试"
                    + (f"；{tail}" if tail else ""))
        if self.kind == "not_listening":
            return "调试端口不可连接" + (f"；{tail}" if tail else "")
        return "浏览器启动超时（调试端口一直没就绪）" + (f"；{tail}" if tail else "")


class BrowserPool:
    def __init__(self, base_dir: Path, stealth_js: str = "",
                 stealth_args: list | None = None):
        self.base = Path(base_dir)
        self.profile_dir = self.base / "pool_profile"   # 实际使用的目录（状态展示用）
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
        self.kernel = ""             # 实际用上的内核：Chrome / Edge / Playwright
        self.browser_version = ""
        self.mobile_size = DEFAULT_MOBILE_SIZE   # 移动端标签页数（按通道分配）
        self.mobile_ready = 0                    # 本次预热实际开出来的移动端标签页数
        # 移动端的 UA/视口覆盖绑定在预热这条 CDP 连接上：连接一断，浏览器就会
        # 退回桌面渲染。所以预热完成后要**一直持有**这条连接，用事件挂住线程。
        self._hold = threading.Event()
        self._errfile = None
        self._errpath: Path | None = None
        self._diag: dict = {"kernels": [], "profile": "", "stderr": ""}

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
                "kernel": self.kernel,
                "browser_version": self.browser_version,
                "mobile_size": self.mobile_size,
                "mobile_ready": self.mobile_ready,
            }

    def diagnose(self) -> dict:
        """给「一键复制诊断」用：把定位问题需要的信息一次性打包"""
        with self._lock:
            d = dict(self._diag)
        if not d.get("kernels"):
            # 还没预热过也要能看到本机有哪些内核可用
            try:
                d["kernels"] = [{"label": lb, "path": p}
                                for lb, p in candidate_browsers()]
            except Exception:
                d["kernels"] = []
        d.update({
            "state": self.state,
            "error": self.error,
            "message": self.message,
            "kernel": self.kernel,
            "browser_version": self.browser_version,
            "compat": self.compat,
            "profile": str(self.profile_dir),
            "profile_root": str(profile_root()),
            "steps": self.steps[-20:],
            "env": {
                k: os.environ.get(k) for k in
                ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "PLAYWRIGHT_BROWSERS_PATH")
                if os.environ.get(k)
            },
            "frozen": bool(getattr(sys, "frozen", False)),
            "platform": sys.platform,
        })
        return d

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
            self.kernel = ""
            self.browser_version = ""
            self.size = max(1, min(int(size or DEFAULT_SIZE), 12))
            self.site = site or "ae"
            self.mobile_size = max(0, min(
                int(DEFAULT_MOBILE_SIZE if mobile_size is None else mobile_size), 6))
            self.mobile_ready = 0
            self._hold = threading.Event()
            self._diag = {"kernels": [], "profile": "", "stderr": ""}
            self.message = "准备中…"
        threading.Thread(target=self._do_preheat, daemon=True).start()
        return True

    # ---------------------------------------------------------- 内核准备
    def _profile_dir(self, label: str) -> Path:
        if label == "Playwright":
            # 兼容老版本：以前 profile 放在 exe 旁边的 pool_profile，
            # 里面可能已经有登录态，别让用户重新过验证码
            legacy = self.base / "pool_profile"
            if (legacy / "Default").exists():
                return legacy
            return profile_root() / "profile_playwright"
        return profile_root() / f"profile_{label.lower()}"

    def _purge_profile(self, udd: Path):
        """清掉半成品配置目录（只在没有登录态时调用）"""
        try:
            if (udd / "Default" / "Cookies").exists():
                return                      # 有登录态，绝不删
            shutil.rmtree(udd, ignore_errors=True)
        except Exception:
            pass

    def _init_profile(self, exe: str, udd: Path, compat: bool = False) -> bool:
        """全新 profile 必须先静默初始化一次，否则正式启动时 CDP 握手会卡死"""
        args = [exe, "--headless=new", "--disable-gpu",
                f"--user-data-dir={udd}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "about:blank"]
        if compat:
            args += SANDBOX_FALLBACK_ARGS
        err = None
        p = None
        ok = False
        mark = udd / "Default" / "Preferences"
        try:
            err = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
            p = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=err, **spawn_kwargs())
            end = time.time() + 40
            while time.time() < end:
                if p.poll() is not None:
                    break
                if mark.exists() and mark.stat().st_size > 0:
                    ok = True
                    break
                time.sleep(0.5)
            if ok:
                time.sleep(1.5)                  # 让首启收尾，避免留下半成品
        except Exception as e:
            self.step(f"初始化启动异常（{str(e)[:60]}）", False)
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
            if err:
                try:
                    err.close()
                    Path(err.name).unlink(missing_ok=True)
                except Exception:
                    pass
            time.sleep(1.0)                      # 等文件锁释放
        return ok

    def _ensure_profile(self, label: str, exe: str) -> Path:
        udd = self._profile_dir(label)
        udd.mkdir(parents=True, exist_ok=True)
        mark = udd / "Default" / "Preferences"
        if mark.exists() and mark.stat().st_size > 0:
            return udd
        self.step(f"{label}：首次使用，初始化配置目录（约 10 秒）")
        ok = self._init_profile(exe, udd)
        if not ok:
            self.step("常规初始化未成功，改用兼容模式（--no-sandbox）重试", False)
            clear_profile_locks(udd)
            ok = self._init_profile(exe, udd, compat=True)
        if not ok:
            self._purge_profile(udd)
            udd.mkdir(parents=True, exist_ok=True)
            self.step("配置目录初始化失败，已清空半成品，改由正式启动创建", False)
        return udd

    # ---------------------------------------------------------- 启动浏览器
    def _heal_profile(self, udd: Path) -> str:
        """重建损坏 / 不兼容的配置目录，但**保留登录态**

        实测：老内核留下的 profile 可能让新内核启动即崩（退出码 0x80000003），
        而只把 `Local State`（含 cookie 解密密钥）和 `Default/Network/Cookies`
        迁到全新目录，内核就能正常启动且登录态不丢。所以这里不整体复制，只搬这两项。
        """
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = udd.with_name(f"{udd.name}_bak_{ts}")
        moved = []
        renamed = False
        try:
            if udd.exists():
                udd.rename(bak)
                renamed = True
        except Exception:
            renamed = False

        if not renamed:
            # 目录被占用时无法整体改名：就地瘦身，只留登录态两项
            try:
                for child in list(udd.iterdir()):
                    if child.name in ("Local State", "Default"):
                        continue
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                dflt = udd / "Default"
                if dflt.is_dir():
                    for child in list(dflt.iterdir()):
                        if child.name == "Network":
                            continue
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                    net = dflt / "Network"
                    if net.is_dir():
                        for child in list(net.iterdir()):
                            if child.name != "Cookies":
                                if child.is_dir():
                                    shutil.rmtree(child, ignore_errors=True)
                                else:
                                    child.unlink(missing_ok=True)
            except Exception:
                pass
            bak = udd

        udd.mkdir(parents=True, exist_ok=True)
        for rel in ("Local State", "Default/Network/Cookies"):
            src = Path(bak) / rel
            if not src.exists():
                continue
            dst = udd / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                moved.append(rel)
            except Exception:
                pass
        self.step(f"已重建配置目录（旧目录备份为 {Path(bak).name}，"
                  f"迁移登录态 {len(moved)} 项）")
        self._prune_backups(udd)
        return str(bak)

    def _prune_backups(self, udd: Path, keep: int = 2):
        """只保留最近几个备份，避免磁盘被撑爆"""
        try:
            baks = sorted(udd.parent.glob(f"{udd.name}_bak_*"),
                          key=lambda p: p.name, reverse=True)
            for old in baks[keep:]:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass

    def _launch(self, exe: str, label: str, udd: Path, compat: bool = False,
                minimal: bool = False) -> int:
        """启动浏览器并等它真正开放调试端口，返回端口号"""
        dp = udd / "DevToolsActivePort"
        # 这个文件是上一轮的，必须删掉，否则会读到过期端口
        try:
            dp.unlink()
        except OSError:
            pass
        clear_profile_locks(udd)

        headless = os.environ.get("APLUS_POOL_HEADLESS") == "1"
        args = [
            exe,
            "--remote-debugging-port=0",     # 让浏览器自己分配端口，避免被抢
            "--remote-allow-origins=*",
            f"--user-data-dir={udd}",
            "--no-first-run", "--no-default-browser-check",
        ]
        if not minimal:
            args += [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=Translate,OptimizationHints,MediaRouter",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--lang=en-US",              # 浏览器 UI 与 JS 语言
                "--accept-lang=en-US,en",    # 影响请求头里的 Accept-Language
            ]
        if headless:
            args.append("--headless=new")
        else:
            args += ["--window-size=1280,900", "--window-position=60,40"]
        if compat:
            args += SANDBOX_FALLBACK_ARGS
        args.append(f"https://www.amazon.{self.site}/")
        if not minimal:
            for a in self.stealth_args:
                if a not in args:
                    args.append(a)

        self.step(f"启动 {label}"
                  + ("（无头）" if headless else "（有头）")
                  + ("［兼容模式 --no-sandbox］" if compat else "")
                  + ("［最小参数］" if minimal else ""))
        self.browser_version = ""

        try:
            if self._errpath:
                try:
                    self._errpath.unlink(missing_ok=True)
                except Exception:
                    pass
            self._errfile = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
            self._errpath = Path(self._errfile.name)
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=self._errfile, **spawn_kwargs())
        except Exception as e:
            raise LaunchError("exited", str(e), rc=None, label=label)
        errpath = self._errpath

        # 等 DevToolsActivePort。进程一死立刻失败，不空等。
        first_run = not (udd / "Default" / "Cookies").exists()
        timeout = 75 if first_run else 50
        deadline = time.time() + timeout
        port = 0
        while time.time() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                tail = _read_tail(errpath)
                # 退出码 0 且没有报错 → 配置目录被老进程接管
                raise LaunchError("exited", tail, rc=rc, label=label)
            if dp.exists():
                try:
                    txt = dp.read_text(encoding="utf-8", errors="replace")
                    first = (txt.splitlines() or [""])[0].strip()
                    if first.isdigit() and int(first) > 0:
                        port = int(first)
                        break
                except OSError:
                    pass
            time.sleep(0.25)

        if not port:
            alive = self.proc.poll() is None
            tail = _read_tail(errpath)
            raise LaunchError("policy" if alive else "exited", tail,
                              rc=self.proc.poll(), label=label)

        # 端口开了还要确认 HTTP 真的应答（且不走代理）
        if not self._probe(port, 15):
            raise LaunchError("not_listening", _read_tail(errpath), label=label)
        return port

    def _probe(self, port: int, timeout: float = 15) -> bool:
        opener = _no_proxy_opener()
        url = f"http://127.0.0.1:{port}"
        end = time.time() + timeout
        while time.time() < end:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                with opener.open(url + "/json/version", timeout=2) as r:
                    if r.status == 200:
                        try:
                            info = json.loads(r.read().decode("utf-8", "replace"))
                            bv = str(info.get("Browser") or "")
                            if bv:
                                self.browser_version = bv
                        except Exception:
                            pass
                        with opener.open(url + "/json/list", timeout=2) as r2:
                            targets = json.loads(r2.read().decode("utf-8", "replace"))
                        if any(t.get("type") == "page" for t in targets or []):
                            return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def _launch_kernel(self, label: str, exe: str):
        """准备配置目录 + 分级自愈启动

        每一级失败都会自动进入下一级，不需要用户干预：
          1 直接启动        —— 正常情况
          2 兼容模式        —— 沙箱初始化失败（安全软件 / WDAC 拦截）
          3 重建配置目录    —— profile 损坏或与内核不兼容（实测会让内核启动即崩）
          4 最小参数        —— 排除反检测参数 / 语言参数引起的异常
        每级内部还会先尝试结束占用配置目录的残留浏览器进程再重试一次。
        """
        udd = self._ensure_profile(label, exe)
        self.profile_dir = udd
        stages = [
            ("直接启动", dict(compat=False, heal=False, minimal=False)),
            ("兼容模式", dict(compat=True, heal=False, minimal=False)),
            ("重建配置目录", dict(compat=True, heal=True, minimal=False)),
            ("最小参数", dict(compat=True, heal=True, minimal=True)),
        ]
        last: LaunchError | None = None
        for name, opt in stages:
            if opt["heal"]:
                try:
                    self._heal_profile(udd)
                except Exception as e:
                    self.step(f"重建配置目录失败（{str(e)[:60]}），继续尝试", False)
                udd.mkdir(parents=True, exist_ok=True)
                self.profile_dir = udd
            tried_kill = False
            while True:
                try:
                    self.port = self._launch(exe, label, udd, opt["compat"], opt["minimal"])
                    self.compat = bool(opt["compat"])
                    if name != "直接启动":
                        self.step(f"{label} 已在「{name}」策略下启动成功")
                    return
                except LaunchError as e:
                    last = e
                    self.step(f"{label} · {name} 失败 —— {e.short}", False)
                    self._kill()
                    # 目录被占用 / 秒退：清掉残留进程再重试一次本级
                    if not tried_kill and (e.kind == "exited" or e.rc == 0):
                        tried_kill = True
                        killed = kill_profile_holders(udd)
                        clear_profile_locks(udd)
                        if killed:
                            self.step(f"发现 {killed} 个残留浏览器进程占用配置目录，"
                                      f"已结束并重试", False)
                            time.sleep(1.2)
                            continue
                    break
        assert last is not None
        raise last

    def _connect(self, p):
        """带重试的 CDP 连接"""
        self.step("启动 Playwright 驱动")
        browser = None
        last = None
        for attempt in range(3):
            try:
                browser = p.chromium.connect_over_cdp(self.cdp_url, timeout=20000)
                break
            except Exception as e:
                last = e
                self.step(f"CDP 连接第 {attempt + 1}/3 次失败（{str(e)[:70]}），3s 后重试", False)
                time.sleep(3)
        if browser is None:
            raise LaunchError("not_listening", f"连不上调试端口：{last}")
        self.step("CDP 已连接")
        return browser

    # ---------------------------------------------------------- 主流程
    def _do_preheat(self):
        hold = self._hold      # 本次预热专属的存活信号；重新预热时会被替换
        try:
            self.step("关闭上一次的浏览器实例")
            self._kill()
            self.kernel = ""
            self.browser_version = ""

            cands = candidate_browsers()
            if not cands:
                raise RuntimeError(
                    "没有找到可用的浏览器：系统里没有 Chrome / Edge，"
                    "Playwright 内核也没装。请到「环境」面板点一次「安装内核」。")
            self._diag["kernels"] = [{"label": lb, "path": p} for lb, p in cands]
            self.step("可用内核：" + "、".join(lb for lb, _ in cands))

            failures: list[str] = []
            for label, exe in cands:
                if hold is not self._hold:
                    return
                try:
                    self._launch_kernel(label, exe)
                except LaunchError as e:
                    failures.append(f"{label}：{e.short}")
                    self.step(f"{label} 不可用 —— {e.short}", False)
                    self._kill()
                    time.sleep(0.6)
                    continue
                except Exception as e:
                    failures.append(f"{label}：{str(e)[:120]}")
                    self.step(f"{label} 不可用 —— {str(e)[:120]}", False)
                    self._kill()
                    time.sleep(0.6)
                    continue

                self.kernel = label
                self.step(f"{label} 就绪"
                          + (f"（{self.browser_version}）" if self.browser_version else "")
                          + f"，调试端口 {self.port}")
                try:
                    self._open_pages(hold)
                    return
                except LaunchError as e:
                    failures.append(f"{label}：{e.short}")
                    self.step(f"{label} 连接失败 —— {e.short}", False)
                    self._kill()
                    time.sleep(0.6)
                except Exception as e:
                    failures.append(f"{label}：{str(e)[:120]}")
                    self.step(f"{label} 连接失败 —— {str(e)[:120]}", False)
                    self._kill()
                    time.sleep(0.6)

            self._diag["stderr"] = _read_tail(self._errpath)
            raise RuntimeError(
                "所有浏览器内核都启动失败：\n" + "\n".join(failures)
                + "\n\n点「复制诊断」把这段发给技术支持。常见处理："
                "\n1) 关闭所有浏览器窗口（含任务管理器里的残留 chrome.exe）后重试"
                "\n2) 把杀毒软件 / 安全卫士对本程序与浏览器内核目录加白名单"
                "\n3) 若公司电脑有应用控制策略，改用系统 Chrome 或 Edge")

        except Exception as e:
            # 上一次预热留下的线程（持有的是旧 event）失败是正常的：新预热已经
            # 把它连着浏览器一起替换掉了，不要覆盖新状态。
            if hold is not self._hold:
                return
            with self._lock:
                self.state = "error"
                self.error = str(e)
            self.step(f"预热失败：{str(e)[:220]}", False)
            self._dump_debug()

    def _open_pages(self, hold: threading.Event):
        """连上浏览器、补齐标签页、开移动端标签页，然后挂住连接"""
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
            for i in range(1, self.size):
                try:
                    pg = ctx.new_page()
                    pg.goto(home, wait_until="domcontentloaded", timeout=45000)
                    pages.append(pg)
                    self.step(f"预热标签页 {i + 1}/{self.size}")
                except Exception as e:
                    self.step(f"标签页 {i + 1} 打开失败（{str(e)[:60]}），"
                              f"稍后抓取时会自动补", False)
                time.sleep(0.5)
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
                time.sleep(0.4)
            mob_real = len(mob_pages)
            try:
                pages[0].bring_to_front()
            except Exception:
                pass

            self.step(f"预热完成：{self.size} 个桌面标签页"
                      + (f" + {mob_real} 个移动端标签页" if mob_real else "")
                      + "，共享同一份登录态")

            with self._lock:
                self.state = "ready"
                self.ready_at = time.time()
                self.mobile_ready = mob_real
                self.message = (f"已就绪 · {self.size} 路并行"
                                + (f" · 移动端 {mob_real} 路" if mob_real else "")
                                + (f" · {self.kernel}" if self.kernel else ""))
            self.step("可以在任意标签页里登录 / 过验证码，然后点「开始抓取」")

            # 移动端的 UA / 视口覆盖绑定在**这条连接**上，连接一断浏览器就会
            # 退回桌面渲染 —— 所以预热完成后不能退出 with，用事件挂住。
            while not hold.wait(1.0):
                if hold is not self._hold or not self._alive():
                    break

    def repair(self) -> dict:
        """一键修复：把「浏览器调不起来」的常见原因挨个清掉并体检

        只做安全动作：结束**我们自己的**配置目录相关的残留进程、清锁、
        检查磁盘与目录完整性、检查内核文件。不动用户的日常浏览器。
        """
        with self._lock:
            if self.state == "starting":
                return {"ok": False, "actions": [],
                        "problems": ["正在预热中，请等预热结束或先关闭浏览器"],
                        "kernels": []}
            self.state = "idle"
            self.error = ""
            self.message = "修复中…"
            self.mobile_ready = 0
            self.steps = []

        actions: list[str] = []
        problems: list[str] = []

        profiles: list[tuple[str, Path]] = []
        for label in ("Chrome", "Edge", "Playwright"):
            try:
                profiles.append((label, self._profile_dir(label)))
            except Exception:
                pass

        # 1) 结束占用配置目录的残留浏览器 + 清锁
        killed_total = 0
        for label, udd in profiles:
            n = kill_profile_holders(udd)
            if n:
                killed_total += n
                actions.append(f"结束 {label} 配置目录的残留浏览器进程 {n} 个")
            c = clear_profile_locks(udd)
            if c:
                actions.append(f"清除 {label} 配置目录的锁文件 {c} 个")
        if not killed_total:
            actions.append("没有发现占用配置目录的残留浏览器进程")

        # 2) 磁盘空间
        try:
            free_gb = shutil.disk_usage(profile_root()).free / (1024 ** 3)
            if free_gb < 2:
                problems.append(f"磁盘剩余空间不足（{free_gb:.1f} GB），"
                                f"浏览器配置写不进去")
            else:
                actions.append(f"磁盘剩余 {free_gb:.1f} GB")
        except Exception:
            pass

        # 3) 配置目录完整性
        for label, udd in profiles:
            if not udd.exists():
                continue
            if not ((udd / "Local State").exists()
                    and (udd / "Default" / "Network" / "Cookies").exists()):
                problems.append(f"{label} 配置目录不完整，预热时会自动重建"
                                f"（登录态会尽量保留）")

        # 4) 内核体检
        cands = candidate_browsers()
        for label, exe in cands:
            try:
                size = Path(exe).stat().st_size
                v = _exe_version(exe)
            except Exception:
                size, v = 0, ""
            if size < 1024:
                problems.append(f"{label} 内核文件可疑（{size} 字节）：{exe}")
            else:
                actions.append(f"{label} 内核可用" + (f"（{v}）" if v else ""))
        if not cands:
            problems.append("系统里没有任何可用浏览器内核，需要下载 Playwright 内核")

        # 5) 配置根目录可写性
        root_ok = False
        try:
            probe = profile_root() / "._repair_probe"
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
            root_ok = True
            actions.append(f"配置目录可写：{profile_root()}")
        except Exception as e:
            problems.append(f"配置根目录不可写（{str(e)[:80]}）")

        if not problems:
            actions.append("体检未发现异常，可以重新预热")
        self.step("修复完成：" + ("；".join(problems) if problems else "未发现异常"),
                  not problems)
        return {
            "ok": root_ok and bool(cands),
            "actions": actions,
            "problems": problems,
            "kernels": [{"label": lb, "path": p} for lb, p in cands],
            "profile_root": str(profile_root()),
            "need_install": not cands,
        }

    def _dump_debug(self):
        """失败时落一份诊断文件，用户直接把这个文件发过来就行"""
        try:
            d = self.diagnose()
            p = profile_root() / "browser_debug.log"
            p.write_text(
                time.strftime("%Y-%m-%d %H:%M:%S") + "\n" +
                json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            self._diag["debug_file"] = str(p)
        except Exception:
            pass

    # ------------------------------------------------------------- 关闭
    def _kill(self):
        p = self.proc
        self.proc = None
        self.port = 0
        if p:
            try:
                if sys.platform == "win32" and p.poll() is None:
                    # /T 连子进程一起收掉，避免 Crashpad / GPU 进程残留占着 profile
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
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
        if self._errfile is not None:
            try:
                self._errfile.close()
            except Exception:
                pass
            self._errfile = None
        # 注意：不删 stderr 临时文件 —— 失败诊断还要读它，下次启动时才覆盖

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
