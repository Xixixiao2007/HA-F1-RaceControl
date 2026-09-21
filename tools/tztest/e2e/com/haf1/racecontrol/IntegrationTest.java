package com.haf1.racecontrol;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 桌面端**集成测试**：拿真实的产品代码去打一个真的 HTTP / WebSocket 服务器。
 *
 * ## 为什么需要它
 * `TzTest` 那种单测是把函数拆出来单独喂参数，验证不了"这两个类连在一起、
 * 隔着真实 socket、按真实协议能不能跑通"。而在这台机器上又装不了 APK
 * （没有模拟器也没有设备）。所以在桌面把 HaClient / HaWebSocket /
 * AlertGate / TrackState 编出来，直接连 `mock_ha.py`。
 *
 * 覆盖的是**最容易出错、单测又碰不到**的那一段：HTTP 细节、属性解析、
 * WebSocket 握手与订阅协议、事件形状。
 *
 * 用法（由 tools/run_e2e.py 驱动）：
 *   java -Dmock.host=127.0.0.1 -Dmock.port=8124 -Dmock.mode=rest ... com.haf1.racecontrol.IntegrationTest
 */
public class IntegrationTest {

    private static int pass = 0;
    private static int fail = 0;

    private static void check(String label, boolean ok, String detail) {
        if (ok) {
            pass++;
        } else {
            fail++;
        }
        System.out.println((ok ? "  [PASS] " : "  [FAIL] ") + label
                + (detail == null || detail.length() == 0 ? "" : "   " + detail));
    }

    private static void info(String s) {
        System.out.println("  ·  " + s);
    }

    private static void section(String s) {
        System.out.println();
        System.out.println("== " + s + " ==");
    }

    public static void main(String[] args) {
        String host = System.getProperty("mock.host", "127.0.0.1");
        int port = Integer.parseInt(System.getProperty("mock.port", "8124"));
        String token = System.getProperty("mock.token", "e2e");
        String entity = System.getProperty("mock.entity", "sensor.f1_race_control_2");
        String mode = System.getProperty("mock.mode", "rest");
        int wsSeconds = Integer.parseInt(System.getProperty("mock.seconds", "20"));
        int histHours = Integer.parseInt(System.getProperty("mock.hours", "72"));

        Prefs p = new Prefs();
        p.base = "http://" + host + ":" + port;
        p.token = token;
        p.entityId = entity;
        p.noiseFilterEnabled = false;       // 统计时先不过滤，才能看到全貌

        System.out.println("mock HA: " + p.baseUrl() + "   实体: " + entity + "   模式: " + mode);

        try {
            restApi(p);
        } catch (Throwable t) {
            check("REST /api/ 连通性", false, t.toString());
        }

        if ("ws".equals(mode)) {
            try {
                webSocket(p, wsSeconds, mode);
            } catch (Throwable t) {
                check("WebSocket 实时通道", false, t.toString());
                t.printStackTrace(System.out);
            }
        } else {
            try {
                history(p, histHours);
            } catch (Throwable t) {
                check("REST 历史接口", false, t.toString());
                t.printStackTrace(System.out);
            }
        }

        System.out.println();
        System.out.println("==================================================");
        System.out.println("  集成测试：通过 " + pass + " 项，失败 " + fail + " 项");
        System.out.println("==================================================");
        if (fail > 0) {
            System.exit(1);
        }
    }

    // ------------------------------------------------------------------
    // 1) /api/ —— 连通性与令牌
    // ------------------------------------------------------------------
    private static void restApi(Prefs p) throws Exception {
        section("/api/ 连通性与令牌校验");
        String ok = HaClient.testConnection(p);
        info(ok.replace("\n", " | "));
        // testConnection 成功会返回"已连上 HA…"，失败会抛 HaException
        check("带正确令牌能连上", ok.indexOf("已连上") >= 0, null);

        Prefs wrong = new Prefs();
        wrong.base = p.base;
        wrong.token = "definitely-wrong-token";
        wrong.entityId = p.entityId;
        boolean rejected = false;
        try {
            HaClient.testConnection(wrong);
        } catch (HaClient.HaException e) {
            rejected = true;
            info("错令牌被拒：" + e.getMessage());
        }
        check("错令牌必须被拒绝（401）", rejected, null);
    }

