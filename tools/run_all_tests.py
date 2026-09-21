#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键跑全套测试。

分三层，缺一层都不算验证过：

  ① 单元测试   tools/run_tests.py        —— 纯逻辑（分类/聚类/去重/状态机/帧编解码）
  ② mock 自测  tools/test_mock_ha.py     —— 假 HA 自己的协议正确性
  ③ 端到端     tools/run_e2e.py          —— **真实客户端代码**隔着真实 socket 连假 HA

③ 是关键：这台机器装不了 APK（没有模拟器也没有设备），"两个类各自单测通过"
不等于"它们按真实协议能连上"。E2E 会把 HaClient / HaWebSocket / AlertGate /
TrackState 真的编出来跑。

用法:
    python tools/run_all_tests.py            # 全部
    python tools/run_all_tests.py --quick    # 跳过剧本那几轮，只跑真实数据回放
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

PORT = 8129                 # 刻意避开真 HA 的 8123 和手工联调常用的 8124
TOKEN = "all-tests-token"


def run(cmd, timeout=900):
    """跑个子进程，返回 (退出码, 输出)。"""
    p = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout


def tail(text, n=12):
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-n:]


def start_mock(extra):
    """起一个 mock HA；stdout 丢给 NUL（不能用管道，沙箱会拒）。"""
    cmd = [PY, os.path.join(HERE, "mock_ha.py"),
           "--port", str(PORT), "--token", TOKEN] + extra
    proc = subprocess.Popen(cmd, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等它就绪：轮询 /api/ 直到通
    url = "http://127.0.0.1:%d/api/" % PORT
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            return proc, False
        try:
            req = urllib.request.Request(url)
            req.add_header("Authorization", "Bearer " + TOKEN)
            with urllib.request.urlopen(req, timeout=2):
                return proc, True
        except urllib.error.HTTPError:
            return proc, True          # 有 HTTP 响应就算起来了
        except Exception:
            time.sleep(0.3)
    return proc, False


def stop_mock(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def e2e(mode, scenario, seconds, hours):
    cmd = [PY, os.path.join(HERE, "run_e2e.py"),
           "--port", str(PORT), "--token", TOKEN,
           "--mode", mode, "--seconds", str(seconds), "--hours", str(hours)]
    if scenario:
        cmd += ["--scenario", scenario]
    return run(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过剧本轮，只跑真实数据回放")
    args = ap.parse_args()

    results = []          # (名称, 是否通过, 摘要)

    # ---------- ① 单元测试 ----------
    print("=" * 74)
    print("① 单元测试 (tools/run_tests.py)")
    print("=" * 74)
    code, out = run([PY, os.path.join(HERE, "run_tests.py")])
    for l in tail(out, 4):
        print("   " + l)
    results.append(("① 单元测试", code == 0, tail(out, 3)[-2] if out.strip() else ""))

    # ---------- ② mock HA 自测 ----------
    print()
    print("=" * 74)
    print("② mock HA 自测 (tools/test_mock_ha.py)")
    print("=" * 74)
    code, out = run([PY, os.path.join(HERE, "test_mock_ha.py")], timeout=1200)
    for l in tail(out, 4):
        print("   " + l)
    results.append(("② mock HA 自测", code == 0, tail(out, 3)[-2] if out.strip() else ""))

    # ---------- ③ 端到端 ----------
    print()
    print("=" * 74)
    print("③ 端到端：真实客户端代码 <=> 假 HA")
    print("=" * 74)

    rounds = [("真实数据回放（REST 历史 + 分类 + 警报次数）", [], "rest", "", 20, 400)]
    if not args.quick:
        rounds += [
            ("剧本 red_flag（WebSocket 实时 + 状态机到 RED）",
             ["--scenario", "red_flag", "--loop"], "ws", "red_flag", 22, 72),
            ("剧本 vsc（WebSocket 实时 + VSC 识别）",
             ["--scenario", "vsc", "--loop"], "ws", "vsc", 18, 72),
            ("剧本 safety_car（WebSocket 实时 + 安全车）",
             ["--scenario", "safety_car", "--loop"], "ws", "safety_car", 18, 72),
        ]

    for name, extra, mode, scen, secs, hours in rounds:
        print()
        print("  ── %s" % name)
        proc, ok = start_mock(extra)
        if not ok:
            print("     [FAIL] mock 服务器没起来")
            stop_mock(proc)
            results.append(("③ " + name, False, "服务器未就绪"))
            continue
        try:
            code, out = e2e(mode, scen, secs, hours)
        finally:
            stop_mock(proc)
        for l in tail(out, 6):
            print("     " + l)
        summary = ""
        for l in out.splitlines():
            if "集成测试：通过" in l:
                summary = l.strip()
        results.append(("③ " + name, code == 0, summary))

    # ---------- 汇总 ----------
    print()
    print("=" * 74)
    print("汇总")
    print("=" * 74)
    bad = 0
    for name, ok, summary in results:
        print("  %-6s %-46s %s" % ("[PASS]" if ok else "[FAIL]", name, summary))
        if not ok:
            bad += 1
    print()
    print("  %d 项，通过 %d，失败 %d" % (len(results), len(results) - bad, bad))
    print("=" * 74)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
