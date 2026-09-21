#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mock_ha.py —— 假 Home Assistant 服务器（纯标准库，无第三方依赖）。

为什么需要它
------------
真实 F1 比赛两周才有一次，而且用户的 HA 在另一个网络（公网 bh4gzk.top:8123）。
App 里那些只有"真数据 + 真协议"才能验证的东西（WebSocket 订阅协议、旗语分类、
双黄聚类、sequence 丢号、7 条/秒突发）平时根本没法测。
所以这里照着 HA 的真实行为造一个替身：REST 历史接口 + 手写 RFC6455 WebSocket，
数据源用已经抓下来的 697 条真实消息。

三个"照抄文档就会写错"的地方（设计依据，见 dsh/files/reports/F1RC_功能规格_v2.0.md）
--------------------------------------------------------------------------------
1. 安全车/VSC 的 `flag` 是**空字符串**，`category` 才是 `"SafetyCar"`。
   App 按 flag 判断会永远不触发 —— 所以剧本里必须忠实复现这一点。
2. `sector` 是**字符串**（"12"），不是数字。
3. HA 的 history 接口返回的第一条不是数据库里的原始记录，而是**它自己合成的
   "期初状态"**：区间开始时仍然生效的那个值，`last_changed` 被改写成**你请求的
   start 时刻**。这一条最容易漏，漏了 App 的时间轴就会错位。

用法
----
    # 真机联调（手机要连得上，必须监听 0.0.0.0；token 换成 App 里填的那个）
    python dsh\\tools\\mock_ha.py --host 0.0.0.0 --port 8123 --token my-token

    # 不依赖真实时间轴，直接演剧本（每步 2 秒）
    python dsh\\tools\\mock_ha.py --scenario vsc

    # 压缩回放整段比赛周末：真实跨度 51 小时，400 倍速约 8 分钟播完
    python dsh\\tools\\mock_ha.py --speed 400 --loop

    # 全速灌数据，压 App 的突发处理
    python dsh\\tools\\mock_ha.py --speed 0 --loop

    # 先空转 10 秒（赛前 unavailable），再开始回放
    python dsh\\tools\\mock_ha.py --speed 20 --start-offset 10

    python dsh\\tools\\mock_ha.py --list-scenarios        # 看有哪些剧本

Ctrl+C 干净退出。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback

