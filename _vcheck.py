# -*- coding: utf-8 -*-
"""源码模式自检：站点收敛 / 版本接口 / 更新接口 / 更新弹窗"""
import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8799"
out = {}


def call(path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


for _ in range(40):
    try:
        call("/api/version", timeout=5)
        break
    except Exception:
        time.sleep(0.5)

try:
    out["version"] = call("/api/version")
except Exception as e:
    out["version"] = {"error": str(e)}

try:
    env = call("/api/env")
    out["env"] = {"version": env.get("version"), "frozen": env.get("frozen"),
                  "python": env.get("python"),
                  "chrome": (env.get("chrome") or {}).get("version"),
                  "playwright_ok": (env.get("playwright") or {}).get("ok")}
except Exception as e:
    out["env"] = {"error": str(e)}

try:
    out["update_check"] = call("/api/update/check?force=1", timeout=40)
except Exception as e:
    out["update_check"] = {"error": str(e)}

try:
    st = call("/api/update/status")
    out["update_status"] = {"current": st.get("current"), "frozen": st.get("frozen"),
                            "download": st.get("download")}
except Exception as e:
    out["update_status"] = {"error": str(e)}

# 页面自检（含更新弹窗元素）
try:
    html = urllib.request.urlopen(BASE + "/", timeout=20).read().decode("utf-8")
    out["page"] = {
        "bytes": len(html),
        "站点只剩两个": html.count('id="siteSeg"') == 1 and 'SITE_LIST = [["ae","阿联酋"],["sa","沙特"]]' in html,
        "有更新按钮": 'id="btnUpdate"' in html,
        "有更新红点": 'id="updDot"' in html,
        "有更新弹窗": 'id="updModal"' in html,
        "有更新日志区": 'id="updBd"' in html,
        "有自动检查": "checkUpdate(true)" in html,
        "有版本KV": 'id="cVer2"' in html,
    }
except Exception as e:
    out["page"] = {"error": str(e)}

# 真浏览器渲染 + 弹窗交互
try:
    from playwright.sync_api import sync_playwright
    errs = []
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_context(viewport={"width": 1440, "height": 1000},
                            device_scale_factor=2).new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="networkidle", timeout=10000)
        pg.wait_for_timeout(3200)          # 等自动检查更新跑完
        inter = {
            "站点数": pg.locator("#siteSeg button").count(),
            "站点文案": pg.locator("#siteSeg").inner_text().replace("\n", "/"),
            "默认站点": pg.input_value("#domain"),
            "版本KV": pg.inner_text("#cVer2"),
            "console_errors": errs[:6],
        }
        pg.click("#btnUpdate")
        pg.wait_for_timeout(2500)
        inter["弹窗打开"] = pg.locator("#updModal.open").count() > 0
        inter["弹窗内容"] = pg.inner_text("#updBd")[:160].replace("\n", " | ")
        pg.screenshot(path="_upd_modal.png", clip={"x": 380, "y": 60, "width": 700, "height": 520})
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)
        inter["Esc关闭"] = pg.locator("#updModal.open").count() == 0
        pg.screenshot(path="_ui2_light.png", full_page=True)
        pg.click("#btnTheme")
        pg.wait_for_timeout(600)
        inter["主题"] = pg.evaluate("()=>document.documentElement.dataset.theme")
        pg.screenshot(path="_ui2_dark.png", clip={"x": 0, "y": 0, "width": 1440, "height": 760})
        br.close()
    out["ui"] = inter
except Exception as e:
    out["ui"] = {"error": f"{type(e).__name__}: {e}"}

Path(HERE / "_vcheck.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
print("done")
