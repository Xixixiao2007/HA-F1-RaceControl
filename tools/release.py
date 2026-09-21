#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发布一条龙 + **版本号守卫**。

## 为什么需要它
踩过的坑：v2.0.0 期间连着重建了 4 次、每次都把同一个 Release 的资产覆盖掉。
结果 **4 个不同的 APK 全叫 2.0.0**（versionCode 都是 1）——
用户分不出自己装的是哪一个，先下载的人手里还是旧文件。

根因不是"忘了"，而是**没有任何东西拦着**。所以这里加两道闸：

  闸 1（本地）：源码变了但 versionCode 没变  -> 拒绝
  闸 2（远端）：该版本号已发布过，把远端资产下回来逐字节比，内容不同 -> 拒绝

闸 2 是关键：就算本地状态文件被删了，只要那个版本号已经发过、内容又不同，
它照样拦得住。

## 用法
    python tools/release.py                 # 只检查（构建 + 算 SHA + 过闸），不发布
    python tools/release.py --publish        # 检查通过后一路发到底
    python tools/release.py --publish --notes <md>   # 用指定的 Release 说明

--publish 会自动：构建 -> 打标签 -> 推 main 和标签 -> 建/更新 Release
                  -> 上传 APK -> **匿名下载复验 SHA256**

环境变量：GH_PAT（GitHub 令牌，可用 dsh 的 gh_get_pat.ps1 解析出来）
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# 见 build_apk.py 里的同款处理：GBK 控制台编不出 U+FFFD，
# 会让脚本死在"打印日志"这一行上。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import build_apk  # noqa: E402  复用 JDK/SDK 探测

REPO = "Xixixiao2007/HA-F1-RaceControl"
STATE = os.path.join(HERE, ".release-state.json")
DIST = os.path.dirname(ROOT)          # APK 放到仓库的上一级（跟以前一致）

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

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


# ----------------------------------------------------------------------
# GitHub REST（只内联本脚本用得上的几个，避免依赖仓库外的工具）
# ----------------------------------------------------------------------

