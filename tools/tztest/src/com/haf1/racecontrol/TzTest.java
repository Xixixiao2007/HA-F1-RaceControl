package com.haf1.racecontrol;

import java.util.List;
import java.util.TimeZone;

import javax.xml.datatype.DatatypeFactory;

/**
 * 桌面端单测。
 *
 * 用桌面 JDK 编译**真实的产品源码**（HaClient / Prefs / RaceMessage / Classifier /
 * AlertGate / TrackState / MessageStore / WsFrame），配合 tztest/stub 下的极简
 * android.* / org.json 桩，所以不需要 Android 设备，也不需要模拟器。
 *
 * 覆盖范围里最有价值的几条，都是被真实比赛数据或实测教训逼出来的：
 *   - 安全车/VSC 没有 flag，只能靠 category（照抄文档会写出永不触发的判断）
 *   - 旗语是 "BLACK AND WHITE" 一个值，不是 BLACK / WHITE
 *   - 双黄旗必须聚类 + 判"测试型"，否则一个周末响 146 次
 *   - 历史接口的期初状态必须丢弃（否则同一条消息显示两遍）
 */
public class TzTest {

    private static int fail = 0;
    private static int pass = 0;

    private static void eq(String label, Object got, Object want) {
        boolean ok = (got == null) ? (want == null) : got.equals(want);
        if (ok) {
            pass++;
        } else {
            fail++;
        }
        System.out.println((ok ? "  OK   " : "  FAIL ") + label
                + "\n         got  = " + got + "\n         want = " + want);
    }

    private static void section(String title) {
        System.out.println();
        System.out.println("== " + title + " ==");
    }

    /** 造一条消息：flag / category / sector / 文本，时间固定自增。 */
    private static long clock = 1700000000000L;

    private static RaceMessage mk(String flag, String cat, String sector, String text) {
        clock += 1000L;
        return new RaceMessage(clock, text, "raw-" + clock, text, cat, flag,
                sector.length() > 0 ? "Sector" : "Track", sector, "", "", "ev-" + clock, 0);
    }

    private static RaceMessage mkSeq(String flag, String cat, String sector, String text,
                                     String eventId, int seq) {
        clock += 1000L;
        return new RaceMessage(clock, text, "raw-" + clock, text, cat, flag,
                sector.length() > 0 ? "Sector" : "Track", sector, "", "", eventId, seq);
    }

    private static byte[] serverFrame(byte[] payload) {
        java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
        b.write(0x80 | WsFrame.OP_TEXT);
        int len = payload.length;
        if (len < 126) {
            b.write(len);
        } else if (len <= 0xFFFF) {
            b.write(126);
            b.write((len >>> 8) & 0xFF);
            b.write(len & 0xFF);
        } else {
            b.write(127);
            long l = len;
            for (int i = 7; i >= 0; i--) {
                b.write((int) ((l >>> (8 * i)) & 0xFF));
            }
        }
        b.write(payload, 0, len);
        return b.toByteArray();
    }

