package com.haf1.racecontrol;

import android.app.Activity;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

/**
 * 设置页。
 *
 * 分组：连接 / 显示与过滤 / 提醒。所有数值项都给默认值并做钳制，
 * 用户填了离谱的值也不会把 App 搞坏。
 */
public class SettingsActivity extends Activity {

    private Prefs p;

    private EditText baseBox;
    private EditText tokenBox;
    private EditText entityBox;
    private EditText hoursBox;
    private EditText pollBox;
    private EditText carBox;
    private EditText excludeBox;
    private EditText cooldownBox;
    private EditText dySecondsBox;
    private EditText autoStopBox;
    private EditText connWarnBox;

    private CheckBox realtimeBox;
    private CheckBox flashBox;
    private CheckBox noiseBox;
    private CheckBox soundBox;
    private CheckBox vibrateBox;
    private CheckBox wakeBox;
    private CheckBox silentBox;
    private CheckBox attentionBox;

    private Spinner dySpinner;
    private TextView testResult;

    private static final String[] DY_LABELS = {
            "双黄旗：只闪动，不提醒",
            "双黄旗：只轻提醒（不升级）",
            "双黄旗：轻提醒 + 持续超时升级（推荐，实测约 23 次/周末）",
            "双黄旗：轻提醒 + 仅大面积（≥3 扇区）升级（实测约 10 次/周末）",
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        p = Prefs.load(this);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(14), dp(16), dp(24));
        root.setBackgroundColor(0xFFFAFAFA);

        header(root, "连接");

        baseBox = field(root, "服务器地址", p.base,
                "例如 http://homeassistant.local:8123 或 http://bh4gzk.top:8123");
        tokenBox = field(root, "长期访问令牌", p.token,
                "HA 网页版 → 个人资料 → 安全 → 长期访问令牌。普通权限用户即可。");
        tokenBox.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        entityBox = field(root, "实体 ID", p.entityId,
                "上游集成 Nicxe/f1_sensor 的赛事控制实体，通常以 _race_control 结尾。");

