package com.haf1.racecontrol;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

/**
 * 提醒闸门 —— 决定"这条消息要不要真的响"。
 *
 * 这是整个 App 最关键的逻辑，因为**字面实现用户的需求会毁掉体验**。
 * 实测（2026-09-11~13 一个比赛周末，697 条消息）：
 *
 *   每条强提醒级消息都响                146 次
 *   同一事件聚类后只响一次               29 次
 *   再滤掉"测试型双黄"                   21 次   <- 采用
 *   再要求双黄 >=3 扇区                   8 次
 *
 * 双黄旗一个周末有 142 条消息，但只对应 24 个真实事件 —— 一次事故会被扩散到
 * 几十个扇区（最极端：52 条消息 / 20 个扇区 / 持续 284 秒）。
 *
 * ## "测试型双黄"怎么判：靠存活时长，不是靠文本
 * 数据里**没有任何含 "TEST" 的消息**。但把每个扇区双黄从"亮"到"CLEAR"的时长
 * 算出来，分布是双峰的：
 *      0–5 秒    8 次   <- 系统抖动
 *      6–15 秒  21 次   <- 测试或瞬时
 *     16–60 秒  19 次   <- 局部事故
 *      1–5 分   21 次   <- 真事故
 *      > 5 分    14 次   <- 重大事故
 * 最短 1 秒。3 秒的双黄不可能是真事故。所以 "存活 <= 15 秒" 是个干净的判据。
 *
 * ## 因此的判定策略
 * 双黄旗到达时**立刻给轻提醒**（闪动是即时的，无信息损失），同时挂一个定时器；
 * 若 `dyEscalateMs` 之后仍未收到该扇区的 CLEAR，才升级为强提醒。
 * 红旗 / 安全车 / VSC 不受影响，**立刻**强提醒。
 *
 * 本类是纯逻辑（时间由调用方传入），因此可以离线单测。
 */
public class AlertGate {

    /** 一次提醒动作。 */
    public static class Action {
        public final RaceMessage msg;
        public final String kind;
        public final int severity;
        /** 是否是"持续未清除"升级而来的（界面上可以标注出来）。 */
        public final boolean escalated;

        Action(RaceMessage msg, String kind, int severity, boolean escalated) {
            this.msg = msg;
            this.kind = kind;
            this.severity = severity;
            this.escalated = escalated;
        }
    }

    // ------------------------------------------------------------------
    // 可调参数（默认值全部来自实测）
    // ------------------------------------------------------------------
    /** 间隔超过它就认为是新事件。30 秒能把 142 条双黄收敛成 29 个事件。 */
    public long clusterGapMs = 30000L;
    /** 双黄存活超过它就升级为强提醒。15 秒是实测"测试型"的上界。 */
    public long dyEscalateMs = 15000L;
    /** 同类型提醒的最小间隔，避免连环炸响。 */
    public long cooldownMs = 60000L;
    /** 双黄升级所需的最少扇区数。0 = 不启用（方案 A）；3 = 方案 B。 */
    public int dyMinSectors = 0;
    /** 双黄旗提醒总开关。 */
    public boolean dyEnabled = true;
    /** 轻提醒（黄旗/黑白旗）总开关。 */
    public boolean attentionEnabled = true;

    // ------------------------------------------------------------------
    // 内部状态
    // ------------------------------------------------------------------
    private static class Pending {
        RaceMessage msg;
        String sectorKey;
        long deadline;
        long clusterStart;
        boolean escalated;
    }

    /** key = 扇区；等待"是否升级为强提醒"的双黄。 */
    private final Map<String, Pending> pending = new HashMap<String, Pending>();
    private final Map<String, Long> lastAlertAt = new HashMap<String, Long>();
    private long clusterStart = 0L;

    public void reset() {
        pending.clear();
        lastAlertAt.clear();
        clusterStart = 0L;
    }

    private static String sectorKey(RaceMessage m) {
        int s = m.sectorNo();
        return s > 0 ? String.valueOf(s) : "*";
    }

    /**
     * 双黄旗「轻提醒」单独用一把冷却钥匙。
     *
     * 必须和升级用的 {@link Classifier#K_DY} 分开：双黄到达时先给一次轻提醒，
     * 15 秒后如果还没清除就要升级为强提醒 —— 如果两者共用一把钥匙，
     * 那次轻提醒刚把冷却期点上，升级就会被自己的冷却期挡掉。
     * （这个 bug 是桌面单测抓出来的，一开始没意识到。）
     */
    private static final String COOLDOWN_DY_SOFT = "DOUBLE_YELLOW_SOFT";

