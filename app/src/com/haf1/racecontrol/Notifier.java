package com.haf1.racecontrol;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.media.ToneGenerator;
import android.net.Uri;
import android.os.Handler;
import android.os.Vibrator;

/**
 * 提醒执行器：声音 + 震动 + 通知栏。
 *
 * ## 两种级别
 * - **轻提醒**（黄旗 / 黑白旗 / 双黄旗刚出现）：一声短音 + 一短震，走通知通道。
 * - **超强提醒**（红旗 / 安全车 / VSC / 持续未清除的双黄）：重复警报音 + 循环震动，
 *   默认走 **`STREAM_ALARM`**，也就是会突破系统静音/振动模式。
 *
 * 为什么用 STREAM_ALARM：用户明确要求"强提醒"，而比赛时手机多半是静音或振动放在桌上。
 * 走通知通道的话，静音模式下红旗不会响 —— 那就完全失去意义了。
 *
 * ## 为什么用 Ringtone 而不是 MediaPlayer
 * v1.4 用的是 `RingtoneManager.getRingtone(...).play()`，在用户那台 Android 6 上验证过能用。
 * v2.0 一度换成 `MediaPlayer` + `setDataSource(ctx, settingsUri)`，结果**真机上没声音** ——
 * 设置铃声是 `content://settings/system/...` 这种"设置项 URI"，MediaPlayer 走
 * `prepare()` 在部分设备上会静默失败。已退回 Ringtone。
 *
 * Ringtone 在 API 23 上不能循环，所以用一个 Handler 定时重放（`RERING_MS`），
 * 效果等价于循环。
 */
public class Notifier {

    private final Context ctx;
    private final Handler handler = new Handler();

    private Ringtone ringtone;
    private ToneGenerator tone;
    private Vibrator vibrator;

    private volatile boolean alarmOn = false;
    /** 重放间隔。比常见提示音略长一点，听起来是"一遍接一遍"而不是急促的连续音。 */
    private static final long RERING_MS = 3000L;

    public Notifier(Context ctx) {
        this.ctx = ctx;
        try {
            vibrator = (Vibrator) ctx.getSystemService(Context.VIBRATOR_SERVICE);
        } catch (Throwable ignored) {
            vibrator = null;
        }
    }

    // ------------------------------------------------------------------
    // 通知栏
    // ------------------------------------------------------------------

    private static final int NOTIFY_ID = 0x51F1;      // "F1" 的十六进制 + 前缀

