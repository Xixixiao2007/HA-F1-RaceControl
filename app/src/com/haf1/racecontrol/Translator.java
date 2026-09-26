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
 * 比赛时根本没空读。翻成「1 号弯事故（博托莱托）：已记录 —— 未遵守赛会指令（逃生通道）」
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
 * 第二个周末（2026-09-24~26，586 条）又暴露了几个缺口，已补上：
 *   删圈速的第二种原因 DOUBLE YELLOW     23 条
 *   `AFTER THE RACE` 的仲裁决定           4 条
 *   安全车 / 排位赛期间的操作指令        6 种 / 8 条
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
    private static final Pattern SECTOR = Pattern.compile("SECTOR\\s+(\\d+)");
    private static final Pattern SECONDS = Pattern.compile("(\\d+)\\s*SECOND");
    /** 排位赛阶段。真实数据里有 `FIA STEWARDS: Q1 INCIDENT INVOLVING CARS 81 (PIA), ...` —— */
    private static final Pattern PHASE = Pattern.compile("\\b(Q[123])\\b");
    private static final Pattern RAIN = Pattern.compile("RISK OF RAIN FOR (.+?) IS (\\d+)%");
    /** `Q1 WILL START AT 16:04` / `Q2 ...` / `Q3 ...`。 */
    private static final Pattern WILL_START = Pattern.compile("\\b(Q[123])\\b WILL START AT (\\d{1,2}:\\d{2})");

    /** 三位缩写 -> 中文姓氏。用数据里实际出现过的车手，查不到就退回缩写。 */
    private static final Map<String, String> DRIVERS = new HashMap<String, String>();

    static {
        DRIVERS.put("VER", "维斯塔潘");
        DRIVERS.put("NOR", "诺里斯");
        DRIVERS.put("PIA", "皮亚");
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
        DRIVERS.put("LIN", "林布拉德");
        DRIVERS.put("BOR", "博托莱托");
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

    /**
     * 处置原因 -> 中文。**按长度降序匹配**，长串必须排在短串前面，
     * 否则短串会抢先吃掉长串的前半截。
     *
     * ★ 复合原因（真实数据里就有的形状，别当成罕见情况）：
     *     FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – ESCAPE ROAD INSTRUCTIONS
     *     FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - MAXIMUM DELTA TIME
     *   连字符后面那半截是**对前半截的限定**（没遵守的是哪一条指令），
     *   所以整条翻成「未遵守赛会指令（逃生通道）」，而不是把后半截丢掉。
     *   只写这几条显式条目就够了 —— 万一将来出现新措辞，
     *   匹配到前半截仍是**正确但不完整**的译文，不会翻错。
     */
    private static final String[][] REASONS = {
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – ESCAPE ROAD INSTRUCTIONS", "未遵守赛会指令（逃生通道）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - ESCAPE ROAD INSTRUCTIONS", "未遵守赛会指令（逃生通道）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – MAXIMUM DELTA TIME", "未遵守赛会指令（超出最大圈速差）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - MAXIMUM DELTA TIME", "未遵守赛会指令（超出最大圈速差）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – PRACTICE START INFRINGEMENT", "未遵守赛会指令（起步练习违规）"},
            {"FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS - PRACTICE START INFRINGEMENT", "未遵守赛会指令（起步练习违规）"},
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
     * 删圈速的原因。加了双黄旗后不再是单一条件，所以单独列表。
     *
     * ★ 双黄旗版本原来一条都翻不出来：判据写死了 `TRACK LIMITS`，
     *   而双黄旗下圈速作废用的是 `DOUBLE YELLOW`。
     *   2026-09-24~26 那个周末 23 条，占删圈速通报的两成。
     */
    private static final String[][] DELETE_REASONS = {
            {"TRACK LIMITS", "超出赛道限制"},
            {"DOUBLE YELLOW", "双黄旗"},
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

        // 集成自带的自测消息：`... - RACE CONTROL TEST`。
        // 它没有车号，落到下面的兜底会变成「某车手」—— 明明没有车手。
        // 统一标成测试（用户定的译法）。
        if (up.indexOf("RACE CONTROL TEST") >= 0) {
            return "赛会判罚测试";
        }

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
        g = lapDeleted(up);
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
        return other(up);
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
     *   FIA STEWARDS: Q1 INCIDENT INVOLVING CARS 81 (PIA), ... AND 77 (BOT) NO FURTHER ACTION - <原因>
     *   FIA STEWARDS: TURN 5 INCIDENT INVOLVING CAR 77 (BOT) WILL BE INVESTIGATED AFTER THE SESSION - <原因> (ts)
     *   FIA STEWARDS: TURN 5 INCIDENT INVOLVING CAR 5 (BOR) UNDER INVESTIGATION - <原因> (ts)
     *   FIA STEWARDS: WARNING FOR CAR 5 (BOR) - MOVING UNDER BRAKING (ts)
     *
     * ★ `NO FURTHER ACTION` 原来不认识 —— 结果整个周末**最长的两条**仲裁消息
     *   （7 辆车、8 辆车的事故）一条译文都没有。而那恰恰就是「不予追究」，
     *   用户明确说过这类不能被忽略。
     *
     * ★ 原因不再用括号包起来 —— 原因自己就带括号（未遵守赛会指令（逃生通道）），
     *   套起来会变成「（未遵守赛会指令（逃生通道））」。改用冒号：「不予追究：原因」。
     */
    private static String stewards(String up) {
        if (up.indexOf("FIA STEWARDS") < 0) {
            return null;
        }
        String who = who(up);
        String where = where(up);
        String reason = reason(up);

        String verdict;
        if (up.indexOf("REVIEWED NO FURTHER") >= 0) {
            verdict = "复核完毕，不予追究";
        } else if (up.indexOf("NO FURTHER ACTION") >= 0) {
            verdict = "不予追究";
        } else if (up.indexOf("WILL BE INVESTIGATED AFTER THE SESSION") >= 0
                || up.indexOf("WILL BE INVESTIGATED AFTER THE RACE") >= 0) {
            // ★ 只认 SESSION 不认 RACE，结果新周末 4 条「赛后再查」整条翻不出来。
            //   `... WILL BE INVESTIGATED AFTER THE RACE - YELLOW FLAG INFRINGEMENT`
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
        if (where.length() > 0) {
            b.append(where).append(' ');
        }
        b.append(who.length() > 0 ? who : "某车手");
        b.append(" —— ").append(verdict);
        if (reason.length() > 0) {
            b.append("：").append(reason);
        }
        return b.toString();
    }

    /**
     * 事故已记录（还没判）。真实模板：
     *   TURN 1 INCIDENT INVOLVING CAR 5 (BOR) NOTED - <原因> (ts)
     *   INCIDENT INVOLVING CAR 41 (LIN) NOTED - UNSAFE RELEASE (ts)     ← 连 TURN 都没有
     *   FIA STEWARDS: Q1 INCIDENT INVOLVING CARS 81 (PIA), ... NOTED - <原因>
     */
    private static String noted(String up) {
        if (up.indexOf("NOTED") < 0 || up.indexOf("INCIDENT") < 0) {
            return null;
        }
        String who = who(up);
        String where = where(up);
        String reason = reason(up);

        StringBuilder b = new StringBuilder();
        String loc = where.length() > 0 ? where : "赛事";
        b.append(loc);
        if (loc.startsWith("Q")) {
            b.append(' ');          // 「Q1事故」不好看，中英之间留个空格
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
     * 删圈速。真实模板（**两种原因**）：
     *   CAR 55 (SAI) TIME 1:43.523 DELETED - TRACK LIMITS AT TURN 15 LAP 26 15:48:45
     *   CAR 77 (BOT) TIME 2:23.403 DELETED - DOUBLE YELLOW AT TURN 7 LAP 6 12:53:52
     *   CAR 1 (NOR) LAP DELETED - TRACK LIMITS AT TURN 1 LAP 28 14:34:00 (PIT)
     *
     * 判据必须是「DELETED + 已知原因」，**不能只看原因**：
     *   `BLACK AND WHITE FLAG FOR CAR 44 (HAM) - TRACK LIMITS`
     *   也含 TRACK LIMITS，但那是黑白旗，不是删圈速。
     *
     * 双黄旗那条只陈述事实、不解释为什么删（用户定）：
     *   「7 号弯双黄旗」，而不是「双黄旗未减速」。
     */
    private static String lapDeleted(String up) {
        if (up.indexOf("DELETED") < 0) {
            return null;
        }
        String why = "";
        for (int i = 0; i < DELETE_REASONS.length; i++) {
            if (up.indexOf(DELETE_REASONS[i][0]) >= 0) {
                why = DELETE_REASONS[i][1];
                break;
            }
        }
        if (why.length() == 0) {
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
            b.append(" —— ").append(turn).append(" 号弯").append(why);
        } else {
            b.append(" —— ").append(why);
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

    /**
     * 其余「内容型」消息 —— 类型徽标说不清楚的那些。
     *
     * ## 为什么单开一类，而不是把旗语也翻了
     * 判据很简单：**徽标已经表达出来的信息不再重复**。
     *   `DOUBLE YELLOW IN TRACK SECTOR 15` 徽标就是「双黄旗」，区段是数字看得懂
     *   -> 不翻（翻了只是把徽标再说一遍）
     *   `YELLOW IN PIT LANE` 徽标只说「黄旗」，没说是**维修区**的
     *   -> 要翻（不翻就丢了关键信息）
     * 所以这里只收「内容型」消息：维修区、比赛环节、天气、赛道状况、车手相关。
     *
     * 这些句式来自一个完整比赛周末的 697 条真实消息，一条不编。
     */
    private static String other(String up) {
        // ---- 2026-09-24~26 新周末出现的指令 ----
        // 这五条都是安全车 / 排位赛期间的操作指令，徽标只能说「其它」。
        if (up.indexOf("ALL CARS THROUGH THE PIT LANE") >= 0) {
            return "所有赛车通过维修区";
        }
        if (up.indexOf("ALL CARS USE START/FINISH STRAIGHT") >= 0) {
            return "所有赛车使用起终点直道";
        }
        if (up.indexOf("RECOVERY VEHICLE ON TRACK") >= 0) {
            // 用户定的译法：维修车（不是「救援车」）
            String t = turn(up);
            return (t.length() > 0 ? t + " 号弯" : "赛道") + "有维修车";
        }
        if (up.indexOf("START OF QUALIFYING WILL BE DELAYED") >= 0) {
            return "排位赛将推迟开始";
        }
        Matcher ws = WILL_START.matcher(up);
        if (ws.find()) {
            return ws.group(1) + " 将于 " + ws.group(2) + " 开始";
        }
        if (up.indexOf("LAPPED CARS MAY NOW OVERTAKE THE SAFETY CAR") >= 0) {
            // 冒号后面是车号（用户确认）。
            String n = afterColon(up);
            return "被套圈车可超越安全车"
                    + (n.length() > 0 ? "：" + n + " 号" : "");
        }
        if (up.indexOf("PINK HEAD PADDING MATERIAL MUST BE USED") >= 0) {
            return "必须使用粉色头枕垫料";
        }
        if (up.indexOf("TRACK SURFACE SLIPPERY") >= 0) {
            String sec = sector(up);
            return "赛道湿滑" + (sec.length() > 0 ? "（" + sec + " 号区段）" : "");
        }
        if (up.indexOf("ALL PASS HOLDERS MAY ACCESS THE PIT LANE") >= 0) {
            return "持通行证者可使用维修区";
        }
        if (up.indexOf("YELLOW IN PIT LANE") >= 0) {
            return "维修区黄旗";
        }
        if (up.indexOf("PIT LANE CLEAR") >= 0) {
            return "维修区解除";
        }
        // `SESSION` 指的是**这一个比赛环节**（一练/排位/正赛），简称就是「比赛」。
        // 译成「会话」是计算机味的误译（用户指出）。
        if (up.indexOf("SESSION WILL RESUME") >= 0) {
            String t = clock(up);
            return "比赛将于 " + (t.length() > 0 ? t : "稍后") + " 重启";
        }
        if (up.indexOf("SESSION WILL BE TEMPORARILY STOPPED") >= 0) {
            return "比赛暂时中止";
        }
        if (up.indexOf("MARSHALS ON TRACK") >= 0) {
            String turn = turn(up);
            // 「马歇尔」是照字面音译。中文 F1 圈通用的是**马修**（marshal 的定名），
            // 用户指出过这一点。
            return (turn.length() > 0 ? turn + " 号弯" : "赛道上") + "有马修";
        }
        if (up.indexOf("MEDICAL CAR DEPLOYED") >= 0) {
            return "医疗车出动";
        }
        if (up.indexOf("DELAYED START") >= 0) {
            return "起步推迟";
        }
        if (up.indexOf("PIT EXIT OPEN") >= 0) {
            return "维修区出口开放";
        }
        if (up.indexOf("PIT EXIT CLOSED") >= 0) {
            return "维修区出口关闭";
        }
        if (up.indexOf("OVERTAKE ENABLED") >= 0) {
            return "允许超车";
        }
        if (up.indexOf("OVERTAKE DISABLED") >= 0) {
            return "禁止超车";
        }
        if (up.indexOf("RACE START") >= 0) {
            return "比赛开始";
        }
        if (up.indexOf("FIRST CAR TO TAKE THE FLAG") >= 0) {
            String who = who(up);
            return "首个冲线：" + (who.length() > 0 ? who : "某车手");
        }
        Matcher m = RAIN.matcher(up);
        if (m.find()) {
            return session(m.group(1)) + "降雨概率 " + m.group(2) + "%";
        }
        return null;
    }

    /** `F1 RACE` / `F2 QUALIFYING` / `F1 FREE PRACTICE 2` / `THE F2 SPRINT RACE` -> 中文赛段名。 */
    static String session(String en) {
        String s = en.trim().toUpperCase(Locale.US);
        if (s.startsWith("THE ")) {
            s = s.substring(4);
        }
        String cls;
        if (s.startsWith("F1")) {
            cls = "F1 ";
        } else if (s.startsWith("F2")) {
            cls = "F2 ";
        } else if (s.startsWith("F3")) {
            cls = "F3 ";
        } else {
            return en.trim();
        }
        String kind;
        if (s.indexOf("FREE PRACTICE 1") >= 0) {
            kind = "一练";
        } else if (s.indexOf("FREE PRACTICE 2") >= 0) {
            kind = "二练";
        } else if (s.indexOf("FREE PRACTICE 3") >= 0) {
            kind = "三练";
        } else if (s.indexOf("SPRINT QUALIFYING") >= 0) {
            kind = "冲刺排位";
        } else if (s.indexOf("SPRINT") >= 0) {
            kind = "冲刺赛";
        } else if (s.indexOf("QUALIFYING") >= 0) {
            kind = "排位赛";
        } else if (s.indexOf("RACE") >= 0) {
            kind = "正赛";
        } else {
            return en.trim();
        }
        return cls + kind;
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

    /**
     * 从 "CAR 55 (SAI)" 取出「塞恩斯(55)」。**一辆都不省。**
     *
     * ★ 原来是最多列两辆 + 「等 N 辆」，被用户否掉了：整个周末最长的两条消息
     *   （8 辆、9 辆车）他一辆都不想漏 —— 车号本身就是要核对的信息。
     *   列全了译文有 70 来个字、手机上要占三四行，所以列表里同时放开了行数限制
     *   （见 MainActivity 的 gloss：不再 setMaxLines/ellipsize）。
     */
    static String who(String up) {
        Matcher m = CAR.matcher(up);
        StringBuilder b = new StringBuilder();
        while (m.find()) {
            if (NOT_DRIVER.contains(m.group(2))) {
                continue;               // "(PIT)" 这类尾巴，不是车手
            }
            if (b.length() > 0) {
                b.append("、");
            }
            b.append(name(m.group(2))).append("(").append(m.group(1)).append(")");
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

    static String sector(String up) {
        Matcher m = SECTOR.matcher(up);
        return m.find() ? m.group(1) : "";
    }

    /**
     * 事故地点：优先「N 号弯」，没有的话退回排位赛阶段「Q1/Q2/Q3」。
     *
     * ★ 原来只会输出「N 号弯」，没有 TURN 就一律写「赛事」——
     *   于是 `FIA STEWARDS: Q1 INCIDENT ...` 变成了「赛事事故」，
     *   把"这是排位赛第一节的事故"这个关键上下文丢了。
     */
    static String where(String up) {
        String turn = turn(up);
        if (turn.length() > 0) {
            return turn + " 号弯";
        }
        Matcher m = PHASE.matcher(up);
        if (m.find()) {
            return m.group(1);
        }
        return "";
    }

    /** 冒号后面那串数字：`... SAFETY CAR: 77` -> "77"。 */
    static String afterColon(String up) {
        Matcher m = Pattern.compile(":\\s*(\\d+)").matcher(up);
        return m.find() ? m.group(1) : "";
    }

    /** 消息里的 `17:47` 这种时刻。 */
    static String clock(String up) {
        Matcher m = Pattern.compile("\\b(\\d{1,2}:\\d{2})\\b").matcher(up);
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