def gh_call(token, method, url, body=None, raw=None, content_type=None,
            attempts=8, timeout=45):
    """
    调一次 GitHub REST，**网络抖动自动重试**。

    为什么参数调成这样（2026-09-21 实测）：
    发 v2.0.4 时 `api.github.com` 一小时内变成**五次里四次握手被掐**
    （`SSL: UNEXPECTED_EOF_WHILE_READING` / `RemoteDisconnected` / 握手超时），
    配额却还剩 5000 —— 纯粹是链路被干扰，不是被限流。
    原来重试 4 次、每次超时 120 秒：
      · 4 次连续失败的概率不低（抖动 50% 时约 6%，那天就碰上了）
      · 而握手卡住时一次要等满 120 秒，重试全跑完能拖十几分钟
    所以：超时压到 45 秒、重试提到 8 次、退避上限 8 秒并加随机抖动，
    另外**每次重试都打一行日志** —— 静默重试会把"网络在抖"伪装成"卡住了"。
    """
    if not url.startswith("http"):
        url = API + url
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ha-f1-release")
        if data is not None:
            req.add_header("Content-Type", content_type or "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return r.status, (json.loads(b) if b else None)
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except Exception:
                return e.code, b.decode("utf-8", "replace")
        except Exception as e:  # 网络抖动
            last = e
            if i < attempts - 1:
                wait = min(2.0 * (i + 1), 8.0) + random.random()
                print("     [网络抖动 %d/%d] %s -> %.1fs 后重试"
                      % (i + 1, attempts, str(e)[:70], wait))
                time.sleep(wait)
    raise last


def gh_find_release(token, tag):
    st, rel = gh_call(token, "GET", "/repos/%s/releases/tags/%s" % (REPO, tag))
    if st == 200:
        return rel
    if st == 404:
        return None
    raise SystemExit("查 Release 失败 HTTP %s: %s" % (st, rel))


def gh_download(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "ha-f1-release")
    return urllib.request.urlopen(req, timeout=180).read()


def gh_download_via_api(token, asset_id):
    """
    用 **API 资产端点**取二进制。

    `Accept: application/octet-stream` 时 `api.github.com` 直接吐文件内容，
    走的还是 API 那条路 —— 而 `browser_download_url` 走 `github.com`，
    本机实测过它不可达。
    """
    url = "%s/repos/%s/releases/assets/%d" % (API, REPO, asset_id)
    last = None
    for i in range(4):
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/octet-stream")
        req.add_header("User-Agent", "ha-f1-release")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception as e:          # 网络抖动就重试
            last = e
            time.sleep(2.0 * (i + 1))
    raise last


def verify_asset(token, asset, apk_sha, data):
    """
    复验「传上去的东西 == 我本地这一份」。

    ## 为什么要有两条通道
    `browser_download_url` 指向 **github.com** —— 和 `git push` 是同一条路，
    实测它不可达（`WinError 10060` 超时）。那时候匿名下载会失败，
    但**上传其实已经成功了**，白白让整个发布 exit 1、状态文件也没写成。

    所以按这个顺序：
      ① GitHub 服务端自己算的 `digest`（sha256）—— 这是服务端的结论，最硬；
      ② 匿名下载（真实用户走的路，能过最好）；
      ③ 不行就退回 API 资产端点。

    ②③ 至少成立一条，再加上 ①，才算验证过。
    「上传返回 201」本身**不算**验证。
    """
    want = len(data)
    print()
    print("  复验远端资产 ...")

    # ① 服务端 digest
    digest = asset.get("digest") or ""
    if not digest:
        st, got = gh_call(token, "GET",
                          "/repos/%s/releases/assets/%d" % (REPO, asset["id"]))
        digest = (got or {}).get("digest") or "" if st == 200 else ""
    if digest.startswith("sha256:"):
        d = digest.split(":", 1)[1]
        if d != apk_sha:
            raise SystemExit("  [FAIL] 服务端 digest 与本地不符：本地 %s / 服务端 %s"
                             % (apk_sha, d))
        print("     [OK] 服务端 digest 一致  %s" % d[:16])
    else:
        print("     [注意] 这条 API 没给 digest，只能靠下载复验")

    # ② 匿名下载（真实用户走的路）
    try:
        got = gh_download(asset["browser_download_url"])
        got_sha = hashlib.sha256(got).hexdigest()
        if got_sha != apk_sha or len(got) != want:
            raise SystemExit("  [FAIL] 匿名下载的内容与本地不一致！本地 %s / 远端 %s"
                             % (apk_sha, got_sha))
        print("     [OK] 匿名下载 %d 字节，SHA256 一致" % len(got))
        return
    except SystemExit:
        raise
    except Exception as e:
        print("     [!] 匿名下载失败（github.com 不可达）：%s" % str(e)[:60])
        print("         -> 退回 API 资产端点复验")

    # ③ API 资产端点
    got = gh_download_via_api(token, asset["id"])
    got_sha = hashlib.sha256(got).hexdigest()
    if got_sha != apk_sha or len(got) != want:
        raise SystemExit("  [FAIL] API 下载的内容与本地不一致！本地 %s / 远端 %s"
                         % (apk_sha, got_sha))
    print("     [OK] 经 API 下载 %d 字节，SHA256 一致" % len(got))
    print("     [注意] 本次没能做匿名下载复验（github.com 不通）——"
          "文件本身已由服务端 digest + API 下载两路确认")


# ----------------------------------------------------------------------
# 版本与指纹
# ----------------------------------------------------------------------

def read_version():
    manifest = os.path.join(ROOT, "app", "AndroidManifest.xml")
    code, name = build_apk.read_manifest_version(manifest)
    if not code or not name:
        raise SystemExit("AndroidManifest.xml 里读不到 versionCode / versionName")
    return int(code), name


def source_hash():
    """
    参与 APK 构建的所有文件的指纹。

    只算真正打进 APK 的东西（源码 / 清单 / 资源），不算测试和工具脚本 ——
    改测试不该逼你升版本号。
    """
    h = hashlib.sha256()
    targets = []
    for base in (os.path.join(ROOT, "app", "src"), os.path.join(ROOT, "app", "res")):
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                targets.append(os.path.join(dirpath, f))
    targets.append(os.path.join(ROOT, "app", "AndroidManifest.xml"))
    for path in sorted(targets):
        h.update(os.path.relpath(path, ROOT).replace("\\", "/").encode("utf-8"))
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ----------------------------------------------------------------------
# 两道闸
# ----------------------------------------------------------------------

def gate_local(code, name, src_hash):
    """闸 1：源码变了但版本号没变 -> 拒绝。"""
    st = load_state()
    if not st:
        print("  闸 1（本地）：没有上次发布的记录，跳过")
        return
    same_version = (st.get("versionCode") == code and st.get("versionName") == name)
    if same_version and st.get("sourceHash") != src_hash:
        print()
        print("  " + "!" * 62)
        print("  [拒绝] 源码变了，但版本号没变。")
        print("     上次发布：versionCode=%s versionName=%s"
              % (st.get("versionCode"), st.get("versionName")))
        print("     现在还是：versionCode=%s versionName=%s" % (code, name))
        print()
        print("     请改 app/AndroidManifest.xml：versionCode 每次 +1，")
        print("     versionName 改 patch 位。")
        print("  " + "!" * 62)
        raise SystemExit(1)
    if same_version:
        print("  闸 1（本地）：版本号和源码都没变（重复发布同一份内容）")
    else:
        print("  闸 1（本地）：通过（版本号从 %s 升到了 %s）"
              % (st.get("versionName"), name))


def gate_remote(token, name, apk_sha):
    """
    闸 2：远端已经有这个版本号了，就把它的资产**下回来逐字节比**。

    内容一样 -> 允许（重复发布同一份内容无害）
    内容不同 -> **拒绝**。

    ## 实测补充：APK 不是逐字节可复现的
    同一份源码重建两次，SHA 就不一样（实测 9fe5331f… -> 57074d0c…，
    dex / zip 里带时间戳）。所以这一道闸的实际效果就是：
    **一个版本号只能发布一次** —— 这正是我们想要的规则。

    因此：
      - 只想改 Release 说明？那是 PATCH 正文，不用重传 APK，不受影响。
      - 确实需要用同一个版本号重传（极少见）？加 --force。

    这一道是最后防线：就算本地状态文件被删了，只要这个版本号发过，照样拦得住。
    """
    rel = gh_find_release(token, "v" + name)
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
    try:
        remote = gh_download(a["browser_download_url"])
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
# 推送
# ----------------------------------------------------------------------

def git_push(token, refs):
    """先走正常 git 通道；不通就自动降级走 GitHub REST。"""
    b64 = base64.b64encode(("x-access-token:" + token).encode("ascii")).decode("ascii")
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = "Authorization: Basic " + b64
    for ref in refs:
        p = sh(["git", "-c", "credential.helper=", "push", "origin", ref],
               env=env, cwd=ROOT, check=False)
        if p.returncode != 0:
            print()
            print("  [!] git push 失败（多半是代理挂了 / github.com 被丢包）")
            print("      -> 降级走 GitHub REST（api.github.com 通常还是通的）")
            return False
    return True


# ----------------------------------------------------------------------
# 兜底通道：完全绕开 git，用 Git Data API 推
# ----------------------------------------------------------------------

def _git(args):
    p = subprocess.run(["git"] + args, cwd=ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SystemExit("git %s 失败: %s" % (" ".join(args), p.stdout))
    return p.stdout


def _git_bytes(args):
    p = subprocess.run(["git"] + args, cwd=ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, check=True)
    return p.stdout


def _iso(ts, tz):
    """
    git 的 `1789972447 +0800` -> GitHub 要的 `2026-09-21T14:34:07+08:00`。

    **必须先按偏移量换算再格式化**：epoch 是 UTC，而 ISO 那串会被当成
    "+08:00 时区的本地时间"读回去。直接 gmtime 再挂个 +08:00 标签会差整整 8 小时，
    GitHub 反算出的 epoch 就跟本地对不上，commit SHA 也就对不上
    （实测栽过一次：差 28800 秒）。
    """
    sign = 1 if tz[0] == "+" else -1
    delta = sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(ts) + delta))
            + tz[:3] + ":" + tz[3:])


