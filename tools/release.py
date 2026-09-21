#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布一条龙 + **版本号守卫**。

## 为什么需要它
踩过的坑：v2.0.0 期间连着重建了 4 次、每次都把同一个 Release 的资产覆盖掉。
结果 **4 个不同的 APK 全叫 2.0.0**（versionCode 都是 1）——
用户分不出自己装的是哪一个，先下载的人手里还是旧文件。

根因不是"忘了"，而是**没有任何东西拦着**。所以这里加两道闸：

  闸 1（本地）：源码变了但 versionCode 没变 -> 直接拒绝
  闸 2（远端）：GitHub 上已经有这个版本号、但资产内容不一样 -> 直接拒绝

闸 2 是关键：就算本地状态文件被删了，只要那个版本号已经发过、内容又不同，
它照样会拦下来。

## 用法
    python tools/release.py                 # 只检查，不发布（构建 + 算 SHA + 过两道闸）
    python tools/release.py --publish        # 检查通过后一路发到底
    python tools/release.py --publish --notes <md>   # 用指定的 Release 说明

    --publish 会自动：构建 -> 打标签 -> 推 main 和标签 -> 建/更新 Release -> 上传 APK -> 匿名下载复验

环境变量：GH_PAT（GitHub 令牌，可用 dsh 的 gh_get_pat.ps1 解析出来）
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build_apk  # noqa: E402  复用 JDK/SDK 探测

REPO = "Xixixiao2007/HA-F1-RaceControl"
STATE = os.path.join(HERE, ".release-state.json")
DIST = os.path.dirname(ROOT)          # APK 放到仓库的上一级（跟以前一致）


def sh(cmd, env=None, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True,
                       encoding="utf-8", errors="replace")
    if p.stdout.strip():
        for line in p.stdout.strip().splitlines():
            print("      " + line)
    if check and p.returncode != 0:
        raise SystemExit("命令失败 (exit %d): %s" % (p.returncode, " ".join(cmd)))
    return p