    public static void main(String[] args) throws Exception {
        DatatypeFactory df = DatatypeFactory.newInstance();

        // ================================================================
        // 1) ISO8601 解析（手写实现，必须和标准实现逐毫秒一致）
        // ================================================================
        section("parseIso：与 javax.xml.datatype 参照实现逐个比对");
        String[] samples = {
                "2026-09-05T14:23:05.123456+08:00",
                "2026-09-05T14:23:05+08:00",
                "2026-09-05T06:23:05.123456+00:00",
                "2026-09-05T06:23:05Z",
                "2026-09-05T06:23:05.123456Z",
                "2026-09-05T01:23:05-05:00",
                "2026-01-01T00:00:00+00:00",
                "2026-12-31T23:59:59+08:00",
                "2026-09-05T06:23:05.5Z",
                "2026-09-05T06:23:05.987654Z",
                "2026-09-05T06:23:05.05Z",
        };
        for (int i = 0; i < samples.length; i++) {
            String s = samples[i];
            long want = df.newXMLGregorianCalendar(s).toGregorianCalendar().getTimeInMillis();
            eq(s, Long.valueOf(HaClient.parseIso(s)), Long.valueOf(want));
        }

        section("isoLocal / 往返一致性");
        long t = df.newXMLGregorianCalendar("2026-09-05T14:23:05+08:00")
                .toGregorianCalendar().getTimeInMillis();
        TimeZone.setDefault(TimeZone.getTimeZone("GMT+08:00"));
        eq("isoLocal @GMT+08:00", HaClient.isoLocal(t), "2026-09-05T14:23:05+08:00");
        eq("round-trip @GMT+08:00", Long.valueOf(HaClient.parseIso(HaClient.isoLocal(t))),
                Long.valueOf(t));
        TimeZone.setDefault(TimeZone.getTimeZone("GMT-05:00"));
        eq("isoLocal @GMT-05:00", HaClient.isoLocal(t), "2026-09-05T01:23:05-05:00");
        TimeZone.setDefault(TimeZone.getTimeZone("GMT+05:30"));
        eq("isoLocal @GMT+05:30（半小时偏移）", HaClient.isoLocal(t),
                "2026-09-05T11:53:05+05:30");

        section("健壮性（坏输入返回 0，不抛异常）");
        String[] bad = {"", "not-a-date", "2026-09-05", null};
        for (int i = 0; i < bad.length; i++) {
            long got;
            try {
                got = HaClient.parseIso(bad[i]);
            } catch (Exception e) {
                got = -999;
            }
            eq("parseIso(" + bad[i] + ")", Long.valueOf(got), Long.valueOf(0L));
        }

        // ================================================================
        // 2) 历史接口的「期初状态」必须被丢弃
        // ================================================================
        section("HaClient.isSyntheticStartState");
        long reqStart = HaClient.parseIso("2026-09-11T12:20:53+00:00");
        long stamped = HaClient.parseIso("2026-09-11T12:20:53+00:00");
        long realPrev = HaClient.parseIso("2026-09-11T12:19:46.072085+00:00");
        eq("首条 + 完整字段 + 时间==请求起点 -> 丢弃",
                Boolean.valueOf(HaClient.isSyntheticStartState(0, stamped, reqStart, true)),
                Boolean.TRUE);
        eq("时间不等于请求起点 -> 保留",
                Boolean.valueOf(HaClient.isSyntheticStartState(0, realPrev, reqStart, true)),
                Boolean.FALSE);
        eq("不是首条 -> 不丢（末条也带完整字段，但它是真实变化）",
                Boolean.valueOf(HaClient.isSyntheticStartState(1, stamped, reqStart, true)),
                Boolean.FALSE);
        eq("不带 entity_id -> 不丢",
                Boolean.valueOf(HaClient.isSyntheticStartState(0, stamped, reqStart, false)),
                Boolean.FALSE);

        // ================================================================
        // 3) 旗语分类 —— 重点是两个"照抄文档就会写错"的坑
        // ================================================================
        section("Classifier.kind：基本旗语");
        eq("RED", Classifier.kind(mk("RED", "Flag", "", "RED FLAG")), Classifier.K_RED);
        eq("DOUBLE YELLOW", Classifier.kind(mk("DOUBLE YELLOW", "Flag", "12",
                "DOUBLE YELLOW IN TRACK SECTOR 12")), Classifier.K_DY);
        eq("YELLOW", Classifier.kind(mk("YELLOW", "Flag", "7",
                "YELLOW IN TRACK SECTOR 7")), Classifier.K_YELLOW);
        eq("CLEAR", Classifier.kind(mk("CLEAR", "Flag", "7",
                "CLEAR IN TRACK SECTOR 7")), Classifier.K_CLEAR);
        eq("GREEN", Classifier.kind(mk("GREEN", "Flag", "",
                "GREEN LIGHT - PIT EXIT OPEN")), Classifier.K_GREEN);
        eq("CHEQUERED", Classifier.kind(mk("CHEQUERED", "Flag", "",
                "CHEQUERED FLAG")), Classifier.K_CHEQUERED);
        eq("BLUE", Classifier.kind(mk("BLUE", "Flag", "",
                "WAVED BLUE FLAG FOR CAR 77 (BOT) TIMED AT 16:08:37")), Classifier.K_BLUE);

        section("坑 A：安全车 / VSC 没有 flag，只能靠 category");
        eq("VSC DEPLOYED（flag 为空）",
                Classifier.kind(mk("", "SafetyCar", "", "VSC DEPLOYED")), Classifier.K_VSC);
        eq("VSC ENDING（flag 为空）",
                Classifier.kind(mk("", "SafetyCar", "", "VSC ENDING")), Classifier.K_SC_END);
        eq("SAFETY CAR DEPLOYED（flag 为空）",
                Classifier.kind(mk("", "SafetyCar", "", "SAFETY CAR DEPLOYED")), Classifier.K_SC);
        eq("SAFETY CAR IN THIS LAP（flag 为空）",
                Classifier.kind(mk("", "SafetyCar", "", "SAFETY CAR IN THIS LAP")),
                Classifier.K_SC_END);
        eq("category 也缺失时的文本兜底",
                Classifier.kind(mk("", "", "", "VSC DEPLOYED")), Classifier.K_VSC);
        eq("VIRTUAL SAFETY CAR 也应识别为 VSC",
                Classifier.kind(mk("", "", "", "VIRTUAL SAFETY CAR DEPLOYED")),
                Classifier.K_VSC);

        section("坑 B：旗语是 BLACK AND WHITE 一个值");
        eq("BLACK AND WHITE",
                Classifier.kind(mk("BLACK AND WHITE", "Flag", "",
                        "BLACK AND WHITE FLAG FOR CAR 55 (SAI) - TRACK LIMITS")),
                Classifier.K_BW);
        eq("兼容文档里那个错的 BLACK 取值",
                Classifier.kind(mk("BLACK", "Flag", "", "BLACK FLAG")), Classifier.K_BW);
        eq("兼容文档里那个错的 WHITE 取值",
                Classifier.kind(mk("WHITE", "Flag", "", "WHITE FLAG")), Classifier.K_BW);

        section("其它类别");
        eq("仲裁判罚", Classifier.kind(mk("", "Other", "",
                "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 55 (SAI)")),
                Classifier.K_PENALTY);
        eq("普通仲裁", Classifier.kind(mk("", "Other", "",
                "TURN 1 INCIDENT INVOLVING CAR 5 (BOR) NOTED")), Classifier.K_OTHER);

        section("Classifier.immediateSeverity");
        eq("红旗 = 立即强提醒",
                Integer.valueOf(Classifier.immediateSeverity(Classifier.K_RED)),
                Integer.valueOf(Classifier.ALARM));
        eq("安全车 = 立即强提醒",
                Integer.valueOf(Classifier.immediateSeverity(Classifier.K_SC)),
                Integer.valueOf(Classifier.ALARM));
        eq("VSC = 立即强提醒",
                Integer.valueOf(Classifier.immediateSeverity(Classifier.K_VSC)),
                Integer.valueOf(Classifier.ALARM));
        eq("双黄 = 先轻提醒（要靠存活时长决定是否升级）",
                Integer.valueOf(Classifier.immediateSeverity(Classifier.K_DY)),
                Integer.valueOf(Classifier.ATTENTION));
        eq("CLEAR = 只闪动",
                Integer.valueOf(Classifier.immediateSeverity(Classifier.K_CLEAR)),
                Integer.valueOf(Classifier.FLASH));

        // ================================================================
        // 4) 提醒闸门 —— 整个 App 最关键的降噪逻辑
        // ================================================================
        section("AlertGate：红旗/安全车立刻强提醒，不受冷却期限制");
        AlertGate g = new AlertGate();
        AlertGate.Action a1 = g.onMessage(mk("RED", "Flag", "", "RED FLAG"), 1000000L);
        eq("红旗 -> ALARM", Integer.valueOf(a1.severity), Integer.valueOf(Classifier.ALARM));
        AlertGate.Action a2 = g.onMessage(
                mk("RED", "Flag", "", "RED FLAG"), 1001000L);
        eq("1 秒后的第二条红旗仍然 ALARM（红旗不受冷却期限制）",
                Integer.valueOf(a2.severity), Integer.valueOf(Classifier.ALARM));

        AlertGate g2 = new AlertGate();
        eq("VSC DEPLOYED -> ALARM",
                Integer.valueOf(g2.onMessage(mk("", "SafetyCar", "", "VSC DEPLOYED"),
                        2000000L).severity), Integer.valueOf(Classifier.ALARM));

        section("AlertGate：双黄先轻提醒，存活够久才升级；测试型不升级");
        AlertGate g3 = new AlertGate();
        AlertGate.Action dy = g3.onMessage(
                mk("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW IN TRACK SECTOR 12"),
                3000000L);
        eq("双黄立刻给 ATTENTION（不是 ALARM）",
                Integer.valueOf(dy.severity), Integer.valueOf(Classifier.ATTENTION));
        eq("升级前 onTick 无动作", Integer.valueOf(g3.onTick(3000000L + 5000L).size()),
                Integer.valueOf(0));
        eq("超过阈值后 onTick 产出一次 ALARM",
                Integer.valueOf(g3.onTick(3000000L + 16000L).size()), Integer.valueOf(1));
        eq("同一次升级只发生一次（再 tick 不重复）",
                Integer.valueOf(g3.onTick(3000000L + 20000L).size()), Integer.valueOf(0));

        AlertGate g4 = new AlertGate();
        g4.onMessage(mk("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW IN TRACK SECTOR 12"),
                4000000L);
        g4.onMessage(mk("CLEAR", "Flag", "12", "CLEAR IN TRACK SECTOR 12"), 4003000L);
        eq("3 秒就被 CLEAR 的双黄 = 测试型，不升级",
                Integer.valueOf(g4.onTick(4000000L + 30000L).size()), Integer.valueOf(0));

        section("AlertGate：双黄方案 B（仅 >=3 扇区才升级）");
        AlertGate g5 = new AlertGate();
        g5.dyMinSectors = 3;
        g5.onMessage(mk("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW IN TRACK SECTOR 12"),
                5000000L);
        eq("只有 1 个扇区 -> 不升级",
                Integer.valueOf(g5.onTick(5000000L + 16000L).size()), Integer.valueOf(0));

        AlertGate g6 = new AlertGate();
        g6.dyMinSectors = 3;
        g6.onMessage(mk("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW IN TRACK SECTOR 12"),
                6000000L);
        g6.onMessage(mk("DOUBLE YELLOW", "Flag", "13", "DOUBLE YELLOW IN TRACK SECTOR 13"),
                6000500L);
        g6.onMessage(mk("DOUBLE YELLOW", "Flag", "14", "DOUBLE YELLOW IN TRACK SECTOR 14"),
                6001000L);
        eq("3 个扇区同时双黄 -> 升级",
                Integer.valueOf(g6.onTick(6000000L + 16000L).size()), Integer.valueOf(1));

        section("AlertGate：高状态出现后，等待中的双黄不再有意义");
        AlertGate g7 = new AlertGate();
        g7.onMessage(mk("DOUBLE YELLOW", "Flag", "5", "DOUBLE YELLOW IN TRACK SECTOR 5"),
                7000000L);
        g7.onMessage(mk("RED", "Flag", "", "RED FLAG"), 7001000L);
        eq("红旗一出，待升级的双黄被清掉",
                Integer.valueOf(g7.onTick(7000000L + 30000L).size()), Integer.valueOf(0));

        section("AlertGate：轻提醒的冷却期");
        AlertGate g8 = new AlertGate();
        g8.cooldownMs = 60000L;
        eq("第一条黄旗给 ATTENTION",
                Integer.valueOf(g8.onMessage(mk("YELLOW", "Flag", "7",
                        "YELLOW IN TRACK SECTOR 7"), 8000000L).severity),
                Integer.valueOf(Classifier.ATTENTION));
        eq("10 秒后第二条黄旗被冷却期压掉",
                g8.onMessage(mk("YELLOW", "Flag", "8", "YELLOW IN TRACK SECTOR 8"),
                        8010000L), null);
        eq("70 秒后恢复", Integer.valueOf(g8.onMessage(
                mk("YELLOW", "Flag", "9", "YELLOW IN TRACK SECTOR 9"), 8070000L).severity),
                Integer.valueOf(Classifier.ATTENTION));

        section("AlertGate：双黄关闭后完全不提醒");
        AlertGate g9 = new AlertGate();
        g9.dyEnabled = false;
        eq("关闭双黄 -> 不提醒",
                g9.onMessage(mk("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW"), 9000000L), null);
        eq("关闭双黄 -> 也不升级",
                Integer.valueOf(g9.onTick(9000000L + 30000L).size()), Integer.valueOf(0));

        // ================================================================
        // 5) 赛道状态机（优先级显示）
        // ================================================================
        section("TrackState：优先级 RED > SC > VSC > 双黄 > 黄");
        TrackState ts = new TrackState();
        eq("初始无旗语", Integer.valueOf(ts.level()), Integer.valueOf(TrackState.NONE));
        ts.onMessage(mk("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW IN TRACK SECTOR 12"));
        eq("双黄", Integer.valueOf(ts.level()), Integer.valueOf(TrackState.DY));
        ts.onMessage(mk("", "SafetyCar", "", "VSC DEPLOYED"));
        eq("VSC 盖过双黄", Integer.valueOf(ts.level()), Integer.valueOf(TrackState.VSC));
        ts.onMessage(mk("", "SafetyCar", "", "VSC DEPLOYED"));
        ts.onMessage(mk("", "SafetyCar", "", "SAFETY CAR DEPLOYED"));
        eq("安全车盖过 VSC", Integer.valueOf(ts.level()), Integer.valueOf(TrackState.SC));
        ts.onMessage(mk("RED", "Flag", "", "RED FLAG"));
        eq("红旗盖过安全车", Integer.valueOf(ts.level()), Integer.valueOf(TrackState.RED));
        eq("红旗期间不给后续双黄抢走显示",
                Boolean.valueOf(ts.level() == TrackState.RED), Boolean.TRUE);

        section("TrackState：CLEAR 之后回落");
        TrackState ts2 = new TrackState();
        ts2.onMessage(mk("YELLOW", "Flag", "5", "YELLOW IN TRACK SECTOR 5"));
        ts2.onMessage(mk("DOUBLE YELLOW", "Flag", "4", "DOUBLE YELLOW IN TRACK SECTOR 4"));
        eq("两个扇区 -> 显示双黄", Integer.valueOf(ts2.level()), Integer.valueOf(TrackState.DY));
        eq("扇区 4 在双黄列表里",
                Boolean.valueOf(ts2.doubleYellowSectors().contains(Integer.valueOf(4))),
                Boolean.TRUE);
        ts2.onMessage(mk("CLEAR", "Flag", "4", "CLEAR IN TRACK SECTOR 4"));
        eq("清掉双黄扇区后回落到黄",
                Integer.valueOf(ts2.level()), Integer.valueOf(TrackState.YELLOW));
        ts2.onMessage(new RaceMessage(1L, "TRACK CLEAR", "r", "TRACK CLEAR", "Flag",
                "CLEAR", "Track", "", "", "", "e", 0));
        eq("TRACK CLEAR -> 全清", Integer.valueOf(ts2.level()), Integer.valueOf(TrackState.NONE));

        section("TrackState：安全车结束信号不应清掉状态");
        TrackState ts3 = new TrackState();
        ts3.onMessage(mk("", "SafetyCar", "", "SAFETY CAR DEPLOYED"));
        ts3.onMessage(mk("", "SafetyCar", "", "SAFETY CAR IN THIS LAP"));
        eq("IN THIS LAP 时安全车仍在",
                Integer.valueOf(ts3.level()), Integer.valueOf(TrackState.SC));

        // ================================================================
        // 6) 去重与存储
        // ================================================================
        section("RaceMessage.key：优先 event_id");
        RaceMessage k1 = mkSeq("YELLOW", "Flag", "7", "YELLOW IN TRACK SECTOR 7", "EV-1", 1);
        RaceMessage k2 = mkSeq("YELLOW", "Flag", "7", "YELLOW IN TRACK SECTOR 7", "EV-1", 1);
        eq("同 event_id -> 同键", k1.key(), k2.key());
        RaceMessage k3 = mkSeq("YELLOW", "Flag", "7", "YELLOW IN TRACK SECTOR 7", "EV-2", 2);
        eq("不同 event_id -> 不同键（即使文本完全相同）",
                Boolean.valueOf(!k1.key().equals(k3.key())), Boolean.TRUE);

        section("MessageStore：去重 / 排序 / 容量");
        MessageStore st = new MessageStore();
        eq("第一条是新记录",
                Boolean.valueOf(st.add(mkSeq("YELLOW", "Flag", "7", "YELLOW IN TRACK SECTOR 7",
                        "EV-A", 1))), Boolean.TRUE);
        eq("同 event_id 再来一次不算新",
                Boolean.valueOf(st.add(mkSeq("YELLOW", "Flag", "7", "YELLOW IN TRACK SECTOR 7",
                        "EV-A", 1))), Boolean.FALSE);
        eq("一条消息只占一个位置", Integer.valueOf(st.size()), Integer.valueOf(1));
        st.add(mkSeq("CLEAR", "Flag", "7", "CLEAR IN TRACK SECTOR 7", "EV-B", 2));
        eq("最新在最上",
                st.sortedDesc().get(0).text(), "CLEAR IN TRACK SECTOR 7");
        st.add(mkSeq("YELLOW", "Flag", "8", "YELLOW IN TRACK SECTOR 8", "EV-C", 3));
        st.add(mkSeq("YELLOW", "Flag", "9", "YELLOW IN TRACK SECTOR 9", "EV-D", 4));
        st.trimTo(2);
        eq("容量上限生效", Integer.valueOf(st.size()), Integer.valueOf(2));

        section("MessageStore：序列化往返");
        MessageStore st2 = new MessageStore();
        st2.add(mkSeq("DOUBLE YELLOW", "Flag", "12", "DOUBLE YELLOW IN TRACK SECTOR 12",
                "EV-X", 11));
        st2.add(mkSeq("", "SafetyCar", "", "VSC DEPLOYED", "EV-Y", 12));
        String blob = st2.serialize();
        MessageStore st3 = new MessageStore();
        st3.load(blob);
        eq("往返后条数一致", Integer.valueOf(st3.size()), Integer.valueOf(st2.size()));
        List<RaceMessage> back = st3.sortedDesc();
        eq("往返后最新一条的文本一致", back.get(0).text(), "VSC DEPLOYED");
        eq("往返后 category 保留", back.get(0).category, "SafetyCar");
        eq("往返后 flag 保留", back.get(1).flag, "DOUBLE YELLOW");
        eq("往返后 sector 保留", back.get(1).sector, "12");

        // ================================================================
        // 7) 过滤
        // ================================================================
        section("Prefs.isNoise：噪音判定（实测占全部消息 57.7%）");
        eq("蓝旗是噪音",
                Boolean.valueOf(Prefs.isNoise(mk("BLUE", "Flag", "",
                        "WAVED BLUE FLAG FOR CAR 77 (BOT)"))), Boolean.TRUE);
        eq("CLEAR 是噪音",
                Boolean.valueOf(Prefs.isNoise(mk("CLEAR", "Flag", "7",
                        "CLEAR IN TRACK SECTOR 7"))), Boolean.TRUE);
        eq("超赛道限制删圈速是噪音",
                Boolean.valueOf(Prefs.isNoise(mk("", "Other", "",
                        "CAR 55 (SAI) TIME 1:43.523 DELETED - TRACK LIMITS AT TURN 15"))),
                Boolean.TRUE);
        eq("仲裁查完没事是噪音",
                Boolean.valueOf(Prefs.isNoise(mk("", "Other", "",
                        "FIA STEWARDS: TURN 1 INCIDENT REVIEWED NO FURTHER INVESTIGATION"))),
                Boolean.TRUE);
        eq("红旗不是噪音",
                Boolean.valueOf(Prefs.isNoise(mk("RED", "Flag", "", "RED FLAG"))), Boolean.FALSE);
        eq("双黄旗不是噪音",
                Boolean.valueOf(Prefs.isNoise(mk("DOUBLE YELLOW", "Flag", "12",
                        "DOUBLE YELLOW IN TRACK SECTOR 12"))), Boolean.FALSE);

        section("Prefs.accept：过滤开关与车号筛选");
        Prefs p = new Prefs();
        p.noiseFilterEnabled = true;
        eq("开启过滤时蓝旗被隐藏",
                Boolean.valueOf(p.accept(mk("BLUE", "Flag", "", "WAVED BLUE FLAG"))),
                Boolean.FALSE);
        p.noiseFilterEnabled = false;
        eq("关闭过滤时蓝旗可见",
                Boolean.valueOf(p.accept(mk("BLUE", "Flag", "", "WAVED BLUE FLAG"))),
                Boolean.TRUE);
        p.carFilter = "44";
        eq("车号 44 命中",
                Boolean.valueOf(p.accept(mkSeq("BLUE", "Flag", "", "WAVED BLUE FLAG",
                        "E1", 1))), Boolean.FALSE);
        RaceMessage car44 = new RaceMessage(1L, "X", "r", "X", "Flag", "BLACK AND WHITE",
                "Driver", "", "44", "", "E2", 2);
        eq("车号 44 的记录保留", Boolean.valueOf(p.accept(car44)), Boolean.TRUE);
        RaceMessage car55 = new RaceMessage(1L, "X", "r", "X", "Flag", "BLACK AND WHITE",
                "Driver", "", "55", "", "E3", 3);
        eq("车号 55 被筛掉", Boolean.valueOf(p.accept(car55)), Boolean.FALSE);
        p.carFilter = "";
        eq("清空车号筛选后都放行",
                Boolean.valueOf(p.accept(mkSeq("YELLOW", "Flag", "3",
                        "YELLOW IN TRACK SECTOR 3", "E4", 4))), Boolean.TRUE);

        section("Prefs.clampPoll 刷新间隔钳制");
        eq("0 -> 下限", Integer.valueOf(Prefs.clampPoll(0)), Integer.valueOf(Prefs.POLL_MIN));
        eq("负数 -> 下限", Integer.valueOf(Prefs.clampPoll(-5)), Integer.valueOf(Prefs.POLL_MIN));
        eq("1 -> 保持", Integer.valueOf(Prefs.clampPoll(1)), Integer.valueOf(1));
        eq("9999 -> 上限", Integer.valueOf(Prefs.clampPoll(9999)),
                Integer.valueOf(Prefs.POLL_MAX));

        section("Prefs.applyTo：配置正确灌进闸门");
        Prefs p2 = new Prefs();
        p2.dyMode = Prefs.DY_ESCALATE_BIG;
        p2.cooldownSec = 30;
        p2.dyEscalateSec = 20;
        AlertGate g10 = new AlertGate();
        p2.applyTo(g10);
        eq("方案 B 启用扇区阈值 3", Integer.valueOf(g10.dyMinSectors), Integer.valueOf(3));
        eq("冷却期换算成毫秒", Long.valueOf(g10.cooldownMs), Long.valueOf(30000L));
        eq("升级阈值换算成毫秒", Long.valueOf(g10.dyEscalateMs), Long.valueOf(20000L));
        p2.dyMode = Prefs.DY_OFF;
        p2.applyTo(g10);
        eq("关闭双黄", Boolean.valueOf(g10.dyEnabled), Boolean.FALSE);

        // ================================================================
        // 8) WebSocket 帧编解码
        // ================================================================
        section("WsFrame 帧编解码（RFC 6455）");
        byte[] mask = new byte[] {0x37, (byte) 0xfa, 0x21, 0x3d};
        String msg = "{\"type\":\"auth\"}";
        byte[] raw = msg.getBytes("UTF-8");
        byte[] frame = WsFrame.encode(WsFrame.OP_TEXT, raw, mask);
        eq("客户端帧 FIN=1", Boolean.valueOf((frame[0] & 0x80) != 0), Boolean.TRUE);
        eq("客户端帧 opcode=TEXT", Integer.valueOf(frame[0] & 0x0F),
                Integer.valueOf(WsFrame.OP_TEXT));
        eq("客户端帧必须带 MASK 位", Boolean.valueOf((frame[1] & 0x80) != 0), Boolean.TRUE);
        int[] sizes = {0, 5, 125, 126, 1000, 65535, 65536, 70000};
        for (int i = 0; i < sizes.length; i++) {
            byte[] payload = new byte[sizes[i]];
            for (int k = 0; k < payload.length; k++) {
                payload[k] = (byte) (k & 0xFF);
            }
            WsFrame f = WsFrame.read(new java.io.ByteArrayInputStream(serverFrame(payload)));
            eq("长度 " + sizes[i] + " 解析出的字节数",
                    Integer.valueOf(f.payload.length), Integer.valueOf(sizes[i]));
        }
        boolean threw = false;
        try {
            WsFrame.read(new java.io.ByteArrayInputStream(
                    new byte[] {(byte) 0x81, 0x05, (byte) 0x61, (byte) 0x62}));
        } catch (Exception e) {
            threw = true;
        }
        eq("截断帧 -> 抛异常", Boolean.valueOf(threw), Boolean.TRUE);

        System.out.println("==================================================");
        System.out.println("  通过 " + pass + " 项，失败 " + fail + " 项");
        System.out.println("==================================================");
        if (fail > 0) {
            System.exit(1);
        }
    }
}
