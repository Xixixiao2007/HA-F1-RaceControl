package com.haf1.racecontrol;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 配置存取（SharedPreferences 包装）。
 *
 * 相比上一代的「关键词黑白名单」，这里换成**结构化过滤 + 分级提醒**：
 * 过滤按 flag / category / 车号，提醒按旗语类型定等级。
 */
public class Prefs {

    private static final String NAME = "ha_f1_racecontrol";

    public static final String DEFAULT_BASE = "http://homeassistant.local:8123";
    /** 上游集成 Nicxe/f1_sensor 的赛事控制实体；用户的实例带 _2 后缀。 */
    public static final String DEFAULT_ENTITY = "sensor.f1_race_control_2";

    // ---- 双黄旗策略 ----
    /** 只闪动，不提醒。 */
    public static final int DY_OFF = 0;
    /** 只给轻提醒，不升级。 */
    public static final int DY_SOFT = 1;
    /** 轻提醒 + 存活超过 dyEscalateSec 才升级为强提醒。实测 23 次/周末。 */
    public static final int DY_ESCALATE = 2;
    /** 同上，但还要求同时 >=3 个扇区。实测 10 次/周末，会漏掉局部事故。 */
    public static final int DY_ESCALATE_BIG = 3;

    public static final int POLL_MIN = 1;
    public static final int POLL_MAX = 60;

    public String base = DEFAULT_BASE;
    public String token = "";
    public String entityId = DEFAULT_ENTITY;

    /** 启动时回溯多少小时。取消息带属性后响应变大，默认从 24 缩到 12。 */
    public int historyHours = 12;
    public int pollSeconds = 2;
    public boolean realtimeEnabled = true;

    // ---- 提醒 ----
    public boolean soundEnabled = true;
    public boolean vibrateEnabled = true;
    public boolean screenWakeEnabled = true;
    /**
     * 强提醒是否突破系统静音/振动模式（走 STREAM_ALARM）。
     * 默认开：真在现场看比赛时，只有突破静音才可能不漏掉红旗。
     */
    public boolean silentOverride = true;
    /** 强提醒自动停止秒数；0 = 必须手动确认。 */
    public int alarmAutoStopSec = 15;
    /** 同类型提醒的最小间隔。 */
    public int cooldownSec = 60;
    /** 双黄存活超过多少秒才算真事件（用来判定"测试型"）。 */
    public int dyEscalateSec = 15;
    /** 双黄升级所需的最少扇区数；0 = 不启用。 */
    public int dyMinSectors = 0;
    /** 双黄策略，见 DY_* 常量。 */
    public int dyMode = DY_ESCALATE;
    /** 黄旗 / 黑白旗等"注意"级是否发声。 */
    public boolean attentionEnabled = true;
    /** 新消息是否闪动。 */
    public boolean flashEnabled = true;

    // ---- 过滤 ----
    /** 默认隐藏噪音（蓝旗 / 解除 / 删除圈速 / 查完没事）。实测省掉 57.7% 的消息。 */
    public boolean noiseFilterEnabled = true;
    /** 只看某辆车（车号），空 = 不筛。 */
    public String carFilter = "";
    /** 用户自定义排除关键词，每行一个。 */
    public List<String> excludeKeywords = new ArrayList<String>();

    /** 连接断了多久没恢复就告警（秒）。 */
    public int connectionWarnSec = 45;
    /** 本地最多保留多少条消息。 */
    public int maxStored = 500;

    // ------------------------------------------------------------------

    public static Prefs load(Context c) {
        SharedPreferences sp = c.getSharedPreferences(NAME, Context.MODE_PRIVATE);
        Prefs p = new Prefs();
        p.base = sp.getString("base", DEFAULT_BASE);
        p.token = sp.getString("token", "");
        p.entityId = sp.getString("entity", DEFAULT_ENTITY);
        p.historyHours = clamp(sp.getInt("hours", 12), 1, 168);
        p.pollSeconds = clampPoll(sp.getInt("poll", 2));
        p.realtimeEnabled = sp.getBoolean("realtime", true);

        p.soundEnabled = sp.getBoolean("sound", true);
        p.vibrateEnabled = sp.getBoolean("vibrate", true);
        p.screenWakeEnabled = sp.getBoolean("wake", true);
        p.silentOverride = sp.getBoolean("silent_override", true);
        p.alarmAutoStopSec = clamp(sp.getInt("autostop", 15), 0, 120);
        p.cooldownSec = clamp(sp.getInt("cooldown", 60), 0, 600);
        p.dyEscalateSec = clamp(sp.getInt("dy_escalate", 15), 1, 120);
        p.dyMinSectors = clamp(sp.getInt("dy_min_sectors", 0), 0, 26);
        p.dyMode = clamp(sp.getInt("dy_mode", DY_ESCALATE), 0, 3);
        p.attentionEnabled = sp.getBoolean("attention", true);
        p.flashEnabled = sp.getBoolean("flash", true);

        p.noiseFilterEnabled = sp.getBoolean("noise", true);
        p.carFilter = sp.getString("car", "");
        p.excludeKeywords = splitLines(sp.getString("exclude", ""));

        p.connectionWarnSec = clamp(sp.getInt("connwarn", 45), 10, 600);
        p.maxStored = clamp(sp.getInt("maxstored", 500), 50, 5000);
        return p;
    }

