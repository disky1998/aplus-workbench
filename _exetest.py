# -*- coding: utf-8 -*-
"""对打包后的 exe 做黑盒验收：
   启动 -> 服务可用 -> 页面可渲染 -> Playwright driver 可用 -> 真实抓一次 -> 收工
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = HERE / "dist" / "APlusWorkbench.exe"
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
out = {"exe_mb": round(EXE.stat().st_size / 1048576, 1) if EXE.exists() else 0}


def call(path, method="GET", body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


env = dict(os.environ)
env["APLUS_PORT"] = str(PORT)
proc = subprocess.Popen([str(EXE)], env=env, cwd=str(HERE),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
out["pid"] = proc.pid

try:
    # 1. 等服务起来（onefile 首次要解包，给足时间）
    t0 = time.time()
    ready = False
    for _ in range(120):
        if proc.poll() is not None:
            out["exit_early"] = proc.returncode
            break
        try:
            call("/api/version", timeout=3)
            ready = True
            break
        except Exception:
            time.sleep(0.5)
    out["boot_seconds"] = round(time.time() - t0, 1)
    out["ready"] = ready

    if ready:
        out["version"] = call("/api/version")
        env_i = call("/api/env")
        out["env"] = {"version": env_i.get("version"), "frozen": env_i.get("frozen"),
                      "python": env_i.get("python"),
                      "chrome": (env_i.get("chrome") or {}).get("version"),
                      "playwright": (env_i.get("playwright") or {}).get("installed"),
                      "outdir": env_i.get("outdir")}
        try:
            out["update"] = call("/api/update/check?force=1", timeout=40)
        except Exception as e:
            out["update"] = {"error": str(e)}

        # 页面（验证 web/index.html 被打进包并能读到）
        try:
            html = urllib.request.urlopen(BASE + "/", timeout=15).read().decode("utf-8")
            out["page_ok"] = "A+ 抓取工作台" in html and 'id="updModal"' in html
            out["page_bytes"] = len(html)
        except Exception as e:
            out["page_ok"] = f"ERR {e}"

        # 2. 真实抓一次 —— 这一步才能证明 exe 里的 playwright driver 可用
        try:
            r = call("/api/run", "POST", {
                "text": "EXE-TEST https://www.amazon.ae/dp/B0BTPX24RV",
                "domain": "ae", "both": False, "aplusMode": "standard",
                "downloadImages": False, "includeBrandStory": False, "screenshot": False,
                "hires": 0, "delay": 0, "retry": 0, "headed": False,
                "proxy": "", "userDataDir": "", "persist": True,
                "buildCombined": False, "packageImages": False}, timeout=60)
            tid = r["task_id"]
            since, logs, final = 0, [], None
            deadline = time.time() + 300
            while time.time() < deadline:
                st = call(f"/api/task/{tid}?since={since}", timeout=60)
                logs += st.get("logs", [])
                since = st.get("next", since)
                if st["status"] in ("done", "error", "stopped"):
                    final = st
                    break
                time.sleep(2)
            out["scrape"] = {
                "status": (final or {}).get("status"),
                "results": (final or {}).get("results"),
                "logs": logs[-24:],
            }
        except Exception as e:
            out["scrape"] = {"error": f"{type(e).__name__}: {e}"}
finally:
    try:
        proc.terminate()
        time.sleep(1.5)
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    out["killed"] = proc.poll() is not None

Path(HERE / "_exetest.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
print("done")
