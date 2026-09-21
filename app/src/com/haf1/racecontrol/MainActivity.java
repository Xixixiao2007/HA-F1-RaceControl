package com.haf1.racecontrol;

import android.animation.ArgbEvaluator;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.animation.LinearInterpolator;
import android.widget.BaseAdapter;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 主页：赛事控制消息流水。
 *
 * 版面自上而下：
 *   1. 标题栏（实体名 + 右上角设置键）
 *   2. **赛道状态条** —— 按优先级显示当前最高级别旗语（RED > SC > VSC > 双黄 > 黄）
 *   3. 状态行（连接方式 / 条数 / 丢号提示 / 过滤开关）
 *   4. 消息列表（新消息闪动 → 定格到等级配色）
 */
public class MainActivity extends Activity {

    private static final int REQ_SETTINGS = 1;

    private static final int COLOR_BAR = 0xFF263238;
    private static final int COLOR_BAR_TEXT = 0xFFFFFFFF;
    private static final int COLOR_BAR_SUB = 0xFFB0BEC5;
    private static final int COLOR_ERROR = 0xFFFF8A80;
    private static final int COLOR_FLASH = 0xFF00E5FF;

    /** 实时在线时的对账间隔：兜住断线重连期间漏掉的变化。 */
    private static final long RECONCILE_MS = 60000L;
    /** 闪动持续时间。 */
    private static final long FLASH_MS = 1200L;
    /** 内部节拍：推动双黄升级判定与断线检测。 */
    private static final long TICK_MS = 500L;
    /** 最低限度静音的提示音间隔。 */
    private static final long BEEP_MIN_GAP_MS = 1500L;

    private static final String STORE_PREFS = "ha_f1_racecontrol_store";
    private static final String STORE_KEY = "blob";

    private Prefs prefs;
    private Notifier notifier;
    private AlertGate gate = new AlertGate();
    private TrackState track = new TrackState();
    private final MessageStore store = new MessageStore();

    private TextView titleView;
    private TextView stateView;
    private TextView stateDetailView;
    private LinearLayout stateBar;
    private TextView statusView;
    private TextView filterToggle;

    private ListView listView;
    private RowAdapter adapter;
    private final List<RaceMessage> shown = new ArrayList<RaceMessage>();
    private final Set<String> flashing = new HashSet<String>();

    private final Handler handler = new Handler();
    private boolean busy = false;
    private boolean autoOpenedSettings = false;
    private String lastSignature = null;
    private boolean backfillDone = false;

