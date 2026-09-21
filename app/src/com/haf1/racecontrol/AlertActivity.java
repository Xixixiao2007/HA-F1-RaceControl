package com.haf1.racecontrol;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 强提醒的全屏横幅。
 *
 * 为什么用 Activity 而不是 Dialog / Toast：
 *   1. 能从后台弹出并**点亮屏幕**（配合下面的窗口标志，不需要 WAKE_LOCK 权限）；
 *   2. 锁屏上也能显示（FLAG_SHOW_WHEN_LOCKED），手机放桌上时能直接看到红旗；
 *   3. 能盖住其它 App —— 比赛时你可能正在看别的页面。
 *
 * launchMode 用 singleTop：连续来两条红旗不会叠出两个页面，而是复用同一个。
 */
public class AlertActivity extends Activity {

    public static final String EXTRA_KIND = "kind";
    public static final String EXTRA_TEXT = "text";
    public static final String EXTRA_TIME = "time";
    public static final String EXTRA_SECTOR = "sector";
    public static final String EXTRA_CAR = "car";
    public static final String EXTRA_ESCALATED = "escalated";
    public static final String EXTRA_AUTOSTOP = "autostop";
    public static final String EXTRA_SOUND = "sound";
    public static final String EXTRA_VIBRATE = "vibrate";
    public static final String EXTRA_SILENT_OVERRIDE = "silent_override";

    private Notifier notifier;
    private final Handler handler = new Handler();

    private LinearLayout root;
    private TextView kindView;
    private TextView textView;
    private TextView glossView;
    private TextView metaView;
    private TextView countdownView;
    private Button ackButton;

    private int autoStopSec = 15;
    private int remaining = 0;

    /** 从任意地方弹出强提醒。 */
    public static void show(Context ctx, String kind, String text, long time,
                            String sector, String car, boolean escalated, Prefs p) {
        Intent i = new Intent(ctx, AlertActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_SINGLE_TOP
                | Intent.FLAG_ACTIVITY_NO_USER_ACTION);
        i.putExtra(EXTRA_KIND, kind);
        i.putExtra(EXTRA_TEXT, text);
        i.putExtra(EXTRA_TIME, time);
        i.putExtra(EXTRA_SECTOR, sector);
        i.putExtra(EXTRA_CAR, car);
        i.putExtra(EXTRA_ESCALATED, escalated);
        i.putExtra(EXTRA_AUTOSTOP, p == null ? 15 : p.alarmAutoStopSec);
        i.putExtra(EXTRA_SOUND, p == null || p.soundEnabled);
        i.putExtra(EXTRA_VIBRATE, p == null || p.vibrateEnabled);
        i.putExtra(EXTRA_SILENT_OVERRIDE, p == null || p.silentOverride);
        ctx.startActivity(i);
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        notifier = new Notifier(this);

        // 亮屏 + 锁屏可见。用窗口标志就够，**不需要 WAKE_LOCK 权限**。
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD);

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(dp(22), dp(30), dp(22), dp(30));

