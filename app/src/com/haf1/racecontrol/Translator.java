package com.haf1.racecontrol;

import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 把赛事控制的英文消息翻成一句中文简述。
 *
 * ## 为什么要它
 * 仲裁消息长这样：
 *
 *     FIA STEWARDS: TURN 1 INCIDENT INVOLVING CAR 5 (BOR) NOTED -
 *     FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – ESCAPE ROAD INSTRUCTIONS (14:27:29)
 *
 * 比赛时根本没空读。翻成「1 号弯事故（博尔托莱托）：已记录 —— 未遵守赛会指令（逃生通道）」
 * 一眼就懂。尤其是**判罚**：谁被罚、罚多少、为什么，这三件事必须能秒读。
 *
 * ## 规则是从真实数据里反推的，不是编的
 * 规则覆盖的模板来自 2026-09-11~13 一个比赛周末的 697 条真实消息，按出现频次排：
 *   超赛道限制删圈速  173 条 / 46 种模板
 *   蓝旗               79 条 / 11 种
 *   事故已记录         55 条 / 51 种
 *   仲裁裁决           35 条 / 33 种
 *   黑白旗              5 条
 *   明确判罚            3 条
 *
 * 翻不出来就返回 null —— **宁可显示原文，也不要瞎猜**。
 *
 * 纯函数、不碰 Android API，所以能离线单测。
 */
public final class Translator {

    private Translator() {
    }

    /**
     * 车号 + 车手缩写。**不能写成 `CAR\s+(\d+)\s*\(([A-Z]{3})\)`**：
     * 真实数据里多车事故是这么写的 ——
     *   FIA STEWARDS: TURN 3 INCIDENT INVOLVING CARS 43 (COL) AND 87 (BEA) ...
     * 第二辆（87）前面压根没有 "CAR"，而且 "CARS" 后面是空格再跟数字，
     * 严格要求 "CAR " 会漏掉一大半。
     * 所以直接匹配裸的「数字 (三字母)」。
     */
    private static final Pattern CAR = Pattern.compile("(\\d{1,2})\\s*\\(([A-Z]{3})\\)");
    private static final Pattern TURN = Pattern.compile("TURN\\s+(\\d+)");
    private static final Pattern LAP = Pattern.compile("LAP\\s+(\\d+)");
    private static final Pattern SECONDS = Pattern.compile("(\\d+)\\s*SECOND");

    /** 三位缩写 -> 中文姓氏。用数据里实际出现过的车手，查不到就退回缩写。 */
    private static final Map<String, String> DRIVERS = new HashMap<String, String>();

    static {
        DRIVERS.put("VER", "维斯塔潘");
        DRIVERS.put("NOR", "诺里斯");
        DRIVERS.put("PIA", "皮亚斯特里");
        DRIVERS.put("LEC", "勒克莱尔");
        DRIVERS.put("HAM", "汉密尔顿");
        DRIVERS.put("RUS", "拉塞尔");
        DRIVERS.put("ALO", "阿隆索");
        DRIVERS.put("SAI", "塞恩斯");
        DRIVERS.put("GAS", "加斯利");
        DRIVERS.put("TSU", "角田");
        DRIVERS.put("ALB", "阿尔本");
        DRIVERS.put("STR", "斯特罗尔");
        DRIVERS.put("BOT", "博塔斯");
        DRIVERS.put("HUL", "霍肯伯格");
        DRIVERS.put("OCO", "奥康");
        DRIVERS.put("BEA", "比尔曼");
        DRIVERS.put("COL", "科拉平托");
        DRIVERS.put("ANT", "安东内利");
        DRIVERS.put("LIN", "林德布拉德");
        DRIVERS.put("BOR", "博尔托莱托");
        DRIVERS.put("PER", "佩雷兹");
        DRIVERS.put("MAG", "马格努森");
        DRIVERS.put("RIC", "里卡多");
        DRIVERS.put("ZHO", "周冠宇");
        DRIVERS.put("SAR", "萨金特");
        DRIVERS.put("DEV", "德弗里斯");
        DRIVERS.put("LAW", "劳森");
        DRIVERS.put("DOO", "杜汉");
        DRIVERS.put("HAD", "哈贾尔");
    }

    /** 处置原因 -> 中文。按长度降序匹配，避免短串抢先。 */
    private static final String[][] REASONS = {
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – ESCAPE ROAD INSTRUCTIONS", "未遵守赛会指令（逃生通道）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - ESCAPE ROAD INSTRUCTIONS", "未遵守赛会指令（逃生通道）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS", "未遵守赛会指令"},
            {"LEAVING THE TRACK AND GAINING AN ADVANTAGE", "离开赛道并获得优势"},
            {"FORCING ANOTHER DRIVER OFF THE TRACK", "把对手逼出赛道"},
            {"SPEEDING IN THE PIT LANE", "维修区超速"},
            {"CAUSING A COLLISION", "造成碰撞"},
            {"REJOINING UNSAFELY", "不安全地回归赛道"},
            {"MOVING UNDER BRAKING", "制动中变线"},
            {"MAXIMUM DELTA TIME", "超出最大圈速差"},
            {"YELLOW FLAG INFRINGEMENT", "黄旗违规"},
            {"UNSAFE RELEASE", "不安全放车"},
            {"IMPEDING", "阻挡他人"},
            {"TRACK LIMITS", "超出赛道限制"},
    };