    private final SimpleDateFormat fmt =
            new SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault());

    // ---- 实时通道 ----
    private volatile boolean wsRunning = false;
    private volatile boolean wsConnected = false;
    private volatile HaWebSocket ws = null;
    private Thread wsThread = null;
    private String connMode = "";
    private String wsNote = "";
    private long lastBeepAt = 0L;

    // ---- 断线告警 ----
    private long lastDataAt = 0L;
    private long wsDownSince = 0L;
    private boolean connAlarmFired = false;

    // ---- 丢号检测 ----
    private int lastSequence = -1;
    private int droppedCount = 0;

    // ------------------------------------------------------------------
    // 界面
    // ------------------------------------------------------------------

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = Prefs.load(this);
        notifier = new Notifier(this);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFFF5F5F5);
        root.addView(buildTitleBar());
        root.addView(buildStateBar());
        root.addView(buildStatusRow());

        listView = new ListView(this);
        listView.setDivider(null);
        listView.setDividerHeight(0);
        listView.setCacheColorHint(0);
        adapter = new RowAdapter();
        listView.setAdapter(adapter);
        root.addView(listView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(root);
        loadStore();
    }

    private View buildTitleBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(COLOR_BAR);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(14), dp(9), dp(6), dp(9));

        titleView = new TextView(this);
        titleView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        titleView.setTextColor(COLOR_BAR_TEXT);
        titleView.setSingleLine(true);
        titleView.setEllipsize(TextUtils.TruncateAt.END);
        bar.addView(titleView, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        TextView settings = new TextView(this);
        settings.setText("设置");
        settings.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        settings.setTextColor(COLOR_BAR_TEXT);
        settings.setPadding(dp(14), dp(8), dp(14), dp(8));
        settings.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                startActivityForResult(new Intent(MainActivity.this, SettingsActivity.class),
                        REQ_SETTINGS);
            }
        });
        bar.addView(settings);
        return bar;
    }

    /** 赛道状态条：整条按当前级别上色，字很大，扫一眼就知道现在什么状况。 */
    private View buildStateBar() {
        stateBar = new LinearLayout(this);
        stateBar.setOrientation(LinearLayout.HORIZONTAL);
        stateBar.setGravity(Gravity.CENTER_VERTICAL);
        stateBar.setPadding(dp(14), dp(8), dp(14), dp(8));
        stateBar.setBackgroundColor(0xFF455A64);

        stateView = new TextView(this);
        stateView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 26);
        stateView.setSingleLine(true);
        stateBar.addView(stateView);

        stateDetailView = new TextView(this);
        stateDetailView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        stateDetailView.setSingleLine(true);
        stateDetailView.setEllipsize(TextUtils.TruncateAt.END);
        stateDetailView.setPadding(dp(12), dp(6), 0, 0);
        stateBar.addView(stateDetailView, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        return stateBar;
    }

    private View buildStatusRow() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setBackgroundColor(0xFFECEFF1);
        row.setPadding(dp(14), dp(3), dp(6), dp(3));

        statusView = new TextView(this);
        statusView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        statusView.setTextColor(0xFF546E7A);
        statusView.setSingleLine(true);
        statusView.setEllipsize(TextUtils.TruncateAt.END);
        row.addView(statusView, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        filterToggle = new TextView(this);
        filterToggle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        filterToggle.setTextColor(0xFF00695C);
        filterToggle.setPadding(dp(10), dp(6), dp(12), dp(6));
        filterToggle.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                prefs.noiseFilterEnabled = !prefs.noiseFilterEnabled;
                prefs.save(MainActivity.this);
                rebuildShown();
                adapter.notifyDataSetChanged();
                updateStatusRow();
            }
        });
        row.addView(filterToggle);
        return row;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    // ------------------------------------------------------------------
    // 生命周期
    // ------------------------------------------------------------------

    @Override
    protected void onResume() {
        super.onResume();
        handler.removeCallbacks(pauseStopTask);   // 回来了，取消"宽限期结束就断开"
        prefs = Prefs.load(this);
        prefs.applyTo(gate);

        String sig = signature(prefs);
        if (lastSignature == null || !sig.equals(lastSignature)) {
            lastSignature = sig;
            // 只清空"显示层"的状态；消息本体留着，避免改个设置就丢历史
            track.reset();
            gate.reset();
            lastBeepAt = 0L;
            lastSequence = -1;
            droppedCount = 0;
            rebuildShown();
            if (adapter != null) {
                adapter.notifyDataSetChanged();
            }
        }

        if (!prefs.isConfigured()) {
            titleView.setText("未配置 · HA-F1-RaceControl");
            renderStateBar();
            setStatus("请点右上角「设置」填写地址、令牌和实体名", false);
            if (!autoOpenedSettings) {
                autoOpenedSettings = true;
                startActivityForResult(new Intent(this, SettingsActivity.class), REQ_SETTINGS);
            }
            return;
        }

        titleView.setText("F1 Race Control · " + prefs.entityId);
        setStatus("", true);
        startRealtime();
        startPolling();
        handler.removeCallbacks(tickTask);
        handler.post(tickTask);
    }

    /**
     * 离开界面后，数据通道还保持多久（毫秒）。
     *
     * 为什么不能一 onPause 就断开：
     *   1. **强提醒本身是个独立 Activity** —— 它一弹出来，主页就 onPause。
     *      如果这时断开 WebSocket，恰恰是在红旗期间掉线，最不该断的时刻。
     *   2. **屏幕熄灭时 App 会暂停。** 比赛时手机多半是锁屏放在桌上，
     *      要是暂停就断线，红旗根本不会响 —— 那就完全违背了这个 App 的用途。
     *
     * 所以给一个宽限期：短时间内离开（弹提醒、切出去看一眼）保持连接；
     * 长时间真的不用了才断开，免得白耗电。
     *
     * 已知局限：Android 6 引入了 Doze，长时间静止+熄屏后会限制网络。
     * 要做到"锁屏几小时也必定收到红旗"，正规做法是把数据通道放进
     * 前台 Service（带常驻通知）。那是下一步的事，现在先用宽限期。
     */
    private static final long PAUSE_GRACE_MS = 30 * 60 * 1000L;

    private final Runnable pauseStopTask = new Runnable() {
        public void run() {
            stopRealtime();
            stopPolling();
            handler.removeCallbacks(tickTask);
            saveStore();
        }
    };

    @Override
    protected void onPause() {
        super.onPause();
        // 先落盘（便宜），数据通道留到宽限期结束再关
        saveStore();
        handler.removeCallbacks(pauseStopTask);
        handler.postDelayed(pauseStopTask, PAUSE_GRACE_MS);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        handler.removeCallbacks(pauseStopTask);
        handler.removeCallbacks(tickTask);
        stopRealtime();
        stopPolling();
        if (notifier != null) {
            notifier.release();
        }
    }

    private static String signature(Prefs p) {
        return p.baseUrl() + "|" + p.token + "|" + p.entityId + "|" + p.historyHours
                + "|" + p.noiseFilterEnabled + "|" + p.carFilter + "|"
                + p.excludeKeywords.size() + "|" + p.realtimeEnabled + "|" + p.dyMode;
    }

    // ------------------------------------------------------------------
    // 落盘
    // ------------------------------------------------------------------

    private void loadStore() {
        try {
            SharedPreferences sp = getSharedPreferences(STORE_PREFS, Context.MODE_PRIVATE);
            store.load(sp.getString(STORE_KEY, ""));
            if (store.size() > 0) {
                // 重建赛道状态，这样刚打开 App 也能看到当前是什么旗
                List<RaceMessage> asc = store.sortedDesc();
                for (int i = asc.size() - 1; i >= 0; i--) {
                    track.onMessage(asc.get(i));
                }
                rebuildShown();
                adapter.notifyDataSetChanged();
            }
        } catch (Throwable ignored) {
            // 读不出来就当没有历史
        }
    }

    private void saveStore() {
        try {
            store.trimTo(prefs.maxStored);
            getSharedPreferences(STORE_PREFS, Context.MODE_PRIVATE)
                    .edit().putString(STORE_KEY, store.serialize()).apply();
        } catch (Throwable ignored) {
            // 存不下也不影响使用
        }
    }

    // ------------------------------------------------------------------
    // 通道一：WebSocket
    // ------------------------------------------------------------------

    private void startRealtime() {
        stopRealtime();
        if (!prefs.realtimeEnabled) {
            connMode = "";
            wsDownSince = System.currentTimeMillis();
            return;
        }
        wsRunning = true;
        connMode = "连接中";

        wsThread = new Thread(new Runnable() {
            public void run() {
                int failures = 0;
                while (wsRunning) {
                    final HaWebSocket sock = new HaWebSocket(prefs, wsListener);
                    ws = sock;
                    boolean ok = false;
                    try {
                        ok = sock.runAndReport();
                    } catch (Throwable ignored) {
                        // runAndReport 内部已兜住
                    }
                    ws = null;
                    if (!wsRunning) {
                        break;
                    }
                    if (ok) {
                        failures = 0;
                    }
                    failures++;
                    long waitSec = Math.min(30L, 1L << Math.min(failures, 5));
                    try {
                        Thread.sleep(waitSec * 1000L);
                    } catch (InterruptedException e) {
                        break;
                    }
                }
            }
        }, "f1-ws");
        wsThread.start();
    }

    private void stopRealtime() {
        wsRunning = false;
        HaWebSocket sock = ws;
        if (sock != null) {
            sock.shutdown();
        }
        ws = null;
        wsConnected = false;
        Thread t = wsThread;
        wsThread = null;
        if (t != null) {
            t.interrupt();
        }
    }

    private final HaWebSocket.Listener wsListener = new HaWebSocket.Listener() {

        public void onOpen() {
            wsConnected = true;
            handler.post(new Runnable() {
                public void run() {
                    wsNote = "";
                    connMode = "实时";
                    connAlarmFired = false;
                    wsDownSince = 0L;
                    lastDataAt = System.currentTimeMillis();
                    if (backfillDone) {
                        fetchOnce();          // 封住回溯结束到订阅建立之间的缝
                    }
                    updateStatusRow();
                    renderStateBar();
                }
            });
        }

        public void onState(final RaceMessage message) {
            handler.post(new Runnable() {
                public void run() {
                    lastDataAt = System.currentTimeMillis();
                    List<RaceMessage> one = new ArrayList<RaceMessage>(1);
                    one.add(message);
                    merge(one, true);
                }
            });
        }

        public void onError(final String message) {
            wsConnected = false;
            handler.post(new Runnable() {
                public void run() {
                    wsNote = "实时中断：" + message;
                    connMode = "轮询";
                    if (wsDownSince == 0L) {
                        wsDownSince = System.currentTimeMillis();
                    }
                    updateStatusRow();
                }
            });
        }

        public void onClose() {
            wsConnected = false;
            handler.post(new Runnable() {
                public void run() {
                    if (wsRunning) {
                        connMode = "重连中";
                    }
                    if (wsDownSince == 0L) {
                        wsDownSince = System.currentTimeMillis();
                    }
                    updateStatusRow();
                }
            });
        }
    };

    // ------------------------------------------------------------------
    // 通道二：REST
    // ------------------------------------------------------------------

    private void startPolling() {
        handler.removeCallbacks(pollTask);
        handler.post(pollTask);
    }

    private void stopPolling() {
        handler.removeCallbacks(pollTask);
        busy = false;
    }

    private final Runnable pollTask = new Runnable() {
        public void run() {
            fetchOnce();
            long gap = wsConnected ? RECONCILE_MS : Prefs.clampPoll(prefs.pollSeconds) * 1000L;
            handler.postDelayed(this, gap);
        }
    };

    private void fetchOnce() {
        if (busy) {
            return;
        }
        busy = true;
        final Prefs p = prefs;
        final boolean isInitial = !backfillDone;

        new Thread(new Runnable() {
            public void run() {
                try {
                    long now = System.currentTimeMillis();
                    long start = (isInitial || store.newestTime() <= 0L)
                            ? now - p.historyHours * 3600000L
                            : store.newestTime();
                    final List<RaceMessage> got = HaClient.fetchHistory(p, start, now);
                    handler.post(new Runnable() {
                        public void run() {
                            lastDataAt = System.currentTimeMillis();
                            connAlarmFired = false;
                            merge(got, false);
                            busy = false;
                        }
                    });
                } catch (final HaClient.HaException e) {
                    handler.post(new Runnable() {
                        public void run() {
                            setStatus(e.getMessage(), false);
                            busy = false;
                        }
                    });
                } catch (final Exception e) {
                    handler.post(new Runnable() {
                        public void run() {
                            setStatus("出错：" + e, false);
                            busy = false;
                        }
                    });
                }
            }
        }, "f1-fetch").start();
    }

    // ------------------------------------------------------------------
    // 合并 / 判定
    // ------------------------------------------------------------------

    /**
     * 合并一批消息。
     *
     * @param realtime true = 实时推送（要闪动、要判定提醒）；
     *                 false = 历史回填（**绝不闪动、绝不提醒**，
     *                 否则一次回填几十条会整屏乱闪、警报连环炸）
     */
    private void merge(List<RaceMessage> items, boolean realtime) {
        List<RaceMessage> fresh = new ArrayList<RaceMessage>();
        for (int i = 0; i < items.size(); i++) {
            RaceMessage m = items.get(i);
            if (store.add(m)) {
                fresh.add(m);
                track.onMessage(m);
                checkSequence(m);
            }
        }

        long cutoff = System.currentTimeMillis() - prefs.historyHours * 3600000L;
        store.dropOlderThan(cutoff);
        store.trimTo(prefs.maxStored);
        rebuildShown();

        // 突发合并：实测峰值 7 条/秒，同一秒到达多条时只闪最新那条、
        // 也只针对最新那条做提醒（前面几条已经被它代表了）。
        RaceMessage newest = null;
        if (realtime && !fresh.isEmpty()) {
            newest = fresh.get(fresh.size() - 1);
            for (int i = 0; i < fresh.size(); i++) {
                RaceMessage m = fresh.get(i);
                if (m.time >= newest.time) {
                    newest = m;
                }
            }
            // 顺序要紧：必须**先**登记"这一行要闪"，再 notifyDataSetChanged()。
            // 反过来的话 getView 拿到的 flashing 集合里还没有它，闪动不会发生。
            if (prefs.flashEnabled) {
                startFlash(newest.key());
            }
        }

        adapter.notifyDataSetChanged();

        if (!fresh.isEmpty()) {
            listView.setSelection(0);
        }
        if (newest != null) {
            dispatch(newest);
        }

        backfillDone = true;
        updateStatusRow();
        renderStateBar();
    }

    private void checkSequence(RaceMessage m) {
        if (m.sequence <= 0) {
            return;
        }
        if (lastSequence > 0 && m.sequence > lastSequence + 1) {
            droppedCount += (m.sequence - lastSequence - 1);
        }
        if (m.sequence > lastSequence) {
            lastSequence = m.sequence;
        }
    }

    /** 交给提醒闸门，需要时响 / 弹全屏。 */
    private void dispatch(RaceMessage m) {
        AlertGate.Action a = gate.onMessage(m, System.currentTimeMillis());
        runAction(a);
    }

    private void runAction(AlertGate.Action a) {
        if (a == null) {
            return;
        }
        if (a.severity >= Classifier.ALARM) {
            if (notifier != null) {
                notifier.stopAlarm();       // 交给 AlertActivity 播，避免两路声音叠加
                // 全屏横幅只管"当场叫醒"，通知栏那条负责"事后还查得到"。
                // 两个都要：横幅一闪而过，你要是正好没看手机就什么都不知道了。
                notifier.pushNotification(a.kind, m(a.msg), a.msg, prefs);
            }
            AlertActivity.show(this, a.kind, m(a.msg), a.msg.time,
                    a.msg.sector, a.msg.carNumber, a.escalated, prefs);
            return;
        }
        if (a.severity == Classifier.ATTENTION) {
            long now = System.currentTimeMillis();
            if (now - lastBeepAt < BEEP_MIN_GAP_MS) {
                return;
            }
            lastBeepAt = now;
            if (notifier != null) {
                notifier.attention(prefs.soundEnabled, prefs.vibrateEnabled);
            }
        }
    }

    private static String m(RaceMessage msg) {
        return msg == null ? "" : msg.text();
    }

    // ------------------------------------------------------------------
    // 闪动
    // ------------------------------------------------------------------

    private void startFlash(final String key) {
        if (key == null) {
            return;
        }
        flashing.add(key);
        handler.postDelayed(new Runnable() {
            public void run() {
                flashing.remove(key);
                if (adapter != null) {
                    adapter.notifyDataSetChanged();
                }
            }
        }, FLASH_MS + 120L);
    }

    // ------------------------------------------------------------------
    // 节拍：双黄升级 + 断线告警
    // ------------------------------------------------------------------

    private final Runnable tickTask = new Runnable() {
        public void run() {
            long now = System.currentTimeMillis();

            // 双黄旗"存活够久"则升级为强提醒
            List<AlertGate.Action> ups = gate.onTick(now);
            for (int i = 0; i < ups.size(); i++) {
                runAction(ups.get(i));
            }

            // 断线告警：比赛中掉线最危险 —— 你会以为"没消息"，实际是漏了红旗
            if (prefs.realtimeEnabled && !wsConnected && wsDownSince > 0L
                    && prefs.connectionWarnSec > 0
                    && (now - wsDownSince) > prefs.connectionWarnSec * 1000L
                    && !connAlarmFired) {
                connAlarmFired = true;
                if (notifier != null) {
                    notifier.attention(prefs.soundEnabled, prefs.vibrateEnabled);
                }
            }
            if (wsConnected) {
                wsDownSince = 0L;
            }

            updateStatusRow();
            handler.postDelayed(this, TICK_MS);
        }
    };

    // ------------------------------------------------------------------
    // 渲染
    // ------------------------------------------------------------------

    private void rebuildShown() {
        List<RaceMessage> all = store.sortedDesc();
        shown.clear();
        for (int i = 0; i < all.size(); i++) {
            RaceMessage m = all.get(i);
            if (prefs.accept(m)) {
                shown.add(m);
            }
        }
    }

    /** 顶部状态条：锁定显示当前最高优先级状态。 */
    private void renderStateBar() {
        stateBar.setBackgroundColor(track.color());
        stateView.setText(track.label());
        stateView.setTextColor(track.textColor());
        stateDetailView.setText(track.detail());
        stateDetailView.setTextColor(track.textColor());
        int pend = gate.pendingCount();
        if (pend > 0 && track.level() < TrackState.DY) {
            stateDetailView.setText("观察中 ×" + pend);
        }
    }

    private void setStatus(String text, boolean ok) {
        statusView.setText(text);
        statusView.setTextColor(ok ? 0xFF546E7A : 0xFFC62828);
        updateStatusRow();
    }

    private void updateStatusRow() {
        StringBuilder sb = new StringBuilder();
        if (connMode.length() > 0) {
            sb.append(wsConnected ? "⚡ " : "⏱ ").append(connMode);
        }
        sb.append("  ").append(shown.size()).append('/').append(store.size()).append(" 条");
        if (droppedCount > 0) {
            sb.append("  ⚠丢号").append(droppedCount);
        }
        if (!prefs.realtimeEnabled) {
            sb.append("  (已关闭实时)");
        }
        statusView.setText(sb.toString());
        statusView.setTextColor(wsNote.length() > 0 ? 0xFFC62828 : 0xFF546E7A);
        if (wsNote.length() > 0) {
            statusView.setText(sb + "  " + wsNote);
        }
        filterToggle.setText(prefs.noiseFilterEnabled ? "精简" : "全部");
    }

    // ------------------------------------------------------------------
    // 列表
    // ------------------------------------------------------------------

    private class RowAdapter extends BaseAdapter {
        public int getCount() {
            return shown.size();
        }

        public Object getItem(int position) {
            return shown.get(position);
        }

        public long getItemId(int position) {
            return shown.get(position).time;
        }

        public View getView(int position, View convertView, ViewGroup parent) {
            Row row = (convertView instanceof Row) ? (Row) convertView : new Row();
            RaceMessage c = shown.get(position);
            row.bind(c, flashing.contains(c.key()));
            return row;
        }
    }

    /** 一行：左侧色条 + 时间 + 类型徽标 + 正文。 */
    private class Row extends LinearLayout {
        final View stripe;
        final TextView time;
        final TextView badge;
        final TextView value;
        final LinearLayout textCol;
        private ValueAnimator anim;

        Row() {
            super(MainActivity.this);
            setOrientation(HORIZONTAL);
            setPadding(0, 0, 0, 0);

            stripe = new View(MainActivity.this);
            addView(stripe, new LinearLayout.LayoutParams(dp(6),
                    ViewGroup.LayoutParams.MATCH_PARENT));

            textCol = new LinearLayout(MainActivity.this);
            textCol.setOrientation(VERTICAL);
            textCol.setPadding(dp(12), dp(8), dp(12), dp(8));
            addView(textCol, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

            LinearLayout head = new LinearLayout(MainActivity.this);
            head.setOrientation(HORIZONTAL);
            head.setGravity(Gravity.CENTER_VERTICAL);

            time = new TextView(MainActivity.this);
            time.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
            time.setTextColor(0xFF78909C);
            head.addView(time);

            badge = new TextView(MainActivity.this);
            badge.setTextSize(TypedValue.COMPLEX_UNIT_SP, 10);
            badge.setTextColor(Color.WHITE);
            badge.setPadding(dp(6), dp(1), dp(6), dp(1));
            LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            blp.leftMargin = dp(8);
            head.addView(badge, blp);

            textCol.addView(head);

            value = new TextView(MainActivity.this);
            value.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
            value.setTextColor(0xFF212121);
            value.setMaxLines(3);
            value.setEllipsize(TextUtils.TruncateAt.END);
            textCol.addView(value);
        }

        void bind(RaceMessage c, boolean flash) {
            String kind = Classifier.kind(c);
            time.setText(fmt.format(new Date(c.time)));

            badge.setText(Classifier.label(kind));
            badge.setBackgroundColor(Classifier.barColor(kind));
            if (Classifier.K_OTHER.equals(kind)) {
                badge.setVisibility(GONE);
            } else {
                badge.setVisibility(VISIBLE);
            }

            value.setText(c.text());

            int base = Classifier.color(kind);
            stripe.setBackgroundColor(Classifier.barColor(kind));

            if (anim != null) {
                anim.cancel();
                anim = null;
            }
            if (flash) {
                // 闪两下再定格到等级配色。用框架自带的 ValueAnimator，不引入 androidx。
                anim = ValueAnimator.ofInt(base, COLOR_FLASH, base, COLOR_FLASH, base);
                anim.setDuration(FLASH_MS);
                anim.setEvaluator(new ArgbEvaluator());
                anim.setInterpolator(new LinearInterpolator());
                final Row self = this;
                anim.addUpdateListener(new ValueAnimator.AnimatorUpdateListener() {
                    public void onAnimationUpdate(ValueAnimator a) {
                        Object v = a.getAnimatedValue();
                        self.textCol.setBackgroundColor(v instanceof Integer
                                ? ((Integer) v).intValue() : 0xFFFFFFFF);
                    }
                });
                anim.start();
            } else {
                textCol.setBackgroundColor(base);
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_SETTINGS) {
            lastSignature = null;       // 触发 onResume 里的重载
        }
    }
}
