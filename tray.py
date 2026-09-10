# -*- coding: utf-8 -*-
"""桌面外壳：后台跑 Web 服务 + 右下角系统托盘

双击 exe / python tray.py 后会：
  * 起本地服务（127.0.0.1:8788）
  * 右下角出现托盘图标
  * 托盘右键：打开前台 / 获取更新 / 打开输出目录 / 关于 / 退出
  * 启动后自动在后台检查一次更新，有新版弹气泡

主线程跑托盘消息循环，服务在后台线程里。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import version as V  # noqa: E402

PORT = int(os.environ.get("APLUS_PORT", "8788"))
URL = f"http://127.0.0.1:{PORT}/"


def _pick_log_dir() -> Path:
    """日志目录：优先 exe 同级的 logs/，不可写则退回 %TEMP%"""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).resolve().parent / "logs")
    cands.append(HERE / "logs")
    cands.append(Path(os.environ.get("TEMP") or ".") / "AmazonWorkbench")
    for d in cands:
        try:
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "workbench.log", "a", encoding="utf-8"):
                pass
            return d
        except Exception:
            continue
    return Path(os.environ.get("TEMP") or ".")


LOG_DIR = _pick_log_dir()
LOG_FILE = LOG_DIR / "workbench.log"


def log(msg: str):
    """无控制台模式下的排障日志"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def ensure_std_streams():
    """修掉「无控制台打包」的致命坑。

    console=False 打包后 sys.stdout / sys.stderr 是 None，而 uvicorn 的
    ColourizedFormatter 初始化时会调 sys.stdout.isatty() → AttributeError，
    被 logging.config.dictConfig 包成
    ValueError: Unable to configure formatter 'default'，服务根本起不来。

    这里把它们换成真实可写的文件流，顺便把服务日志落到 logs/server.log。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_DIR / "server.log", "a", encoding="utf-8",
                      errors="replace", buffering=1)
    except Exception:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _excepthook(exc_type, exc, tb):
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log("UNCAUGHT:\n" + text)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, f"程序出错：\n{_short_tb(text)}\n\n日志：{LOG_FILE}",
            V.APP_NAME, 0x10)
    except Exception:
        pass


sys.excepthook = _excepthook


# ---------------------------------------------------------------- 运行环境
def prepare_env():
    """打包后：复用系统里已下载的 Playwright 内核（不打进 exe）"""
    if not getattr(sys, "frozen", False):
        return
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(base) / "ms-playwright")


def open_front(*_):
    try:
        os.startfile(URL) if sys.platform == "win32" else None  # noqa: S606
    except Exception:
        import webbrowser
        webbrowser.open(URL)


def already_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/version", timeout=1.6) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------- 服务线程
SERVER_ERROR: list = []

# 不用 uvicorn 内置的 LOGGING_CONFIG：它引用 uvicorn.logging.ColourizedFormatter，
# 该类初始化会读 sys.stdout.isatty()，在无控制台环境里直接抛异常。
# 这里用纯标准库 Formatter，不碰 isatty。
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


def _short_tb(tb: str, n: int = 5) -> str:
    lines = [l for l in (tb or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def start_server():
    try:
        import uvicorn
        import webapp
        log(f"starting server on {URL} (frozen={getattr(sys, 'frozen', False)}, "
            f"uvicorn={getattr(uvicorn, '__version__', '?')}, "
            f"stdout={'None' if sys.stdout is None else 'ok'})")
        cfg = uvicorn.Config(webapp.app, host="127.0.0.1", port=PORT,
                             log_config=UVICORN_LOG_CONFIG, log_level="info",
                             access_log=False)
        uvicorn.Server(cfg).run()
    except Exception as e:                      # 端口占用 / 依赖缺失
        import traceback
        tb = traceback.format_exc()
        log("server error: " + repr(e))
        log(tb)
        SERVER_ERROR.append(_short_tb(tb))


# ---------------------------------------------------------------- 托盘
def do_check_update(icon, item):
    import webapp
    up = webapp.UPDATER
    icon.notify("正在检查更新…", V.APP_NAME)
    info = up.check(force=True)
    if not info.get("ok"):
        icon.notify(info.get("error") or "检查失败", V.APP_NAME)
        return
    if not info.get("has_update"):
        icon.notify(f"已是最新版本 v{info.get('current', V.__version__)}", V.APP_NAME)
        return

    url = info.get("download_url")
    if not url:
        icon.notify(f"发现 v{info['latest']}，请到发布页下载", V.APP_NAME)
        open_front()
        return

    icon.notify(f"发现新版本 v{info['latest']}，开始下载…", V.APP_NAME)
    if not up.start_download(url, V.ASSET_NAME, info.get("download_asset_id")):
        icon.notify("已有下载在进行中", V.APP_NAME)
        return

    for _ in range(900):                        # 最长等 15 分钟
        d = up.download
        if d.get("ok") is False:
            icon.notify("更新失败：" + str(d.get("msg") or ""), V.APP_NAME)
            return
        if up.should_exit():
            time.sleep(2.0)
            try:
                icon.stop()
            except Exception:
                pass
            os._exit(0)
        time.sleep(1)
    icon.notify("下载超时，请稍后在页面里重试", V.APP_NAME)


def do_open_out(icon, item):
    try:
        import webapp
        os.startfile(str(webapp.WEB_OUT))       # noqa: S606
    except Exception:
        pass


def do_about(icon, item):
    import webapp
    info = webapp.UPDATER.check()
    latest = info.get("latest") or "—"
    icon.notify(f"当前 v{V.__version__} · 最新 v{latest}\n源：{V.GITHUB_REPO}", V.APP_NAME)


def do_quit(icon, item):
    try:
        icon.stop()
    except Exception:
        pass
    os._exit(0)


def run_tray():
    import pystray
    import webapp
    from appicon import build_icon_image

    menu = pystray.Menu(
        pystray.MenuItem("打开前台", open_front, default=True),
        pystray.MenuItem("获取更新", do_check_update),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开输出目录", do_open_out),
        pystray.MenuItem("关于", do_about),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", do_quit),
    )
    icon = pystray.Icon(
        "amazon-workbench", build_icon_image(64),
        f"{V.APP_NAME} v{V.__version__}", menu)

    def on_setup(ic):
        ic.visible = True
        time.sleep(1.2)
        # 启动后自动检查一次更新
        try:
            info = webapp.UPDATER.check()
            if info.get("ok") and info.get("has_update"):
                ic.notify(f"发现新版本 v{info['latest']}（当前 v{V.__version__}）\n"
                          f"右键托盘图标即可更新", V.APP_NAME)
            elif not info.get("ok"):
                pass                            # 没网就静默
        except Exception:
            pass

    threading.Thread(target=on_setup, args=(icon,), daemon=True).start()
    icon.run()


# ---------------------------------------------------------------- 入口
def main():
    ensure_std_streams()        # 必须最先做：无控制台环境下补 sys.stdout / sys.stderr
    prepare_env()

    if already_running():
        open_front()
        return

    threading.Thread(target=start_server, daemon=True).start()

    # 等服务起来再开前台
    for _ in range(50):
        if already_running():
            break
        if SERVER_ERROR:
            break
        time.sleep(0.2)

    if SERVER_ERROR:
        msg = SERVER_ERROR[0]
        log("启动失败，弹窗提示用户")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"服务启动失败：\n{msg}\n\n详细日志：{LOG_FILE}",
                V.APP_NAME, 0x10)
        except Exception:
            pass
        return

    open_front()
    try:
        run_tray()
    except Exception as e:
        # 托盘不可用（缺 pystray / 无桌面）时，退化为纯服务常驻
        log(f"托盘启动失败：{e}")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
