# -*- coding: utf-8 -*-
"""给「一键学完」造一个真的多分 P 任务，供 tools/e2e_learn 里的两个脚本驱动。

不 mock：从 `subs/` 里取一份真下载下来的字幕，按顺序切成几段当几个分 P，用项目
自己的 `Store` 写进一个临时 `tasks.db`。阅读页走真路由、模型走真接口。

    python tools/e2e_learn/setup.py            # 4 篇的合集，打印 TASK_ID
    python tools/e2e_learn/setup.py --single   # 只有 1 篇，用来验按钮不该出现

产物写在 `tools/e2e_learn/_work/`（`subs/` `notes/` `tasks.db` 都在 .gitignore 里）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WORK = HERE / "_work"
sys.path.insert(0, str(ROOT))

from asr_polish.server.store import Store   # noqa: E402

BV = "BV1E2EKb9Xy"
SRC = "BV1tTxd6KEeL-全网最细！40分钟速通Codex！【GenJi深度教程】.srt"

#: 4 个分 P：标题 + 从源字幕里取第几条到第几条。切短只是为了少花点 token，
#: 时间轴和文本都是原样的。
CUTS = [
    ("为什么要用 Codex", 0, 30),
    ("环境准备与安装", 30, 62),
    ("第一次对话", 62, 92),
    ("常见报错", 92, 118),
]


def blocks(text: str) -> list[str]:
    """按空行切 srt 块。"""
    return [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]


def safe(name: str) -> str:
    """和 export 那边的文件名规则一致：去掉路径里不能用的字符。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "未命名"


def write(path: Path, picked: list[str]) -> None:
    """序号行重排，时间轴原样保留——每段仍落在它真实的那一秒上。"""
    body = "\n\n".join(
        "%d\n%s" % (i + 1, re.sub(r"^\d+\s*\n", "", b, count=1).strip())
        for i, b in enumerate(picked)
    )
    path.write_text(body + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--single", action="store_true",
                    help="造一个只有 1 篇的任务（「一键学完」不该出现）")
    args = ap.parse_args()

    out = WORK / "subs"
    out.mkdir(parents=True, exist_ok=True)
    # 不清空目录：先造合集再造单篇（或者反过来）时，清空会把上一个任务的产物
    # 一起删掉——那个任务已经写进库了，于是它变成「文件已不存在」。名字是定死的，
    # 重跑自然覆盖。

    src = blocks((ROOT / "subs" / SRC).read_text(encoding="utf-8-sig"))
    files = []
    if args.single:
        path = out / "BV1ONLY12345-只有一个分P.srt"
        write(path, src[CUTS[0][1]:CUTS[0][2]])
        files.append(str(path))
        print("%s  1 篇" % path.name)
        target = "BV1ONLY12345"
    else:
        for page, (title, lo, hi) in enumerate(CUTS, 1):
            picked = src[lo:hi]
            path = out / ("%s-P%d-%s.srt" % (BV, page, safe(title)))
            write(path, picked)
            files.append(str(path))
            first = picked[0].splitlines()[1].split("-->")[0].strip()
            print("%s  %d 段  起于 %s" % (path.name, len(picked), first))
        target = BV

    db = WORK / "tasks.db"
    store = Store(str(db))
    task = store.create("subs", {"target": target, "formats": ["srt"]})
    claimed = store.claim("e2e")
    assert claimed is not None and claimed.id == task.id, "没人抢到任务"
    store.finish(task.id, {
        "target": target, "outcome": "complete", "videos": 1,
        "parts_written": len(files), "parts_failed": 0,
        "files": files, "outdir": str(out), "failures": [],
    }, attempt=claimed.attempt)
    store.close()
    print("\nTASK_ID=%s" % task.id)


if __name__ == "__main__":
    main()