def load_gh():
    """把 gh_release.py 当模块加载，复用它的 REST 客户端。"""
    spec = importlib.util.spec_from_file_location(
        "ghrel", os.path.join(HERE, "gh_release.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# 版本与指纹
# ----------------------------------------------------------------------

def read_version():
    manifest = os.path.join(ROOT, "app", "AndroidManifest.xml")
    code, name = build_apk.read_manifest_version(manifest)
    if not code or not name:
        raise SystemExit("AndroidManifest.xml 里读不到 versionCode / versionName")
    return int(code), name, manifest


def source_hash():
    """
    参与 APK 构建的所有文件的指纹。

    只算真正打进 APK 的东西（源码 / 清单 / 资源），不算测试和工具脚本 ——
    改测试不该逼你升版本号。
    """
    h = hashlib.sha256()
    targets = []
    src = os.path.join(ROOT, "app", "src")
    res = os.path.join(ROOT, "app", "res")
    for base in (src, res):
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                targets.append(os.path.join(dirpath, f))
    targets.append(os.path.join(ROOT, "app", "AndroidManifest.xml"))
    for path in sorted(targets):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 闸门
# ----------------------------------------------------------------------

def gate_local(code, name, src_hash):
    """闸 1：源码变了但版本号没变 -> 拒绝。"""
    st = load_state()
    if not st:
        print("  闸 1（本地）：没有上次发布的记录，跳过")
        return
    same_version = (st.get("versionCode") == code and st.get("versionName") == name)
    same_source = (st.get("sourceHash") == src_hash)
    if same_version and not same_source:
        print()
        print("  " + "!" * 62)
        print("  [拒绝] 源码变了，但版本号没变。")
        print("     上次发布：versionCode=%s versionName=%s"
              % (st.get("versionCode"), st.get("versionName")))
        print("     现在还是：versionCode=%s versionName=%s" % (code, name))
        print()
        print("     请改 app/AndroidManifest.xml：versionCode 每次 +1，")
        print("     versionName 改 patch 位（%s -> 下一个）。" % name)
        print("  " + "!" * 62)
        raise SystemExit(1)
    if same_version and same_source:
        print("  闸 1（本地）：版本号和源码都没变（重复发布同一份内容）")
    else:
        print("  闸 1（本地）：通过（版本号已从 %s 升到 %s）"
              % (st.get("versionName"), name))


def gate_remote(gh, token, name, apk_sha):
    """
    闸 2：远端已经有这个版本号了，就把它的资产**下回来逐字节比**。

    内容一样 -> 允许（重复发布同一份内容无害）
    内容不同 -> **拒绝**。这正是 v2.0.0 踩的坑：同一个版本号下换过 4 个不同的包，
                先下载的人手里是旧文件，而且没人看得出来。

    这一道是最后防线：就算本地状态文件被删了，只要这个版本号发过、内容又不同，
    照样拦得住。
    """
    rel = gh.find_release(token, REPO, "v" + name)
    if not rel:
        print("  闸 2（远端）：v%s 还没发布过，通过" % name)
        return
    assets = rel.get("assets", [])
    if not assets:
        print("  闸 2（远端）：v%s 已存在但没有资产，通过" % name)
        return
    a = assets[0]
    print("  闸 2（远端）：v%s 已发布，资产 %s（%d 字节），下载比对 ..."
          % (name, a["name"], a["size"]))
    req = urllib.request.Request(a["browser_download_url"])
    req.add_header("User-Agent", "release-gate")
    try:
        remote = urllib.request.urlopen(req, timeout=120).read()
    except Exception as e:
        print("     下载失败（%s），跳过这一道闸" % e)
        return
    remote_sha = hashlib.sha256(remote).hexdigest()
    if remote_sha == apk_sha:
        print("     [通过] 远端内容与本次构建完全一致（重复发布同一份）")
        return
    print()
    print("  " + "!" * 62)
    print("  [拒绝] 版本号 %s 已经发布过，但这次要发的内容**不一样**。" % name)
    print("     远端 SHA256 : %s" % remote_sha)
    print("     本次 SHA256 : %s" % apk_sha)
    print()
    print("     请先改 app/AndroidManifest.xml 的版本号再发。")
    print("  " + "!" * 62)
    raise SystemExit(1)


# ----------------------------------------------------------------------
# 发布
# ----------------------------------------------------------------------

def git_push(token, refs):
    """沙箱里 credential.helper 必然死在命名管道上，用 extraHeader 注入认证。"""
    b64 = base64.b64encode(("x-access-token:" + token).encode("ascii")).decode("ascii")
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic " + b64
    for ref in refs:
        sh(["git", "-c", "credential.helper=", "push", "origin", ref], env=env, cwd=ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true", help="检查通过后真的发布")
    ap.add_argument("--notes", default="", help="Release 说明 markdown 路径")
    ap.add_argument("--force", action="store_true",
                    help="跳过闸门（只在你明确知道自己在干什么时用）")
    args = ap.parse_args()

    code, name, manifest = read_version()
    tag = "v" + name
    apk = os.path.join(DIST, "HA-F1-RaceControl-%s.apk" % tag)

    print("=" * 70)
    print("  版本 : versionCode=%d  versionName=%s   tag=%s" % (code, name, tag))
    print("=" * 70)

    src_hash = source_hash()
    print("  源码指纹 : %s" % src_hash[:16])
    print()

    if not args.force:
        gate_local(code, name, src_hash)

    # ---- 构建 ----
    print()
    print("  构建 APK ...")
    env = dict(os.environ)
    env.setdefault("HAF1_SDK", build_apk.find_sdk() or "")
    sh([sys.executable, os.path.join(HERE, "build_apk.py"),
        "--out", apk, "--name", os.path.basename(apk)[:-4]], env=env, cwd=ROOT)
    apk_sha = hashlib.sha256(open(apk, "rb").read()).hexdigest()
    print("  产物     : %s" % apk)
    print("  大小     : %d 字节" % os.path.getsize(apk))
    print("  SHA256   : %s" % apk_sha)

    if not args.publish:
        print()
        print("  （只做了检查，没发布。加 --publish 才真发。）")
        return 0

    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("  需要 GH_PAT 环境变量（可用 dsh 的 gh_get_pat.ps1 解析）")
    gh = load_gh()

    if not args.force:
        gate_remote(gh, token, name, apk_sha)

    # ---- 打标签 + 推送 ----
    print()
    print("  打标签 %s 并推送 ..." % tag)
    sh(["git", "tag", "-d", tag], cwd=ROOT, check=False)
    sh(["git", "tag", "-a", tag, "-m",
        "HA-F1-RaceControl %s\n\nversionCode %d。详见 CHANGELOG.md。" % (tag, code)],
       cwd=ROOT)
    git_push(token, ["main", "refs/tags/" + tag])

    # ---- 发 Release ----
    print()
    print("  发布 Release ...")
    rel = gh.find_release(token, REPO, tag)
    body = ""
    if args.notes and os.path.exists(args.notes):
        body = open(args.notes, encoding="utf-8").read()
    if rel:
        st, _ = gh.call("PATCH", "/repos/%s/releases/%d" % (REPO, rel["id"]), token,
                        body={"body": body, "name": tag})
        print("     更新已有 Release -> HTTP %s" % st)
        for a in rel.get("assets", []):
            gh.call("DELETE", "/repos/%s/releases/assets/%d" % (REPO, a["id"]), token)
            print("     删旧资产 %s" % a["name"])
    else:
        st, rel = gh.call("POST", "/repos/%s/releases" % REPO, token, body={
            "tag_name": tag, "name": tag, "body": body, "draft": False, "prerelease": False})
        if st != 201:
            raise SystemExit("  建 Release 失败 HTTP %s: %s" % (st, rel))
        print("     新建 Release -> %s" % rel["html_url"])

    data = open(apk, "rb").read()
    url = ("https://uploads.github.com/repos/%s/releases/%d/assets?name=%s"
           % (REPO, rel["id"], urllib.parse.quote(os.path.basename(apk))))
    st, res = gh.call("POST", url, token, raw=data,
                      content_type="application/vnd.android.package-archive")
    if st != 201:
        raise SystemExit("  上传失败 HTTP %s: %s" % (st, res))
    print("     上传 %s (%d 字节)" % (os.path.basename(apk), len(data)))

    # ---- 匿名下载复验 ----
    print()
    print("  匿名下载复验 ...")
    req = urllib.request.Request(res["browser_download_url"])
    req.add_header("User-Agent", "release-verify")
    got = urllib.request.urlopen(req, timeout=120).read()
    got_sha = hashlib.sha256(got).hexdigest()
    if got_sha != apk_sha or len(got) != len(data):
        raise SystemExit("  [FAIL] 远端内容与本地不一致！本地 %s / 远端 %s"
                         % (apk_sha, got_sha))
    print("     [OK] 匿名下载 %d 字节，SHA256 一致" % len(got))

    save_state({"versionCode": code, "versionName": name, "sourceHash": src_hash,
                "apkSha256": apk_sha, "tag": tag, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    print()
    print("=" * 70)
    print("  已发布 %s" % rel["html_url"])
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
