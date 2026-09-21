package com.haf1.racecontrol;

import java.io.BufferedReader;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;

/**
 * 诊断工具：把一批真实消息喂给 {@link Translator} 和 {@link Prefs#isNoise}，
 * 打印「会不会被隐藏」+「中文简述是什么」。
 *
 * 为什么要它：翻译规则是从真实措辞反推的，光看代码说"支持"不算数 ——
 * 得拿真句子跑一遍看输出。这个工具就是干这个的，不依赖 Android 设备。
 *
 * 用法：
 *   java ... GlossDump &lt;samples.tsv&gt;              逐条详细打印（人看）
 *   java ... GlossDump &lt;samples.tsv&gt; --compact    一行一条，制表符分隔（喂给脚本）
 *
 * compact 模式每行：
 *   隐藏标记 \t kind \t 类型中文 \t 中文简述（空=没翻出来） \t 原文
 * 一个周末 700 条，块状格式翻不动，必须一行一条。
 */
public class GlossDump {

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.out.println("用法: GlossDump <samples.tsv> [--compact]");
            return;
        }
        boolean compact = args.length > 1 && "--compact".equals(args[1]);
        List<RaceMessage> list = new ArrayList<RaceMessage>();
        BufferedReader r = new BufferedReader(new InputStreamReader(
                new FileInputStream(args[0]), "UTF-8"));
        String line;
        long t = 1700000000000L;
        while ((line = r.readLine()) != null) {
            if (line.trim().length() == 0) {
                continue;
            }
            String[] p = line.split("\t", -1);
            if (p.length < 3) {
                continue;
            }
            t += 1000L;
            list.add(new RaceMessage(t, p[2], "r" + t, p[2], p[1], p[0],
                    "", "", "", "", "e" + t, 0));
        }
        r.close();

        int hidden = 0, glossed = 0;
        for (int i = 0; i < list.size(); i++) {
            RaceMessage m = list.get(i);
            String kind = Classifier.kind(m);
            boolean noise = Prefs.isNoise(m);
            String g = Translator.gloss(m.text());
            if (noise) {
                hidden++;
            }
            if (g != null) {
                glossed++;
            }
            if (compact) {
                System.out.println((noise ? "隐藏" : "显示")
                        + "\t" + kind
                        + "\t" + Classifier.label(kind)
                        + "\t" + (g == null ? "" : g)
                        + "\t" + m.text());
                continue;
            }
            System.out.println("--------------------------------------------------------------");
            System.out.println("原文 : " + m.text());
            System.out.println("类型 : " + Classifier.label(kind) + "   (" + kind + ")");
            System.out.println("默认 : " + (noise ? "【隐藏】" : "【显示】")
                    + "    背景色 " + hex(Classifier.color(kind))
                    + (Classifier.isCheckered(kind) ? " + 灰白棋盘格" : ""));
            System.out.println("简述 : " + (g == null ? "(无 —— 显示英文原文)" : g));
        }
        System.out.println("==============================================================");
        System.out.println("共 " + list.size() + " 条；默认隐藏 " + hidden
                + " 条；有中文简述 " + glossed + " 条");
    }

    private static String hex(int c) {
        return String.format("#%08X", c);
    }
}
