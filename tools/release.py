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

def gh_call(token, method, url, body=None, raw=None, content_type=None, attempts=4):
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
            with urllib.request.urlopen(req, timeout=120) as r:
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
                time.sleep(2.0 * (i + 1))
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


def push_via_api(token, tag):
    """
    代理不可用时的兜底：用 Git Data API 把本地 HEAD 原样推上去。

    为什么值得写：`git push` 走 github.com（这个 IP 在部分网络下被丢包），
    而 `api.github.com` 通常还是通的。两者不是同一条路。

    ## 关键点：推上去的 commit SHA 必须和本地**完全一样**
    不是"内容一样"就行 —— 我把本地 commit 的 tree / parent / author / committer /
    message 原样交给 API，GitHub 算出来的 SHA 就会和本地一致。
    这样本地和远端不会分叉，也就不需要事后 reset。
    blob 也一样：上传后 API 返回的 SHA 必须等于 `git ls-tree` 给出的 SHA，
    不等就说明内容变了，直接报错（这是比"上传成功"硬得多的证据）。
    """
    # 1) 本地 HEAD 的完整信息
    head = _git(["rev-parse", "HEAD"]).strip()
    tree_sha = _git(["rev-parse", "HEAD^{tree}"]).strip()
    parents = _git(["rev-list", "--parents", "-n", "1", "HEAD"]).split()[1:]
    raw = _git(["log", "-1", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"])
    parts = raw.split("\x00")
    an, ae, ad, cn, ce, cd = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
    message = parts[6]
    print("     本地 HEAD %s（%d 个父提交）" % (head[:8], len(parents)))

    # 2) 远端现在是什么
    st, ref = gh_call(token, "GET", "/repos/%s/git/ref/heads/main" % REPO)
    if st != 200:
        raise SystemExit("  读远端 main 失败 HTTP %s" % st)
    remote_sha = ref["object"]["sha"]
    if remote_sha == head:
        print("     远端已经是这个 commit，无需推送")
        return
    st, rc = gh_call(token, "GET", "/repos/%s/git/commits/%s" % (REPO, remote_sha))
    remote_tree = rc["tree"]["sha"]
    st, rt = gh_call(token, "GET",
                     "/repos/%s/git/trees/%s?recursive=1" % (REPO, remote_tree))
    remote_blobs = {i["path"]: i["sha"] for i in rt.get("tree", []) if i["type"] == "blob"}
    print("     远端 main %s，%d 个文件" % (remote_sha[:8], len(remote_blobs)))

    # 3) 本地全部文件
    local = {}
    for line in _git(["ls-tree", "-r", "HEAD"]).splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, typ, sha = meta.split()
        local[path] = (sha, mode)

    # 4) 只传变了的 blob，并**逐个核对 SHA**
    entries = []
    uploaded = 0
    for path, (sha, mode) in sorted(local.items()):
        if remote_blobs.get(path) == sha:
            continue
        with open(os.path.join(ROOT, path), "rb") as f:
            data = f.read()
        st, blob = gh_call(token, "POST", "/repos/%s/git/blobs" % REPO,
                           body={"content": base64.b64encode(data).decode("ascii"),
                                 "encoding": "base64"})
        if st != 201:
            raise SystemExit("  上传 blob 失败 %s HTTP %s: %s" % (path, st, blob))
        if blob["sha"] != sha:
            raise SystemExit("  [FAIL] %s 上传后 SHA 不符：本地 %s / 远端 %s"
                             % (path, sha, blob["sha"]))
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        uploaded += 1
    # 远端有、本地没有的 -> 删掉
    for path in remote_blobs:
        if path not in local:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print("     删除远端多出的文件: %s" % path)
    print("     上传 %d 个 blob（其余 %d 个远端已有）"
          % (uploaded, len(local) - uploaded))

    # 5) 造 tree + commit（字段照抄本地，SHA 才会一致）
    st, nt = gh_call(token, "POST", "/repos/%s/git/trees" % REPO,
                     body={"base_tree": remote_tree, "tree": entries})
    if st != 201:
        raise SystemExit("  建 tree 失败 HTTP %s: %s" % (st, nt))
    if nt["sha"] != tree_sha:
        raise SystemExit("  [FAIL] 远端 tree 与本地不一致：本地 %s / 远端 %s"
                         % (tree_sha, nt["sha"]))
    print("     tree %s [OK] 与本地一致" % nt["sha"][:8])

    st, nc = gh_call(token, "POST", "/repos/%s/git/commits" % REPO, body={
        "message": message, "tree": tree_sha, "parents": parents,
        "author": {"name": an, "email": ae, "date": ad},
        "committer": {"name": cn, "email": ce, "date": cd}})
    if st != 201:
        raise SystemExit("  建 commit 失败 HTTP %s: %s" % (st, nc))
    if nc["sha"] != head:
        raise SystemExit("  [FAIL] 远端 commit 与本地不一致：本地 %s / 远端 %s\n"
                         "     内容是对的，但 SHA 不同 —— 请检查 author/committer 字段"
                         % (head, nc["sha"]))
    print("     commit %s [OK] 与本地一致" % nc["sha"][:8])

    st, _ = gh_call(token, "PATCH", "/repos/%s/git/refs/heads/main" % REPO,
                    body={"sha": head, "force": False})
    if st != 200:
        raise SystemExit("  更新 main 失败 HTTP %s" % st)
    print("     main -> %s" % head[:8])

    # 6) 标签：本地是 annotated tag，tagger 和消息都照抄，SHA 才对得上
    raw = _git(["cat-file", "-p", "refs/tags/" + tag]).rstrip("\n")
    lines = raw.split("\n")
    tagger_line = next(l for l in lines if l.startswith("tagger "))
    empty = lines.index("")                     # tagger 与消息之间的那个空行
    tag_msg = "\n".join(lines[empty + 1:]) + "\n"
    tm = re.match(r"^tagger (.*) <(.*)> (\d+) ([+-]\d{4})$", tagger_line)
    off = tm.group(4)
    tagger = {"name": tm.group(1), "email": tm.group(2),
              "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(tm.group(3))))
                      + off[:3] + ":" + off[3:]}
    local_tag_sha = _git(["rev-parse", "refs/tags/" + tag]).strip()
    st, nt2 = gh_call(token, "POST", "/repos/%s/git/tags" % REPO, body={
        "tag": tag, "message": tag_msg, "object": head, "type": "commit",
        "tagger": tagger})
    if st != 201:
        raise SystemExit("  建 tag 对象失败 HTTP %s: %s" % (st, nt2))
    st, _ = gh_call(token, "POST", "/repos/%s/git/refs" % REPO,
                    body={"ref": "refs/tags/" + tag, "sha": nt2["sha"]})
    if st not in (201, 422):
        raise SystemExit("  建 tag ref 失败 HTTP %s" % st)
    print("     tag %s -> %s%s" % (tag, nt2["sha"][:8],
                                   "  [OK] 与本地一致" if nt2["sha"] == local_tag_sha
                                   else "  [注意] 本地是 %s（内容一致即正常）"
                                        % local_tag_sha[:8]))



# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true", help="检查通过后真的发布")
    ap.add_argument("--notes", default="", help="Release 说明 markdown 路径")
    ap.add_argument("--force", action="store_true",
                    help="跳过闸门（只在你明确知道自己在干什么时用）")
    args = ap.parse_args()

    code, name = read_version()
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

    # ---- 匿名下载复验 ----
    print()
    print("  匿名下载复验 ...")
    got = gh_download(res["browser_download_url"])
    got_sha = hashlib.sha256(got).hexdigest()
    if got_sha != apk_sha or len(got) != len(data):
        raise SystemExit("  [FAIL] 远端内容与本地不一致！本地 %s / 远端 %s"
                         % (apk_sha, got_sha))
    print("     [OK] 匿名下载 %d 字节，SHA256 一致" % len(got))

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