        kindView = new TextView(this);
        kindView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 44);
        kindView.setGravity(Gravity.CENTER);
        kindView.setTextColor(Color.WHITE);
        root.addView(kindView);

        textView = new TextView(this);
        textView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        textView.setGravity(Gravity.CENTER);
        textView.setTextColor(Color.WHITE);
        textView.setPadding(0, dp(18), 0, dp(6));
        root.addView(textView);

        // 中文简述（仲裁/判罚类才有）
        glossView = new TextView(this);
        glossView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        glossView.setGravity(Gravity.CENTER);
        glossView.setTextColor(0xFFFFFFFF);
        glossView.setBackgroundColor(0x33000000);
        glossView.setPadding(dp(12), dp(8), dp(12), dp(8));
        glossView.setVisibility(View.GONE);
        root.addView(glossView);

        metaView = new TextView(this);
        metaView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        metaView.setGravity(Gravity.CENTER);
        metaView.setTextColor(0xCCFFFFFF);
        metaView.setPadding(0, dp(12), 0, 0);
        root.addView(metaView);

        countdownView = new TextView(this);
        countdownView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        countdownView.setGravity(Gravity.CENTER);
        countdownView.setTextColor(0x99FFFFFF);
        countdownView.setPadding(0, dp(10), 0, dp(20));
        root.addView(countdownView);

        ackButton = new Button(this);
        ackButton.setText("知道了");
        ackButton.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        ackButton.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                finish();
            }
        });
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        bp.topMargin = dp(6);
        root.addView(ackButton, bp);

        setContentView(root);
        applyIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        applyIntent(intent);
    }

    private void applyIntent(Intent i) {
        String kind = i.getStringExtra(EXTRA_KIND);
        if (kind == null) {
            kind = Classifier.K_OTHER;
        }
        String text = i.getStringExtra(EXTRA_TEXT);
        String sector = i.getStringExtra(EXTRA_SECTOR);
        String car = i.getStringExtra(EXTRA_CAR);
        boolean escalated = i.getBooleanExtra(EXTRA_ESCALATED, false);

        root.setBackgroundColor(Classifier.barColor(kind));
        kindView.setText(Classifier.label(kind));
        textView.setText(text == null || text.length() == 0 ? "(无文本)" : text);

        // 有中文简述就补一行 —— 横幅上字号大，原文往往是长串术语，来不及读
        String gloss = Translator.gloss(text);
        if (gloss == null || gloss.length() == 0) {
            glossView.setVisibility(View.GONE);
        } else {
            glossView.setVisibility(View.VISIBLE);
            glossView.setText(gloss);
        }

        StringBuilder meta = new StringBuilder();
        if (escalated) {
            meta.append("持续未解除 · ");
        }
        if (sector != null && sector.length() > 0) {
            meta.append("区段 ").append(sector).append(" · ");
        }
        if (car != null && car.length() > 0) {
            meta.append("车号 ").append(car).append(" · ");
        }
        long t = i.getLongExtra(EXTRA_TIME, 0L);
        if (t > 0) {
            meta.append(new java.text.SimpleDateFormat("HH:mm:ss",
                    java.util.Locale.getDefault()).format(new java.util.Date(t)));
        }
        metaView.setText(meta.toString());

        autoStopSec = i.getIntExtra(EXTRA_AUTOSTOP, 15);
        startAlert(i);
    }

    private void startAlert(Intent i) {
        handler.removeCallbacks(tick);
        if (notifier != null) {
            notifier.startAlarm(i.getStringExtra(EXTRA_KIND),
                    i.getBooleanExtra(EXTRA_SOUND, true),
                    i.getBooleanExtra(EXTRA_VIBRATE, true),
                    i.getBooleanExtra(EXTRA_SILENT_OVERRIDE, true));
        }
        if (autoStopSec <= 0) {
            countdownView.setText("点「知道了」停止");
            return;
        }
        remaining = autoStopSec;
        handler.post(tick);
    }

    private final Runnable tick = new Runnable() {
        public void run() {
            if (remaining <= 0) {
                finish();
                return;
            }
            countdownView.setText(remaining + " 秒后自动停止");
            remaining--;
            handler.postDelayed(this, 1000L);
        }
    };

    /** 用户按返回键等于点了「知道了」。 */
    @Override
    public void onBackPressed() {
        finish();
    }

    @Override
    protected void onPause() {
        super.onPause();
        // 只要不是被新的提醒替换，离开界面就停掉声音
        if (!isFinishing() && !isChangingConfigurations()) {
            stopAlert();
        }
    }

    @Override
    protected void onDestroy() {
        stopAlert();
        super.onDestroy();
    }

    private void stopAlert() {
        handler.removeCallbacks(tick);
        if (notifier != null) {
            notifier.stopAlarm();
            notifier.release();
            notifier = null;
        }
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }
}
