#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
桌面端**集成测试**驱动：编译真实产品代码，连上一个真的 mock HA 服务器。

为什么要有它：这台机器上装不了 APK（没有模拟器也没有设备），
而"两个类各自单测通过"不等于"它们隔着真实 socket 按真实协议能跑通"。
所以把 HaClient / HaWebSocket / AlertGate / TrackState 编出来，直接连
tools/mock_ha.py。覆盖的正是最容易出错、单测又碰不到的那一段。

服务器由调用方先起好（见 --help），本脚本只负责编译 + 运行。

用法:
    python tools/run_e2e.py --port 8124 --token e2e --mode rest --hours 72
    python tools/run_e2e.py --port 8124 --token e2e --mode ws --scenario red_flag
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_apk  # noqa: E402  复用它的 JDK 探测

T = os.path.join(HERE, "tztest")
OUT = os.path.join(T, "e2eout")

# 集成测试要连真实的 socket，所以除了纯逻辑文件，还要把 WebSocket 客户端编进来
SOURCES = (
    "HaClient.java",
    "Prefs.java",
    "RaceMessage.java",
    "Classifier.java",
    "AlertGate.java",
    "TrackState.java",
    "MessageStore.java",
    "WsFrame.java",
    "HaWebSocket.java",
)


def collect(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".java"):
                out.append(os.path.join(dirpath, f))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--token", default="e2e")
    ap.add_argument("--entity", default="sensor.f1_race_control_2")
    ap.add_argument("--mode", default="rest", choices=["rest", "ws"])
    ap.add_argument("--scenario", default="")
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--hours", type=int, default=72)
    args = ap.parse_args()

    jdk = build_apk.find_jdk()
    if jdk is None:
        print("[e2e] 找不到 JDK", file=sys.stderr)
        return 2
    javac = os.path.join(jdk, "bin", build_apk.exe("javac"))
    java = os.path.join(jdk, "bin", build_apk.exe("java"))

    proj = None
    root = os.path.dirname(HERE)
    for c in (os.path.join(root, "app"), os.path.join(HERE, "app")):
        if os.path.isfile(os.path.join(c, "AndroidManifest.xml")):
            proj = c
            break
    appsrc = os.path.join(proj, "src", "com", "haf1", "racecontrol")

    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)

    sources = collect(os.path.join(T, "stub"))
    # 集成测试单独放在 tztest/e2e 下，不能和单元测试共用 src/ ——
    # 否则 run_tests.py 会把 IntegrationTest 也一起编译，而它依赖 HaWebSocket，
    # 那个文件不在单元测试的源文件清单里，会编译失败。
    sources += collect(os.path.join(T, "e2e"))
    for name in SOURCES:
        sources.append(os.path.join(appsrc, name))

    argfile = os.path.join(T, "e2e.args")
    with open(argfile, "w", encoding="utf-8") as f:
        f.write("\n".join('"%s"' % p.replace("\\", "/") for p in sources))

    print("[e2e] 编译 %d 个源文件" % len(sources))
    p = subprocess.run([javac, "-J-Duser.language=en", "-J-Duser.country=US",
                        "-encoding", "UTF-8", "-d", OUT, "@" + argfile],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, encoding="utf-8", errors="replace")
    if p.stdout.strip():
        print(p.stdout)
    if p.returncode != 0:
        print("[e2e] 编译失败")
        return 1

    print("[e2e] 连接 mock HA: http://%s:%d   模式=%s" % (args.host, args.port, args.mode))
    cmd = [java, "-cp", OUT,
           "-Dmock.host=" + args.host,
           "-Dmock.port=%d" % args.port,
           "-Dmock.token=" + args.token,
           "-Dmock.entity=" + args.entity,
           "-Dmock.mode=" + args.mode,
           "-Dmock.scenario=" + args.scenario,
           "-Dmock.seconds=%d" % args.seconds,
           "-Dmock.hours=%d" % args.hours,
           "com.haf1.racecontrol.IntegrationTest"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, encoding="utf-8", errors="replace")
    sys.stdout.write(p.stdout)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