# Windows 控制台默认可能是 GBK，中文日志会炸；这里强制 UTF-8。
# 放在 import 之后立刻做，保证后面的 print 都不会因为编码抛异常。
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # 老 Python 或被重定向到不支持 reconfigure 的对象
        pass

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_data():
    """找一个可用的真实数据 TSV。

    这个脚本有两种存在形态，数据也跟着放两个地方：
      - 在 HA-F1-RaceControl 仓库里：tools/mock_data/racecontrol_history.tsv
      - 在开发工作区里：dsh/files/data/racecontrol_history.tsv
    两边都试，谁先存在用谁；都没有就返回第一个候选，让调用方去报错。
    """
    cands = [
        os.path.join(HERE, "mock_data", "racecontrol_history.tsv"),
        os.path.join(os.path.dirname(HERE), "dsh", "files", "data",
                     "racecontrol_history.tsv"),
        os.path.join(os.path.dirname(os.path.dirname(HERE)), "dsh", "files", "data",
                     "racecontrol_history.tsv"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


# 兼容旧代码里对 REPO_ROOT 的引用
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_DATA = _find_data()

HA_VERSION = "2026.9.0"                 # 和真机 HA 版本对齐，App 侧若有版本判断不会翻车
FRIENDLY_NAME = "F1 - Officials Race control"
ICON = "mdi:flag-outline"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC6455 规定的握手魔数
HISTORY_KEEP = 5                        # 上游集成 attributes.history 只保留最近 5 条

# 无比赛时的样子。真实 HA 里实体没有状态就是 unavailable，并且**只剩**这两个属性。
UNAVAILABLE_ATTRIBUTES = {"friendly_name": FRIENDLY_NAME, "icon": ICON}

TSV_COLUMNS = ["local_time", "last_changed", "flag", "category", "scope",
               "sector", "car_number", "event_id", "state", "message"]


def _log(msg):
    sys.stdout.write("[mock-ha] %s\n" % msg)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def utc_now_iso():
    """当前 UTC，带微秒和 +00:00 偏移 —— 和 HA 的 last_changed 格式保持一致。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def drop_microseconds(ts):
    """把带微秒的 ISO 时间戳降到秒精度，但**保留时区偏移**。

    为什么要自己拼而不用 strftime("%Y-%m-%dT%H:%M:%S%z")：
    %z 在有些平台上给 "+0000" 而不是 "+00:00"，而 App 端按 ISO8601 解析，
    属性 `utc` 期望的是 "2026-09-12T10:56:34+00:00" 这个形状。
    """
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return dt.replace(microsecond=0).isoformat()


def parse_iso_param(raw):
    """解析 URL 里的时间参数。HA 接受 2021-01-01T00:00:00+00:00 / 带 Z / 纯日期。"""
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    # 没写时区就按本机时区理解（HA 也是这么干的）
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


# ---------------------------------------------------------------------------
# 状态对象：这是整个 mock 的核心数据结构，形状必须和真 HA 一模一样
# ---------------------------------------------------------------------------

def build_attributes(flag, category, scope, sector, car_number, event_id, message,
                     utc_sec, received_at, sequence, history):
    """按上游集成（Nicxe/f1_sensor）的属性集合构造 attributes。

    注意 flag/category/scope/sector/car_number **即使为空也要保留键**：
    App 的分类逻辑读的是 `attributes["flag"] == ""`，键丢了会变成 None，
    和真实数据的行为不一致，测出来的结论就不作数。
    """
    return {
        "friendly_name": FRIENDLY_NAME,
        "icon": ICON,
        "utc": utc_sec,
        "received_at": received_at,
        "category": category,
        "flag": flag,
        "scope": scope,
        "sector": sector,              # 字符串！"12" 不是 12
        "car_number": car_number,
        "message": message,
        "event_id": event_id,
        "sequence": sequence,
        "history": list(history),
        "raw_message": {},             # 真身很大会被 recorder 丢掉，这里给空对象即可
    }


def make_state(entity_id, flag, category, scope, sector, car_number, event_id,
               state, message, iso_time, sequence, history, attributes=True):
    """构造一个完整状态对象。

    iso_time 同时充当 last_changed / last_updated —— 真 HA 里消息是一瞬间到达的，
    实测这两个字段相等，所以不引入额外的时间漂移。
    """
    obj = {
        "entity_id": entity_id,
        "state": state,
    }
    if attributes:
        obj["attributes"] = build_attributes(
            flag, category, scope, sector, car_number, event_id, message,
            drop_microseconds(iso_time), iso_time, sequence, history)
    obj["last_changed"] = iso_time
    obj["last_updated"] = iso_time
    return obj


def unavailable_state(entity_id):
    """比赛还没开始/没数据时的状态：state=unavailable，属性只剩名字和图标。"""
    now = utc_now_iso()
    return {
        "entity_id": entity_id,
        "state": "unavailable",
        "attributes": dict(UNAVAILABLE_ATTRIBUTES),
        "last_changed": now,
        "last_updated": now,
    }


def strip_attributes(row):
    """no_attributes 时，每条都不带 attributes 键（不是给个空对象）。"""
    return {k: v for k, v in row.items() if k != "attributes"}


def minimalize(row):
    """minimal_response 下中间条目的形状：只有 state + last_changed。

    entity_id / last_updated / attributes 全部丢掉，和真 HA 一致 —— 这也是为什么
    App 不能依赖 minimal_response 里的 entity_id 做实体识别。
    """
    return {"state": row.get("state"), "last_changed": row.get("last_changed")}


# ---------------------------------------------------------------------------
# 数据源：697 条真实消息
# ---------------------------------------------------------------------------

def load_rows(path):
    """读 TSV，返回按 last_changed 升序排列的 dict 列表。

    文件是 UTF-8，但有 BOM 的可能（上游导出工具的差异），所以用 utf-8-sig。
    空行跳过；列数不足的行补齐 —— 手工数据文件经常最后少一列。
    """
    rows = []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        header = None
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            if header is None:
                header = parts
                continue
            if len(parts) < len(TSV_COLUMNS):
                parts += [""] * (len(TSV_COLUMNS) - len(parts))
            rec = dict(zip(TSV_COLUMNS, parts))
            if not rec.get("last_changed"):
                continue
            rows.append(rec)
    # 排序用解析后的 datetime 而不是字符串：偏移量理论上可能不同，字符串排序会错
    def key(rec):
        dt = parse_iso_param(rec["last_changed"])
        return dt or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    rows.sort(key=key)
    return rows


# ---------------------------------------------------------------------------
# 剧本
# ---------------------------------------------------------------------------
# 每步：delay 秒（相对上一步）→ 一条消息；interval 字段可整体覆盖间隔。
# ★ flag="" + category="SafetyCar" 是安全车/VSC 的真实形状，不能"顺手补上" SC 旗。

SCENARIOS = {
    # ------------------------------------------------------------------
    # 下面每一条 message 都是**逐字取自** dsh/files/data/racecontrol_history.tsv
    # （2026-09-11~13 一个比赛周末的 697 条真实消息）。
    #
    # 为什么不用短句凑：像 "TRACK LIMITS AT TURN 4" 这种写法在真实数据里根本不存在。
    # 真实的形状是
    #   CAR 44 (HAM) TIME 1:34.625 DELETED - TRACK LIMITS AT TURN 17 LAP 20 14:17:09
    # 用编的短句去测，测的就不是分类 / 默认过滤 / 中文简述的真实输入 ——
    # 那等于没测。
    #
    # ★ 唯一的例外：safety_car 里的两条 SAFETY CAR 消息。那一整个周末只出了 VSC，
    #   没有真正的安全车，所以那两条是按上游集成的形状合成的（desc 里注明了）。
    # ------------------------------------------------------------------

    "race_start": {
        "desc": "起跑 → 5 条真实的超赛道限制删圈速 → 格子旗",
        "interval": 2.0,
        "steps": [
            {"message": "GREEN LIGHT - PIT EXIT OPEN",
             "flag": "GREEN", "category": "Flag", "scope": "Track"},
            {"message": "CAR 12 (ANT) TIME 1:57.307 DELETED - TRACK LIMITS AT TURN 18 LAP 3 13:34:28",
             "flag": "", "category": "Other"},
            {"message": "CAR 16 (LEC) TIME 2:13.896 DELETED - TRACK LIMITS AT TURN 6 LAP 5 13:36:42",
             "flag": "", "category": "Other"},
            {"message": "CAR 63 (RUS) TIME 2:26.963 DELETED - TRACK LIMITS AT TURN 5 LAP 6 13:37:50",
             "flag": "", "category": "Other"},
            {"message": "CAR 1 (NOR) TIME 1:37.251 DELETED - TRACK LIMITS AT TURN 15 LAP 5 13:38:23",
             "flag": "", "category": "Other"},
            {"message": "CAR 5 (BOR) LAP DELETED - TRACK LIMITS AT TURN 5 LAP 7 13:41:22 (PIT)",
             "flag": "", "category": "Other"},
            {"message": "CHEQUERED FLAG",
             "flag": "CHEQUERED", "category": "Flag", "scope": "Track"},
        ],
    },

    "red_flag": {
        "desc": "黄旗 → 双黄扩散到 10/11/12 区段 → 红旗 → 逐区段清除 → 赛道清空",
        "interval": 2.0,
        "steps": [
            {"message": "YELLOW IN TRACK SECTOR 9",
             "flag": "YELLOW", "category": "Flag", "scope": "Sector", "sector": "9"},
            {"message": "DOUBLE YELLOW IN TRACK SECTOR 10",
             "flag": "DOUBLE YELLOW", "category": "Flag", "scope": "Sector", "sector": "10"},
            {"message": "DOUBLE YELLOW IN TRACK SECTOR 11",
             "flag": "DOUBLE YELLOW", "category": "Flag", "scope": "Sector", "sector": "11"},
            {"message": "DOUBLE YELLOW IN TRACK SECTOR 12",
             "flag": "DOUBLE YELLOW", "category": "Flag", "scope": "Sector", "sector": "12"},
            {"message": "RED FLAG",
             "flag": "RED", "category": "Flag", "scope": "Track"},
            {"message": "CLEAR IN TRACK SECTOR 9",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "9"},
            {"message": "CLEAR IN TRACK SECTOR 10",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "10"},
            {"message": "CLEAR IN TRACK SECTOR 11",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "11"},
            {"message": "CLEAR IN TRACK SECTOR 12",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "12"},
            {"message": "MEDICAL CAR DEPLOYED",
             "flag": "", "category": "Other"},
            {"message": "TRACK CLEAR",
             "flag": "CLEAR", "category": "Flag", "scope": "Track"},
        ],
    },

    "safety_car": {
        "desc": "黄旗 → 安全车出动 → 本圈进站 → 赛道清空"
                "（★ SAFETY CAR 两条是合成的：那个周末只出了 VSC，没有真安全车）",
        "interval": 2.0,
        "steps": [
            {"message": "YELLOW IN TRACK SECTOR 9",
             "flag": "YELLOW", "category": "Flag", "scope": "Sector", "sector": "9"},
            {"message": "SAFETY CAR DEPLOYED",
             "flag": "", "category": "SafetyCar"},
            {"message": "SAFETY CAR IN THIS LAP",
             "flag": "", "category": "SafetyCar"},
            {"message": "CLEAR IN TRACK SECTOR 9",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "9"},
            {"message": "TRACK CLEAR",
             "flag": "CLEAR", "category": "Flag", "scope": "Track"},
        ],
    },

    "vsc": {
        "desc": "黄旗 22/23 区段 → VSC 出动 → 马修上赛道 → 清除 → VSC 结束",
        "interval": 2.0,
        "steps": [
            {"message": "YELLOW IN TRACK SECTOR 23",
             "flag": "YELLOW", "category": "Flag", "scope": "Sector", "sector": "23"},
            {"message": "YELLOW IN TRACK SECTOR 22",
             "flag": "YELLOW", "category": "Flag", "scope": "Sector", "sector": "22"},
            {"message": "VSC DEPLOYED",
             "flag": "", "category": "SafetyCar"},
            {"message": "MARSHALS ON TRACK AT TURN 20",
             "flag": "", "category": "Other"},
            {"message": "DOUBLE YELLOW IN TRACK SECTOR 5",
             "flag": "DOUBLE YELLOW", "category": "Flag", "scope": "Sector", "sector": "5"},
            {"message": "CLEAR IN TRACK SECTOR 22",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "22"},
            {"message": "CLEAR IN TRACK SECTOR 23",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "23"},
            {"message": "CLEAR IN TRACK SECTOR 5",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "5"},
            {"message": "VSC ENDING",
             "flag": "", "category": "SafetyCar"},
            {"message": "TRACK CLEAR",
             "flag": "CLEAR", "category": "Flag", "scope": "Track"},
        ],
    },

    "test_double_yellow": {
        "desc": "双黄区段 3 → 3 秒后 CLEAR（复现真实的 20:35:56 / 20:35:59 那一对）",
        "steps": [
            {"message": "DOUBLE YELLOW IN TRACK SECTOR 3",
             "flag": "DOUBLE YELLOW", "category": "Flag", "scope": "Sector", "sector": "3"},
            {"delay": 3.0, "message": "CLEAR IN TRACK SECTOR 3",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "3"},
        ],
    },

    "penalty": {
        "desc": "判罚全链条：多车事故（8/9 辆，译文必须列全）→ 事故已记录 → 仲裁复核 → 5 秒罚时"
                " → 罚时执行 → 黑白旗 → 蓝旗（这几条都有中文简述，专门用来看翻译）",
        "interval": 2.5,
        "steps": [
            # 整个周末最长的两条。用户要求：车号一个都不能省、译文换行也要显示全。
            # 放在最前面 —— 展开剧本第一眼就该看到它们够不够长、会不会被截断。
            {"message": "FIA STEWARDS: Q1 INCIDENT INVOLVING CARS 81 (PIA), 63 (RUS), 3 (VER), 5 (BOR), 27 (HUL), 10 (GAS), 43 (COL), 22 (TSU) AND 77 (BOT) NOTED - FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - MAXIMUM DELTA TIME",
             "flag": "", "category": "Other"},
            {"message": "FIA STEWARDS: Q1 INCIDENT INVOLVING CARS 81 (PIA), 63 (RUS), 3 (VER), 27 (HUL), 10 (GAS), 43 (COL), 22 (TSU) AND 77 (BOT) NO FURTHER ACTION - FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - MAXIMUM DELTA TIME",
             "flag": "", "category": "Other"},
            {"message": "TURN 1 INCIDENT INVOLVING CAR 43 (COL) NOTED - FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – ESCAPE ROAD INSTRUCTIONS (14:27:00)",
             "flag": "", "category": "Other"},
            {"message": "FIA STEWARDS: TURN 3 INCIDENT INVOLVING CARS 43 (COL) AND 87 (BEA) REVIEWED NO FURTHER INVESTIGATION - IMPEDING (14:13:45)",
             "flag": "", "category": "Other"},
            {"message": "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 55 (SAI) (15:23:42)",
             "flag": "", "category": "Other"},
            {"message": "FIA STEWARDS: PENALTY SERVED - 5 SECOND TIME PENALTY FOR CAR 55 (SAI) (15:23:42)",
             "flag": "", "category": "Other"},
            {"message": "BLACK AND WHITE FLAG FOR CAR 1 (NOR) - FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS (16:47:39)",
             "flag": "BLACK AND WHITE", "category": "Flag", "scope": "Driver"},
            {"message": "WAVED BLUE FLAG FOR CAR 14 (ALO) TIMED AT 15:42:56",
             "flag": "BLUE", "category": "Flag", "scope": "Driver"},
            # 内容型消息（徽标说不清楚的那些）也顺手放在这里，一次看完
            {"message": "MARSHALS ON TRACK AT TURN 20",
             "flag": "", "category": "Other"},
            {"message": "YELLOW IN PIT LANE",
             "flag": "", "category": "Other"},
            {"message": "SESSION WILL RESUME AT 17:47",
             "flag": "", "category": "Other"},
        ],
    },

    "burst": {
        "desc": "同一秒内 7 条 —— 直接取真实数据里那一秒（09-11 23:35:33）的全部 7 条",
        "interval": 0.0,
        "steps": [
            {"message": "RED FLAG",
             "flag": "RED", "category": "Flag", "scope": "Track"},
            {"message": "CLEAR IN TRACK SECTOR 12",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "12"},
            {"message": "CLEAR IN TRACK SECTOR 13",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "13"},
            {"message": "CLEAR IN TRACK SECTOR 14",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "14"},
            {"message": "CLEAR IN TRACK SECTOR 15",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "15"},
            {"message": "CLEAR IN TRACK SECTOR 16",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "16"},
            {"message": "CLEAR IN TRACK SECTOR 17",
             "flag": "CLEAR", "category": "Flag", "scope": "Sector", "sector": "17"},
        ],
    },
}


# ---------------------------------------------------------------------------
# WebSocket 连接对象：一个客户端 + 一把发送锁
# ---------------------------------------------------------------------------

class WSClient:
    """一条已升级为 WebSocket 的连接。

    为什么要单独包一层：广播发生在回放线程里，而同一时刻可能有多个回放/剧本线程
    和 N 个客户端，帧必须**整帧原子写**，否则两个线程交叉写会把帧头写坏。
    所以每连接一把写锁。
    """

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.wlock = threading.Lock()
        self.sub_lock = threading.Lock()   # 保护 subscriptions：写订阅的是 WS 读线程，
                                           # 遍历订阅的是回放线程，两边都会碰
        self.alive = True
        self.subscriptions = {}      # msg_id -> entity_id
        self.authenticated = False

    # -- 发送 ---------------------------------------------------------------
    def send_frame(self, opcode, payload):
        """按 RFC6455 发一个**不掩码**帧（服务端→客户端禁止掩码）。

        长度用 7 位 / 16 位 / 64 位三种编码，App 端的解析器（或中间代理）
        如果只处理了 7 位，这里能把它打出来。
        """
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        n = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)          # FIN=1
        if n < 126:
            header.append(n)
        elif n <= 0xFFFF:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        with self.wlock:
            if not self.alive:
                raise ConnectionError("连接已关闭")
            self.sock.sendall(bytes(header) + payload)

    def send_json(self, obj):
        self.send_frame(0x1, json.dumps(obj, ensure_ascii=False))

    def send_pong(self, payload):
        self.send_frame(0xA, payload)

    def send_close(self, code=1000, reason=""):
        self.send_frame(0x8, struct.pack(">H", code) + reason.encode("utf-8"))

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 服务器状态机
# ---------------------------------------------------------------------------

class MockHA:
    """整个 mock 的中枢：维护当前状态、订阅者、回放进度、剧本。

    线程模型：
      - 一个 HTTP 线程池（ThreadingHTTPServer）处理 REST 和 WS 握手
      - 每个 WS 客户端一个读线程
      - 一个回放/剧本线程（同一时刻只有一个，靠 generation 号抢占）
    """

    def __init__(self, args):
        self.args = args
        self.entity_id = args.entity
        self.token = args.token

        self.lock = threading.RLock()          # 保护 current / subscribers / 进度
        self.subscribers = []                  # [WSClient]
        self.current = None                    # 最近一条完整状态对象（None = 还没开始）
        self.current_history = []              # 最近 5 条精简历史
        self.sequence = 0

        self.rows = []
        self.data_path = os.path.abspath(args.data)
        self.mode = "race"                     # race | scenario
        self.scenario_name = args.scenario or ""
        self.replay_index = 0
        self.replay_started_at = None
        self.playback_offset = 0.0             # 已推进的"比赛时间"秒数
        self.total_span = 0.0                  # 整段数据的比赛时间跨度
        self.generation = 0
        self.stop_event = threading.Event()
        self._thread = None
        self._thread_lock = threading.Lock()
        self.stats = {"events_sent": 0, "clients_total": 0, "scenario_switches": 0}

    # -- 数据 ---------------------------------------------------------------
    def load(self):
        if os.path.exists(self.data_path):
            self.rows = load_rows(self.data_path)
            if self.rows:
                first = parse_iso_param(self.rows[0]["last_changed"])
                last = parse_iso_param(self.rows[-1]["last_changed"])
                self.total_span = (last - first).total_seconds()
            _log("已载入 %d 条真实消息：%s" % (len(self.rows), self.data_path))
        else:
            _log("[warn] 数据文件不存在，真实回放不可用：%s" % self.data_path)
            _log("[warn] 可以用 --data 指定路径，或直接用 --scenario 跑剧本。")

    # -- 状态发布 -----------------------------------------------------------
    def publish(self, flag, category, scope, sector, car_number, state, message,
                iso_time=None, event_id=None):
        """发布一条新消息：更新当前状态 → 广播给所有订阅者。

        返回刚构造出来的状态对象。sequence 自增（App 用它做丢号检测，
        所以这里绝不能跳号，哪怕消息被判定为重复）。
        """
        iso_time = iso_time or utc_now_iso()
        with self.lock:
            self.sequence += 1
            seq = self.sequence
            history = list(self.current_history)
            prev = self.current
            if event_id is None:
                # 上游的 event_id 是 "秒级UTC|category|message" 复合键
                event_id = "%s|%s|%s" % (drop_microseconds(iso_time), category or "", message)
            st = make_state(self.entity_id, flag or "", category or "", scope or "",
                            sector or "", car_number or "", event_id, state, message,
                            iso_time, seq, history)
            self.current = st
            # history 是"最近 5 条"，不含当前这条（当前这条在 state/attributes 里）
            self.current_history.append({
                "event_id": event_id,
                "utc": drop_microseconds(iso_time),
                "category": category or "",
                "flag": flag or "",
                "message": message,
            })
            del self.current_history[:-HISTORY_KEEP]
            self.stats["events_sent"] += 1

        self.broadcast_state(st, prev)
        return st

    def trigger_event(self, to_state, from_state=None):
        """subscribe_trigger 平台 state 的事件包形状。App 取的是
        event.variables.trigger.to_state —— 层级不能改。"""
        return {
            "id": None,   # 真 HA 这里回填的是订阅 id，由调用方覆盖
            "type": "event",
            "event": {
                "variables": {
                    "trigger": {
                        "platform": "state",
                        "entity_id": self.entity_id,
                        "from_state": from_state,
                        "to_state": to_state,
                    }
                }
            },
        }

    def broadcast_state(self, to_state, from_state=None):
        """按每个订阅者自己的订阅 id 组装事件。

        为什么不给每个连接发一份"通用"事件：一个 App 可能同时开多个
        subscribe_trigger（主界面 + 前台服务各一个），它们 id 不同；真 HA 是按
        订阅分别推送的，id 对不上 App 会把自己的事件丢掉。没订阅的连接不推。
        """
        with self.lock:
            targets = list(self.subscribers)
        for client in targets:
            with client.sub_lock:
                ids = list(client.subscriptions.keys())
            for msg_id in ids:
                payload = self.trigger_event(to_state, from_state)
                payload["id"] = msg_id
                try:
                    client.send_json(payload)
                except Exception:
                    self.remove_subscriber(client)
                    break

    # -- 订阅者管理 ---------------------------------------------------------
    def add_subscriber(self, client):
        with self.lock:
            self.subscribers.append(client)
            self.stats["clients_total"] += 1
            n = len(self.subscribers)
        _log("WS 客户端接入 %s（当前 %d 个）" % (client.addr, n))

    def remove_subscriber(self, client):
        with self.lock:
            if client in self.subscribers:
                self.subscribers.remove(client)
                n = len(self.subscribers)
            else:
                return
        client.close()
        _log("WS 客户端断开 %s（剩余 %d 个）" % (client.addr, n))

    # -- 回放线程控制 -------------------------------------------------------
    def _spawn(self, fn, name):
        with self._thread_lock:
            old = self._thread
        if old is not None and old.is_alive():
            old.join(timeout=5.0)
        t = threading.Thread(target=fn, name=name, daemon=True)
        with self._thread_lock:
            self._thread = t
        t.start()
        return t

    def start_replay(self):
        with self.lock:
            self.mode = "race"
            self.generation += 1
        self._spawn(self._replay_loop, "mock-ha-replay")

    def start_scenario(self, name, interval=None):
        if name not in SCENARIOS:
            raise KeyError(name)
        with self.lock:
            self.mode = "scenario"
            self.scenario_name = name
            self.generation += 1
            self.replay_index = 0
            self.stats["scenario_switches"] += 1
            gen = self.generation
        self._spawn(lambda: self._scenario_loop(name, gen, interval), "mock-ha-scenario-%s" % name)

    def switch_scenario(self, name, interval=None):
        """运行中切换剧本：先让旧线程看到 generation 变了自行退出，再起新的。

        不能直接 kill 线程（Python 没有安全的线程终止），所以用"代数号"协作式退出。
        """
        if name not in SCENARIOS:
            raise KeyError(name)
        with self.lock:
            self.generation += 1              # 旧的剧本/回放线程下一次检查就会退出
        self.start_scenario(name, interval)

    def _alive(self, gen):
        return gen == self.generation and not self.stop_event.is_set()

    def _wait(self, seconds, gen):
        """可被打断的等待：每秒醒一次看代数号，避免切场景后还傻等 20 小时。"""
        if seconds <= 0:
            self.stop_event.wait(0.005)
            return self._alive(gen)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._alive(gen):
                return False
            self.stop_event.wait(min(0.1, max(0.0, deadline - time.monotonic())))
        return self._alive(gen)

    # -- 真实数据回放 -------------------------------------------------------
    def _replay_loop(self):
        if not self.rows:
            return
        speed = float(self.args.speed)
        _log("开始回放真实数据：%d 条，speed=%s，loop=%s" % (len(self.rows), speed, self.args.loop))
        with self.lock:
            gen = self.generation
            self.replay_started_at = time.time()
            self.playback_offset = 0.0
        try:
            while self._alive(gen):
                with self.lock:
                    self.replay_index = 0
                    self.replay_started_at = time.time()
                    self.playback_offset = 0.0
                prev_dt = None
                # --start-offset：先空转 n 秒（比赛还没开始）再推第一条。
                # 两个用处：(1) 跳过大段赛前/夜间隔，直接从比赛中间开始；
                # (2) 让人能在真机上确认"无比赛时 App 显示 unavailable 而不是崩"。
                offset = float(getattr(self.args, "start_offset", 0.0) or 0.0)
                if offset > 0:
                    _log("按 --start-offset=%.1fs 空转，期间 /api/states 仍是 unavailable" % offset)
                    if not self._wait(offset if speed <= 0 else offset / speed, gen):
                        return
                for idx, rec in enumerate(self.rows):
                    if not self._alive(gen):
                        return
                    dt = parse_iso_param(rec["last_changed"])
                    # 第一条不等待：启动（+offset）即为"第一条到点"。否则用户要盯着
                    # unavailable 干等第一段间隔（数据里第一条前面可能有大空档）。
                    wait = 0.0 if prev_dt is None else (dt - prev_dt).total_seconds()
                    if wait > 0:
                        if speed > 0:
                            if not self._wait(wait / speed, gen):
                                return
                        else:
                            # speed<=0 = 尽可能快。但仍要留一点点间隔，
                            # 否则 697 条会在几毫秒内灌完，TCP 缓冲和 App 的 UI 都会崩。
                            if not self._wait(0.02, gen):
                                return
                    prev_dt = dt
                    with self.lock:
                        self.replay_index = idx + 1
                        self.playback_offset = ((dt - parse_iso_param(self.rows[0]["last_changed"]))
                                                .total_seconds())
                    self.publish(rec.get("flag", ""), rec.get("category", ""),
                                 rec.get("scope", ""), rec.get("sector", ""),
                                 rec.get("car_number", ""), rec.get("state", ""),
                                 rec.get("message") or rec.get("state", ""),
                                 iso_time=rec["last_changed"],
                                 event_id=rec.get("event_id") or None)
                if not self.args.loop:
                    _log("真实数据回放完毕（未开 --loop）")
                    return
                _log("回放完毕，--loop 生效，从头再来")
        except Exception:
            _log("[error] 回放线程异常：\n" + traceback.format_exc())

    # -- 剧本回放 -----------------------------------------------------------
    def _scenario_loop(self, name, gen, interval_override=None):
        spec = SCENARIOS[name]
        base_interval = spec.get("interval", 2.0)
        if interval_override is not None:
            base_interval = float(interval_override)
        _log("开始剧本 %s（%s），间隔 %.2fs" % (name, spec["desc"], base_interval))
        try:
            while self._alive(gen):
                for step in spec["steps"]:
                    if not self._alive(gen):
                        return
                    # delay 支持单步覆盖（test_double_yellow 的 3 秒就是这个），
                    # 没写就用手册整体间隔
                    delay = step.get("delay", base_interval)
                    if delay > 0 and not self._wait(max(delay, 0.0), gen):
                        return
                    self.publish(step.get("flag", ""), step.get("category", ""),
                                 step.get("scope", ""), step.get("sector", ""),
                                 step.get("car_number", ""), step["message"], step["message"])
                if not self.args.loop:
                    _log("剧本 %s 播放完毕（未开 --loop）" % name)
                    return
                # loop 模式下剧本之间留口气，方便肉眼分辨轮次
                if not self._wait(1.0, gen):
                    return
        except Exception:
            _log("[error] 剧本线程异常：\n" + traceback.format_exc())

    # -- 查询 ---------------------------------------------------------------
    def get_state(self):
        with self.lock:
            if self.current is None:
                return unavailable_state(self.entity_id)
            return self.current

    def get_newest(self):
        with self.lock:
            return self.current

    def snapshot_for_history(self):
        """历史接口需要"每条记录当时的完整状态"。这里用回放记录重建，
        而不是缓存 697 个对象 —— 内存和一致性都更好。"""
        return self.rows

    def status(self):
        with self.lock:
            progress = {
                "mode": self.mode,
                "scenario": self.scenario_name if self.mode == "scenario" else None,
                "index": self.replay_index,
                "total": len(self.rows),
                "percent": round(100.0 * self.replay_index / len(self.rows), 2) if self.rows else 0.0,
                "playback_seconds": round(self.playback_offset, 1),
                "total_span_seconds": round(self.total_span, 1),
                "speed": self.args.speed,
                "loop": bool(self.args.loop),
            }
            return {
                "entity_id": self.entity_id,
                "sequence": self.sequence,
                "subscribers": len(self.subscribers),
                "current_state": (self.current or {}).get("state", "unavailable"),
                "progress": progress,
                "stats": dict(self.stats),
                "ha_version": HA_VERSION,
            }

    def shutdown(self):
        """停掉所有回放/剧本/WS 线程。幂等且可重入（cleanup 可能被调两次）。"""
        with self.lock:
            self.stop_event.set()
            self.generation += 1
            clients = list(self.subscribers)
            self.subscribers = []
        for c in clients:
            c.close()
        # join 当前回放/剧本线程，确保它真的退出了再返回 ——
        # 否则测试脚本检查"有没有残留线程"会偶发失败
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockHA/1.0"
    # WS 连接会长期挂在 handler 里，不能被 HTTP 超时踢掉
    timeout = None

    # 默认的访问日志会往 stderr 打字，沙箱/GBK 控制台下很吵；--verbose 才开
    def log_message(self, fmt, *a):
        if getattr(self.server, "mock_verbose", False):
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % a))

    # -- 工具 ---------------------------------------------------------------
    @property
    def mock(self) -> MockHA:
        return self.server.mock

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, code, obj, extra=None):
        self._send(code, obj, "application/json; charset=utf-8", extra)

    def _authorized(self, query=None):
        """HA 只认 Authorization: Bearer <token>。这里额外容忍 ?access_token=
        （HA 的图片/相机接口确实支持这种写法），方便浏览器里直接点。"""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            if auth[7:].strip() == self.mock.token:
                return True
        if query:
            tok = (query.get("access_token") or [""])[0]
            if tok and tok == self.mock.token:
                return True
        return False

    def _split(self):
        """返回 (path, query_dict)。用原始 requestline 自己切，不依赖 urlparse 的
        path 处理 —— history 接口的路径里带 ISO 时间戳，冒号容易被误伤。"""
        raw = self.path
        if "?" in raw:
            path, qs = raw.split("?", 1)
        else:
            path, qs = raw, ""
        return unquote(path), parse_qs(qs, keep_blank_values=True)

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_websocket()
            return
        path, query = self._split()
        try:
            if path.rstrip("/") == "/api":
                self._api_root()
                return
            if path.startswith("/api/history/period"):
                self._api_history(path, query)
                return
            if path.startswith("/api/states"):
                self._api_states(path, query)
                return
            if path == "/_mock/status":
                self._json(200, self.mock.status())
                return
            if path == "/_mock/newest":
                st = self.mock.get_newest()
                if st is None:
                    self._json(200, {"message": "no data yet",
                                     "state": unavailable_state(self.mock.entity_id)})
                else:
                    self._json(200, st)
                return
            if path in ("/", "/index.html"):
                self._json(200, {"message": "mock Home Assistant for F1 race control",
                                 "endpoints": ["/api/", "/api/states/<entity_id>",
                                               "/api/history/period/<start>",
                                               "/api/websocket", "/_mock/status",
                                               "/_mock/newest",
                                               "POST /_mock/scenario?name=<scenario>"]})
                return
            self._json(404, {"message": "Not found."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            _log("[error] 处理 %s 失败：\n%s" % (path, traceback.format_exc()))
            self._json(500, {"message": "Internal error."})

    def do_POST(self):
        path, query = self._split()
        try:
            if path == "/_mock/scenario":
                self._mock_scenario(query)
                return
            self._json(404, {"message": "Not found."})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- 各接口实现 ---------------------------------------------------------
    def _api_root(self):
        if not self._authorized():
            self._json(401, {"message": "Unauthorized."})
            return
        self._json(200, {"message": "API running."})

    def _api_states(self, path, query):
        if not self._authorized(query):
            self._json(401, {"message": "Unauthorized."})
            return
        rest = path[len("/api/states"):].strip("/")
        entity_id = rest or self.mock.entity_id
        if entity_id != self.mock.entity_id:
            self._json(404, {"message": "Entity not found."})
            return
        self._json(200, self.mock.get_state())

    def _api_history(self, path, query):
        if not self._authorized(query):
            self._json(401, {"message": "Unauthorized."})
            return
        rest = path[len("/api/history/period"):].strip("/")
        start = parse_iso_param(rest)
        if start is None:
            self._json(400, {"message": "Invalid start time."})
            return
        end = parse_iso_param((query.get("end_time") or [""])[0]) or datetime.datetime.now(
            datetime.timezone.utc)

        entity_ids = []
        for v in query.get("filter_entity_id", []):
            entity_ids += [x for x in v.split(",") if x]
        if not entity_ids:
            entity_ids = [self.mock.entity_id]

        # 不认识的实体：HA 直接给空数组（不是 404）
        rows_out = []
        for eid in entity_ids:
            if eid != self.mock.entity_id:
                rows_out.append([])
                continue
            rows_out.append(self._history_for(start, end, query))
        self._json(200, rows_out)

    def _history_for(self, start, end, query):
        """忠实复刻 HA 的 history 语义。

        HA 返回的第一条**不是**数据库里的原始行，而是它合成的"期初状态"：
        区间开始那一刻仍然生效的值，且 last_changed 被改写成请求的 start。
        这是历史回填最容易踩的坑 —— App 拿它当锚点算持续时间，
        少这一条或者时间戳没改写，时间轴就会整段错位。

        一处有意的强化：当请求的 start **早于所有数据**（典型的"从比赛开始回填"
        场景）时，真 HA 因为查不到期初状态会直接从第一条真实变更开始返回；
        这里的做法是拿区间内第一条的值合成一条期初状态。这样做对 App 更安全的
        原因是：App 的回填逻辑需要"区间起点有一个锚点"才能画出持续时长的起点，
        没有锚点最前面那条旗语会变成零时长。行为差异只在 start 早于全部数据时出现，
        而那种情况下 HA 本来就给不出更准的值。
        """
        minimal = "minimal_response" in query
        no_attrs = "no_attributes" in query
        rows = self.mock.snapshot_for_history()

        in_range = []
        opening = None
        for rec in rows:
            dt = parse_iso_param(rec["last_changed"])
            if dt < start:
                opening = rec           # start 之前最后一条 = 期初值来源
            elif dt <= end:
                in_range.append(rec)
            else:
                break                   # rows 已按时间升序，后面的不用看了

        if opening is None and not in_range:
            # 区间内没有任何变更，start 之前也从来没有过值 —— 真实 HA 给空列表
            return []

        # 期初状态的取值：start 之前最后一条；若 start 早于所有数据，
        # 就用区间内第一条的值 —— 因为"区间开始那一刻生效的值"就是它。
        # 注意无论哪种情况都要**单独合成一条**并改写时间戳，不能直接复用原记录：
        # 复用就等于把真实变更时间当成了 start，App 算持续时长会直接错。
        source = opening if opening is not None else (in_range[0] if in_range else None)
        out = []
        if source is not None:
            st = self._record_to_state(source, history_slice=[])
            st["last_changed"] = start.isoformat()
            st["last_updated"] = start.isoformat()
            out.append(st)

        # 区内每一条真实变更，按原时间戳跟在期初状态后面
        for i, rec in enumerate(in_range):
            prev_rows = ([opening] if opening is not None else []) + in_range[:i]
            st = self._record_to_state(rec, history_slice=prev_rows[-HISTORY_KEEP:])
            out.append(st)

        # minimal_response：中间条目砍到只剩 state + last_changed，
        # 但首尾两条永远给完整的 —— HA 就是这么"偏心"，App 依赖末条拿属性。
        if minimal and len(out) > 1:
            result = [out[0]]
            for st in out[1:-1]:
                result.append(minimalize(st))
            result.append(out[-1])
            out = result

        if no_attrs:
            out = [strip_attributes(st) for st in out]
        return out

    def _record_to_state(self, rec, history_slice):
        """把 TSV 的一行还原成当时的完整状态对象（含 attributes）。"""
        hist = [{
            "event_id": r.get("event_id", ""),
            "utc": drop_microseconds(r.get("last_changed", "")),
            "category": r.get("category", ""),
            "flag": r.get("flag", ""),
            "message": r.get("message") or r.get("state", ""),
        } for r in history_slice]
        return make_state(
            self.mock.entity_id,
            rec.get("flag", ""), rec.get("category", ""), rec.get("scope", ""),
            rec.get("sector", ""), rec.get("car_number", ""),
            rec.get("event_id", ""), rec.get("state", ""),
            rec.get("message") or rec.get("state", ""),
            rec.get("last_changed", ""), self.mock.sequence, hist)

    def _mock_scenario(self, query):
        name = (query.get("name") or [""])[0]
        if name not in SCENARIOS:
            self._json(404, {"message": "Unknown scenario: %r" % name,
                             "available": sorted(SCENARIOS)})
            return
        interval = (query.get("interval") or [None])[0]
        interval = float(interval) if interval not in (None, "") else None
        self.mock.switch_scenario(name, interval)
        self._json(200, {"ok": True, "scenario": name,
                         "steps": len(SCENARIOS[name]["steps"]),
                         "interval": interval if interval is not None
                         else SCENARIOS[name].get("interval", 2.0),
                         "description": SCENARIOS[name]["desc"]})

    # -- WebSocket ----------------------------------------------------------
    def _handle_websocket(self):
        """手写升级握手。标准库没有 WebSocket 服务端，只能自己按 RFC6455 来。

        Sec-WebSocket-Accept = base64(sha1(key + 258EAFA5-E914-47DA-95CA-C5AB0DC85B11))
        """
        key = self.headers.get("Sec-WebSocket-Key", "")
        version = self.headers.get("Sec-WebSocket-Version", "")
        if not key or version != "13":
            self._json(400, {"message": "Invalid WebSocket handshake."})
            return
        # ★ 真 HA 的 WS 由 aiohttp 接管，它会把 key 按 base64 解出来，并要求
        #   **正好 16 字节**，否则回 400 `Handshake error: '<key>'`。
        #   这里必须一样严 —— 曾经只判「非空」，结果中继把 key 写成
        #   os.urandom(16).hex()（32 个十六进制字符：本身是合法 base64，解出
        #   24 字节）时，本地全套测试全绿，一装到 VPS 打真 HA 就 400 拒绝升级。
        try:
            _key_raw = base64.b64decode(key.encode("ascii"), validate=True)
        except Exception:
            _key_raw = b""
        if len(_key_raw) != 16:
            self._json(400, {"message": "Handshake error: '%s'" % key})
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept)
        try:
            self.wfile.write(resp.encode("ascii"))
            self.wfile.flush()
        except OSError:
            return

        # 关闭 Nagle：小帧（一条旗语几百字节）不需要攒，攒了反而增加延迟
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        client = WSClient(self.connection, self.client_address)
        self.mock.add_subscriber(client)
        self.close_connection = True       # 升级之后不再是 HTTP 连接，别让基类再解析

        try:
            # 1) 上来先要认证，和真 HA 一致
            client.send_json({"type": "auth_required", "ha_version": HA_VERSION})
            self._ws_session(client)
        except (OSError, ConnectionError):
            pass
        except Exception:
            _log("[error] WS 会话异常：\n" + traceback.format_exc())
        finally:
            self.mock.remove_subscriber(client)

    def _ws_session(self, client):
        buf = bytearray()
        fragmented_op = None           # 分片消息：记住首帧的 opcode
        fragments = []
        sock = self.connection
        sock.settimeout(1.0)           # 让循环有机会检查 stop_event，同时不影响阻塞读

        while client.alive and not self.mock.stop_event.is_set():
            try:
                frame = self._read_frame(sock, buf)
            except socket.timeout:
                continue
            except (OSError, ConnectionError):
                return
            if frame is None:
                return                # 对端正常关闭
            fin, opcode, payload = frame

            if opcode == 0x8:          # close：回一个 close 再走
                try:
                    client.send_close()
                except Exception:
                    pass
                return
            if opcode == 0x9:          # ping -> pong（RFC 要求带回同样的 payload）
                try:
                    client.send_pong(payload)
                except Exception:
                    return
                continue
            if opcode == 0xA:          # pong：不需要处理
                continue

            if opcode == 0x0:          # 续帧
                if fragmented_op is None:
                    continue
                fragments.append(payload)
                if not fin:
                    continue
                opcode, payload = fragmented_op, b"".join(fragments)
                fragments, fragmented_op = [], None
            elif opcode == 0x1 or opcode == 0x2:
                if not fin:
                    fragmented_op = opcode
                    fragments = [payload]
                    continue
            else:
                continue               # 其它保留 opcode 一律忽略

            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if not self._ws_message(client, msg):
                return

    def _ws_message(self, client, msg):
        """处理一条 HA 协议消息。返回 False 表示要关连接。"""
        mtype = msg.get("type")
        msg_id = msg.get("id")

        if mtype == "auth":
            if msg.get("access_token") == self.mock.token:
                client.authenticated = True
                client.send_json({"type": "auth_ok", "ha_version": HA_VERSION})
                return True
            client.send_json({"type": "auth_invalid",
                              "message": "Invalid access token or password"})
            return False               # 认证失败按 Protocol 要求立刻断开

        if not client.authenticated:
            client.send_json({"type": "auth_invalid",
                              "message": "Invalid access token or password"})
            return False

        if mtype == "subscribe_trigger":
            trigger = msg.get("trigger") or {}
            entity_id = trigger.get("entity_id") or self.mock.entity_id
            if isinstance(entity_id, list):
                entity_id = entity_id[0] if entity_id else self.mock.entity_id
            if entity_id != self.mock.entity_id:
                client.send_json({"id": msg_id, "type": "result", "success": False,
                                  "error": {"code": "not_found",
                                            "message": "Entity not found."}})
                return True
            with client.sub_lock:
                client.subscriptions[msg_id] = entity_id
            client.send_json({"id": msg_id, "type": "result", "success": True, "result": None})
            # 这里**故意不补发**当前状态。真 HA 的 state trigger 只在状态变化时推，
            # 刚订阅时不会立刻给一条。App 依赖"订阅后第一条 event 一定是新消息"
            # 这个假设，mock 要是好心补发，就会掩盖 App 里真实的边界 bug。
            # 想拿当前值，App 应该走 REST /api/states/<id> —— 这也是真实行为。
            return True

        if mtype == "unsubscribe_events":
            with client.sub_lock:
                client.subscriptions.pop(msg_id, None)
            client.send_json({"id": msg_id, "type": "result", "success": True, "result": None})
            return True

        if mtype == "ping":
            client.send_json({"id": msg_id, "type": "pong"})
            return True

        # 其它命令（call_service / get_states 等）mock 不实现，但要按协议回 result，
        # 否则 App 会一直等超时。
        client.send_json({"id": msg_id, "type": "result", "success": False,
                          "error": {"code": "unknown_command",
                                    "message": "Unknown command: %s" % mtype}})
        return True

    def _read_frame(self, sock, buf):
        """从 socket 读一个完整的 WebSocket 帧。

        客户端→服务端的帧**必须**带掩码（RFC6455 §5.1），所以这里强制要求 mask；
        没掩码的直接当协议错误断掉 —— 这样 App 侧如果忘了掩码能立刻暴露。
        返回 (fin, opcode, payload) 或 None（对端关闭）。
        """
        def need(n):
            # nonlocal 必须有：buf 是调用方传进来的 bytearray，这里要往里追加
            # （Python 里对名字重新赋值 = 新建局部变量，会直接把缓冲区搞丢）
            nonlocal buf
            while len(buf) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    return False
                buf += chunk
            return True

        if not need(2):
            return None
        b0 = buf[0]
        b1 = buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        del buf[:2]

        if length == 126:
            if not need(2):
                return None
            length = struct.unpack(">H", bytes(buf[:2]))[0]
            del buf[:2]
        elif length == 127:
            if not need(8):
                return None
            length = struct.unpack(">Q", bytes(buf[:8]))[0]
            del buf[:8]

        if not masked:
            raise ConnectionError("客户端帧缺少掩码，违反 RFC6455")
        if not need(4):
            return None
        mask = bytes(buf[:4])
        del buf[:4]

        if length:
            if not need(length):
                return None
            payload = bytes(buf[:length])
            del buf[:length]
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        else:
            payload = b""
        return fin, opcode, payload


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="mock Home Assistant（F1 race control 专用，纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址。真机测试要填 0.0.0.0 手机才连得上")
    p.add_argument("--port", type=int, default=8123, help="监听端口（默认 8123）")
    p.add_argument("--token", default="mock-token", help="长期访问令牌（默认 mock-token）")
    p.add_argument("--speed", type=float, default=1.0,
                   help="回放倍速；<=0 表示尽可能快（默认 1.0）")
    p.add_argument("--loop", action="store_true", help="回放完/剧本完从头再来")
    p.add_argument("--start-offset", type=float, default=0.0,
                   help="回放开始前先空转 N 秒（赛前状态，/api/states 为 unavailable）。"
                        "想从比赛中间开始、或想验证 App 的\"无比赛\"显示时用得上")
    p.add_argument("--scenario", default="",
                   help="直接跑剧本，忽略真实数据：%s" % ", ".join(sorted(SCENARIOS)))
    p.add_argument("--entity", default="sensor.f1_race_control_2", help="实体 ID")
    p.add_argument("--data", default=DEFAULT_DATA, help="真实数据 TSV 路径")
    p.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    p.add_argument("--list-scenarios", action="store_true", help="列出剧本后退出")
    return p


def make_server(mock, host, port):
    """建好但还没 serve 的服务器。测试脚本要拿随机端口，所以拆成两部分。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.mock = mock
    httpd.mock_verbose = mock.args.verbose
    # 端口 0 -> 让内核挑一个空闲端口，测试脚本靠这个避免冲突
    httpd.real_port = httpd.server_address[1]
    return httpd


def run(mock, httpd, blocking=True, autostart=True):
    """启动服务。blocking=False 时返回服务线程（自测脚本用）。

    autostart=False：只起 HTTP，不启动回放/剧本线程。
    自测脚本要自己控制什么时候推数据，否则真实回放的 2 天时间轴会和剧本抢推送。
    （这里不调 mock.load()，让调用方决定数据加载时机。）
    """
    if autostart:
        mock.load()
        if mock.args.scenario:
            mock.start_scenario(mock.args.scenario)
        else:
            mock.start_replay()

    if blocking:
        _log("mock HA 就绪：http://%s:%d  token=%s  entity=%s"
             % (mock.args.host, httpd.real_port, mock.token, mock.entity_id))
        _log("REST: GET /api/states/%s   WS: ws://%s:%d/api/websocket"
             % (mock.entity_id, mock.args.host, httpd.real_port))
        _log("Ctrl+C 退出")
        try:
            httpd.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            _log("收到 Ctrl+C，正在退出…")
        finally:
            cleanup(httpd, mock)
        return 0

    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                         name="mock-ha-http", daemon=True)
    httpd.serve_thread = t       # cleanup() 要 join 它，理由见 cleanup 的注释
    t.start()
    return t


def cleanup(httpd, mock):
    """干净退出：先停回放/剧本线程，再关客户端，最后关监听套接字。

    为什么要显式 join serve_forever 线程：socketserver 的 shutdown() 是"设标志 +
    等事件"，而 serve_forever 进循环时会**无条件清掉这个标志**。如果 shutdown()
    赶在 serve_forever 真正开跑之前调用（本机多实例、快速起停时真的会撞上），
    标志被清掉，循环永远不退出 —— 线程就这么漏了。
    所以这里 shutdown() 之后再 join 一次，等不回来就直接关监听套接字兜底。
    """
    mock.shutdown()
    t = getattr(httpd, "serve_thread", None)
    if t is None:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        return
    try:
        httpd.shutdown()
    except Exception:
        pass
    t.join(timeout=5.0)
    if t.is_alive():
        # 上面那个竞态真的发生了：shutdown() 没被认领。直接关套接字让
        # serve_forever 的 select 立刻出错退出，别把线程留在那儿。
        try:
            httpd.socket.close()
        except Exception:
            pass
        t.join(timeout=3.0)
    try:
        httpd.server_close()
    except Exception:
        pass


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.list_scenarios:
        for name in sorted(SCENARIOS):
            spec = SCENARIOS[name]
            _log("%-20s %s（%d 步，间隔 %ss）"
                 % (name, spec["desc"], len(spec["steps"]), spec.get("interval", 2.0)))
        return 0

    if args.scenario and args.scenario not in SCENARIOS:
        _log("未知剧本：%s（可选：%s）" % (args.scenario, ", ".join(sorted(SCENARIOS))))
        return 2

    # 端口写进环境变量，方便手机测试时别人问你连哪个端口
    os.environ.setdefault("MOCK_HA_TOKEN", args.token)

    mock = MockHA(args)
    try:
        httpd = make_server(mock, args.host, args.port)
    except OSError as exc:
        _log("监听 %s:%d 失败：%s" % (args.host, args.port, exc))
        return 1
    try:
        return run(mock, httpd, blocking=True)
    except KeyboardInterrupt:
        cleanup(httpd, mock)
        return 0


if __name__ == "__main__":
    sys.exit(main())