        Button test = new Button(this);
        test.setText("测试连接");
        test.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                testConnection();
            }
        });
        root.addView(test);

        testResult = new TextView(this);
        testResult.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        testResult.setPadding(0, dp(6), 0, dp(10));
        root.addView(testResult);

        header(root, "显示与过滤");

        hoursBox = number(root, "启动时回溯小时数", String.valueOf(p.historyHours),
                "带上属性后历史响应会变大，建议 6~12 小时。");
        pollBox = number(root, "轮询间隔（秒）", String.valueOf(p.pollSeconds),
                "实时断开时的兜底刷新间隔。");
        realtimeBox = check(root, "使用 WebSocket 实时推送", p.realtimeEnabled,
                "开启后延迟约 50~150 毫秒；关闭则退回 REST 轮询（约 3.5 秒）。");
        flashBox = check(root, "新消息闪动", p.flashEnabled,
                "到达的新消息先闪两下再定格配色。历史回填不会闪。");
        noiseBox = check(root, "默认隐藏噪音", p.noiseFilterEnabled,
                "蓝旗 / 解除 / 删圈速通报。实测占全部消息的 54.8%，"
                        + "正赛里占 68.5%。");
        carBox = field(root, "只看某辆车（车号）", p.carFilter, "留空 = 不筛选。例如 44。");
        excludeBox = multiline(root, "排除关键词（每行一个）", Prefs.joinLines(p.excludeKeywords),
                "对消息全文做不区分大小写的子串匹配。");

        header(root, "提醒");

        soundBox = check(root, "声音", p.soundEnabled, "关掉后只震动不发声。");
        vibrateBox = check(root, "震动", p.vibrateEnabled, "不同类型给不同节奏，闭着眼也能分辨。");
        wakeBox = check(root, "超强提醒时点亮屏幕", p.screenWakeEnabled,
                "用窗口标志实现，不需要额外的系统权限。");
        silentBox = check(root, "超强提醒突破静音模式", p.silentOverride,
                "走闹钟通道。现场看比赛时手机多半是静音，不突破就可能漏掉红旗。");
        attentionBox = check(root, "黄旗 / 黑白旗给轻提醒", p.attentionEnabled,
                "关掉后这两类只闪动、不发声。");
        autoStopBox = number(root, "超强提醒自动停止（秒，0=必须手动确认）",
                String.valueOf(p.alarmAutoStopSec), "默认 15 秒。");
        cooldownBox = number(root, "同类型提醒冷却（秒）", String.valueOf(p.cooldownSec),
                "避免连环炸响。红旗不受此限制。");
        connWarnBox = number(root, "断线多久后告警（秒）", String.valueOf(p.connectionWarnSec),
                "比赛中掉线最危险 —— 你会以为赛道没消息，实际是漏了红旗。");

        // ---- 试听：不用连 HA 也能当场验证声音和震动 ----
        // 光看设置项没法知道"到底响不响、震不震得出来"，
        // 尤其是静音模式下走闹钟通道这件事，必须真听一次。
        header(root, "试听");
        TextView tryHint = new TextView(this);
        tryHint.setText("先按上面的开关调好，再点下面两下听听看。"
                + "注意「超强提醒」走的是**闹钟音量**，不是通知音量 —— "
                + "如果没声，先把手机闹钟音量调起来。");
        tryHint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        tryHint.setTextColor(0xFF90A4AE);
        root.addView(tryHint);

        Button tryAttention = new Button(this);
        tryAttention.setText("试听：轻提醒（黄旗那种）");
        tryAttention.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                collect();
                Notifier n = new Notifier(SettingsActivity.this);
                n.attention(p.soundEnabled, p.vibrateEnabled);
            }
        });
        root.addView(tryAttention);

        Button tryAlarm = new Button(this);
        tryAlarm.setText("试听：超强提醒（红旗那种，响 4 秒）");
        tryAlarm.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                collect();
                final Notifier n = new Notifier(SettingsActivity.this);
                n.startAlarm(Classifier.K_RED, p.soundEnabled, p.vibrateEnabled, p.silentOverride);
                new android.os.Handler().postDelayed(new Runnable() {
                    public void run() {
                        n.release();
                    }
                }, 4000L);
            }
        });
        root.addView(tryAlarm);

        Button tryFull = new Button(this);
        tryFull.setText("试听：全屏横幅（超强提醒的样子）");
        tryFull.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                collect();
                AlertActivity.show(SettingsActivity.this, Classifier.K_RED,
                        "RED FLAG", System.currentTimeMillis(), "", "", false, p);
            }
        });
        root.addView(tryFull);

        TextView dyLabel = new TextView(this);
        dyLabel.setText("双黄旗策略");
        dyLabel.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        dyLabel.setPadding(0, dp(10), 0, dp(4));
        root.addView(dyLabel);

        dySpinner = new Spinner(this);
        ArrayAdapter<String> dyAdapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_spinner_item, DY_LABELS);
        dyAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        dySpinner.setAdapter(dyAdapter);
        dySpinner.setSelection(Prefs.clamp(p.dyMode, 0, 3));
        root.addView(dySpinner);

        TextView dyHint = new TextView(this);
        dyHint.setText("实测：一个周末有 142 条双黄消息，但只对应 24 个真实事件；"
                + "其中 29 次存活不到 15 秒（系统测试/抖动，最短 1 秒）。"
                + "所以双黄先给轻提醒，持续超过下面这个秒数才升级为超强提醒。");
        dyHint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        dyHint.setTextColor(0xFF78909C);
        dyHint.setPadding(0, dp(4), 0, dp(8));
        root.addView(dyHint);

        dySecondsBox = number(root, "双黄升级阈值（秒）", String.valueOf(p.dyEscalateSec),
                "存活超过它就认为不是测试。默认 15。");

        Button save = new Button(this);
        save.setText("保存");
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp.topMargin = dp(18);
        save.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                collect();
                p.save(SettingsActivity.this);
                finish();
            }
        });
        root.addView(save, sp);

        Button defaults = new Button(this);
        defaults.setText("恢复默认（不动地址与令牌）");
        defaults.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Prefs d = new Prefs();
                d.base = p.base;
                d.token = p.token;
                d.entityId = p.entityId;
                p = d;
                fill();
            }
        });
        root.addView(defaults);

        scroll.addView(root);
        setContentView(scroll);
    }

    // ------------------------------------------------------------------
    // 控件工厂
    // ------------------------------------------------------------------

    private void header(LinearLayout root, String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        t.setTextColor(0xFF00695C);
        t.setPadding(0, dp(16), 0, dp(6));
        root.addView(t);
    }

    private EditText field(LinearLayout root, String label, String value, String hint) {
        label(root, label, hint);
        EditText e = new EditText(this);
        e.setText(value == null ? "" : value);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        e.setSingleLine(true);
        root.addView(e);
        return e;
    }

    private EditText multiline(LinearLayout root, String label, String value, String hint) {
        label(root, label, hint);
        EditText e = new EditText(this);
        e.setText(value == null ? "" : value);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        e.setMinLines(2);
        e.setGravity(Gravity.TOP | Gravity.START);
        root.addView(e);
        return e;
    }

    private EditText number(LinearLayout root, String label, String value, String hint) {
        label(root, label, hint);
        EditText e = new EditText(this);
        e.setText(value);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        e.setSingleLine(true);
        e.setInputType(InputType.TYPE_CLASS_NUMBER);
        root.addView(e);
        return e;
    }

    private CheckBox check(LinearLayout root, String text, boolean value, String hint) {
        CheckBox c = new CheckBox(this);
        c.setText(text);
        c.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        c.setChecked(value);
        root.addView(c);
        if (hint != null) {
            label(root, null, hint);
        }
        return c;
    }

    private void label(LinearLayout root, String text, String hint) {
        if (text != null) {
            TextView t = new TextView(this);
            t.setText(text);
            t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
            t.setPadding(0, dp(8), 0, dp(2));
            root.addView(t);
        }
        if (hint != null) {
            TextView h = new TextView(this);
            h.setText(hint);
            h.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
            h.setTextColor(0xFF90A4AE);
            h.setPadding(0, 0, 0, dp(2));
            root.addView(h);
        }
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    private static int parse(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Exception e) {
            return def;
        }
    }

    // ------------------------------------------------------------------

    private void collect() {
        p.base = baseBox.getText().toString().trim();
        p.token = tokenBox.getText().toString().trim();
        p.entityId = entityBox.getText().toString().trim();
        p.historyHours = Prefs.clamp(parse(hoursBox.getText().toString(), 12), 1, 168);
        p.pollSeconds = Prefs.clampPoll(parse(pollBox.getText().toString(), 2));
        p.realtimeEnabled = realtimeBox.isChecked();
        p.flashEnabled = flashBox.isChecked();
        p.noiseFilterEnabled = noiseBox.isChecked();
        p.carFilter = carBox.getText().toString().trim();
        p.excludeKeywords = Prefs.splitLines(excludeBox.getText().toString());

        p.soundEnabled = soundBox.isChecked();
        p.vibrateEnabled = vibrateBox.isChecked();
        p.screenWakeEnabled = wakeBox.isChecked();
        p.silentOverride = silentBox.isChecked();
        p.attentionEnabled = attentionBox.isChecked();
        p.alarmAutoStopSec = Prefs.clamp(parse(autoStopBox.getText().toString(), 15), 0, 120);
        p.cooldownSec = Prefs.clamp(parse(cooldownBox.getText().toString(), 60), 0, 600);
        p.connectionWarnSec = Prefs.clamp(parse(connWarnBox.getText().toString(), 45), 10, 600);
        p.dyMode = Prefs.clamp(dySpinner.getSelectedItemPosition(), 0, 3);
        p.dyEscalateSec = Prefs.clamp(parse(dySecondsBox.getText().toString(), 15), 1, 120);

        hoursBox.setText(String.valueOf(p.historyHours));
        pollBox.setText(String.valueOf(p.pollSeconds));
        autoStopBox.setText(String.valueOf(p.alarmAutoStopSec));
        cooldownBox.setText(String.valueOf(p.cooldownSec));
        connWarnBox.setText(String.valueOf(p.connectionWarnSec));
        dySecondsBox.setText(String.valueOf(p.dyEscalateSec));
    }

    private void fill() {
        baseBox.setText(p.base);
        tokenBox.setText(p.token);
        entityBox.setText(p.entityId);
        hoursBox.setText(String.valueOf(p.historyHours));
        pollBox.setText(String.valueOf(p.pollSeconds));
        realtimeBox.setChecked(p.realtimeEnabled);
        flashBox.setChecked(p.flashEnabled);
        noiseBox.setChecked(p.noiseFilterEnabled);
        carBox.setText(p.carFilter);
        excludeBox.setText(Prefs.joinLines(p.excludeKeywords));
        soundBox.setChecked(p.soundEnabled);
        vibrateBox.setChecked(p.vibrateEnabled);
        wakeBox.setChecked(p.screenWakeEnabled);
        silentBox.setChecked(p.silentOverride);
        attentionBox.setChecked(p.attentionEnabled);
        autoStopBox.setText(String.valueOf(p.alarmAutoStopSec));
        cooldownBox.setText(String.valueOf(p.cooldownSec));
        connWarnBox.setText(String.valueOf(p.connectionWarnSec));
        dySpinner.setSelection(Prefs.clamp(p.dyMode, 0, 3));
        dySecondsBox.setText(String.valueOf(p.dyEscalateSec));
    }

    private void testConnection() {
        collect();
        final Prefs snapshot = p;
        testResult.setTextColor(0xFF546E7A);
        testResult.setText("测试中…");
        new Thread(new Runnable() {
            public void run() {
                final String msg;
                try {
                    msg = HaClient.testConnection(snapshot);
                } catch (final HaClient.HaException e) {
                    runOnUiThread(new Runnable() {
                        public void run() {
                            testResult.setTextColor(0xFFC62828);
                            testResult.setText(e.getMessage());
                        }
                    });
                    return;
                }
                runOnUiThread(new Runnable() {
                    public void run() {
                        testResult.setTextColor(0xFF2E7D32);
                        testResult.setText(msg);
                    }
                });
            }
        }, "f1-test").start();
    }
}