    private void noteCluster(long now) {
        if (now - clusterStart > clusterGapMs) {
            clusterStart = now;
        }
    }

    private boolean inCooldown(String kind, long now) {
        Long last = lastAlertAt.get(kind);
        return last != null && (now - last) < cooldownMs;
    }

    private void markAlert(String kind, long now) {
        lastAlertAt.put(kind, now);
    }

    /**
     * 处理一条新消息。返回需要立刻执行的提醒动作；返回 null 表示只入列表、不发声。
     *
     * 注意：**闪动不在这里管**。每条新消息都应该闪，那是 UI 的事。
     * 这里只决定"要不要响 / 要不要弹全屏"。
     */
    public Action onMessage(RaceMessage m, long now) {
        if (m == null || m.isUnavailable()) {
            return null;
        }
        String kind = Classifier.kind(m);
        noteCluster(now);

        // 红旗 / 安全车 / VSC 一出，正在等待升级的双黄就没意义了（已有更高级别）
        boolean topState = Classifier.K_RED.equals(kind)
                || Classifier.K_SC.equals(kind)
                || Classifier.K_VSC.equals(kind);
        if (topState) {
            pending.clear();
        }

        // ---- 双黄：立刻轻提醒，并挂定时器等升级 ----
        if (Classifier.K_DY.equals(kind)) {
            if (!dyEnabled) {
                return null;
            }
            if (m.sectorNo() > 0) {
                Pending p = new Pending();
                p.msg = m;
                p.sectorKey = sectorKey(m);
                p.deadline = now + dyEscalateMs;
                p.clusterStart = clusterStart;
                p.escalated = false;
                pending.put(p.sectorKey, p);
            }
            if (!attentionEnabled || inCooldown(COOLDOWN_DY_SOFT, now)) {
                return null;
            }
            markAlert(COOLDOWN_DY_SOFT, now);
            return new Action(m, kind, Classifier.ATTENTION, false);
        }

        // ---- CLEAR：取消该扇区的待升级；若它还没升级，说明是测试型 ----
        if (Classifier.K_CLEAR.equals(kind)) {
            if (m.sectorNo() > 0) {
                pending.remove(sectorKey(m));
            }
            return null;
        }

        int sev = Classifier.immediateSeverity(kind);
        if (sev < Classifier.ATTENTION) {
            return null;
        }
        if (Classifier.ATTENTION == sev && !attentionEnabled) {
            return null;
        }
        // 红旗不受冷却期限制 —— 实测一个周末只 3 次，每次都是大事，绝不能吞掉
        if (!Classifier.K_RED.equals(kind) && inCooldown(kind, now)) {
            return null;
        }
        markAlert(kind, now);
        return new Action(m, kind, sev, false);
    }

    /**
     * 定时推进：把"存活够久"的双黄升级为强提醒。调用方（UI）应每隔几百毫秒调一次。
     */
    public List<Action> onTick(long now) {
        List<Action> out = new ArrayList<Action>();
        if (!dyEnabled) {
            pending.clear();
            return out;
        }
        Iterator<Map.Entry<String, Pending>> it = pending.entrySet().iterator();
        while (it.hasNext()) {
            Pending p = it.next().getValue();
            if (p.escalated || now < p.deadline) {
                continue;
            }
            p.escalated = true;         // 只升级一次
            // 方案 B：要求同一事件里同时有足够多扇区在双黄
            if (dyMinSectors > 0 && countSectorsInCluster(p.clusterStart) < dyMinSectors) {
                continue;
            }
            if (inCooldown(Classifier.K_DY, now)) {
                continue;
            }
            markAlert(Classifier.K_DY, now);
            out.add(new Action(p.msg, Classifier.K_DY, Classifier.ALARM, true));
        }
        return out;
    }

    /** 同一事件（时间相近）里当前有多少个扇区处于双黄。 */
    private int countSectorsInCluster(long start) {
        int n = 0;
        for (Pending p : pending.values()) {
            if (Math.abs(p.clusterStart - start) <= clusterGapMs) {
                n++;
            }
        }
        return n;
    }

    /** 供界面显示：还有几个双黄在"观察期"内。 */
    public int pendingCount() {
        return pending.size();
    }
}
