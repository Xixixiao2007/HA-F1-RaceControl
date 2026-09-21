package com.haf1.racecontrol;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 赛道当前状态机 —— 供顶部状态条使用。
 *
 * ## 优先级（这是用户明确提出的需求）
 * 用户说：「非比赛状态（红旗及安全车）下优先显示红旗安全车」。
 *
 * 实测表明这**不是**降噪手段：红旗期间双黄 0 条、VSC 期间只有 1 条，
 * 整个周末只有 1 条消息能被这类规则抑制（1%）。所以它是一条**显示优先级**规则：
 *
 *      RED(6) > SC(5) > VSC(4) > DOUBLE YELLOW(3) > YELLOW(2) > GREEN(1) > 无(0)
 *
 * 高优先级状态生效期间，状态条锁定显示它，不被随后涌入的双黄消息覆盖。
 * 解除信号是 `TRACK CLEAR`（flag=CLEAR 且 scope=Track）或 `GREEN LIGHT`。
 * 实测的红旗窗口：18:53:19 RED FLAG → 18:56:17 TRACK CLEAR，约 3 分钟。
 */
public class TrackState {

    public static final int NONE = 0;
    public static final int GREEN = 1;
    public static final int YELLOW = 2;
    public static final int DY = 3;
    public static final int VSC = 4;
    public static final int SC = 5;
    public static final int RED = 6;

    /** 全局状态：NONE / GREEN / VSC / SC / RED。 */
    private int global = NONE;
    /** 区段级旗语：区段号 -> YELLOW(2) 或 DY(3)。 */
    private final Map<Integer, Integer> sectors = new HashMap<Integer, Integer>();
    private long updatedAt = 0L;

    public void reset() {
        global = NONE;
        sectors.clear();
        updatedAt = 0L;
    }

    /** 喂一条消息进去，更新状态。 */
    public void onMessage(RaceMessage m) {
        if (m == null || m.isUnavailable()) {
            return;
        }
        String kind = Classifier.kind(m);
        String scope = m.scope == null ? "" : m.scope.trim();
        int sec = m.sectorNo();
        updatedAt = m.time;

        if (Classifier.K_RED.equals(kind)) {
            global = RED;
            sectors.clear();
            return;
        }
        if (Classifier.K_SC.equals(kind)) {
            global = SC;
            return;
        }
        if (Classifier.K_VSC.equals(kind)) {
            global = VSC;
            return;
        }
        if (Classifier.K_SC_END.equals(kind)) {
            // "SAFETY CAR IN THIS LAP" / "VSC ENDING" —— 安全车还在，只是即将结束。
            // 不能在这里清掉全局状态，真正的解除信号是 TRACK CLEAR。
            return;
        }
        if (Classifier.K_CLEAR.equals(kind)) {
            if ("TRACK".equals(scope.toUpperCase(Locale.US))) {
                // TRACK CLEAR：整条赛道解除
                global = NONE;
                sectors.clear();
            } else if (sec > 0) {
                sectors.remove(Integer.valueOf(sec));
            }
            return;
        }
        if (Classifier.K_DY.equals(kind)) {
            if (sec > 0) {
                sectors.put(Integer.valueOf(sec), Integer.valueOf(DY));
            }
            return;
        }
        if (Classifier.K_YELLOW.equals(kind)) {
            if (sec > 0 && !Integer.valueOf(DY).equals(sectors.get(Integer.valueOf(sec)))) {
                sectors.put(Integer.valueOf(sec), Integer.valueOf(YELLOW));
            }
            return;
        }
        if (Classifier.K_GREEN.equals(kind)) {
            // 绿灯（出场口开放）意味着上一轮的红旗/安全车已经结束
            if (global == RED || global == SC || global == VSC || global == NONE) {
                global = GREEN;
            }
            sectors.clear();
        }
    }

    /** 当前应当显示的最高级别。 */
    public int level() {
        int lvl = global;
        for (Integer v : sectors.values()) {
            if (v.intValue() > lvl) {
                lvl = v.intValue();
            }
        }
        return lvl;
    }

    public int globalLevel() {
        return global;
    }

    /** 当前双黄的区段列表（已排序）。 */
    public List<Integer> doubleYellowSectors() {
        List<Integer> out = new ArrayList<Integer>();
        for (Map.Entry<Integer, Integer> e : sectors.entrySet()) {
            if (e.getValue().intValue() == DY) {
                out.add(e.getKey());
            }
        }
        Collections.sort(out);
        return out;
    }

    public List<Integer> yellowSectors() {
        List<Integer> out = new ArrayList<Integer>();
        for (Map.Entry<Integer, Integer> e : sectors.entrySet()) {
            if (e.getValue().intValue() == YELLOW) {
                out.add(e.getKey());
            }
        }
        Collections.sort(out);
        return out;
    }

    public long updatedAt() {
        return updatedAt;
    }

    /** 状态条的标题文字。 */
    public String label() {
        int lvl = level();
        if (lvl == RED) {
            return "红旗";
        }
        if (lvl == SC) {
            return "安全车";
        }
        if (lvl == VSC) {
            return "虚拟安全车";
        }
        if (lvl == DY) {
            return "双黄旗";
        }
        if (lvl == YELLOW) {
            return "黄旗";
        }
        if (lvl == GREEN) {
            return "绿旗";
        }
        return "无旗语";
    }

    /**
     * 标题栏的补充说明（旗语旁边的浅色小字）。
     *
     * ★ 这里原来有三条**英文**：`SESSION SUSPENDED` / `SAFETY CAR` /
     *   `VIRTUAL SAFETY CAR` —— 中文界面上最要紧的红旗状态，旁边跟一串英文。
     *   现在全中文：
     *     · 红旗 -> 比赛暂停（SESSION 就是**这一个比赛环节**，不是「会话」）
     *     · 安全车 / VSC -> 全场（和双黄旗的「区段 12,13」形成对照：
     *       整条赛道 vs 某几个区段）
     */
    public String detail() {
        int lvl = level();
        if (lvl == NONE || lvl == GREEN) {
            return "赛道正常";
        }
        if (lvl == RED) {
            return "比赛暂停";
        }
        if (lvl == SC || lvl == VSC) {
            return "全场";
        }
        List<Integer> list = (lvl == DY) ? doubleYellowSectors() : yellowSectors();
        if (list.isEmpty()) {
            return "";
        }
        StringBuilder sb = new StringBuilder("区段 ");
        for (int i = 0; i < list.size() && i < 12; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(list.get(i).intValue());
        }
        if (list.size() > 12) {
            sb.append("…共 ").append(list.size()).append(" 个");
        }
        return sb.toString();
    }

    /** 状态条底色。 */
    public int color() {
        int lvl = level();
        if (lvl == RED) {
            return 0xFFD32F2F;
        }
        if (lvl == SC) {
            return 0xFFF57C00;
        }
        if (lvl == VSC) {
            return 0xFFF9A825;
        }
        if (lvl == DY) {
            return 0xFFFFEB3B;
        }
        if (lvl == YELLOW) {
            return 0xFFFFF176;
        }
        if (lvl == GREEN) {
            return 0xFF388E3C;
        }
        return 0xFF455A64;
    }

    /** 状态条上文字的颜色（浅底用深字）。 */
    public int textColor() {
        int lvl = level();
        if (lvl == DY || lvl == YELLOW || lvl == VSC) {
            return 0xFF212121;
        }
        return 0xFFFFFFFF;
    }
}