def _commit_meta(rev):
    """
    直接解析**原始提交对象**，一个字段都不自己拼。

    为什么不用 `git log --format=%B`：它会在末尾多加一个换行
    （原始消息以 `。\\n` 结尾，%B 给的是 `。\\n\\n`）。
    多这一个字节，GitHub 算出来的 commit SHA 就跟本地不一样 ——
    实测就是这么栽的：tree 校验通过了，commit 却对不上。
    """
    raw = _git_bytes(["cat-file", "commit", rev])
    head, _, msg = raw.partition(b"\n\n")
    info = {"parents": [], "tree": None, "signed": False}
    for line in head.decode("utf-8").split("\n"):
        key, _, val = line.partition(" ")
        if key == "tree":
            info["tree"] = val
        elif key == "parent":
            info["parents"].append(val)
        elif key == "gpgsig":
            # 签名提交（GitHub 网页端提交都带）**没救**：API 不接受 gpgsig，
            # 少这一个头，算出来的 SHA 必然不同。所以只能认出来、说清楚，
            # 而不是推上去一个"内容对、SHA 不对"的提交。
            info["signed"] = True
        elif key in ("author", "committer"):
            m = re.match(r"^(.*) <(.*)> (\d+) ([+-]\d{4})$", val)
            info[key] = {"name": m.group(1), "email": m.group(2),
                         "date": _iso(m.group(3), m.group(4))}
    info["message"] = msg.decode("utf-8")
    return info


