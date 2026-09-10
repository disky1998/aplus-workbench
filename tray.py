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
LOG_FILE = Path(os.environ.get("TEMP") or ".") / "aplus_workbench.log"


def log(msg: str):
    """无控制台模式下的排障日志"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _excepthook(exc_type, exc, tb):
    import traceback
    log("UNCAUGHT: " + "".join(traceback.format_exception(exc_type, exc, tb)))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, f"程序出错：\n{exc}", V.APP_NAME, 0x10)
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


def start_server():
    try:
        import uvicorn
        import webapp
        log(f"starting server on {URL} (frozen={getattr(sys,'frozen',False)})")
        uvicorn.run(webapp.app, host="127.0.0.1", port=PORT, log_level="warning")
    except Exception as e:                      # 端口占用 / 依赖缺失
        log("server error: " + repr(e))
        import traceback
        log(traceback.format_exc())
        SERVER_ERROR.append(str(e))


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
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"服务启动失败：\n{msg}", V.APP_NAME, 0x10)
        except Exception:
            print("服务启动失败:", msg)
        return

    open_front()
    try:
        run_tray()
    except Exception as e:
        # 托盘不可用（缺 pystray / 无桌面）时，退化为纯服务常驻
        print(f"[warn] 托盘启动失败：{e}")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