    // ------------------------------------------------------------------
    // 2) REST 历史 —— 属性解析 + 全量回放跑分类/聚类
    // ------------------------------------------------------------------
    private static void history(Prefs p, int hours) throws Exception {
        section("REST 历史接口（带属性，这是本代与上一代的关键差别）");
        long now = System.currentTimeMillis();
        long start = now - hours * 3600000L;
        long t0 = System.currentTimeMillis();
        List<RaceMessage> list = HaClient.fetchHistory(p, start, now);
        long ms = System.currentTimeMillis() - t0;
        info("拿到 " + list.size() + " 条，耗时 " + ms + " ms");
        check("历史接口返回了数据", list.size() > 100, list.size() + " 条");
        if (list.isEmpty()) {
            return;
        }

        // 属性有没有真的解析出来 —— 这是整个分类体系的地基
        int withFlag = 0, withCat = 0, withSector = 0, withEventId = 0;
        Map<String, Integer> kinds = new HashMap<String, Integer>();
        Map<String, Integer> flags = new HashMap<String, Integer>();
        Map<String, Integer> cats = new HashMap<String, Integer>();
        for (int i = 0; i < list.size(); i++) {
            RaceMessage m = list.get(i);
            if (m.flag.length() > 0) {
                withFlag++;
                bump(flags, m.flag);
            }
            if (m.category.length() > 0) {
                withCat++;
                bump(cats, m.category);
            }
            if (m.sector.length() > 0) {
                withSector++;
            }
            if (m.eventId.length() > 0) {
                withEventId++;
            }
            bump(kinds, Classifier.kind(m));
        }
        info("带 flag 的 " + withFlag + " 条；带 category 的 " + withCat
                + " 条；带 sector 的 " + withSector + " 条；带 event_id 的 " + withEventId + " 条");
        check("属性真的解析出来了（flag）", withFlag > 100, null);
        check("category 解析出来了", withCat > 100, null);
        check("event_id 解析出来了（去重键依赖它）", withEventId > 100, null);

        info("flag 取值分布：" + flags);
        info("category 取值分布：" + cats);

        // ---- 分类的两个坑，必须在真实数据上验到 ----
        section("真实数据上的两个分类坑");
        int sc = kinds.containsKey(Classifier.K_VSC) ? kinds.get(Classifier.K_VSC) : 0;
        int scEnd = kinds.containsKey(Classifier.K_SC_END) ? kinds.get(Classifier.K_SC_END) : 0;
        info("识别出 VSC：" + sc + " 条；安全车结束类：" + scEnd + " 条");
        check("VSC 靠 category=SafetyCar 被识别出来了（按 flag 判会永远为 0）",
                sc > 0, "VSC=" + sc);
        int dy = kinds.containsKey(Classifier.K_DY) ? kinds.get(Classifier.K_DY) : 0;
        int red = kinds.containsKey(Classifier.K_RED) ? kinds.get(Classifier.K_RED) : 0;
        info("双黄旗 " + dy + " 条；红旗 " + red + " 条");
        check("双黄旗数量接近实测的 142", dy > 100 && dy < 200, "double yellow=" + dy);
        check("红旗数量接近实测的 3", red >= 1 && red <= 10, "red=" + red);

        // ---- 全量回放：让真实的 AlertGate 在真实数据上跑一遍 ----
        section("全量回放 → AlertGate 会响多少次（与 Python 模拟器交叉验证）");
        List<RaceMessage> asc = new ArrayList<RaceMessage>(list);
        Collections.sort(asc, new Comparator<RaceMessage>() {
            public int compare(RaceMessage a, RaceMessage b) {
                return a.time < b.time ? -1 : (a.time > b.time ? 1 : 0);
            }
        });

        AlertGate gate = new AlertGate();        // 默认设置 = 方案 A
        int[] r = replay(asc, gate);
        int alarms = r[0], attentions = r[1], escalations = r[2];
        Map<String, Integer> alarmKinds = kindsOf(asc, gate);

        info("【方案 A：默认】警报（全屏级）" + alarms + " 次，其中由双黄升级而来 "
                + escalations + " 次；轻提醒 " + attentions + " 次");
        info("            构成：" + alarmKinds);

        // 方案 B 也实测一遍 —— 设置页里给它标了次数，那个数不该是拍的
        AlertGate gateB = new AlertGate();
        gateB.dyMinSectors = 3;
        int[] rb = replay(asc, gateB);
        info("【方案 B：双黄仅 ≥3 区段】警报 " + rb[0] + " 次");

        System.out.println("   >>> 对照：独立写的 Python 模拟器在同一份数据上算出 21 次/周末");
        System.out.println("   >>> 字面实现（每条双黄都响）是 146 次/周末");

        check("方案 A 警报次数落在合理区间（15~35）", alarms >= 15 && alarms <= 35, alarms + " 次");
        check("字面实现会响 146 次，聚合成事故后必须显著更少", alarms < 60, alarms + " 次");
        check("方案 B 比方案 A 更安静", rb[0] <= alarms, "A=" + alarms + " B=" + rb[0]);
        check("红旗一定触发过警报",
                alarmKinds.containsKey(Classifier.K_RED),
                "构成=" + alarmKinds);
        check("CLEAR / 蓝旗绝不会触发警报",
                !alarmKinds.containsKey(Classifier.K_CLEAR)
                        && !alarmKinds.containsKey(Classifier.K_BLUE), null);
    }

