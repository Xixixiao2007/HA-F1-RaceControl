#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
桌面端单元测试。

用桌面 JDK 编译真实的 app 源码（HaClient / Prefs / Change / WsFrame），
配合 tztest/stub 下的极简 android.* / org.json 桩，因此不需要 Android 设备。

覆盖：ISO8601 时间解析与生成、时区往返、黑白名单判定、刷新间隔钳制、
      RFC 6455 WebSocket 帧编解码。

用法: python tools/run_tests.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_apk  # noqa: E402  复用它的 JDK 探测与平台适配

T = os.path.join(HERE, "tztest")
OUT = os.path.join(T, "out")
_root = os.path.dirname(HERE)
_proj = None
for _c in (os.path.join(_root, "app"), os.path.join(_root, "apk"),
           os.path.join(HERE, "app"), os.path.join(HERE, "apk")):
    if os.path.isfile(os.path.join(_c, "AndroidManifest.xml")):
        _proj = _c
        break
if _proj is None:
    _proj = os.path.join(_root, "app")
APPSRC = os.path.join(_proj, "src", "com", "haf1", "racecontrol")

# 参与桌面单测的产品源码：只挑**不依赖 android.* 运行时**的纯逻辑文件。
# 这不是偷懒 —— UI 与网络那几层本来就该靠真机验证，硬塞进桌面单测只会
# 逼出一堆假装能用的桩。
TESTED_SOURCES = (
    "HaClient.java",      # ISO8601 解析/生成、期初状态判定
    "Prefs.java",         # 过滤与钳制
    "RaceMessage.java",   # 解析与去重键
    "Classifier.java",    # 旗语分类（含两个坑）
    "AlertGate.java",     # 聚类 / 升级 / 冷却
    "TrackState.java",    # 优先级状态机
    "MessageStore.java",  # 去重 / 容量 / 序列化
    "Translator.java",    # 判罚等消息的中文简述
    "WsFrame.java",       # RFC6455 帧编解码
)


def collect(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".java"):
                out.append(os.path.join(dirpath, f))
    return out


def main():
    jdk = build_apk.find_jdk()
    if jdk is None:
        print("[test] 找不到 JDK。请安装 JDK 8~17，"
              "或设置环境变量 HAF1_JDK / JAVA_HOME 指向 JDK 根目录。", file=sys.stderr)
        return 2

    javac = os.path.join(jdk, "bin", build_apk.exe("javac"))
    java = os.path.join(jdk, "bin", build_apk.exe("java"))
    if not os.path.exists(javac):
        print("[test] JDK 里没有 javac: %s" % javac, file=sys.stderr)
        return 2

    print("[test] JDK: %s" % jdk)

    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)

    sources = []
    sources += collect(os.path.join(T, "stub"))
    sources += collect(os.path.join(T, "src"))
    for name in TESTED_SOURCES:
        sources.append(os.path.join(APPSRC, name))

    argfile = os.path.join(T, "javac.args")
    with open(argfile, "w", encoding="utf-8") as f:
        f.write("\n".join('"%s"' % p.replace("\\", "/") for p in sources))

    print("[test] 编译 %d 个源文件" % len(sources))
    p = subprocess.run([javac, "-J-Duser.language=en", "-J-Duser.country=US",
                        "-encoding", "UTF-8", "-d", OUT, "@" + argfile],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, encoding="utf-8", errors="replace")
    if p.stdout.strip():
        print(p.stdout)
    if p.returncode != 0:
        print("[test] 编译失败")
        return 1

    print("[test] 运行 TzTest")
    print()
    p = subprocess.run([java, "-cp", OUT, "com.haf1.racecontrol.TzTest"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, encoding="utf-8", errors="replace")
    sys.stdout.write(p.stdout)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