    public void save(Context c) {
        c.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit()
                .putString("base", base)
                .putString("token", token)
                .putString("entity", entityId)
                .putInt("hours", historyHours)
                .putInt("poll", clampPoll(pollSeconds))
                .putBoolean("realtime", realtimeEnabled)
                .putBoolean("sound", soundEnabled)
                .putBoolean("vibrate", vibrateEnabled)
                .putBoolean("wake", screenWakeEnabled)
                .putBoolean("silent_override", silentOverride)
                .putInt("autostop", alarmAutoStopSec)
                .putInt("cooldown", cooldownSec)
                .putInt("dy_escalate", dyEscalateSec)
                .putInt("dy_min_sectors", dyMinSectors)
                .putInt("dy_mode", dyMode)
                .putBoolean("attention", attentionEnabled)
                .putBoolean("flash", flashEnabled)
                .putBoolean("noise", noiseFilterEnabled)
                .putString("car", carFilter)
                .putString("exclude", joinLines(excludeKeywords))
                .putInt("connwarn", connectionWarnSec)
                .putInt("maxstored", maxStored)
                .apply();
    }

    /** 把配置灌进提醒闸门。 */
    public void applyTo(AlertGate g) {
        g.cooldownMs = cooldownSec * 1000L;
        g.dyEscalateMs = dyEscalateSec * 1000L;
        g.dyMinSectors = (dyMode == DY_ESCALATE_BIG) ? Math.max(3, dyMinSectors) : 0;
        g.dyEnabled = dyMode != DY_OFF;
        g.attentionEnabled = attentionEnabled;
    }

    public boolean isConfigured() {
        return base != null && base.trim().length() > 0
                && token != null && token.trim().length() > 0
                && entityId != null && entityId.trim().length() > 0;
    }

    public String baseUrl() {
        String b = base == null ? "" : base.trim();
        while (b.endsWith("/")) {
            b = b.substring(0, b.length() - 1);
        }
        return b;
    }

    public static int clampPoll(int v) {
        return clamp(v, POLL_MIN, POLL_MAX);
    }

    public static int clamp(int v, int lo, int hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    // ------------------------------------------------------------------
    // 过滤
    // ------------------------------------------------------------------

    /**
     * 这条消息是不是"噪音"。
     *
     * 实测一个比赛周末 697 条消息里 **57.7%** 是噪音；正赛更夸张，**64.6%**
     * （蓝旗占 42.9%，超赛道限制删圈速占 21.7%）。所以默认过滤不是锦上添花，
     * 是决定 App 能不能用的东西。
     */
    public static boolean isNoise(RaceMessage m) {
        if (m == null) {
            return true;
        }
        String kind = Classifier.kind(m);
        if (Classifier.K_BLUE.equals(kind) || Classifier.K_CLEAR.equals(kind)) {
            return true;
        }
        String up = m.text().toUpperCase(Locale.US);
        if (up.indexOf("TRACK LIMITS") >= 0) {
            return true;
        }
        if (up.indexOf("LAP DELETED") >= 0) {
            return true;
        }
        if (up.indexOf("NO FURTHER") >= 0) {
            return true;
        }
        return false;
    }

    /** 这条消息是否应当出现在列表里。 */
    public boolean accept(RaceMessage m) {
        if (m == null) {
            return false;
        }
        if (noiseFilterEnabled && isNoise(m)) {
            return false;
        }
        String car = carFilter == null ? "" : carFilter.trim();
        if (car.length() > 0) {
            String c = m.carNumber == null ? "" : m.carNumber.trim();
            if (!car.equals(c)) {
                return false;
            }
        }
        if (excludeKeywords != null && !excludeKeywords.isEmpty()) {
            String up = m.text().toUpperCase(Locale.US);
            for (int i = 0; i < excludeKeywords.size(); i++) {
                String k = excludeKeywords.get(i);
                if (k == null || k.length() == 0) {
                    continue;
                }
                if (up.indexOf(k.trim().toUpperCase(Locale.US)) >= 0) {
                    return false;
                }
            }
        }
        return true;
    }

    static List<String> splitLines(String s) {
        List<String> out = new ArrayList<String>();
        if (s == null) {
            return out;
        }
        String[] parts = s.split("\n");
        for (int i = 0; i < parts.length; i++) {
            String t = parts[i].trim();
            if (t.length() > 0) {
                out.add(t);
            }
        }
        return out;
    }

    static String joinLines(List<String> list) {
        StringBuilder sb = new StringBuilder();
        if (list != null) {
            for (int i = 0; i < list.size(); i++) {
                String t = list.get(i) == null ? "" : list.get(i).trim();
                if (t.length() > 0) {
                    if (sb.length() > 0) {
                        sb.append('\n');
                    }
                    sb.append(t);
                }
            }
        }
        return sb.toString();
    }
}
