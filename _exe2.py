# -*- coding: utf-8 -*-
"""重新打包后的 exe 快速冒烟（不跑真实抓取）"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = HERE / "dist" / "APlusWorkbench.exe"
PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
out = {"exe_mb": round(EXE.stat().st_size / 1048576, 1)}


def call(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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
    out["has_state"] = e.get("has_state")
    out["frozen"] = e.get("frozen")
    out["outdir"] = e.get("outdir")
    html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode("utf-8")
    out["page"] = {
        "bytes": len(html),
        "站点两个": 'SITE_LIST = [["ae","阿联酋"],["sa","沙特"]]' in html,
        "登录态徽章": 'id="tipState"' in html,
        "首用引导": "aplus_first_hint" in html,
        "拦截引导": "被亚马逊拦了" in html,
        "更新弹窗": 'id="updModal"' in html,
    }
finally:
    p.terminate(); time.sleep(1.5)
    if p.poll() is None: p.kill()
    out["killed"] = p.poll() is not None

Path(HERE / "_exe2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
print("done")