    /**
     * 把一份消息按真实时间轴喂给闸门，并按 UI 的节拍推进 onTick。
     *
     * ★ 必须按节拍推进，不能"只在消息到达时 tick"。真实 App 里 UI 每 500ms
     *   调一次 {@link AlertGate#onTick}，双黄旗"存活超过 15 秒才升级"靠的就是它。
     *   如果只在消息到达时 tick，一次事故结束后长时间没有新消息就再也没有 tick，
     *   升级永远不会触发 —— 实测报警数会从 23 次掉到 11 次。
     *   这里按 2 秒一步模拟（比真实的 500ms 粗，够用）。
     *
     * @return {警报数, 轻提醒数, 由双黄升级而来的警报数}
     */
    private static int[] replay(List<RaceMessage> asc, AlertGate gate) {
        final long TICK_STEP_MS = 2000L;
        gate.reset();
        int alarms = 0, attentions = 0, escalations = 0;
        long simNow = asc.get(0).time;

        for (int i = 0; i < asc.size(); i++) {
            RaceMessage m = asc.get(i);
            while (simNow + TICK_STEP_MS <= m.time) {
                simNow += TICK_STEP_MS;
                int n = gate.onTick(simNow).size();
                alarms += n;
                escalations += n;
            }
            if (m.time > simNow) {
                simNow = m.time;
            }
            AlertGate.Action a = gate.onMessage(m, m.time);
            if (a != null) {
                if (a.severity >= Classifier.ALARM) {
                    alarms++;
                } else {
                    attentions++;
                }
            }
        }
        // 收尾：再推进 10 分钟，让最后一批"待升级"的走完流程
        for (int k = 0; k < 300; k++) {
            simNow += TICK_STEP_MS;
            int n = gate.onTick(simNow).size();
            alarms += n;
            escalations += n;
        }
        return new int[] {alarms, attentions, escalations};
    }

    /** 警报的类型构成。 */
    private static Map<String, Integer> kindsOf(List<RaceMessage> asc, AlertGate gate) {
        final long TICK_STEP_MS = 2000L;
        Map<String, Integer> out = new HashMap<String, Integer>();
        gate.reset();
        long simNow = asc.get(0).time;
        for (int i = 0; i < asc.size(); i++) {
            RaceMessage m = asc.get(i);
            while (simNow + TICK_STEP_MS <= m.time) {
                simNow += TICK_STEP_MS;
                List<AlertGate.Action> ups = gate.onTick(simNow);
                for (int k = 0; k < ups.size(); k++) {
                    bump(out, ups.get(k).kind);
                }
            }
            if (m.time > simNow) {
                simNow = m.time;
            }
            AlertGate.Action a = gate.onMessage(m, m.time);
            if (a != null && a.severity >= Classifier.ALARM) {
                bump(out, a.kind);
            }
        }
        for (int k = 0; k < 300; k++) {
            simNow += TICK_STEP_MS;
            List<AlertGate.Action> ups = gate.onTick(simNow);
            for (int q = 0; q < ups.size(); q++) {
                bump(out, ups.get(q).kind);
            }
        }
        return out;
    }

