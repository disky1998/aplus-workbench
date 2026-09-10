# -*- coding: utf-8 -*-
"""更新检查 / 下载 / 自替换

更新源：GitHub Release
  1) 优先走 API  /repos/<owner>/<repo>/releases/latest
  2) API 限流或不可达时，回退读仓库根目录的 version.json

运行形态：
  * 打包成 exe（frozen）—— 可下载新 exe 并自动替换重启
  * 源码 / python 运行 —— 只能检查与跳转下载，不自动替换
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

import version as V

TIMEOUT = 15
CACHE_TTL = 600          # 检查结果缓存 10 分钟
USER_AGENT = f"APlusWorkbench/{V.__version__} (+{V.GITHUB_REPO})"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """exe 所在目录（frozen）或项目目录"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def read_token() -> str | None:
    """GitHub 访问令牌（私有仓库必需）。

    刻意**不打进 exe**，按优先级从外部读取，避免密钥随 exe 分发：
      1. 环境变量 APLUS_GH_TOKEN
      2. 程序同目录 gh_token.txt
      3. 程序同目录 gh_token.py（TOKEN = "..."）
    """
    t = (os.environ.get("APLUS_GH_TOKEN") or "").strip()
    if t:
        return t
    base = app_dir()
    p = base / "gh_token.txt"
    if p.exists():
        try:
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return v
        except Exception:
            pass
    p = base / "gh_token.py"
    if p.exists():
        try:
            ns: dict = {}
            exec(p.read_text(encoding="utf-8"), ns)   # noqa: S102
            v = (ns.get("TOKEN") or "").strip()
            if v:
                return v
        except Exception:
            pass
    return None


