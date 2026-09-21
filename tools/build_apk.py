#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Minimal Android APK build pipeline -- no Gradle, no Android Studio, no JDK 17.

手工 Android APK 构建流水线（不依赖 Gradle / Android Studio / JDK 17）。

流水线:
    aapt2 link    -> 含二进制 AndroidManifest.xml 的 APK 骨架
    javac (JDK8)  -> .class
    d8            -> classes.dex
    zipfile       -> 把 classes.dex 塞进 APK
    zipalign      -> 4 字节对齐（必须在签名前）
    apksigner     -> v1+v2 签名

为什么要自己搭而不是用 Gradle：
  1. 本项目的 minSdk/targetSdk 是 23（Android 6.0），依赖为零，用不上 Gradle 的
     依赖解析与变体体系。
  2. 现代 AGP 要求 JDK 17，而 build-tools 30.0.3 的 d8 在 JDK 8 下就能跑，
     这样整条链路只需要一个 JDK 8。
  3. 一次完整构建约 10 秒，产物不到 40 KB。

用法:
    python build_apk.py <project_dir> [--out <apk_path>] [--name <base_name>]

project_dir 结构:
    AndroidManifest.xml
    src/**.java
    res/            (可选)
    assets/         (可选)

环境变量（都可省略，省略时自动探测）:
    HAF1_JDK / JAVA_HOME     JDK 根目录（需含 bin/javac）
    ANDROID_SDK_ROOT / ANDROID_HOME   Android SDK 根目录
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile

IS_WIN = os.name == "nt"
HERE = os.path.dirname(os.path.abspath(__file__))

# build-tools 选 30.0.3 的原因：它的 d8 只需要 JDK 8。
# build-tools 31+ 的 d8 需要 JDK 11，会把整条链路的前置要求抬高。
BUILD_TOOLS_VERSION = "30.0.3"
PLATFORM = "android-23"          # 对应 Android 6.0
MIN_SDK = "23"
TARGET_SDK = "23"

KS_PASS = "android"
KS_ALIAS = "androiddebugkey"

# 由 main() 填充
JDK = None
SDK = None
BUILD_TOOLS = None
ANDROID_JAR = None
KEYSTORE = None


def find_project_dir():
    """自动探测 Android 工程目录，兼容 app/ 与 apk/ 两种布局。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for cand in (os.path.join(root, "app"), os.path.join(root, "apk"),
                 os.path.join(here, "app"), os.path.join(here, "apk")):
        if os.path.isfile(os.path.join(cand, "AndroidManifest.xml")):
            return cand
    return os.path.join(root, "app")


def exe(name):
    """按平台补上可执行文件后缀。"""
    return name + ".exe" if IS_WIN else name


def _looks_like_jdk(path):
    return bool(path) and os.path.exists(os.path.join(path, "bin", exe("javac")))


def find_jdk():
    """依次从环境变量、常见安装位置寻找可用的 JDK。"""
    for var in ("HAF1_JDK", "JAVA_HOME", "ANDROID_JAVA_HOME"):
        p = os.environ.get(var)
        if _looks_like_jdk(p):
            return os.path.abspath(p)

    roots = [
        r"C:\Program Files\Android\jdk",       # Android Studio 附带的 JDK
        r"C:\Program Files\Java",
        r"C:\Program Files\Eclipse Adoptium",
        r"C:\Program Files\Microsoft",
        r"C:\Program Files\Zulu",
        r"C:\Program Files\BellSoft",
        os.path.expanduser("~/.jdks"),         # IntelliJ 下载的 JDK
        "/usr/lib/jvm",
        "/Library/Java/JavaVirtualMachines",
        "/opt/homebrew/opt/openjdk",
    ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        # 目录本身可能就是 JDK
        if _looks_like_jdk(root):
            found.append(os.path.abspath(root))
        # 否则往下找 3 层（Android Studio 的布局是 jdk/<name>/<jdk-root>）
        base_depth = root.rstrip("/\\").count(os.sep)
        for dirpath, dirnames, _files in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= 3:
                dirnames[:] = []
                continue
            if _looks_like_jdk(dirpath):
                found.append(os.path.abspath(dirpath))
                dirnames[:] = []
    if found:
        found.sort()
        return found[0]
    return None


def _sdk_has_needed_build_tools(path):
    return bool(path) and os.path.exists(
        os.path.join(path, "build-tools", BUILD_TOOLS_VERSION))


def find_sdk():
    """
    寻找 Android SDK。

    ⚠️ 顺序很关键：GitHub Actions 的 ubuntu 镜像**预装了** Android SDK 并设置了
    ANDROID_SDK_ROOT / ANDROID_HOME，但里面没有本项目需要的 build-tools 30.0.3。
    所以不能无条件信任环境变量，必须优先选「真的含有目标 build-tools」的那一个。
    """
    candidates = []
    local = os.path.join(HERE, "android-sdk")       # fetch_sdk.py 下载的，最优先
    if os.path.isdir(local):
        candidates.append(local)
    for var in ("HAF1_SDK", "ANDROID_SDK_ROOT", "ANDROID_HOME"):
        p = os.environ.get(var)
        if p:
            ap = os.path.abspath(p)
            if ap not in candidates:
                candidates.append(ap)

    for c in candidates:                            # 先要版本对得上的
        if _sdk_has_needed_build_tools(c):
            return c
    for c in candidates:                            # 退一步，交给 preflight 报缺什么
        if os.path.isdir(os.path.join(c, "build-tools")):
            return c
    return None


def describe_sdk(sdk):
    """列出某个 SDK 里实际有什么，用于定位「找到的 SDK 版本不对」这类问题。"""
    if not sdk or not os.path.isdir(sdk):
        return "      (目录不存在)"
    lines = []
    for sub in ("build-tools", "platforms"):
        d = os.path.join(sdk, sub)
        if os.path.isdir(d):
            try:
                names = sorted(os.listdir(d))
            except OSError:
                names = []
            lines.append("      %-12s: %s" % (sub, ", ".join(names) if names else "(空)"))
    return "\n".join(lines) if lines else "      (既无 build-tools 也无 platforms)"


def log(msg):
    print("[build] " + msg)


def run(cmd, env=None, cwd=None, allow_fail=False):
    printable = " ".join('"%s"' % c if " " in str(c) else str(c) for c in cmd)
    log("$ " + printable)
    # javac / java 在中文 Windows 上会输出 GBK 编码的错误信息，
    # 按 UTF-8 硬解码会抛 UnicodeDecodeError 把真正的编译错误吞掉。
    p = subprocess.run(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True,
                       encoding="utf-8", errors="replace")
    if p.stdout.strip():
        for line in p.stdout.strip().splitlines():
            print("        " + line)
    if p.returncode != 0 and not allow_fail:
        raise SystemExit("命令失败 (exit %d): %s" % (p.returncode, printable))
    return p


def base_env():
    env = dict(os.environ)
    env["JAVA_HOME"] = JDK
    env["PATH"] = os.path.join(JDK, "bin") + os.pathsep + BUILD_TOOLS + os.pathsep + env.get("PATH", "")
    return env


def java(args):
    """直接调用 java，绕开 build-tools 里 *.bat 的 find_java 探测。

    重要: d8.bat / dx.bat 依赖完整 SDK 的 tools/lib/find_java.bat。
    若 SDK 是手工裁剪的最小集合，没有那个文件，批处理会静默 `goto :EOF`
    （退出码 0 且不产出任何东西），极难排查。所以一律直调 jar。
    """
    return [os.path.join(JDK, "bin", exe("java")), "-Xmx1024M"] + list(args)


def ensure_keystore():
    if os.path.exists(KEYSTORE):
        return
    log("生成 debug 签名密钥库: %s" % KEYSTORE)
    run([os.path.join(JDK, "bin", exe("keytool")), "-genkeypair", "-v",
         "-keystore", KEYSTORE, "-alias", KS_ALIAS,
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
         "-storepass", KS_PASS, "-keypass", KS_PASS,
         "-dname", "CN=Android Debug,O=Android,C=CN"], env=base_env())


def collect(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            out.append(os.path.join(dirpath, f))
    return out


def preflight():
    problems = []
    if JDK is None:
        problems.append(
            "找不到 JDK。请安装 JDK 8~17，或设置环境变量 HAF1_JDK / JAVA_HOME 指向 JDK 根目录。")
    if SDK is None:
        problems.append(
            "找不到 Android SDK。请先运行 `python fetch_sdk.py` 下载最小工具集，\n"
            "        或设置环境变量 ANDROID_SDK_ROOT 指向已有 SDK。")
    else:
        for tool in ("aapt2", "zipalign"):
            if not os.path.exists(os.path.join(BUILD_TOOLS, exe(tool))):
                problems.append("缺少构建工具 %s（期望在 %s）" % (tool, BUILD_TOOLS))
        if not os.path.exists(os.path.join(BUILD_TOOLS, "lib", "d8.jar")):
            problems.append("缺少 %s" % os.path.join(BUILD_TOOLS, "lib", "d8.jar"))
        if not os.path.exists(os.path.join(BUILD_TOOLS, "lib", "apksigner.jar")):
            problems.append("缺少 %s" % os.path.join(BUILD_TOOLS, "lib", "apksigner.jar"))
        if not os.path.exists(ANDROID_JAR):
            problems.append("缺少 %s" % ANDROID_JAR)
    if problems:
        print("构建前置检查未通过：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print("", file=sys.stderr)
        print("诊断信息：", file=sys.stderr)
        print("  选中的 JDK : %s" % (JDK or "(未找到)"), file=sys.stderr)
        print("  选中的 SDK : %s" % (SDK or "(未找到)"), file=sys.stderr)
        print("  需要的版本 : build-tools %s / platforms %s"
              % (BUILD_TOOLS_VERSION, PLATFORM), file=sys.stderr)
        print("  该 SDK 实际有：", file=sys.stderr)
        print(describe_sdk(SDK), file=sys.stderr)
        print("  相关环境变量：", file=sys.stderr)
        for var in ("HAF1_SDK", "ANDROID_SDK_ROOT", "ANDROID_HOME", "HAF1_JDK", "JAVA_HOME"):
            print("      %-17s= %s" % (var, os.environ.get(var, "(未设置)")), file=sys.stderr)
        print("", file=sys.stderr)
        print("  提示：运行 `python fetch_sdk.py` 会下载所需的最小工具集，"
              "默认落在 tools/android-sdk/。", file=sys.stderr)
        raise SystemExit(2)


def read_manifest_version(manifest):
    """从 AndroidManifest.xml 读出 versionCode / versionName，仅用于日志核对。

    用正则而不是 XML 解析器：清单结构固定，正则足够，也省掉命名空间处理。
    """
    try:
        with open(manifest, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, None
    code = re.search(r'android:versionCode\s*=\s*"([^"]*)"', text)
    name = re.search(r'android:versionName\s*=\s*"([^"]*)"', text)
    return (code.group(1) if code else None), (name.group(1) if name else None)


def main():
    global JDK, SDK, BUILD_TOOLS, ANDROID_JAR, KEYSTORE

    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?", default=None,
                    help="Android 工程目录（含 AndroidManifest.xml）。省略则自动探测 app/ 或 apk/")
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default="app")
    args = ap.parse_args()

    JDK = find_jdk()
    SDK = find_sdk()
    BUILD_TOOLS = os.path.join(SDK, "build-tools", BUILD_TOOLS_VERSION) if SDK else None
    ANDROID_JAR = os.path.join(SDK, "platforms", PLATFORM, "android.jar") if SDK else None
    KEYSTORE = os.path.join(HERE, "debug.keystore")

    preflight()

    log("JDK     : %s" % JDK)
    log("SDK     : %s" % SDK)

    proj = os.path.abspath(args.project) if args.project else find_project_dir()
    manifest = os.path.join(proj, "AndroidManifest.xml")
    if not os.path.exists(manifest):
        raise SystemExit("找不到 AndroidManifest.xml: " + manifest)

    env = base_env()
    work = os.path.join(proj, "build")
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    classes_dir = os.path.join(work, "classes")
    os.makedirs(classes_dir)

    ensure_keystore()

    # ---------- 1) aapt2 link ----------
    base_apk = os.path.join(work, "base.apk")
    aapt2 = os.path.join(BUILD_TOOLS, exe("aapt2"))
    link_cmd = [aapt2, "link",
                "-o", base_apk,
                "--manifest", manifest,
                "-I", ANDROID_JAR,
                "--min-sdk-version", MIN_SDK,
                "--target-sdk-version", TARGET_SDK]
    # 版本号以 AndroidManifest.xml 为唯一来源：**不传** --version-code /
    # --version-name。上一代固定传 1 / 1.0，而清单里写的是 5 / 1.4，
    # 产物里的版本号到底是哪个全看 aapt2 的实现细节 —— 不该留这种不确定性。
    vcode, vname = read_manifest_version(manifest)
    log("清单版本: versionCode=%s versionName=%s" % (vcode or "?", vname or "?"))

    res_dir = os.path.join(proj, "res")
    if os.path.isdir(res_dir) and collect(res_dir):
        compiled = os.path.join(work, "res.zip")
        log("编译资源")
        run([aapt2, "compile", "--dir", res_dir, "-o", compiled], env=env)
        link_cmd.append(compiled)
    assets_dir = os.path.join(proj, "assets")
    if os.path.isdir(assets_dir):
        link_cmd += ["-A", assets_dir]

    run(link_cmd, env=env)

    # ---------- 2) javac ----------
    src_dir = os.path.join(proj, "src")
    java_files = [f for f in collect(src_dir) if f.endswith(".java")] if os.path.isdir(src_dir) else []
    if java_files:
        log("编译 Java (%d 个文件)" % len(java_files))
        # javac 的 @argfile 会把反斜杠当转义符，必须改成正斜杠
        argfile = os.path.join(work, "javac.args")
        with open(argfile, "w", encoding="utf-8") as f:
            f.write("\n".join('"%s"' % p.replace("\\", "/") for p in java_files))
        run([os.path.join(JDK, "bin", exe("javac")),
             "-J-Duser.language=en", "-J-Duser.country=US",  # 强制英文报错，避开 GBK 乱码
             "-source", "1.8", "-target", "1.8",
             "-encoding", "UTF-8",
             "-bootclasspath", ANDROID_JAR,
             "-d", classes_dir,
             "@" + argfile], env=env)

        # ---------- 3) d8 ----------
        class_files = [f for f in collect(classes_dir) if f.endswith(".class")]
        log("dex 转换 (%d 个 class)" % len(class_files))
        dex_out = os.path.join(work, "dex")
        os.makedirs(dex_out, exist_ok=True)
        run(java(["-cp", os.path.join(BUILD_TOOLS, "lib", "d8.jar"),
                  "com.android.tools.r8.D8",
                  "--min-api", MIN_SDK,
                  "--lib", ANDROID_JAR,
                  "--output", dex_out] + class_files), env=env)

        # ---------- 4) classes.dex -> apk ----------
        dex = os.path.join(dex_out, "classes.dex")
        if not os.path.exists(dex):
            raise SystemExit("d8 未产出 classes.dex")
        merged = os.path.join(work, "merged.apk")
        log("打包 classes.dex")
        with zipfile.ZipFile(base_apk) as zin, \
             zipfile.ZipFile(merged, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.write(dex, "classes.dex")
        unsigned = merged
    else:
        log("无 Java 源码，跳过 dex（纯资源 APK）")
        unsigned = base_apk

    # ---------- 5) zipalign ----------
    aligned = os.path.join(work, "aligned.apk")
    log("zipalign")
    run([os.path.join(BUILD_TOOLS, exe("zipalign")), "-f", "-p", "4", unsigned, aligned], env=env)

    # ---------- 6) 签名 ----------
    out_apk = args.out or os.path.join(HERE, args.name + ".apk")
    out_apk = os.path.abspath(out_apk)
    os.makedirs(os.path.dirname(out_apk), exist_ok=True)
    log("签名 -> %s" % out_apk)
    run(java(["-jar", os.path.join(BUILD_TOOLS, "lib", "apksigner.jar"), "sign",
              "--ks", KEYSTORE, "--ks-key-alias", KS_ALIAS,
              "--ks-pass", "pass:" + KS_PASS, "--key-pass", "pass:" + KS_PASS,
              "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
              "--out", out_apk, aligned]), env=env)

    log("校验签名")
    run(java(["-jar", os.path.join(BUILD_TOOLS, "lib", "apksigner.jar"),
              "verify", "--verbose", out_apk]), env=env)

    size = os.path.getsize(out_apk)
    print()
    print("=" * 62)
    print(" 构建成功: %s" % out_apk)
    print(" 体积:     %.1f KB" % (size / 1024.0))
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
