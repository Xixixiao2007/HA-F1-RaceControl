#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
release.py 的「对象重建」自测：**在本地就能证明推送上去的 SHA 会一致**。

## 为什么要单独测这个
`tools/release.py` 在 `git push` 不通时会降级走 GitHub Git Data API，
把本地提交**原样重放**到远端。它的正确性标准只有一条：

    远端算出来的 commit / tag SHA，必须和本地**一模一样**。

差一个字节就不一样，而内容看着还是"对的" —— 这种错最难发现。
踩过的两次：

  1. 消息用 `git log --format=%B` 取，它在末尾会**多一个换行**
     （原始消息以 `。\\n` 结尾，%B 给的是 `。\\n\\n`）。
     结果 tree 校验通过、commit SHA 对不上。
  2. 时间戳按 ISO 格式化时忘了先按偏移量换算，
     `+0800` 的提交整整差了 28800 秒。

（另有一类**测不了**的：GPG 签名的提交。GitHub 网页端的提交都带 `gpgsig`
头，而 API 不接受这个头，SHA 根本没法复现。所以 `release.py` 碰到签名提交
会直接拒绝推送；这个自测把它们单独列出来，不混进"通过"里。）

## 怎么在本地测
GitHub 的 `POST /git/commits` 就是拿我们给的字段拼出对象再算 SHA。
所以这里**自己拼一遍**、算 SHA、和 `git rev-parse` 比 ——
不用发任何网络请求，就能把上面那两类错误钉死在本地。

用法:
    python tools/test_release_meta.py
