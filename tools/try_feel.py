#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
手感测试台：起一个假 HA，按数字键就往手机推一段剧本。

为什么要有它：`run_all_tests.py` 验证的是"逻辑对不对"，但**闪动快不快、
震动分不分得清、警报音够不够响、15 秒自动停是不是太长** —— 这些只能在真机上用手感判断。
而比赛两周才有一次，不能等。

关键是**按键就放**：不用等回放、不用重启服务器、不用改配置。

用法:
    python tools/try_feel.py                      # 自动探测局域网 IP 并起服务器
    python tools/try_feel.py --port 8123 --token feel
    python tools/try_feel.py --fire red_flag      # 非交互：只触发一个剧本（自测用）
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# 剧本 -> (菜单描述, 该看什么)
MENU = [
    ("test_double_yellow", "双黄旗 3 秒后清除", "★ 应该【只闪动 + 一短震】，绝不弹全屏（判为系统测试）"),
    ("red_flag",           "红旗 + 双黄扩散",   "★ 全屏红 + 三长震 + 循环警报音；状态条锁红"),
    ("vsc",                "虚拟安全车",        "★ 全屏黄 + 一长震；状态条显示 VSC"),
    ("safety_car",         "安全车",            "★ 全屏橙 + 两震；状态条显示「安全车」"),
    ("penalty",            "判罚全链条",        "★ 看列表里每条的【中文简述】：罚几秒、罚谁、为什么"),
    ("race_start",         "起跑 + 一堆噪音",   "黄旗/黑白旗的轻提醒手感；噪音默认被过滤掉"),
    ("burst",              "7 条/秒突发",       "★ 只闪最新那条，不该整屏乱闪或卡顿"),
]


def local_ip():
    """拿到本机在局域网里的地址（不真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def post(url, token):
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "try-feel")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def get_status(port, token):
    req = urllib.request.Request("http://127.0.0.1:%d/_mock/status" % port)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "try-feel")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def wait_ready(proc, port, token, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if get_status(port, token) is not None:
            return True
        time.sleep(0.3)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--token", default="feel")
    ap.add_argument("--entity", default="sensor.f1_race_control_2")
    ap.add_argument("--fire", default="", help="非交互：只触发这一个剧本然后退出")
    ap.add_argument("--hold", type=float, default=25.0,
                    help="配合 --fire：触发后让服务器再活多少秒（默认 25，够演完一个剧本）")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="剧本速度。0.25 = 慢放四倍，看闪动动画和震动节奏时很有用（默认 1.0）")
    args = ap.parse_args()

    ip = local_ip()

    # --start-offset 给一个极大的值：服务器起来后一直处于「无比赛」状态
    # （/api/states 是 unavailable），直到我们按键切到某个剧本。
    # 否则它一上来就按 400 倍速猛灌整个比赛周末，手机上全是历史回填，没法看手感。
    cmd = [PY, os.path.join(HERE, "mock_ha.py"),
           "--host", "0.0.0.0", "--port", str(args.port),
           "--token", args.token, "--entity", args.entity,
           "--speed", str(args.speed),
           "--start-offset", "100000"]
    print("[手感测试台] 启动假 HA: %s" % " ".join(cmd[1:]))
    proc = subprocess.Popen(cmd, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if not wait_ready(proc, args.port, args.token):
            print("[FAIL] 服务器没起来（端口 %d 被占用？）" % args.port)
            return 1

        print()
        print("=" * 68)
        print("  手机上这样填（App 右上角「设置」）")
        print("=" * 68)
        print("     服务器地址   http://%s:%d" % (ip, args.port))
        print("     访问令牌     %s" % args.token)
        print("     实体 ID      %s" % args.entity)
        print()
        print("  手机要和这台电脑在同一个 WiFi 下。填完点「测试连接」再「保存」。")
        print("=" * 68)

        if args.fire:
            st, body = post("http://127.0.0.1:%d/_mock/scenario?name=%s"
                            % (args.port, args.fire), args.token)
            print("触发 %s -> HTTP %s %s" % (args.fire, st, body[:140]))
            # 剧本要演完才有意义 —— 触发完立刻关服务器的话，脚本才放了一条就被掐了
            seq0 = None
            deadline = time.time() + max(1.0, args.hold)
            while time.time() < deadline:
                time.sleep(1.0)
                s = get_status(args.port, args.token)
                if s:
                    seq0 = s.get("sequence")
                    sys.stdout.write("\r   已推送 %s 条…" % seq0)
                    sys.stdout.flush()
            print()
            s = get_status(args.port, args.token)
            if s:
                print("   最终：sequence=%s mode=%s scenario=%s"
                      % (s.get("sequence"), s.get("progress", {}).get("mode"),
                         s.get("progress", {}).get("scenario")))
            return 0 if st == 200 else 1

        while True:
            print()
            print("-" * 68)
            for i, (name, desc, _hint) in enumerate(MENU, 1):
                print("   [%d] %-12s %s" % (i, name, desc))
            print("   [s] 看当前状态     [q] 退出")
            print("-" * 68)
            try:
                choice = input("选一个回车: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if choice in ("q", "quit", "exit", ""):
                if choice == "":
                    continue
                break
            if choice == "s":
                s = get_status(args.port, args.token)
                print("   状态:", json.dumps(s, ensure_ascii=False) if s else "(读不到)")
                continue
            if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
                print("   输入 1-%d，或 s / q" % len(MENU))
                continue

            name, desc, hint = MENU[int(choice) - 1]
            st, body = post("http://127.0.0.1:%d/_mock/scenario?name=%s"
                            % (args.port, name), args.token)
            if st == 200:
                print()
                print("   >>> 已放：%s（%s）" % (name, desc))
                print("   >>> 盯着手机看：%s" % hint)
            else:
                print("   [FAIL] 触发失败 HTTP %s %s" % (st, body[:150]))
        return 0
    finally:
        print()
        print("[手感测试台] 关掉假 HA")
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