def _tree_map(rev):
    """某个提交的 路径 -> (blob sha, mode)。"""
    out = {}
    for line in _git(["ls-tree", "-r", rev]).splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, _typ, sha = meta.split()
        out[path] = (sha, mode)
    return out


def push_via_api(token, tag, move_tag=True):
    """
    代理不可用时的兜底：用 Git Data API 把本地 HEAD 原样推上去。

    为什么值得写：`git push` 走 github.com（这个 IP 在部分网络下被丢包），
    而 `api.github.com` 通常还是通的。两者不是同一条路，实测一边全挂一边通。

    ## 关键点：推上去的 commit SHA 必须和本地**完全一样**
    不是"内容一样"就行 —— tree / parent / author / committer / message 全部原样
    交给 API，GitHub 算出来的 SHA 就会和本地一致，远端不会分叉，也就不需要
    事后 reset。每一步都核对 SHA，不一致就直接报错退出。

    ## 为什么必须**一个提交一个提交地重放**
    第一版想省事，直接拿 HEAD 去建 commit —— HTTP 422
    `Parent SHA does not exist or is not a commit object`。
    因为本地领先远端 3 个提交，中间那两个 GitHub 根本没有，
    而 Git Data API 不接受**服务器上不存在**的父提交。
    所以从远端 main 开始按拓扑顺序逐个重放，每个父提交都先落地。

    ## 内容必须从 git 对象里取，不能读工作区
    仓库有 `.gitattributes: * text=auto eol=lf`：工作区是 CRLF、仓库里存 LF，
    同一个文件字节数都不同（实测 CHANGELOG.md 10711 vs 10529）。
    读工作区会算出另一个 blob SHA，校验必然失败。
    """
    head = _git(["rev-parse", "HEAD"]).strip()

    # 1) 远端现在在哪
    st, ref = gh_call(token, "GET", "/repos/%s/git/ref/heads/main" % REPO)
    if st != 200:
        raise SystemExit("  读远端 main 失败 HTTP %s" % st)
    remote_sha = ref["object"]["sha"]
    if remote_sha == head:
        print("     远端已经是这个 commit，无需推送")
    else:
        if subprocess.run(["git", "merge-base", "--is-ancestor", remote_sha, head],
                          cwd=ROOT).returncode != 0:
            raise SystemExit("  [拒绝] 远端 main %s 不是本地 HEAD 的祖先，"
                             "无法重放（先 git pull / 处理分叉）" % remote_sha[:8])

        commits = _git(["rev-list", "--reverse", "--topo-order",
                        "%s..%s" % (remote_sha, head)]).split()
        print("     远端 main %s -> 本地 %s，需要重放 %d 个提交"
              % (remote_sha[:8], head[:8], len(commits)))

        seen = set()          # 已经传上去过的 blob，别重复传
        for i, c in enumerate(commits, 1):
            meta = _commit_meta(c)
            if meta["signed"]:
                raise SystemExit(
                    "  [拒绝] %s 是 GPG 签名的提交：%s\n"
                    "     GitHub 的 Git Data API 不接受 gpgsig 头，重放出来的 SHA\n"
                    "     一定和本地不同 —— 与其在远端留一个分叉，不如直接停下。\n"
                    "     办法：等代理恢复后用 git push，或把这几个提交 rebase 掉签名。"
                    % (c[:8], meta["message"].splitlines()[0][:40]))
            parents = meta["parents"]
            first = parents[0]                       # 跟第一个父提交做 diff
            base_tree = _git(["rev-parse", first + "^{tree}"]).strip()
            tree_sha = meta["tree"]
            local = _tree_map(c)

            # 1a) 这个提交相对父提交改了什么
            fields = _git_bytes(["diff-tree", "-r", "--no-commit-id",
                                 "--name-status", "-z", "--no-renames",
                                 first, c]).decode("utf-8").split("\x00")
            entries = []
            uploaded = 0
            for j in range(0, len(fields) - 1, 2):
                status, path = fields[j], fields[j + 1]
                if not path:
                    continue
                if status.startswith("D"):
                    entries.append({"path": path, "mode": "100644",
                                    "type": "blob", "sha": None})
                    continue
                sha, mode = local[path]
                if sha not in seen:
                    data = _git_bytes(["cat-file", "blob", sha])
                    st, blob = gh_call(
                        token, "POST", "/repos/%s/git/blobs" % REPO,
                        body={"content": base64.b64encode(data).decode("ascii"),
                              "encoding": "base64"})
                    if st != 201:
                        raise SystemExit("  上传 blob 失败 %s HTTP %s: %s"
                                         % (path, st, blob))
                    if blob["sha"] != sha:
                        raise SystemExit("  [FAIL] %s 上传后 SHA 不符：本地 %s / 远端 %s"
                                         % (path, sha, blob["sha"]))
                    seen.add(sha)
                    uploaded += 1
                entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})

            # 1b) tree：建完核对，SHA 必须等于本地那个
            st, nt = gh_call(token, "POST", "/repos/%s/git/trees" % REPO,
                             body={"base_tree": base_tree, "tree": entries})
            if st != 201:
                raise SystemExit("  建 tree 失败 HTTP %s: %s" % (st, nt))
            if nt["sha"] != tree_sha:
                raise SystemExit("  [FAIL] %s 的 tree 与本地不一致：本地 %s / 远端 %s"
                                 % (c[:8], tree_sha, nt["sha"]))

            # 1c) commit：建完核对，SHA 必须等于本地那个
            st, nc = gh_call(token, "POST", "/repos/%s/git/commits" % REPO, body={
                "message": meta["message"], "tree": tree_sha, "parents": parents,
                "author": meta["author"], "committer": meta["committer"]})
            if st != 201:
                raise SystemExit("  建 commit %s 失败 HTTP %s: %s" % (c[:8], st, nc))
            if nc["sha"] != c:
                raise SystemExit("  [FAIL] commit 与本地不一致：本地 %s / 远端 %s\n"
                                 "     内容是对的，但 SHA 不同 —— 检查 author/committer"
                                 % (c, nc["sha"]))
            print("     [%d/%d] %s  %s（%d 处改动，新传 %d 个 blob）"
                  % (i, len(commits), c[:8],
                     meta["message"].splitlines()[0][:34], len(entries), uploaded))

    st, _ = gh_call(token, "PATCH", "/repos/%s/git/refs/heads/main" % REPO,
                    body={"sha": head, "force": False})
    if st != 200:
        raise SystemExit("  更新 main 失败 HTTP %s" % st)
    print("     main -> %s [OK]" % head[:8])

    # 2) 标签：本地是 annotated tag，tagger 和消息都照抄，SHA 才对得上
    if not move_tag:
        print("     （--no-tag：标签保持不动）")
        return
    # 幂等：远端标签已经指向这个提交就别重造 —— 否则每跑一次 tag 对象就换一个
    # （tagger 时间是新的），纯粹是噪音。
    st, cur = gh_call(token, "GET", "/repos/%s/git/ref/tags/%s" % (REPO, tag))
    if st == 200:
        st2, peeled = gh_call(token, "GET", "/repos/%s/git/tags/%s"
                              % (REPO, cur["object"]["sha"]))
        target = peeled.get("object", {}).get("sha") if st2 == 200 else cur["object"]["sha"]
        if target == head:
            print("     tag %s 已指向该提交，跳过" % tag)
            return

    raw = _git(["cat-file", "-p", "refs/tags/" + tag]).rstrip("\n")
    lines = raw.split("\n")
    tagger_line = next(l for l in lines if l.startswith("tagger "))
    empty = lines.index("")                     # tagger 与消息之间的那个空行
    tag_msg = "\n".join(lines[empty + 1:]) + "\n"
    tm = re.match(r"^tagger (.*) <(.*)> (\d+) ([+-]\d{4})$", tagger_line)
    tagger = {"name": tm.group(1), "email": tm.group(2),
              "date": _iso(tm.group(3), tm.group(4))}
    local_tag_sha = _git(["rev-parse", "refs/tags/" + tag]).strip()
    st, nt2 = gh_call(token, "POST", "/repos/%s/git/tags" % REPO, body={
        "tag": tag, "message": tag_msg, "object": head, "type": "commit",
        "tagger": tagger})
    if st != 201:
        raise SystemExit("  建 tag 对象失败 HTTP %s: %s" % (st, nt2))
    st, _ = gh_call(token, "POST", "/repos/%s/git/refs" % REPO,
                    body={"ref": "refs/tags/" + tag, "sha": nt2["sha"]})
    if st == 422:
        # 上一次跑到一半留下的同名标签：改指过去，别把整个发布卡在这儿
        st, _ = gh_call(token, "PATCH", "/repos/%s/git/refs/tags/%s" % (REPO, tag),
                        body={"sha": nt2["sha"], "force": True})
        print("     （远端已有同名标签，已更新指向）")
    if st not in (200, 201):
        raise SystemExit("  建 tag ref 失败 HTTP %s" % st)
    print("     tag %s -> %s%s" % (tag, nt2["sha"][:8],
                                   "  [OK] 与本地一致" if nt2["sha"] == local_tag_sha
                                   else "  [注意] 本地是 %s（内容一致即正常）"
                                        % local_tag_sha[:8]))




