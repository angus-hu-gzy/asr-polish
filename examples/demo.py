"""演示 asr-polish 三个核心能力。

运行：python examples/demo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from asr_polish import correct_text, cross_check, confidence_report, assess


def demo_correction():
    print("=" * 50)
    print("① 术语订正：把 ASR 转错的术语改回正确写法")
    raw = "我们今天讲色考和PASS，还有福拉斯克框架，以及预写日子和Agentic Coding。"
    result = correct_text(raw)
    print("原文：", raw)
    print("订正：", result.text)
    print(result.summary())
    print()


def demo_crosscheck():
    print("=" * 50)
    print("② 交叉验证：云端 ASR vs 本地 whisper")
    cloud = "今天讲SQL和Python，还有Flask框架，以及预写日志和键值数据库。"
    local = "今天讲色考和PASS，还有福拉斯克框架，以及预写日子和键子数据库。"
    result = cross_check(cloud, local)
    print("云端：", cloud)
    print("本地：", local)
    print(confidence_report(result))
    print()


def demo_quality():
    print("=" * 50)
    print("③ 质量自评：一段干净文本 vs 一段带错字/重复/乱码的文本")
    clean = "今天我们讲SQL和Python，还有Flask框架以及预写日志。"
    report = assess(clean, duration_seconds=6)
    print("干净文本：")
    print(report.summary())
    print()
    dirty = "今天讲色考和PASS。。。福拉斯克框架很好很好很好很好很好\x00，"
    report2 = assess(dirty, duration_seconds=6)
    print("问题文本：")
    print(report2.summary())


if __name__ == "__main__":
    demo_correction()
    demo_crosscheck()
    demo_quality()
