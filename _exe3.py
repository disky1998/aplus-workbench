# -*- coding: utf-8 -*-
"""新版 exe 冒烟：名字 / 商品信息抓取贯通 / 导出"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = HERE / "dist" / "AmazonWorkbench.exe"
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
out = {"exe": EXE.name, "exe_mb": round(EXE.stat().st_size / 1048576, 1) if EXE.exists() else 0}


def call(path, method="GET", body=None, timeout=90, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        return ({"status": r.status, "bytes": len(b), "_b": b} if raw
                else json.loads(b.decode("utf-8")))


env = dict(os.environ); env["APLUS_PORT"] = str(PORT)
p = subprocess.Popen([str(EXE)], env=env, cwd=str(HERE),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    t0 = time.time()
    for _ in range(120):
        try:
            call("/api/version", timeout=3); break
        except Exception:
            time.sleep(0.5)
    out["boot_s"] = round(time.time() - t0, 1)
    out["version"] = call("/api/version")
    e = call("/api/env")
    out["env"] = {"frozen": e.get("frozen"), "has_state": e.get("has_state"),
                  "outdir": e.get("outdir")}
    html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode("utf-8")
    out["page"] = {"亚马逊采集工作台": "亚马逊采集工作台" in html,
                   "抓取内容分段": 'id="wantSeg"' in html,
                   "商品信息列": "商品信息</th>" in html,
                   "下载图片默认开": 'id="dlImg" checked' in html,
                   "有头默认开": 'id="headed" checked' in html}

    # 真抓一次（all 模式）
    r = call("/api/run", "POST", {
        "text": "EXE-ALL https://www.amazon.ae/dp/B0BTPX24RV",
        "domain": "ae", "want": "all", "both": True, "aplusMode": "auto",
        "downloadImages": True, "includeBrandStory": False, "screenshot": False,
        "hires": 1464, "delay": 0, "retry": 0, "headed": False,
        "proxy": "", "userDataDir": "", "persist": True,
        "buildCombined": True, "packageImages": True}, timeout=60)
    tid = r["task_id"]
    since, logs, final = 0, [], None
    deadline = time.time() + 300
    while time.time() < deadline:
        st = call(f"/api/task/{tid}?since={since}", timeout=60)
        logs += st.get("logs", [])
        since = st.get("next", since)
        if st["status"] in ("done", "error", "stopped"):
            final = st; break
        time.sleep(2)
    res = (final or {}).get("results") or [{}]
    out["run"] = {"status": (final or {}).get("status"), "ok": res[0].get("ok"),
                  "kind": res[0].get("kind"), "product": res[0].get("product"),
                  "combined": (final or {}).get("combined"),
                  "zipfile": (final or {}).get("zipfile"), "logs": logs[-14:]}
    d = (final or {}).get("combined", "").split("/")[0]
    if d:
        ex = call(f"/api/export/product.xlsx?dir={d}", raw=True)
        out["export"] = {"status": ex["status"], "bytes": ex["bytes"]}
finally:
    p.terminate(); time.sleep(1.5)
    if p.poll() is None: p.kill()
    out["killed"] = p.poll() is not None

HERE.joinpath("_exe3.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
print("done")
