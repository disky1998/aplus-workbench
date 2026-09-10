# -*- coding: utf-8 -*-
"""端到端：新名字 / 抓取内容分段 / 商品信息抓取 / 导出接口"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8799"
out = {}


def call(path, method="GET", body=None, timeout=90, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        if raw:
            return {"status": r.status, "bytes": len(b),
                    "cd": r.headers.get("Content-Disposition"),
                    "ctype": r.headers.get("Content-Type")}
        return json.loads(b.decode("utf-8"))


for _ in range(40):
    try:
        call("/api/version", timeout=5)
        break
    except Exception:
        time.sleep(0.5)

out["version"] = call("/api/version")

# 页面元素
html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode("utf-8")
out["page"] = {
    "标题": "亚马逊采集工作台" in html,
    "抓取内容分段": 'id="wantSeg"' in html,
    "模式区块id": 'id="modeField"' in html,
    "产物区块id": 'id="prodField"' in html,
    "结果表商品列": "商品信息</th>" in html,
    "导出按钮": "/api/export/product.xlsx" in html,
    "下载图片默认开": 'id="dlImg" checked' in html,
    "有头默认开": 'id="headed" checked' in html,
    "站点两个": 'SITE_LIST = [["ae","阿联酋"],["sa","沙特"]]' in html,
}

# 真浏览器交互
try:
    from playwright.sync_api import sync_playwright
    errs = []
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_context(viewport={"width": 1440, "height": 1080},
                            device_scale_factor=2).new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="networkidle", timeout=12000)
        pg.wait_for_timeout(2500)
        ui = {
            "console_errors": errs[:6],
            "抓取内容按钮数": pg.locator("#wantSeg button").count(),
            "默认抓取内容": pg.input_value("#want"),
            "A+模式可见": pg.locator("#modeField").is_visible(),
            "产物可见": pg.locator("#prodField").is_visible(),
            "下载图片勾选": pg.is_checked("#dlImg"),
            "有头已勾选": pg.is_checked("#headed"),
        }
        pg.locator('#wantSeg button[data-v="product"]').click()
        pg.wait_for_timeout(500)
        ui["切到商品信息后_A+模式隐藏"] = not pg.locator("#modeField").is_visible()
        ui["切到商品信息后_产物隐藏"] = not pg.locator("#prodField").is_visible()
        pg.locator('#wantSeg button[data-v="all"]').click()
        pg.wait_for_timeout(500)
        ui["切回全部后_A+模式可见"] = pg.locator("#modeField").is_visible()
        ui["wantInd"] = pg.evaluate("()=>document.getElementById('wantInd').style.width")
        pg.screenshot(path="_ui3_light.png", full_page=True)
        br.close()
    out["ui"] = ui
except Exception as e:
    out["ui"] = {"error": f"{type(e).__name__}: {e}"}

# 真实抓取：只抓商品信息
try:
    r = call("/api/run", "POST", {
        "text": "PROD-TEST https://www.amazon.ae/dp/B0BTPX24RV",
        "domain": "ae", "want": "product", "both": False, "aplusMode": "standard",
        "downloadImages": False, "includeBrandStory": False, "screenshot": False,
        "hires": 0, "delay": 0, "retry": 0, "headed": False,
        "proxy": "", "userDataDir": "", "persist": True,
        "buildCombined": False, "packageImages": False}, timeout=60)
    tid = r["task_id"]
    since, logs, final = 0, [], None
    deadline = time.time() + 240
    while time.time() < deadline:
        st = call(f"/api/task/{tid}?since={since}", timeout=60)
        logs += st.get("logs", [])
        since = st.get("next", since)
        if st["status"] in ("done", "error", "stopped"):
            final = st
            break
        time.sleep(2)
    out["live"] = {"task": tid, "status": (final or {}).get("status"),
                   "results": (final or {}).get("results"), "logs": logs[-18:],
                   "dir": (final or {}).get("outdir", "").split("\\")[-1]}
    d = out["live"]["dir"]
    if d:
        for kind, path in (("xlsx", f"/api/export/product.xlsx?dir={d}"),
                           ("csv", f"/api/export/product.csv?dir={d}")):
            try:
                out["live"][f"export_{kind}"] = call(path, raw=True)
            except urllib.error.HTTPError as e:
                out["live"][f"export_{kind}"] = {"status": e.code,
                                                 "body": e.read().decode("utf-8")[:120]}
except Exception as e:
    out["live"] = {"error": f"{type(e).__name__}: {e}"}

# 历史接口
try:
    h = call("/api/history")
    out["history"] = [{"id": it["id"], "has_product": it["has_product"],
                       "product_count": it["product_count"],
                       "sku_count": it["sku_count"]} for it in h["items"][:4]]
except Exception as e:
    out["history"] = {"error": str(e)}

HERE.joinpath("_e2e2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
print("done")