    /**
     * 翻成中文简述。翻不出来返回 null（调用方应显示原文）。
     */
    public static String gloss(String text) {
        if (text == null) {
            return null;
        }
        String raw = text.trim();
        if (raw.length() == 0) {
            return null;
        }
        String up = raw.toUpperCase(Locale.US);

        String g = penalty(up);
        if (g != null) {
            return g;
        }
        g = stewards(up);
        if (g != null) {
            return g;
        }
        g = noted(up);
        if (g != null) {
            return g;
        }
        g = trackLimits(up);
        if (g != null) {
            return g;
        }
        g = blueFlag(up);
        if (g != null) {
            return g;
        }
        g = blackAndWhite(up);
        if (g != null) {
            return g;
        }
        return null;
    }

    // ------------------------------------------------------------------
    // 各类模板
    // ------------------------------------------------------------------

    /**
     * 明确判罚。真实模板：
     *   FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 55 (SAI) (15:23:42)
     *   FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 10 (GAS) - SPEEDING IN THE PIT LANE (16:42:11)
     *   FIA STEWARDS: PENALTY SERVED - 5 SECOND TIME PENALTY FOR CAR 55 (SAI) (15:49:21)
     */
    private static String penalty(String up) {
        if (up.indexOf("PENALTY") < 0) {
            return null;
        }
        boolean served = up.indexOf("PENALTY SERVED") >= 0;
        String who = who(up);
        String secs = secs(up);
        String reason = reason(up);

        if (served) {
            StringBuilder b = new StringBuilder("仲裁：");
            b.append(who.length() > 0 ? who : "某车手").append(" 已执行 ");
            if (secs.length() > 0) {
                b.append(secs).append(" 秒罚时");
            } else {
                b.append("处罚");
            }
            return b.toString();
        }
        StringBuilder b = new StringBuilder("★ 判罚：");
        b.append(who.length() > 0 ? who : "某车手");
        if (secs.length() > 0) {
            b.append(" 罚时 ").append(secs).append(" 秒");
        } else {
            b.append(" 被处罚");
        }
        if (reason.length() > 0) {
            b.append(" —— ").append(reason);
        }
        return b.toString();
    }

    /**
     * 仲裁裁决。真实模板：
     *   FIA STEWARDS: TURN 1 INCIDENT INVOLVING CAR 5 (BOR) REVIEWED NO FURTHER INVESTIGATION - <原因> (ts)
     *   FIA STEWARDS: TURN 5 INCIDENT INVOLVING CAR 77 (BOT) WILL BE INVESTIGATED AFTER THE SESSION - <原因> (ts)
     *   FIA STEWARDS: TURN 5 INCIDENT INVOLVING CAR 5 (BOR) UNDER INVESTIGATION - <原因> (ts)
     *   FIA STEWARDS: WARNING FOR CAR 5 (BOR) - MOVING UNDER BRAKING (ts)
     */
    private static String stewards(String up) {
        if (up.indexOf("FIA STEWARDS") < 0) {
            return null;
        }
        String who = who(up);
        String turn = turn(up);
        String reason = reason(up);

        String verdict;
        if (up.indexOf("REVIEWED NO FURTHER") >= 0) {
            verdict = "复核完毕，不予追究";
        } else if (up.indexOf("WILL BE INVESTIGATED AFTER THE SESSION") >= 0) {
            verdict = "赛后调查";
        } else if (up.indexOf("UNDER INVESTIGATION") >= 0) {
            verdict = "调查中";
        } else if (up.indexOf("WARNING") >= 0) {
            verdict = "警告";
        } else if (up.indexOf("SERVED") >= 0) {
            return null;                    // 交给 penalty() 处理
        } else {
            return null;
        }

        StringBuilder b = new StringBuilder("仲裁：");
        if (turn.length() > 0) {
            b.append(turn).append(" 号弯 ");
        }
        b.append(who.length() > 0 ? who : "某车手");
        b.append(" —— ").append(verdict);
        if (reason.length() > 0) {
            b.append("（").append(reason).append("）");
        }
        return b.toString();
    }