    /** 再跑一遍，收集警报的类型构成。 */
    private static Map<String, Integer> kindsOf(List<RaceMessage> asc, AlertGate gate,
                                                Map<String, Integer> sink) {
        final long TICK_STEP_MS = 2000L;
        Map<String, Integer> out = new HashMap<String, Integer>();
        gate.reset();
        long simNow = asc.get(0).time;
        for (int i = 0; i < asc.size(); i++) {
            RaceMessage m = asc.get(i);
            while (simNow + TICK_STEP_MS <= m.time) {
                simNow += TICK_STEP_MS;
                List<AlertGate.Action> ups = gate.onTick(simNow);
                for (int k = 0; k < ups.size(); k++) {
                    bump(out, ups.get(k).kind);
                }
            }
            if (m.time > simNow) {
                simNow = m.time;
            }
            AlertGate.Action a = gate.onMessage(m, m.time);
            if (a != null && a.severity >= Classifier.ALARM) {
                bump(out, a.kind);
            }
        }
        for (int k = 0; k < 300; k++) {
            simNow += TICK_STEP_MS;
            List<AlertGate.Action> ups = gate.onTick(simNow);
            for (int q = 0; q < ups.size(); q++) {
                bump(out, ups.get(q).kind);
            }
        }
        return out;
    }

    // ------------------------------------------------------------------
    // 3) WebSocket 实时 —— 握手 + 认证 + 订阅 + 事件形状
    // ------------------------------------------------------------------
    private static void webSocket(Prefs p, int seconds, String mode) throws Exception {
        section("WebSocket 实时通道（握手 / 认证 / subscribe_trigger / 事件形状）");

        final List<RaceMessage> got = Collections.synchronizedList(new ArrayList<RaceMessage>());
        final TrackState track = new TrackState();
        final AlertGate gate = new AlertGate();
        final List<String> alarms = Collections.synchronizedList(new ArrayList<String>());
        final boolean[] opened = new boolean[1];
        final int[] trackLevel = new int[1];
        final String[] err = new String[1];

        HaWebSocket.Listener lis = new HaWebSocket.Listener() {
            public void onOpen() {
                opened[0] = true;
            }

            public void onState(RaceMessage m) {
                got.add(m);
                track.onMessage(m);
                int lv = track.level();
                if (lv > trackLevel[0]) {
                    trackLevel[0] = lv;
                }
                AlertGate.Action a = gate.onMessage(m, System.currentTimeMillis());
                if (a != null && a.severity >= Classifier.ALARM) {
                    alarms.add(a.kind);
                }
            }

            public void onError(String message) {
                if (err[0] == null) {
                    err[0] = message;
                }
            }

            public void onClose() {
            }
        };

        final HaWebSocket ws = new HaWebSocket(p, lis);
        Thread t = new Thread(new Runnable() {
            public void run() {
                ws.runAndReport();
            }
        }, "e2e-ws");
        t.setDaemon(true);
        t.start();

        long deadline = System.currentTimeMillis() + seconds * 1000L;
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(200L);
        }
        ws.shutdown();
        t.join(3000L);

        info("收到事件 " + got.size() + " 条；连接状态 opened=" + opened[0]
                + "；最高赛道状态=" + track.label());
        if (err[0] != null) {
            info("通道报错：" + err[0]);
        }

        check("完成了 auth 握手并订阅成功（onOpen）", opened[0], null);
        check("收到了实时事件", got.size() > 0, got.size() + " 条");
        if (!got.isEmpty()) {
            RaceMessage f = got.get(0);
            info("首条事件：flag=" + f.flag + " category=" + f.category
                    + " sector=" + f.sector + " seq=" + f.sequence + " text=" + f.text());
            check("事件里带了 attributes（WebSocket 路径也要能解析属性）",
                    f.category.length() > 0 || f.flag.length() > 0, null);
            check("事件带 sequence（丢号检测依赖它）", f.sequence > 0, "seq=" + f.sequence);
        }

        // 剧本模式下断言状态机走到了预期级别
        if ("red_flag".equals(System.getProperty("mock.scenario", ""))) {
            check("红旗剧本来到了 RED 级别",
                    trackLevel[0] >= TrackState.RED, "level=" + trackLevel[0]);
        } else if ("vsc".equals(System.getProperty("mock.scenario", ""))) {
            check("VSC 剧本来到了 VSC 级别",
                    trackLevel[0] >= TrackState.VSC, "level=" + trackLevel[0]);
        } else if ("safety_car".equals(System.getProperty("mock.scenario", ""))) {
            check("安全车剧本来到了 SC 级别",
                    trackLevel[0] >= TrackState.SC, "level=" + trackLevel[0]);
        }

        check("实时通道没有报错", err[0] == null, err[0]);
    }

    private static void bump(Map<String, Integer> m, String k) {
        Integer v = m.get(k);
        m.put(k, Integer.valueOf(v == null ? 1 : v.intValue() + 1));
    }
}
