# -*- coding: utf-8 -*-
"""端到端验证：真实网络下确认节流生效、任务仍然能正常跑完。

用法：先起服务（默认 8040），再 python tools/verify_throttle_live.py
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8042")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with _opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    h0 = call("GET", "/api/health")
    print("提交前 throttle.waited =", h0["throttle"]["waited"])

    t0 = time.time()
    task = call("POST", "/api/tasks", {
        "kind": "subs", "target": "up:329954990", "limit": 2,
        "formats": ["txt"], "outdir": "/tmp/throttle_out",
    })
    print("已提交", task["id"], "状态", task["status"])

    last_msg = ""
    while True:
        d = call("GET", "/api/tasks/" + task["id"])
        if d["message"] != last_msg:
            last_msg = d["message"]
            print("  %.1fs  %-8s %s" % (time.time() - t0, d["status"], last_msg))
        if d["status"] in ("done", "failed"):
            break
        time.sleep(0.5)

    elapsed = time.time() - t0
    h1 = call("GET", "/api/health")
    paid = h1["throttle"]["waited"] - h0["throttle"]["waited"]

    print()
    print("=" * 60)
    print("状态      :", d["status"])
    if d["error"]:
        print("错误      :", d["error"][:300])
    res = d.get("result") or {}
    if res:
        print("视频数    :", res.get("videos"))
        print("写入文件  :", res.get("parts_written"))
        print("无字幕    :", res.get("no_subtitle"))
        print("失败      :", res.get("failed"))
    print("总耗时    : %.1fs" % elapsed)
    print("其中限速  : %.1fs" % paid)
    print("=" * 60)
    return 0 if d["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