    /**
     * 事故已记录（还没判）。真实模板：
     *   TURN 1 INCIDENT INVOLVING CAR 5 (BOR) NOTED - <原因> (ts)
     */
    private static String noted(String up) {
        if (up.indexOf("NOTED") < 0 || up.indexOf("INCIDENT") < 0) {
            return null;
        }
        String who = who(up);
        String turn = turn(up);
        String reason = reason(up);

        StringBuilder b = new StringBuilder();
        if (turn.length() > 0) {
            b.append(turn).append(" 号弯");
        } else {
            b.append("赛事");
        }
        b.append("事故");
        if (who.length() > 0) {
            b.append("（").append(who).append("）");
        }
        b.append("：已记录");
        if (reason.length() > 0) {
            b.append(" —— ").append(reason);
        }
        return b.toString();
    }

    /**
     * 超出赛道限制删圈速。真实模板：
     *   CAR 55 (SAI) TIME 1:43.523 DELETED - TRACK LIMITS AT TURN 15 LAP 26 15:48:45
     *   CAR 1 (NOR) LAP DELETED - TRACK LIMITS AT TURN 1 LAP 28 14:34:00 (PIT)
     */
    private static String trackLimits(String up) {
        if (up.indexOf("TRACK LIMITS") < 0 || up.indexOf("DELETED") < 0) {
            return null;
        }
        String who = who(up);
        String turn = turn(up);
        String lap = lap(up);
        boolean wholeLap = up.indexOf("LAP DELETED") >= 0;

        StringBuilder b = new StringBuilder();
        b.append(who.length() > 0 ? who : "某车手");
        b.append(wholeLap ? "：整圈成绩被删" : "：单圈成绩被删");
        if (turn.length() > 0) {
            b.append(" —— ").append(turn).append(" 号弯超出赛道限制");
        } else {
            b.append(" —— 超出赛道限制");
        }
        if (lap.length() > 0) {
            b.append("（第 ").append(lap).append(" 圈）");
        }
        return b.toString();
    }

    /** WAVED BLUE FLAG FOR CAR 77 (BOT) TIMED AT 16:08:37 */
    private static String blueFlag(String up) {
        if (up.indexOf("BLUE FLAG") < 0) {
            return null;
        }
        String who = who(up);
        return "蓝旗（让车）：" + (who.length() > 0 ? who : "某车手");
    }

    /** BLACK AND WHITE FLAG FOR CAR 55 (SAI) - TRACK LIMITS */
    private static String blackAndWhite(String up) {
        if (up.indexOf("BLACK AND WHITE") < 0) {
            return null;
        }
        String who = who(up);
        String reason = reason(up);
        StringBuilder b = new StringBuilder("黑白旗警告：");
        b.append(who.length() > 0 ? who : "某车手");
        if (reason.length() > 0) {
            b.append(" —— ").append(reason);
        }
        return b.toString();
    }

    // ------------------------------------------------------------------
    // 零件
    // ------------------------------------------------------------------

    /**
     * 「数字 (三字母)」里不是车手的标记。
     *
     * 实测：整个比赛周末的数据里这种形状共出现 23 种三字母，其中**只有 `PIT` 不是车手**——
     * 它是"这一圈进过站"的尾巴，出现 41 次：
     *   CAR 5 (BOR) LAP DELETED - TRACK LIMITS AT TURN 5 LAP 7 13:41:22 (PIT)
     * 不排除的话会被解析成"车手 22 号 PIT"，翻译里就多出个不存在的人。
     */
    private static final java.util.Set<String> NOT_DRIVER = new java.util.HashSet<String>(
            java.util.Arrays.asList("PIT", "LAP", "TBC"));

    /** 从 "CAR 55 (SAI)" 取出「塞恩斯(55)」；多车事故最多列两辆，其余用「等」带过。 */
    static String who(String up) {
        Matcher m = CAR.matcher(up);
        StringBuilder b = new StringBuilder();
        int n = 0;
        boolean more = false;
        while (m.find()) {
            if (NOT_DRIVER.contains(m.group(2))) {
                continue;               // "(PIT)" 这类尾巴，不是车手
            }
            if (n >= 2) {
                more = true;
                break;
            }
            if (b.length() > 0) {
                b.append("、");
            }
            b.append(name(m.group(2))).append("(").append(m.group(1)).append(")");
            n++;
        }
        if (b.length() == 0) {
            return "";
        }
        if (more) {
            b.append(" 等");
        }
        return b.toString();
    }

    static String name(String code) {
        String cn = DRIVERS.get(code);
        return cn == null ? code : cn;
    }

    static String turn(String up) {
        Matcher m = TURN.matcher(up);
        return m.find() ? m.group(1) : "";
    }

    static String lap(String up) {
        Matcher m = LAP.matcher(up);
        return m.find() ? m.group(1) : "";
    }

    static String secs(String up) {
        Matcher m = SECONDS.matcher(up);
        return m.find() ? m.group(1) : "";
    }

    /** 处置原因，按最长匹配优先。 */
    static String reason(String up) {
        for (int i = 0; i < REASONS.length; i++) {
            if (up.indexOf(REASONS[i][0]) >= 0) {
                return REASONS[i][1];
            }
        }
        return "";
    }
}