    /**
     * 往通知栏推一条超强提醒，**锁屏可见**。
     *
     * 为什么全屏横幅之外还要这个：
     *   全屏横幅（{@link AlertActivity}）负责"当场叫醒你"，但它是转瞬即逝的 ——
     *   你要是正好没看手机，回头完全不知道刚才响过什么。通知栏这条会留在那里，
     *   下拉就能看到是哪条旗语。
     */
    public void pushNotification(String kind, String text, RaceMessage msg) {
        try {
            NotificationManager nm = (NotificationManager)
                    ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }
            Intent open = new Intent(ctx, AlertActivity.class);
            open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            open.putExtra(AlertActivity.EXTRA_KIND, kind);
            open.putExtra(AlertActivity.EXTRA_TEXT, text);
            if (msg != null) {
                open.putExtra(AlertActivity.EXTRA_TIME, msg.time);
                open.putExtra(AlertActivity.EXTRA_SECTOR, msg.sector);
                open.putExtra(AlertActivity.EXTRA_CAR, msg.carNumber);
            }
            // 从通知点进去的这一次不该自己消失，也不该再响一遍（刚响过了）
            open.putExtra(AlertActivity.EXTRA_AUTOSTOP, 0);
            open.putExtra(AlertActivity.EXTRA_SOUND, false);
            open.putExtra(AlertActivity.EXTRA_VIBRATE, false);
            PendingIntent pi = PendingIntent.getActivity(ctx, 0, open,
                    PendingIntent.FLAG_UPDATE_CURRENT);

            String title = "F1 " + Classifier.label(kind);
            StringBuilder body = new StringBuilder(text == null ? "" : text);
            String gloss = Translator.gloss(text);
            if (gloss != null) {
                body.append('\n').append(gloss);
            }

            Notification n = new Notification.Builder(ctx)
                    .setSmallIcon(android.R.drawable.stat_sys_warning)
                    .setContentTitle(title)
                    .setContentText(body.toString())
                    .setStyle(new Notification.BigTextStyle().bigText(body.toString()))
                    .setPriority(Notification.PRIORITY_HIGH)
                    .setVisibility(Notification.VISIBILITY_PUBLIC)
                    .setCategory(Notification.CATEGORY_ALARM)
                    .setDefaults(0)          // 声音震动由本类自己放，不要系统再放一遍
                    .setAutoCancel(true)
                    .setContentIntent(pi)
                    .build();
            nm.notify(NOTIFY_ID, n);
        } catch (Throwable ignored) {
            // 通知发不出去不影响主流程（全屏横幅还是会弹）
        }
    }

    /** 撤掉通知栏那条。 */
    public void clearNotification() {
        try {
            NotificationManager nm = (NotificationManager)
                    ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.cancel(NOTIFY_ID);
            }
        } catch (Throwable ignored) {
            // ignore
        }
    }

    // ------------------------------------------------------------------
    // 震动节奏：不同类型给不同手感，闭着眼也能分辨
    // ------------------------------------------------------------------

    /** 红旗：三长震（最急促）。 */
    public static final long[] PATTERN_RED = {0, 500, 250, 500, 250, 500};
    /** 安全车：两震。 */
    public static final long[] PATTERN_SC = {0, 400, 200, 400};
    /** 虚拟安全车：一长震。 */
    public static final long[] PATTERN_VSC = {0, 450};
    /** 双黄旗：三短震。 */
    public static final long[] PATTERN_DY = {0, 180, 120, 180, 120, 180};
    /** 轻提醒：一短震。 */
    public static final long[] PATTERN_ATTENTION = {0, 90};

    public static long[] patternFor(String kind) {
        if (Classifier.K_RED.equals(kind)) {
            return PATTERN_RED;
        }
        if (Classifier.K_SC.equals(kind)) {
            return PATTERN_SC;
        }
        if (Classifier.K_VSC.equals(kind)) {
            return PATTERN_VSC;
        }
        if (Classifier.K_DY.equals(kind)) {
            return PATTERN_DY;
        }
        return PATTERN_ATTENTION;
    }

    // ------------------------------------------------------------------
    // 轻提醒
    // ------------------------------------------------------------------

    /**
     * 一声短音 + 一短震。
     *
     * ⚠️ 顺序要紧：**先停声音、再起震动**。早先的写法是先震动再调 stopAlarm()，
     * 而 stopAlarm() 里有 vibrator.cancel() —— 刚起的震动被自己立刻取消了，
     * 表现就是"轻提醒完全没感觉"。
     */
    public void attention(boolean sound, boolean vibrate) {
        stopSound();                        // 只停声音，不碰震动
        if (vibrate) {
            vibrateOnce(PATTERN_ATTENTION);
        }
        if (!sound) {
            return;
        }
        if (loadRingtone(RingtoneManager.TYPE_NOTIFICATION, false) && ringOnce()) {
            return;
        }
        beep(AudioManager.STREAM_NOTIFICATION, 250);
    }

    // ------------------------------------------------------------------
    // 超强提醒
    // ------------------------------------------------------------------

    /**
     * 重复警报音 + 循环震动，直到 {@link #stopAlarm()} 或自动停止。
     *
     * @param kind           决定震动节奏
     * @param silentOverride true 用闹钟通道（突破静音），false 用通知通道
     */
    public void startAlarm(String kind, boolean sound, boolean vibrate, boolean silentOverride) {
        stopAlarm();
        alarmOn = true;

        if (vibrate) {
            vibrateLoop(patternFor(kind));
        }
        if (!sound) {
            return;
        }
        int type = silentOverride ? RingtoneManager.TYPE_ALARM : RingtoneManager.TYPE_NOTIFICATION;
        int stream = silentOverride ? AudioManager.STREAM_ALARM : AudioManager.STREAM_NOTIFICATION;

        if (loadRingtone(type, silentOverride)) {
            ringOnce();
            handler.postDelayed(alarmTicker, RERING_MS);
        } else {
            // 系统里没有可用铃声：用 ToneGenerator 反复发警报音
            beepLoop(stream);
        }
    }

    /** 定时重放：Ringtone 在 API 23 上不能自己循环。 */
    private final Runnable alarmTicker = new Runnable() {
        public void run() {
            if (!alarmOn) {
                return;
            }
            ringOnce();
            handler.postDelayed(this, RERING_MS);
        }
    };

    /** 只停声音，**不动震动**。轻提醒要用它，否则会把刚起的震动取消掉。 */
    private void stopSound() {
        alarmOn = false;
        handler.removeCallbacks(alarmTicker);
        if (ringtone != null) {
            try {
                if (ringtone.isPlaying()) {
                    ringtone.stop();
                }
            } catch (Throwable ignored) {
                // ignore
            }
        }
        if (tone != null) {
            try {
                tone.stopTone();
            } catch (Throwable ignored) {
                // ignore
            }
            try {
                tone.release();
            } catch (Throwable ignored) {
                // ignore
            }
            tone = null;
        }
    }

    /** 停止一切正在进行的提醒（声音 + 震动）。 */
    public void stopAlarm() {
        stopSound();
        if (vibrator != null) {
            try {
                vibrator.cancel();
            } catch (Throwable ignored) {
                // ignore
            }
        }
    }

    public boolean isAlarming() {
        return alarmOn;
    }

    // ------------------------------------------------------------------
    // 底层
    // ------------------------------------------------------------------

    /**
     * 准备好铃声对象。**不用 MediaPlayer**：设置铃声是 content://settings/... 这种
     * "设置项 URI"，MediaPlayer 的 prepare() 在部分设备上会静默失败（真机上踩过）。
     * Ringtone 是系统专门为这件事提供的 API。
     */
    private boolean loadRingtone(int type, boolean alarmUsage) {
        try {
            if (ringtone != null && ringtone.isPlaying()) {
                ringtone.stop();
            }
            Uri uri = RingtoneManager.getDefaultUri(type);
            if (uri == null) {
                ringtone = null;
                return false;
            }
            Ringtone r = RingtoneManager.getRingtone(ctx, uri);
            if (r == null) {
                ringtone = null;
                return false;
            }
            // USAGE_ALARM 才会走闹钟音量（也就是能突破静音）
            r.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(alarmUsage ? AudioAttributes.USAGE_ALARM
                            : AudioAttributes.USAGE_NOTIFICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build());
            ringtone = r;
            return true;
        } catch (Throwable t) {
            ringtone = null;
            return false;
        }
    }

    private boolean ringOnce() {
        try {
            if (ringtone == null) {
                return false;
            }
            if (!ringtone.isPlaying()) {
                ringtone.play();
            }
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    private void beep(int stream, int ms) {
        try {
            ToneGenerator t = new ToneGenerator(stream, 90);
            t.startTone(ToneGenerator.TONE_PROP_BEEP, ms);
            final ToneGenerator toRelease = t;
            handler.postDelayed(new Runnable() {
                public void run() {
                    try {
                        toRelease.release();
                    } catch (Throwable ignored) {
                        // ignore
                    }
                }
            }, ms + 400L);
        } catch (Throwable ignored) {
            // 连蜂鸣都放不出来就算了，不能因为提示音把主流程搞崩
        }
    }

    /** 用 ToneGenerator 模拟循环警报：每 900ms 发一声音，直到 stopAlarm。 */
    private void beepLoop(final int stream) {
        try {
            tone = new ToneGenerator(stream, 95);
        } catch (Throwable ignored) {
            tone = null;
        }
        if (tone == null) {
            return;
        }
        Runnable r = new Runnable() {
            public void run() {
                if (!alarmOn || tone == null) {
                    return;
                }
                try {
                    tone.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 600);
                } catch (Throwable ignored) {
                    // ignore
                }
                handler.postDelayed(this, 900L);
            }
        };
        handler.post(r);
    }

    private void vibrateOnce(long[] pattern) {
        if (vibrator == null) {
            return;
        }
        try {
            vibrator.vibrate(pattern, -1);
        } catch (Throwable ignored) {
            // 没震动权限或没有马达都无所谓
        }
    }

    private void vibrateLoop(long[] pattern) {
        if (vibrator == null) {
            return;
        }
        try {
            // repeat=0 表示从下标 0 开始无限循环
            vibrator.vibrate(pattern, 0);
        } catch (Throwable ignored) {
            // ignore
        }
    }

    /** Activity 销毁时调用，释放底层音频资源。 */
    public void release() {
        stopAlarm();
        vibrator = null;
    }
}
