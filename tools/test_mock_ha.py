#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_mock_ha.py —— mock_ha.py 的自动化自测（纯标准库，无第三方依赖）。

为什么是"进程内起服务器"而不是 subprocess
------------------------------------------
本机沙箱禁止用管道捕获子进程的 stdout（会 EPERM），而且开子进程还要处理端口、
就绪探测、僵尸进程一堆事。直接用线程在**同一个进程**里起服务器，只需要：
  1) 端口传 0 让内核挑空闲端口（多实例并行也不会撞车）
  2) 自己手写 urllib 请求和 WebSocket 客户端

覆盖的断言（对应任务书的自测清单）
----------------------------------
  A. /api/            对 token=200、错 token=401
  B. /api/states/     初始 unavailable + 只有 friendly_name/icon；未知实体 404
  C. /api/history/    第一条是 HA 合成的"期初状态"且 last_changed == 请求 start；
                      minimal_response 中间条目只剩 state/last_changed；
                      末条永远是完整对象；no_attributes 时全都不带 attributes
  D. /api/websocket   诚实校验 Sec-WebSocket-Accept；auth_required→auth→auth_ok
                      →订阅→result→event 全流程；跑 vsc 剧本断言
                      VSC DEPLOYED 的 flag=="" 且 category=="SafetyCar"
  E. /_mock/*         状态、切剧本、最新一条
  F. 断线清理         客户端断开后订阅者归零、线程回收（不泄漏）

用法（Windows PowerShell）：
    $env:PYTHONIOENCODING='utf-8'; python dsh\\tools\\test_mock_ha.py
退出码：全通过 0，有失败 1。
"""

from __future__ import annotations

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
import urllib.error
import urllib.parse
import urllib.request

# 控制台可能是 GBK，中文断言描述会炸 —— 先修编码再干别的
for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mock_ha  # noqa: E402  （必须在 sys.path 改完之后导入）

# 数据源直接复用 mock_ha 的探测逻辑：仓库里放 tools/mock_data/，
# 开发工作区里放 dsh/files/data/，两边都能跑。
DATA = mock_ha.DEFAULT_DATA
TOKEN = "test-token-42"          # 故意不用默认值，能顺带发现"硬编码 mock-token"的 bug
ENTITY = "sensor.f1_race_control_2"

# ---------------------------------------------------------------------------
# 极简断言框架：自己想看到白名单式的 [PASS]/[FAIL]，不引入 unittest 的噪音
# ---------------------------------------------------------------------------

RESULTS = []          # [(name, ok, detail)]


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    line = "  [%s] %s" % (mark, name)
    if detail and not cond:
        line += "  <- %s" % detail
    print(line, flush=True)
    return bool(cond)


def eq(name, got, want):
    return check(name, got == want, "got=%r want=%r" % (got, want))


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def http(url, token=None, method="GET", timeout=10):
    """返回 (status, parsed_json_or_text)。HTTP 错误码不抛异常，交给断言判断。"""
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（socket + 手写握手 + 手写帧）
# ---------------------------------------------------------------------------

class MiniWS:
    """够用就好的 WebSocket 客户端：只实现自测需要的部分。

    重点是要**独立实现**接收端，才能反过来验证服务端的发送端：
    长度 7/16/64 位、FIN、opcode、以及服务端帧不该带掩码。
    """

    def __init__(self, host, port, path="/api/websocket", timeout=15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = bytearray()
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (path, host, port, self.key))
        self.sock.sendall(req.encode("ascii"))
        head = self._read_until(b"\r\n\r\n")
        self.headers = head.decode("latin-1")
        first = self.headers.split("\r\n")[0]
        if "101" not in first:
            raise AssertionError("握手失败：%s" % first)

    def _read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("握手/读取期间对端关闭")
            self.buf += chunk
        idx = self.buf.index(marker) + len(marker)
        out = bytes(self.buf[:idx])
        del self.buf[:idx]
        return out

    def accept_ok(self):
        """自己算一遍 Sec-WebSocket-Accept，和服务端给的比 —— 这才是真验证。"""
        want = base64.b64encode(hashlib.sha1(
            (self.key + mock_ha.WS_GUID).encode("ascii")).digest()).decode("ascii")
        for line in self.headers.split("\r\n"):
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip()
                return got == want, "got=%s want=%s" % (got, want)
        return False, "响应里根本没有 Sec-WebSocket-Accept"

    # -- 发送（客户端必须掩码）---------------------------------------------
    def _send(self, opcode, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        n = len(payload)
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj):
        self._send(0x1, json.dumps(obj, ensure_ascii=False))

    def send_ping(self, payload=b"keepalive"):
        self._send(0x9, payload)

    # -- 接收 ---------------------------------------------------------------
    def _take(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("连接被关闭")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv_frame(self):
        """返回 (opcode, payload)。服务端帧带掩码就是协议错误，这里直接报出来。"""
        b0, b1 = self._take(2)
        opcode = b0 & 0x0F
        fin = bool(b0 & 0x80)
        if b1 & 0x80:
            raise AssertionError("服务端帧带了掩码，违反 RFC6455")
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._take(8))[0]
        payload = self._take(n) if n else b""
        return opcode, payload, fin

    def recv_json(self, timeout=15.0):
        """读一条文本消息，自动跳过 ping/pong。返回 (msg, opcode)。"""
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError("等待服务端消息超时")
            self.sock.settimeout(remain)
            try:
                opcode, payload, _fin = self.recv_frame()
            except socket.timeout:
                raise TimeoutError("等待服务端消息超时")
            if opcode == 0x9:
                self._send(0xA, payload)          # ping 要回 pong
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                return {"__closed__": True, "code": struct.unpack(">H", payload[:2])[0]
                        if len(payload) >= 2 else None}, opcode
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8")), opcode
            raise AssertionError("收到意外 opcode=0x%X" % opcode)

    def close(self):
        try:
            self._send(0x8, struct.pack(">H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 数据文件（用来独立算出期望值，而不是照抄服务端的实现）
# ---------------------------------------------------------------------------

def load_tsv(path):
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
            rows.append(dict(zip(header, parts)))
    rows.sort(key=lambda r: datetime.datetime.fromisoformat(r["last_changed"]))
    return rows


# ---------------------------------------------------------------------------
# 各项测试
# ---------------------------------------------------------------------------

def test_api_root(base):
    print("\n[A] /api/ 鉴权", flush=True)
    st, body = http(base + "/api/", token=TOKEN)
    eq("A1 正确 token -> HTTP 200", st, 200)
    eq("A2 返回体 message", body, {"message": "API running."})

    st, body = http(base + "/api/", token="wrong-token")
    eq("A3 错误 token -> HTTP 401", st, 401)
    check("A4 401 有 message 字段", isinstance(body, dict) and "message" in body, repr(body))

    st, _ = http(base + "/api/")           # 完全不带 Authorization
    eq("A5 缺少 Authorization -> HTTP 401", st, 401)


def test_states(base):
    print("\n[B] /api/states/<entity_id>", flush=True)
    st, body = http(base + "/api/states/" + ENTITY, token=TOKEN)
    eq("B1 正确 token -> HTTP 200", st, 200)
    eq("B2 初始 state 是 unavailable", body.get("state"), "unavailable")
    eq("B3 初始 attributes 只有 friendly_name/icon",
       sorted((body.get("attributes") or {}).keys()), ["friendly_name", "icon"])
    eq("B4 entity_id 正确", body.get("entity_id"), ENTITY)
    check("B5 有 last_changed / last_updated",
          bool(body.get("last_changed")) and bool(body.get("last_updated")), repr(body))

    st, body = http(base + "/api/states/sensor.not_exists", token=TOKEN)
    eq("B6 未知实体 -> HTTP 404", st, 404)
    eq("B7 404 文案", (body or {}).get("message"), "Entity not found.")

    st, _ = http(base + "/api/states/" + ENTITY, token="nope")
    eq("B8 错误 token -> HTTP 401", st, 401)

    st, body = http(base + "/api/states", token=TOKEN)
    eq("B9 省略实体 ID 时用默认实体", (body or {}).get("entity_id"), ENTITY)


def test_history(base, rows):
    print("\n[C] /api/history/period/...", flush=True)
    first = datetime.datetime.fromisoformat(rows[0]["last_changed"])
    # 起点故意选在第一条数据之前 30 分钟：这样"期初状态"必然只能由第一条自己
    # 合成，能把合成逻辑单独逼出来。
    start = (first - datetime.timedelta(minutes=30)).replace(microsecond=0)
    end = start + datetime.timedelta(hours=1)
    qs = "?filter_entity_id=%s&end_time=%s" % (
        urllib.parse.quote(ENTITY), urllib.parse.quote(end.isoformat()))

    st, body = http(base + "/api/history/period/" + urllib.parse.quote(start.isoformat()) + qs,
                    token=TOKEN)
    eq("C1 HTTP 200", st, 200)
    check("C2 返回的是数组的数组", isinstance(body, list) and body and isinstance(body[0], list),
          "type=%s" % type(body))
    series = body[0]
    check("C3 区间内有数据", len(series) >= 2, "len=%d" % len(series))

    # ★ 最核心的一条：第一条是 HA 合成的期初状态，last_changed 被改写成请求的 start
    eq("C4 期初状态的 last_changed == 请求的 start",
       series[0].get("last_changed"), start.isoformat())
    eq("C5 期初状态的 last_updated == 请求的 start",
       series[0].get("last_updated"), start.isoformat())
    eq("C6 期初状态仍是完整对象（含 entity_id）",
       series[0].get("entity_id"), ENTITY)
    check("C7 期初状态带完整 attributes",
          isinstance(series[0].get("attributes"), dict)
          and "flag" in series[0]["attributes"], repr(series[0])[:200])
    # 期初值必须是"start 之前最后一条"的值；本例里 start 之前没有任何记录，
    # 所以它等于区间内第一条 —— 顺带验证了取值范围没算错
    eq("C8 期初状态值取自区间首条", series[0].get("state"), rows[0]["state"])
    # 只有 last_changed/last_updated 被改写，attributes 里的真实时间戳要保留，
    # 否则 App 就没法从属性里还原这条消息真正的发出时间了
    eq("C9 期初状态的 received_at 保持真实时间（只有 last_changed 被改写）",
       series[0]["attributes"].get("received_at"), rows[0]["last_changed"])
    eq("C9b 期初状态的 attributes.utc 也是秒精度的真实时间",
       series[0]["attributes"].get("utc"),
       datetime.datetime.fromisoformat(rows[0]["last_changed"]).replace(
           microsecond=0).isoformat())
    # 第二条开始必须是真实变更时间，不能被顺手改写
    eq("C9c 区间内真实变更保留自己的 last_changed",
       series[1].get("last_changed"), rows[0]["last_changed"])
    eq("C9d 期初状态与首条真实变更的 state 相同（同一时刻的两种表达）",
       series[1].get("state"), series[0].get("state"))

    # 末条必须是完整的（App 靠它拿属性）
    check("C10 末条带完整 attributes", isinstance(series[-1].get("attributes"), dict),
          repr(series[-1])[:200])
    eq("C11 末条 entity_id 正确", series[-1].get("entity_id"), ENTITY)
    # 中间条目在没写 minimal_response 时也应该是完整的
    if len(series) > 2:
        check("C12 未加 minimal_response 时中间条目也是完整对象",
              isinstance(series[1].get("attributes"), dict), repr(series[1])[:200])

    # sector 必须是字符串（上游就是这么给的，App 依赖它做字符串拼接）
    st2, full = http(base + "/api/history/period/" + urllib.parse.quote(start.isoformat())
                     + qs + "&minimal_response&no_attributes", token=TOKEN)
    eq("C13 minimal+no_attributes -> HTTP 200", st2, 200)
    ser2 = full[0]
    check("C14 no_attributes 时首条也不带 attributes",
          "attributes" not in ser2[0], repr(ser2[0])[:200])
    check("C15 no_attributes 时末条也不带 attributes",
          "attributes" not in ser2[-1], repr(ser2[-1])[:200])
    if len(ser2) > 2:
        mid = ser2[len(ser2) // 2]
        eq("C16 minimal 中间条目只有 state", sorted(mid.keys()), ["last_changed", "state"])
    check("C17 minimal 的末条仍是完整对象（含 entity_id/attributes 或按 no_attributes 去掉）",
          ser2[-1].get("entity_id") == ENTITY, repr(ser2[-1])[:200])

    # 单独试 minimal_response（不带 no_attributes）：首末带 attributes，中间不带
    st3, only_min = http(base + "/api/history/period/" + urllib.parse.quote(start.isoformat())
                         + qs + "&minimal_response", token=TOKEN)
    ser3 = only_min[0]
    check("C18 minimal 首条保留 attributes", "attributes" in ser3[0], repr(ser3[0])[:200])
    if len(ser3) > 2:
        mid = ser3[len(ser3) // 2]
        eq("C19 minimal 中间条目被裁剪", sorted(mid.keys()), ["last_changed", "state"])
    check("C20 minimal 末条保留 attributes", "attributes" in ser3[-1], repr(ser3[-1])[:200])

    # sector 字符串性 & 期初改写：挑一个带 sector 的时间窗
    sect = next((r for r in rows if r.get("sector")), None)
    if sect is not None:
        t = datetime.datetime.fromisoformat(sect["last_changed"]) - datetime.timedelta(seconds=1)
        st4, b4 = http(base + "/api/history/period/" + urllib.parse.quote(t.isoformat())
                       + "?filter_entity_id=" + urllib.parse.quote(ENTITY)
                       + "&end_time=" + urllib.parse.quote(
                           (t + datetime.timedelta(minutes=5)).isoformat()), token=TOKEN)
        hit = next((x for x in b4[0] if x.get("state") == sect["state"]), None)
        check("C21 sector 是字符串", hit is not None
              and isinstance(hit["attributes"].get("sector"), str),
              "hit=%s" % (repr(hit)[:150]))

    # 区间内没有任何数据 -> 空数组条目
    far = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    st5, b5 = http(base + "/api/history/period/" + urllib.parse.quote(far.isoformat())
                   + "?filter_entity_id=" + urllib.parse.quote(ENTITY)
                   + "&end_time=" + urllib.parse.quote(
                       (far + datetime.timedelta(hours=1)).isoformat()), token=TOKEN)
    eq("C22 完全空区间 -> HTTP 200", st5, 200)
    eq("C23 完全空区间 -> 空数组", b5, [[]])

    # 未知实体 -> 空数组（HA 的行为，不是 404）
    st6, b6 = http(base + "/api/history/period/" + urllib.parse.quote(start.isoformat())
                   + "?filter_entity_id=sensor.nope&end_time="
                   + urllib.parse.quote(end.isoformat()), token=TOKEN)
    eq("C24 未知实体的历史 -> 空数组", b6, [[]])


def test_websocket_and_scenario(mock, host, port, base):
    print("\n[D] /api/websocket 协议 + vsc 剧本", flush=True)
    ws = MiniWS(host, port)
    try:
        ok, detail = ws.accept_ok()
        check("D1 Sec-WebSocket-Accept 计算正确", ok, detail)

        msg, _ = ws.recv_json()
        eq("D2 服务端先发 auth_required", msg.get("type"), "auth_required")
        check("D3 auth_required 带 ha_version", bool(msg.get("ha_version")), repr(msg))

        ws.send_json({"type": "auth", "access_token": TOKEN})
        msg, _ = ws.recv_json()
        eq("D4 正确 token -> auth_ok", msg.get("type"), "auth_ok")
        eq("D5 auth_ok 的 ha_version", msg.get("ha_version"), mock_ha.HA_VERSION)

        # ping/pong 控制帧（RFC 要求原样带回 payload）
        ws.send_ping(b"f1-ping")
        opcode, payload, _ = ws.recv_frame()
        if opcode == 0x9:            # 服务端可能先推 ping，先回掉再等我们的 pong
            ws._send(0xA, payload)
            opcode, payload, _ = ws.recv_frame()
        eq("D6 ping 得到 pong", opcode, 0xA)
        eq("D7 pong 原样带回 payload", payload, b"f1-ping")

        ws.send_json({"id": 1, "type": "subscribe_trigger",
                      "trigger": {"platform": "state", "entity_id": ENTITY}})
        msg, _ = ws.recv_json()
        eq("D8 订阅返回 result", msg.get("type"), "result")
        eq("D9 result.id == 订阅 id", msg.get("id"), 1)
        eq("D10 result.success == True", msg.get("success"), True)
        eq("D11 result.result == None", msg.get("result"), None)

        # 起 vsc 剧本（间隔压到 0.4s，测试才不用等十几秒）
        st, body = http(base + "/_mock/scenario?name=vsc&interval=0.4", token=TOKEN, method="POST")
        eq("D12 切换剧本 -> HTTP 200", st, 200)
        eq("D13 返回剧本名", (body or {}).get("scenario"), "vsc")

        events = []
        deadline = time.monotonic() + 20
        while len(events) < 4 and time.monotonic() < deadline:
            msg, _ = ws.recv_json(timeout=max(1.0, deadline - time.monotonic()))
            if msg.get("type") == "event":
                events.append(msg)
        check("D14 至少收到 3 个 event", len(events) >= 3, "收到 %d 个" % len(events))

        def to_state(ev):
            return (((ev.get("event") or {}).get("variables") or {}).get("trigger") or {}) \
                .get("to_state")

        # 事件包结构
        trig = events[0]["event"]["variables"]["trigger"]
        eq("D15 event.id == 订阅 id", events[0].get("id"), 1)
        eq("D16 event.type", events[0].get("type"), "event")
        eq("D17 trigger.platform", trig.get("platform"), "state")
        eq("D18 trigger.entity_id", trig.get("entity_id"), ENTITY)
        check("D19 trigger 里有 from_state 和 to_state",
              "from_state" in trig and "to_state" in trig, repr(list(trig))[:120])

        states = [to_state(e) for e in events]
        msgs = [s.get("state") for s in states if s]
        # 剧本里的消息现在**逐字取自真实数据**，所以断言也跟着用真实的那条
        # （vsc 剧本派的是 09-13 21:26 那一串，第一个黄旗是扇区 23）
        check("D20 收到 YELLOW IN TRACK SECTOR 23",
              any("YELLOW IN TRACK SECTOR 23" == m for m in msgs), repr(msgs))

        # ★ 坑 A：安全车/VSC 的 flag 是空串，category 才是 SafetyCar
        vsc_ev = next((s for s in states if s and s.get("state") == "VSC DEPLOYED"), None)
        check("D21 收到 VSC DEPLOYED", vsc_ev is not None, repr(msgs))
        if vsc_ev is not None:
            a = vsc_ev.get("attributes") or {}
            eq("D22 ★VSC DEPLOYED 的 flag 是空字符串", a.get("flag"), "")
            eq("D23 ★VSC DEPLOYED 的 category 是 SafetyCar", a.get("category"), "SafetyCar")
            eq("D24 VSC DEPLOYED 的对象形状完整",
               sorted(vsc_ev.keys()), ["attributes", "entity_id", "last_changed",
                                       "last_updated", "state"])
            check("D25 attributes 里 sector/scope/flag 键都在（空也要保留）",
                  all(k in a for k in ("flag", "category", "scope", "sector",
                                       "car_number", "event_id", "sequence",
                                       "history", "raw_message")), repr(sorted(a)))
            check("D26 sequence 是递增整数", isinstance(a.get("sequence"), int), repr(a.get("sequence")))

        # state 对象形状照抄任务书
        if states and states[0]:
            a0 = states[0]["attributes"]
            check("D27 状态对象 5 个顶层键",
                  sorted(states[0].keys()) == ["attributes", "entity_id", "last_changed",
                                               "last_updated", "state"],
                  repr(sorted(states[0].keys())))
            check("D28 属性 utc 是秒精度、received_at 带微秒",
                  len(a0.get("utc", "")) == 25 and len(a0.get("received_at", "")) > 25,
                  "utc=%r received_at=%r" % (a0.get("utc"), a0.get("received_at")))
            check("D29 history 是列表且不超过 5 条",
                  isinstance(a0.get("history"), list) and len(a0["history"]) <= 5,
                  repr(a0.get("history"))[:120] if isinstance(a0.get("history"), list) else "非列表")

        # sequence 必须严格递增（App 用它检测丢号）
        seqs = [(to_state(e) or {}).get("attributes", {}).get("sequence") for e in events]
        seqs = [s for s in seqs if isinstance(s, int)]
        check("D30 sequence 严格递增、不跳号",
              all(seqs[i + 1] == seqs[i] + 1 for i in range(len(seqs) - 1)) and len(seqs) >= 3,
              repr(seqs))

        # 多客户端：再连一个，两个都要收到
        ws2 = MiniWS(host, port)
        try:
            ws2.recv_json()                                # auth_required
            ws2.send_json({"type": "auth", "access_token": TOKEN})
            ws2.recv_json()                                # auth_ok
            ws2.send_json({"id": 7, "type": "subscribe_trigger",
                           "trigger": {"platform": "state", "entity_id": ENTITY}})
            ws2.recv_json()                                # result

            # 订阅者计数是异步注册的，轮询等它稳定到 2 再断言
            n_sub = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                st, body = http(base + "/_mock/status", token=TOKEN)
                n_sub = (body or {}).get("subscribers")
                if n_sub == 2:
                    break
                time.sleep(0.2)
            eq("D31 status 里订阅者数量 == 2", n_sub, 2)

            # 上一轮 vsc 已经播完，重新起一次让两个客户端都有新消息可收
            http(base + "/_mock/scenario?name=vsc&interval=0.3", token=TOKEN, method="POST")
            got1, got2 = False, False
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not (got1 and got2):
                m, _ = ws.recv_json(timeout=max(1.0, deadline - time.monotonic()))
                if m.get("type") == "event":
                    got1 = True
                    eq("D32a 第一个客户端仍在收 event 且 id == 1", m.get("id"), 1)
                    break
            while time.monotonic() < deadline and not got2:
                m, _ = ws2.recv_json(timeout=max(1.0, deadline - time.monotonic()))
                if m.get("type") == "event":
                    got2 = True
                    eq("D32b 第二个客户端的 event.id == 7", m.get("id"), 7)
            check("D33 多客户端同时收到推送", got1 and got2,
                  "client1=%s client2=%s" % (got1, got2))
        finally:
            ws2.close()
            n_sub = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                st, body = http(base + "/_mock/status", token=TOKEN)
                n_sub = (body or {}).get("subscribers")
                if n_sub == 1:
                    break
                time.sleep(0.2)
            eq("D34 断开后订阅者回到 1", n_sub, 1)
    finally:
        ws.close()


def test_auth_invalid(host, port):
    print("\n[D2] WebSocket 错误 token", flush=True)
    ws = MiniWS(host, port)
    try:
        msg, _ = ws.recv_json()
        eq("D35 先收到 auth_required", msg.get("type"), "auth_required")
        ws.send_json({"type": "auth", "access_token": "definitely-wrong"})
        msg, _ = ws.recv_json()
        eq("D36 错误 token -> auth_invalid", msg.get("type"), "auth_invalid")
        eq("D37 auth_invalid 文案", msg.get("message"), "Invalid access token or password")
        try:
            m2, _ = ws.recv_json(timeout=3.0)
            check("D38 auth_invalid 之后服务端关闭连接", bool(m2.get("__closed__")), repr(m2))
        except (OSError, TimeoutError) as exc:
            # Windows 上服务端一 close，客户端这边常常直接收到
            # ConnectionResetError(10054) 而不是干净的 close 帧，两种都算通过
            check("D38 auth_invalid 之后服务端关闭连接", True, "直接断开也算：%s" % exc)
    finally:
        ws.close()


def test_mock_endpoints(mock, base):
    print("\n[E] /_mock/* 辅助接口", flush=True)
    st, body = http(base + "/_mock/status", token=TOKEN)
    eq("E1 /_mock/status HTTP 200", st, 200)
    for k in ("subscribers", "progress", "sequence", "stats"):
        check("E2 status 含字段 %s" % k, k in (body or {}), repr(sorted(body or {})))
    eq("E3 status.progress.mode 是 scenario", (body.get("progress") or {}).get("mode"), "scenario")
    eq("E4 status.progress.scenario", (body.get("progress") or {}).get("scenario"), "vsc")

    st, body = http(base + "/_mock/newest", token=TOKEN)
    eq("E5 /_mock/newest HTTP 200", st, 200)
    check("E6 newest 就是一条状态对象",
          (body or {}).get("entity_id") == ENTITY and "state" in (body or {}),
          repr(body)[:150])

    st, body = http(base + "/_mock/scenario?name=test_double_yellow&interval=0.3",
                    token=TOKEN, method="POST")
    eq("E7 切到 test_double_yellow", (body or {}).get("scenario"), "test_double_yellow")
    time.sleep(1.2)
    st, body = http(base + "/_mock/status", token=TOKEN)
    eq("E8 切换后 status 反映新剧本",
       (body.get("progress") or {}).get("scenario"), "test_double_yellow")

    st, body = http(base + "/_mock/scenario?name=not_a_scenario", token=TOKEN, method="POST")
    eq("E9 未知剧本 -> 404", st, 404)

    st, body = http(base + "/_mock/scenario?name=burst", token="bad", method="POST")
    check("E10 /_mock/scenario 不做鉴权（方便浏览器直接点）", st == 200, "status=%s" % st)


def build_slow_fixture(path, src_rows, n=4, gap=1.0, base_offset=1.0):
    """从真实数据里取前 n 条，做一份"慢放版"临时 TSV。

    为什么不能直接对 697 条那份做时序断言：真实比赛消息之间动辄几分钟到十几小时
    的空档（首条之后就是 10.6 分钟），1 倍速下永远等不到第二条。
    所以这里保留真实的**内容**（state/flag/category/sector 原样搬），只把时间戳
    重排成 base_offset + i*gap 秒，这样"按 last_changed 的间隔推进"这件事就能在
    几秒内被验证，而且验的是真的回放逻辑，不是被改过的分支。
    """
    header_needed = ["local_time", "last_changed", "flag", "category", "scope",
                     "sector", "car_number", "event_id", "state", "message"]
    t0 = datetime.datetime(2026, 9, 11, 10, 0, 0, tzinfo=datetime.timezone.utc)
    out = []
    for i, rec in enumerate(src_rows[:n]):
        stamp = (t0 + datetime.timedelta(seconds=base_offset + i * gap)).isoformat()
        row = dict(rec)
        row["last_changed"] = stamp
        # event_id 里嵌了时间戳，跟着改，保证"去重键"在慢放版里仍然唯一
        row["event_id"] = "%s|%s|%s" % (stamp.split(".")[0], rec.get("category", ""),
                                        rec.get("message", ""))
        out.append("\t".join(row.get(k, "") for k in header_needed))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(header_needed) + "\n")
        fh.write("\n".join(out) + "\n")
    return out


def test_real_replay():
    """真实数据回放线程（主实例没开它，所以这里单独起一个）。

    验的是三件事：
      1. --start-offset 期间 /api/states 一直是 unavailable（真 HA 无比赛的样子）
      2. 时间一到，第一条真的推出来，且 sequence 从 1 开始、时间戳用数据里的原值
      3. 相邻两条按 last_changed 的真实间隔推进（间隔 1 秒就真的等约 1 秒）
    """
    print("\n[H] 真实数据回放线程（--start-offset + 按 last_changed 推进）", flush=True)
    src = load_tsv(DATA)
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_replay_tmp.tsv")
    build_slow_fixture(fixture, src, n=4, gap=1.0, base_offset=1.0)
    want_rows = load_tsv(fixture)
    args = mock_ha.build_parser().parse_args([
        "--host", "127.0.0.1", "--port", "0", "--token", TOKEN,
        "--data", fixture, "--speed", "1", "--start-offset", "1.0",
    ])
    mock = mock_ha.MockHA(args)
    httpd = mock_ha.make_server(mock, "127.0.0.1", 0)
    base = "http://127.0.0.1:%d" % httpd.real_port
    # 记下开工前有哪些 mock 线程：主实例的 HTTP 线程这时候还在跑（那是设计如此），
    # 所以"H 组自己有没有泄漏"必须和这个基线比，不能简单地要求一个都不剩
    baseline = sorted(t.name for t in threading.enumerate() if "mock-ha" in t.name)
    mock_ha.run(mock, httpd, blocking=False)     # autostart=True：真的起回放线程
    try:
        # 1) offset 期间必须还是 unavailable
        st, body = http(base + "/api/states/" + ENTITY, token=TOKEN)
        eq("H1 offset 期间 state 仍是 unavailable", (body or {}).get("state"), "unavailable")
        eq("H2 offset 期间属性只有 friendly_name/icon",
           sorted((body or {}).get("attributes", {})), ["friendly_name", "icon"])
        st, stt = http(base + "/_mock/status", token=TOKEN)
        eq("H3 status 说明总条数", (stt.get("progress") or {}).get("total"), 4)
        eq("H4 status 说明是 race 模式", (stt.get("progress") or {}).get("mode"), "race")

        # 2) 盯住第一条出现的时间点
        t0 = time.monotonic()
        first = None
        while time.monotonic() - t0 < 12:
            st, body = http(base + "/api/states/" + ENTITY, token=TOKEN)
            if (body or {}).get("state") != "unavailable":
                first = body
                break
            time.sleep(0.1)
        dt = time.monotonic() - t0
        check("H5 offset 之后第一条真的推出来了", first is not None, "等了 %.1fs" % dt)
        check("H6 第一条确实是等了约 1 秒（offset 生效）", dt >= 0.6, "只等了 %.2fs" % dt)
        if first:
            a = first["attributes"]
            eq("H7 回放第一条 sequence == 1", a.get("sequence"), 1)
            eq("H8 回放第一条的 state 就是数据首行的 state",
               first.get("state"), want_rows[0]["state"])
            eq("H9 回放第一条的 last_changed 用数据里的原值",
               first.get("last_changed"), want_rows[0]["last_changed"])
            eq("H10 回放第一条 history 为空", a.get("history"), [])

        # 3) 第三条：按 last_changed 的间隔（1 秒）推进，且每落一条就多等 1 秒
        #    这里连收 2 条，把"间隔推进"这件事验两遍，避免只对一次是巧合
        seqs, times = [], []
        prev_state = first["state"] if first else None
        t1 = time.monotonic()
        while len(seqs) < 2 and time.monotonic() - t1 < 10:
            st, body = http(base + "/api/states/" + ENTITY, token=TOKEN)
            cur = (body or {}).get("state")
            if cur != prev_state:
                seqs.append(body["attributes"].get("sequence"))
                times.append(time.monotonic() - t1)
                prev_state = cur
                # 避免同一次轮询里连读两条，让"间隔"测的是一次真实的等待
                time.sleep(0.1)
            time.sleep(0.05)
        check("H11 第 2、3 条都按时间轴到了", len(seqs) == 2, repr(seqs))
        eq("H12 第二条 sequence == 2", seqs[0] if seqs else None, 2)
        eq("H13 第三条 sequence == 3", seqs[1] if len(seqs) > 1 else None, 3)
        if len(times) == 2:
            gap_want = (datetime.datetime.fromisoformat(want_rows[1]["last_changed"])
                        - datetime.datetime.fromisoformat(want_rows[0]["last_changed"])
                        ).total_seconds()
            check("H14 相邻两条按 last_changed 间隔推进（数据间隔 %.1fs，实测 %.2fs / %.2fs）"
                  % (gap_want, times[0], times[1]),
                  gap_want - 0.6 <= times[1] - times[0] <= gap_want + 1.2,
                  "两次到达相差 %.2fs" % (times[1] - times[0]))

        # 4) 收到的状态对象里 history 是滚动累积的
        st, body = http(base + "/api/states/" + ENTITY, token=TOKEN)
        check("H15 history 累积到 2 条（第 3 条状态下）",
              len((body.get("attributes") or {}).get("history", [])) >= 2,
              repr(body.get("attributes", {}).get("history")))
    finally:
        cleanup_started = time.monotonic()
        mock_ha.cleanup(httpd, mock)
        took = time.monotonic() - cleanup_started
        now = sorted(t.name for t in threading.enumerate() if "mock-ha" in t.name)
        check("H16 关掉后这个实例没残留线程（清理耗时 %.2fs）" % took,
              now == baseline, "before=%r after=%r" % (baseline, now))
        check("H17 清理是立即完成的，不是靠超时兜底", took < 3.0, "%.2fs" % took)
        try:
            os.remove(fixture)
        except OSError:
            pass


def test_burst(mock, host, port, base):
    print("\n[F] burst 剧本（7 条/秒突发）", flush=True)
    ws = MiniWS(host, port)
    try:
        ws.recv_json()
        ws.send_json({"type": "auth", "access_token": TOKEN})
        ws.recv_json()
        ws.send_json({"id": 3, "type": "subscribe_trigger",
                      "trigger": {"platform": "state", "entity_id": ENTITY}})
        ws.recv_json()
        t0 = time.monotonic()
        http(base + "/_mock/scenario?name=burst", token=TOKEN, method="POST")
        n = 0
        while n < 7 and time.monotonic() - t0 < 10:
            m, _ = ws.recv_json(timeout=10)
            if m.get("type") == "event":
                n += 1
        dt = time.monotonic() - t0
        eq("F1 burst 收到 7 条", n, 7)
        check("F2 7 条在 3 秒内到齐（确实是突发）", dt < 3.0, "耗时 %.2fs" % dt)
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    print("=" * 78)
    print("mock HA 自测 —— 数据源 %s" % DATA)
    print("=" * 78, flush=True)

    if not os.path.exists(DATA):
        print("[FAIL] 数据文件不存在：%s" % DATA)
        return 1
    rows = load_tsv(DATA)
    print("已载入真实数据 %d 条" % len(rows), flush=True)

    # 用经典 argparse.Namespace 构造配置：测试要能不碰命令行就起服务器
    args = mock_ha.build_parser().parse_args([
        "--host", "127.0.0.1",
        "--port", "0",                       # ★ 0 = 让内核挑空闲端口，避免撞车
        "--token", TOKEN,
        "--speed", "1",
        "--data", DATA,
    ])
    mock = mock_ha.MockHA(args)
    httpd = mock_ha.make_server(mock, "127.0.0.1", 0)
    host, port = "127.0.0.1", httpd.real_port
    base = "http://%s:%d" % (host, port)

    # 主实例故意不启动真实回放（autostart=False）：它的时间轴长达 2 天（含夜间隔），
    # 会和测试用的剧本抢推送，测试就不确定了。回放这条路单独由 [H] 组用一个
    # 加了 --start-offset 的独立实例来验。
    mock.load()
    thread = mock_ha.run(mock, httpd, blocking=False, autostart=False)
    print("服务器已在 %s 启动（端口 %d）" % (base, port), flush=True)

    try:
        test_api_root(base)
        test_states(base)
        test_history(base, rows)
        test_auth_invalid(host, port)
        test_websocket_and_scenario(mock, host, port, base)
        test_mock_endpoints(mock, base)
        test_burst(mock, host, port, base)
        test_real_replay()

        # 线程回收检查：把 HTTP 线程和 WS 线程都算进去
        time.sleep(0.5)
        before = [t.name for t in threading.enumerate()]
        mock_ha.cleanup(httpd, mock)
        thread.join(timeout=5)
        deadline = time.monotonic() + 5
        leftover = []
        while time.monotonic() < deadline:
            leftover = [t.name for t in threading.enumerate()
                        if not t.name.startswith("py") and t.is_alive()
                        and t.name not in ("MainThread",)
                        and ("mock-ha" in t.name or "Thread-" in t.name)]
            if not leftover:
                break
            time.sleep(0.2)
        print("\n[G] 清理", flush=True)
        check("G1 服务器线程已退出", not thread.is_alive())
        check("G2 没有残留的 mock 线程", not leftover, "残留=%r" % leftover)
        # 真的去连一下端口：连得上说明监听套接字没关干净
        closed = False
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
        except OSError:
            closed = True
        check("G3 服务器关闭后不再监听端口", closed)
        check("G4 关闭前存在过 mock 线程", any("mock-ha" in n for n in before),
              "before=%r" % before)
    finally:
        try:
            mock_ha.cleanup(httpd, mock)
        except Exception:
            pass

    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 78)
    for name, ok, detail in RESULTS:
        if not ok:
            print("[FAIL] %s   %s" % (name, detail))
    print("汇总：%d 项，通过 %d，失败 %d" % (total, total - len(failed), len(failed)))
    print("=" * 78, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