class Updater:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {"ts": 0, "data": None}
        self.download = {"running": False, "percent": 0, "msg": "", "ok": None,
                         "path": None, "updated": 0}

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        h = {"User-Agent": USER_AGENT, "Accept": accept}
        tok = read_token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    @staticmethod
    def has_token() -> bool:
        return bool(read_token())

    # ------------------------------------------------------------ 检查
    def check(self, force: bool = False) -> dict:
        with self._lock:
            if (not force and self._cache["data"]
                    and time.time() - self._cache["ts"] < CACHE_TTL):
                return self._cache["data"]

        status = {"api": None, "raw": None, "err": None}
        data = (self._check_api(status) or self._check_raw(status)
                or {"ok": False, "error": self._explain(status),
                    "current": V.__version__, "has_update": False})
        data.setdefault("current", V.__version__)
        data["frozen"] = is_frozen()
        data["repo"] = V.GITHUB_REPO
        data["has_token"] = self.has_token()
        data["releases_page"] = V.RELEASES_PAGE

        with self._lock:
            self._cache = {"ts": time.time(), "data": data}
        return data

    def _explain(self, status: dict) -> str:
        codes = [c for c in (status.get("api"), status.get("raw")) if c]
        if 404 in codes and not self.has_token():
            return (f"读不到更新信息：{V.GITHUB_REPO} 上还没有 Release 或 version.json，"
                    f"或该仓库是私有的（私有仓库需要把只读令牌放到程序同目录的 gh_token.txt）。")
        if 404 in codes:
            return (f"还没发布过版本（{V.GITHUB_REPO} 上没有 Release 或 version.json）。"
                    f"首次发布后此页面就能检查更新。")
        if 401 in codes:
            return "访问令牌无效或已过期，请更新 gh_token.txt"
        if 403 in codes or 429 in codes:
            return "GitHub 接口限流，稍后再试（或直接去发布页看）"
        if status.get("err"):
            return f"网络不通：{status['err']}"
        return "无法连接 GitHub，请检查网络"

    def _check_api(self, status: dict | None = None) -> dict | None:
        try:
            r = requests.get(V.RELEASE_API, timeout=TIMEOUT, headers=self._headers())
            if status is not None:
                status["api"] = r.status_code
            if r.status_code != 200:
                return None
            j = r.json()
        except Exception as e:
            if status is not None:
                status["err"] = str(e)
            return None

        tag = j.get("tag_name") or j.get("name") or ""
        assets = []
        for a in j.get("assets") or []:
            assets.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "size": a.get("size"),
                "url": a.get("browser_download_url"),
                "is_exe": str(a.get("name", "")).lower().endswith(".exe"),
            })
        exe = next((a for a in assets if a["is_exe"]), None)
        return {
            "ok": True,
            "source": "release",
            "latest": tag.lstrip("v"),
            "tag": tag,
            "notes": j.get("body") or "",
            "published": j.get("published_at") or "",
            "has_update": V.is_newer(tag, V.__version__),
            "assets": assets,
            "download_url": (exe or {}).get("url"),
            "download_asset_id": (exe or {}).get("id"),
            "download_size": (exe or {}).get("size"),
            "html_url": j.get("html_url") or V.RELEASES_PAGE,
        }

    def _check_raw(self, status: dict | None = None) -> dict | None:
        """私有仓库的 raw.githubusercontent 也要认证，所以走 contents API"""
        url = f"https://api.github.com/repos/{V.GITHUB_REPO}/contents/version.json"
        try:
            r = requests.get(url, timeout=TIMEOUT,
                             headers=self._headers("application/vnd.github.raw+json"))
            if status is not None:
                status["raw"] = r.status_code
            if r.status_code != 200:
                return None
            j = json.loads(r.text)
        except Exception as e:
            if status is not None and not status.get("err"):
                status["err"] = str(e)
            return None
        latest = str(j.get("version") or "").lstrip("v")
        return {
            "ok": True,
            "source": "version.json",
            "latest": latest,
            "tag": "v" + latest,
            "notes": j.get("notes") or "",
            "published": j.get("published") or "",
            "has_update": V.is_newer(latest, V.__version__),
            "assets": j.get("assets") or [],
            "download_url": j.get("download_url"),
            "download_asset_id": None,
            "download_size": j.get("size"),
            "html_url": j.get("html_url") or V.RELEASES_PAGE,
        }

    # ------------------------------------------------------------ 下载
    def start_download(self, url: str, name: str = V.ASSET_NAME,
                       asset_id=None) -> bool:
        if self.download.get("running"):
            return False
        self.download.update({"running": True, "percent": 0, "msg": "连接中…",
                              "ok": None, "path": None, "updated": time.time()})
        threading.Thread(target=self._do_download, args=(url, name, asset_id),
                         daemon=True).start()
        return True

    def _set(self, **kw):
        self.download.update(kw)
        self.download["updated"] = time.time()

    def _do_download(self, url: str, name: str, asset_id=None):
        try:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or V.ASSET_NAME
            dest_dir = app_dir() / "_update"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / safe

            # 私有仓库：走 API 资产端点（302 到预签名地址），带令牌
            headers = self._headers("application/octet-stream")
            if asset_id and read_token():
                url = f"https://api.github.com/repos/{V.GITHUB_REPO}/releases/assets/{asset_id}"
            else:
                headers.pop("Authorization", None)

            with requests.get(url, stream=True, timeout=60, headers=headers) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        pct = int(done * 100 / total) if total else min(95, done // (1024 * 1024))
                        self._set(percent=pct,
                                  msg=f"下载中 {done // 1048576:.1f}"
                                      + (f" / {total / 1048576:.1f} MB" if total else " MB"))

            if dest.stat().st_size < 1024 * 100:
                raise RuntimeError("下载文件过小，可能不是有效的 exe")

            if is_frozen() and dest.suffix.lower() == ".exe":
                self._set(percent=100, msg="下载完成，准备重启替换…")
                if self._apply_and_restart(dest):
                    self._set(running=False, ok=True, path=str(dest),
                              msg="已开始替换并重启，请稍候…")
                    return
            self._set(running=False, ok=True, path=str(dest), percent=100,
                      msg="下载完成")
            self._open_folder(dest.parent)
        except Exception as e:
            self._set(running=False, ok=False, msg=f"下载失败：{e}")

    # ------------------------------------------------------------ 自替换
    def _apply_and_restart(self, new_exe: Path) -> bool:
        if not is_frozen():
            return False
        cur = Path(sys.executable).resolve()
        if not cur.exists():
            return False
        bat = tempfile.gettempdir() / f"_aplus_patch_{os.getpid()}.bat"
        bat.write_text(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            ":w\r\n"
            f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  ping -n 2 127.0.0.1 >nul\r\n"
            "  goto w\r\n"
            ")\r\n"
            f'move /y "{new_exe}" "{cur}" >nul\r\n'
            f'start "" "{cur}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8")
        try:
            subprocess.Popen(["cmd", "/c", str(bat)], close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            return True
        except Exception:
            return False

    @staticmethod
    def _open_folder(p: Path):
        try:
            if sys.platform == "win32":
                os.startfile(str(p))          # noqa: S606
        except Exception:
            pass

    # ------------------------------------------------------------ 状态
    def status(self) -> dict:
        with self._lock:
            cache = self._cache["data"]
        return {
            "download": dict(self.download),
            "last": cache,
            "current": V.__version__,
            "frozen": is_frozen(),
            "repo": V.GITHUB_REPO,
        }

    def should_exit(self) -> bool:
        """自替换已启动 —— 主进程该退出了"""
        d = self.download
        return bool(d.get("ok") and d.get("percent") == 100
                    and is_frozen() and d.get("path"))