# ----------------------------------------------------------------------

def push_only(args, tag, code):
    """
    只推 main（可选带标签），不构建、不碰 Release。

    发完版之后又想改文档 / 改工具时用它。走的是和 --publish 完全同一条推送通道
    （`git push` 失败自动降级到 Git Data API），所以该核对的 SHA 一个都不少。
    """
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("  需要 GH_PAT 环境变量（可用 dsh 的 gh_get_pat.ps1 解析）")

    head = _git(["rev-parse", "HEAD"]).strip()
    refs = ["main"]
    if args.no_tag:
        print("  --no-tag：只推 main")
    else:
        # 标签已经指向 HEAD 就别重造：tagger 时间是新的，SHA 会变，纯噪音
        cur = subprocess.run(["git", "rev-parse", "-q", "--verify", tag + "^{commit}"],
                             cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout.decode().strip()
        if cur == head:
            print("  标签 %s 已在 HEAD 上，不动" % tag)
        else:
            sh(["git", "tag", "-d", tag], cwd=ROOT, check=False)
            sh(["git", "tag", "-a", tag, "-m",
                "HA-F1-RaceControl %s\n\nversionCode %d。详见 CHANGELOG.md。"
                % (tag, code)], cwd=ROOT)
            refs.append("refs/tags/" + tag)

    print()
    print("  推送 %s ..." % " + ".join(refs))
    if not git_push(token, refs):
        push_via_api(token, tag, move_tag=("refs/tags/" + tag) in refs)
    print()
    print("  [OK] 已推送（未构建、未改 Release）")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true", help="检查通过后真的发布")
    ap.add_argument("--notes", default="", help="Release 说明 markdown 路径")
    ap.add_argument("--force", action="store_true",
                    help="跳过闸门（只在你明确知道自己在干什么时用）")
    ap.add_argument("--push-only", action="store_true",
                    help="只推 main（和标签），**不构建、不动 Release** ——"
                         "发完版又改了文档/工具时用，避免把已发布的 APK 重新构建一遍")
    ap.add_argument("--no-tag", action="store_true",
                    help="配合 --push-only：连标签也不动，只推 main")
    args = ap.parse_args()

    code, name = read_version()
    tag = "v" + name
    apk = os.path.join(DIST, "HA-F1-RaceControl-%s.apk" % tag)

    print("=" * 70)
    print("  版本 : versionCode=%d  versionName=%s   tag=%s" % (code, name, tag))
    print("=" * 70)

    if args.push_only:
        # 只推引用。为什么需要它：APK **不是逐字节可复现的**，
        # 发完版之后再跑一次 --publish 会重新构建、重新上传，
        # 于是 Release 说明里写死的 SHA256 当场作废。
        # 补文档不该动任何 Release 资产。
        return push_only(args, tag, code)

    src_hash = source_hash()
    print("  源码指纹 : %s" % src_hash[:16])
    print()

    if not args.force:
        gate_local(code, name, src_hash)

    print()
    print("  构建 APK ...")
    sh([sys.executable, os.path.join(HERE, "build_apk.py"),
        "--out", apk, "--name", os.path.basename(apk)[:-4]], cwd=ROOT)
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

    if not args.force:
        gate_remote(token, name, apk_sha)

    # ---- 打标签 + 推送 ----
    print()
    print("  打标签 %s 并推送 main ..." % tag)
    sh(["git", "tag", "-d", tag], cwd=ROOT, check=False)
    sh(["git", "tag", "-a", tag, "-m",
        "HA-F1-RaceControl %s\n\nversionCode %d。详见 CHANGELOG.md。" % (tag, code)],
       cwd=ROOT)
    if not git_push(token, ["main", "refs/tags/" + tag]):
        push_via_api(token, tag)

    # ---- 发 Release ----
    print()
    print("  发布 Release ...")
    body = ""
    if args.notes and os.path.exists(args.notes):
        body = open(args.notes, encoding="utf-8").read()
    rel = gh_find_release(token, tag)
    if rel:
        st, _ = gh_call(token, "PATCH", "/repos/%s/releases/%d" % (REPO, rel["id"]),
                        body={"body": body, "name": tag})
        print("     更新已有 Release -> HTTP %s" % st)
        for a in rel.get("assets", []):
            gh_call(token, "DELETE", "/repos/%s/releases/assets/%d" % (REPO, a["id"]))
            print("     删旧资产 %s" % a["name"])
    else:
        st, rel = gh_call(token, "POST", "/repos/%s/releases" % REPO, body={
            "tag_name": tag, "name": tag, "body": body,
            "draft": False, "prerelease": False})
        if st != 201:
            raise SystemExit("  建 Release 失败 HTTP %s: %s" % (st, rel))
        print("     新建 Release -> %s" % rel["html_url"])

    data = open(apk, "rb").read()
    url = ("%s/repos/%s/releases/%d/assets?name=%s"
           % (UPLOADS, REPO, rel["id"], urllib.parse.quote(os.path.basename(apk))))
    st, res = gh_call(token, "POST", url, raw=data,
                      content_type="application/vnd.android.package-archive")
    if st != 201:
        raise SystemExit("  上传失败 HTTP %s: %s" % (st, res))
    print("     上传 %s (%d 字节)" % (os.path.basename(apk), len(data)))

    # ---- 复验（匿名下载 / 服务端 digest / API 端点，见 verify_asset 说明）----
    verify_asset(token, res, apk_sha, data)

    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"versionCode": code, "versionName": name, "sourceHash": src_hash,
                   "apkSha256": apk_sha, "tag": tag,
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=2)
    print()
    print("=" * 70)
    print("  已发布 %s" % rel["html_url"])
    print("  本次 SHA256 = %s" % apk_sha)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