"""
import hashlib
import importlib.util
import os
import re
import subprocess
import sys

# Windows 控制台默认可能是 GBK：这里要打中文提交信息，不换编码会自己崩在 print 上。
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 直接加载 release.py（它是脚本，不是包）
spec = importlib.util.spec_from_file_location("release", os.path.join(HERE, "release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-6s %s%s" % ("[OK]" if ok else "[FAIL]", name,
                           ("   " + detail) if detail else ""))


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True).stdout


def git_text(*args):
    return git(*args).decode("utf-8", "replace")


def obj_sha(kind, body):
    """git 的对象哈希：sha1("<kind> <长度>\\0" + body)。"""
    return hashlib.sha1(("%s %d" % (kind, len(body))).encode() + b"\x00" + body).hexdigest()


def iso_to_git(iso):
    """_iso() 的逆运算：`2026-09-21T14:34:07+08:00` -> `1789972447 +0800`。"""
    import calendar
    import time as _t
    date, off = iso[:-6], iso[-6:]
    dt = _t.strptime(date, "%Y-%m-%dT%H:%M:%S")
    sign = 1 if off[0] == "+" else -1
    delta = sign * (int(off[1:3]) * 3600 + int(off[4:6]) * 60)
    return "%d %s" % (calendar.timegm(dt) - delta, off.replace(":", ""))


def rebuild_commit(rev):
    """用 _commit_meta() 的结果把提交对象拼回去。"""
    m = release._commit_meta(rev)
    body = ("tree %s\n" % m["tree"]).encode("utf-8")
    for p in m["parents"]:
        body += ("parent %s\n" % p).encode("utf-8")
    for role in ("author", "committer"):
        body += ("%s %s <%s> %s\n"
                 % (role, m[role]["name"], m[role]["email"],
                    iso_to_git(m[role]["date"]))).encode("utf-8")
    body += b"\n" + m["message"].encode("utf-8")
    return body


def rebuild_tag(tag):
    """用和 release.push_via_api 里同一套解析把 tag 对象拼回去。"""
    raw = git_text("cat-file", "-p", "refs/tags/" + tag).rstrip("\n")
    lines = raw.split("\n")
    tagger_line = next(l for l in lines if l.startswith("tagger "))
    empty = lines.index("")
    msg = "\n".join(lines[empty + 1:]) + "\n"
    tm = re.match(r"^tagger (.*) <(.*)> (\d+) ([+-]\d{4})$", tagger_line)
    obj = next(l for l in lines if l.startswith("object ")).split(" ", 1)[1]
    typ = next(l for l in lines if l.startswith("type ")).split(" ", 1)[1]
    body = ("object %s\ntype %s\ntag %s\ntagger %s <%s> %s\n\n%s"
            % (obj, typ, tag, tm.group(1), tm.group(2),
               iso_to_git(release._iso(tm.group(3), tm.group(4))), msg)).encode("utf-8")
    return body


def main():
    # ---- 1. 时区换算：这一条单独测，因为它错过一次（差 8 小时）----
    print("① 时间戳换算（_iso）")
    cases = [("1789972447", "+0800", "2026-09-21T14:34:07+08:00"),
             ("0", "+0000", "1970-01-01T00:00:00+00:00"),
             ("1789972447", "+0000", "2026-09-21T06:34:07+00:00"),
             ("1789972447", "-0500", "2026-09-21T01:34:07-05:00")]
    for ts, tz, want in cases:
        got = release._iso(ts, tz)
        check("_iso(%s %s) = %s" % (ts, tz, want), got == want, "得到 " + got)
    # 往返：iso -> git -> iso 必须回到原样
    for ts, tz, _w in cases:
        iso = release._iso(ts, tz)
        back = iso_to_git(iso)
        check("往返 %s %s" % (ts, tz), back == "%s %s" % (ts, tz), "得到 " + back)

    # ---- 2. 提交对象重建 ----
    print()
    print("② 提交对象重建（GitHub 就是拿这些字段算 SHA 的）")
    revs = git_text("rev-list", "-n", "30", "HEAD").split()
    revs.reverse()
    skipped = []
    if not revs:
        check("仓库里有提交", False, "一个提交都没有，测不了")
    for rev in revs:
        meta = release._commit_meta(rev)
        subject = meta["message"].splitlines()[0][:34]
        if meta["signed"]:
            # GPG 签名的提交（GitHub 网页端提交都带）**没法用 API 复现**：
            # 接口不接受 gpgsig 头。这不算我们算错，所以不记为失败，
            # 但要单独报出来 —— release.py 遇到它会直接拒绝推送。
            skipped.append("%s %s" % (rev[:8], subject))
            continue
        body = rebuild_commit(rev)
        got = obj_sha("commit", body)
        want = git_text("rev-parse", rev).strip()
        check("%s  %s" % (rev[:8], subject), got == want,
              "" if got == want else "重建 %s / 实际 %s" % (got[:8], want[:8]))
    if skipped:
        print()
        print("  （%d 个 GPG 签名提交跳过，API 无法复现其 SHA）" % len(skipped))
        for s in skipped:
            print("      - " + s)

    # ---- 3. 标签对象重建（有标签才测）----
    print()
    print("③ 标签对象重建")
    tags = git_text("tag", "--list").split()
    if not tags:
        print("  （仓库里没有标签，跳过）")
    for tag in tags:
        # 只测 annotated tag（lightweight tag 没有自己的对象）
        typ = git_text("cat-file", "-t", "refs/tags/" + tag).strip()
        if typ != "tag":
            print("  （%s 是 lightweight tag，没有独立对象，跳过）" % tag)
            continue
        body = rebuild_tag(tag)
        got = obj_sha("tag", body)
        want = git_text("rev-parse", "refs/tags/" + tag).strip()
        check("tag %s" % tag, got == want,
              "" if got == want else "重建 %s / 实际 %s" % (got[:8], want[:8]))

    # ---- 4. 参数顺序不能乱：tree/parent/author/committer ----
    print()
    print("④ 头部字段顺序")
    rev = revs[-1]
    m = release._commit_meta(rev)
    raw_head = git("cat-file", "commit", rev).split(b"\n\n", 1)[0].decode("utf-8")
    order = [l.split(" ")[0] for l in raw_head.split("\n")]
    check("tree 在最前", order[0] == "tree", "实际 " + order[0])
    check("parent 紧随其后", order[1] == "parent", "实际 " + order[1])
    check("author 在 committer 之前",
          order.index("author") < order.index("committer"))
    check("解析出的 message 和原对象逐字节一致",
          release._commit_meta(rev)["message"].encode("utf-8")
          == git("cat-file", "commit", rev).split(b"\n\n", 1)[1])

    print()
    print("=" * 62)
    total = len(PASS) + len(FAIL)
    print("  对象重建自测：共 %d 项，通过 %d，失败 %d" % (total, len(PASS), len(FAIL)))
    for name in FAIL:
        print("     [FAIL] " + name)
    print("=" * 62)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
