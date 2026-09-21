package com.haf1.racecontrol;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.media.ToneGenerator;
import android.net.Uri;
import android.os.Vibrator;

/**
 * 提醒执行器：声音 + 震动 + 通知栏。
 *
 * ## 两种级别
 * - **轻提醒**（黄旗 / 黑白旗 / 双黄旗刚出现）：一声短音 + 一短震，走通知通道。
 * - **强提醒**（红旗 / 安全车 / VSC / 持续未清除的双黄）：**循环**警报音 + 循环震动，
 *   默认走 **`STREAM_ALARM`**，也就是会突破系统静音/振动模式。
 *
 * 为什么用 STREAM_ALARM：用户明确要求"强提醒"，而比赛时手机多半是静音或振动放在桌上。
 * 走通知通道的话，静音模式下红旗不会响 —— 那就完全失去意义了。
 * 这是可配置项（`Prefs.silentOverride`）。
 *
 * 不用任何音频资源文件：铃声取系统默认闹钟铃声，取不到就退回 ToneGenerator 蜂鸣。
 */
public class Notifier {

    private final Context ctx;

    private MediaPlayer alarm;
    private ToneGenerator tone;
    private Vibrator vibrator;
    private boolean alarmOn = false;

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
     * 往通知栏推一条强提醒，**锁屏可见**。
     *
     * 为什么全屏横幅之外还要这个：
     *   全屏横幅（{@link AlertActivity}）负责"当场叫醒你"，但它是转瞬即逝的 ——
     *   你要是正好没看手机，回头完全不知道刚才响过什么。通知栏这条会留在那里，
     *   下拉就能看到是哪条旗语。锁屏上也能看到（`VISIBILITY_PUBLIC`）。
     *
     * 用 `PRIORITY_HIGH` 让它在锁屏上方显示。targetSdk 是 23，
     * 不需要通知渠道（渠道是 API 26 才有的）。
     */
    public void pushNotification(String kind, String text, RaceMessage msg, Prefs p) {
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
            // 自动停止秒数设 0：从通知点进去的这一次不该自己消失
            open.putExtra(AlertActivity.EXTRA_AUTOSTOP, 0);
            open.putExtra(AlertActivity.EXTRA_SOUND, false);     // 通知点开时别再响一遍
            open.putExtra(AlertActivity.EXTRA_VIBRATE, false);
            PendingIntent pi = PendingIntent.getActivity(ctx, 0, open,
                    PendingIntent.FLAG_UPDATE_CURRENT);

            String title = "F1 " + Classifier.label(kind);
            StringBuilder body = new StringBuilder(text == null ? "" : text);
            if (msg != null && msg.sector != null && msg.sector.length() > 0) {
                body.append("  ·  扇区 ").append(msg.sector);
            }

            Notification n = new Notification.Builder(ctx)
                    .setSmallIcon(android.R.drawable.stat_sys_warning)
                    .setContentTitle(title)
                    .setContentText(body.toString())
                    .setStyle(new Notification.BigTextStyle().bigText(body.toString()))
                    .setPriority(Notification.PRIORITY_HIGH)
                    .setVisibility(Notification.VISIBILITY_PUBLIC)
                    .setCategory(Notification.CATEGORY_ALARM)
                    .setDefaults(0)                    // 声音震动由本类自己放，不要系统再放一遍
                    .setAutoCancel(true)
                    .setContentIntent(pi)
                    .build();
            nm.notify(NOTIFY_ID, n);
        } catch (Throwable ignored) {
            // 通知发不出去不能影响主流程（全屏横幅还是会弹）
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

    /** 一声短音 + 一短震。会打断上一次轻提醒，避免叠成噪音。 */
    public void attention(boolean sound, boolean vibrate) {
        if (vibrate) {
            vibrateOnce(PATTERN_ATTENTION);
        }
        if (!sound) {
            return;
        }
        stopAlarm();                    // 轻提醒不该和强提醒的警报音叠在一起
        try {
            Uri uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
            if (uri != null) {
                MediaPlayer mp = new MediaPlayer();
                mp.setAudioStreamType(AudioManager.STREAM_NOTIFICATION);
                mp.setDataSource(ctx, uri);
                mp.prepare();
                mp.start();
                final MediaPlayer toRelease = mp;
                mp.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                    public void onCompletion(MediaPlayer m) {
                        try {
                            m.release();
                        } catch (Throwable ignored) {
                            // ignore
                        }
                    }
                });
                // 兜底：极短铃声 + 异常路径都不能留下泄漏的播放器
                new android.os.Handler().postDelayed(new Runnable() {
                    public void run() {
                        try {
                            toRelease.release();
                        } catch (Throwable ignored) {
                            // ignore
                        }
                    }
                }, 3000L);
                return;
            }
        } catch (Throwable ignored) {
            // 落到下面的蜂鸣
        }
        beep(AudioManager.STREAM_NOTIFICATION, 250);
    }

    // ------------------------------------------------------------------
    // 强提醒
    // ------------------------------------------------------------------

    /**
     * 循环警报音 + 循环震动，直到 {@link #stopAlarm()} 或自动停止。
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
        int stream = silentOverride ? AudioManager.STREAM_ALARM : AudioManager.STREAM_NOTIFICATION;
        try {
            Uri uri = RingtoneManager.getDefaultUri(
                    silentOverride ? RingtoneManager.TYPE_ALARM : RingtoneManager.TYPE_NOTIFICATION);
            if (uri != null) {
                alarm = new MediaPlayer();
                alarm.setAudioStreamType(stream);
                alarm.setDataSource(ctx, uri);
                alarm.setLooping(true);
                alarm.prepare();
                alarm.start();
                return;
            }
        } catch (Throwable ignored) {
            // 落到下面的蜂鸣
        }
        // 没有可用铃声：用 ToneGenerator 反复发警报音
        beepLoop(stream);
    }

    /** 停止一切正在进行的提醒。 */
    public void stopAlarm() {
        alarmOn = false;
        MediaPlayer mp = alarm;
        alarm = null;
        if (mp != null) {
            try {
                if (mp.isPlaying()) {
                    mp.stop();
                }
            } catch (Throwable ignored) {
                // ignore
            }
            try {
                mp.release();
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

    private void beep(int stream, int ms) {
        try {
            ToneGenerator t = new ToneGenerator(stream, 90);
            t.startTone(ToneGenerator.TONE_PROP_BEEP, ms);
            final ToneGenerator toRelease = t;
            new android.os.Handler().postDelayed(new Runnable() {
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

    /** 用 ToneGenerator 模拟循环警报：每 700ms 发一声音，直到 stopAlarm。 */
    private void beepLoop(final int stream) {
        try {
            tone = new ToneGenerator(stream, 95);
        } catch (Throwable ignored) {
            tone = null;
        }
        if (tone == null) {
            return;
        }
        final android.os.Handler h = new android.os.Handler();
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
                h.postDelayed(this, 900L);
            }
        };
        h.post(r);
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
