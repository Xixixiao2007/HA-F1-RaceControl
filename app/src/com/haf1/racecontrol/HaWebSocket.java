package com.haf1.racecontrol;

import android.util.Base64;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.security.SecureRandom;

import javax.net.ssl.SSLSocketFactory;

/**
 * 极简 Home Assistant WebSocket 客户端（纯 framework，零第三方依赖）。
 *
 * 为什么值得自己写：REST 的历史接口读的是 recorder 数据库，要等 commit_interval
 * （默认 5 秒）批量落库，延迟下不来。WebSocket 的 state_changed 事件是即时推送的，
 * 完全不经过数据库，因此能把端到端延迟打到网络往返级别。
 *
 * 流程：
 *   TCP 连接 -> HTTP Upgrade 握手 -> 收 auth_required -> 发 auth -> 收 auth_ok
 *   -> 发 subscribe_trigger（服务端按 entity_id 过滤，弱机不必解析全屋事件）
 *   -> 收 result(success) -> 之后不断收 event
 *
 * runAndReport() 是阻塞的，请放到独立线程里跑；返回 true 表示本次至少成功订阅过一次。
 */
public class HaWebSocket {

    public interface Listener {
        /** 鉴权并订阅成功，实时流已建立。 */
        void onOpen();

        /**
         * 收到该实体的一次状态变化。
         *
         * 上一代只回调 (时间, 值, 原始串)。本 App 需要 flag / category / sector
         * 来做旗语分类，所以直接回调解析好的 {@link RaceMessage}（含全部属性）。
         */
        void onState(RaceMessage message);

        /** 连接异常（之后会被上层重连）。 */
        void onError(String message);

        /** 连接结束（无论正常还是异常）。 */
        void onClose();
    }

    /** 空闲多久没收到任何数据就发一次心跳（毫秒）。 */
    private static final int IDLE_TIMEOUT_MS = 45000;
    /** 连续多少个空闲周期都没有响应就判定连接已死。 */
    private static final int MAX_IDLE_ROUNDS = 2;

    private static final int CONNECT_TIMEOUT_MS = 8000;
    private static final int SUBSCRIBE_ID = 1;

    private final Prefs prefs;
    private final Listener listener;

    private volatile boolean closed = false;
    private volatile Socket socket;

    private InputStream in;
    private OutputStream out;
    private int idleRounds = 0;
    private boolean opened = false;

    public HaWebSocket(Prefs prefs, Listener listener) {
        this.prefs = prefs;
        this.listener = listener;
    }

    /** 从外部线程调用以中止连接：关掉 socket 会让阻塞的 read 抛异常退出。 */
    public void shutdown() {
        closed = true;
        Socket s = socket;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // ignore
            }
        }
    }

    // ------------------------------------------------------------------

    /**
     * 阻塞运行直到连接结束。返回 true 表示本次至少成功订阅过一次
     * （上层用它来决定重连退避是否清零）。
     */
    public boolean runAndReport() {
        boolean subscribedOk = false;
        try {
            String base = prefs.baseUrl();
            URI uri = URI.create(base);
            String scheme = uri.getScheme() == null ? "http" : uri.getScheme();
            boolean tls = "https".equalsIgnoreCase(scheme) || "wss".equalsIgnoreCase(scheme);
            String host = uri.getHost();
            if (host == null || host.length() == 0) {
                throw new IOException("地址里解析不出主机名：" + base);
            }
            int port = uri.getPort();
            if (port < 0) {
                port = tls ? 443 : 80;
            }
            String path = uri.getPath();
            if (path == null) {
                path = "";
            }
            while (path.endsWith("/")) {
                path = path.substring(0, path.length() - 1);
            }
            String wsPath = path + "/api/websocket";

            Socket s;
            if (tls) {
                s = SSLSocketFactory.getDefault().createSocket();
            } else {
                s = new Socket();
            }
            s.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
            s.setSoTimeout(IDLE_TIMEOUT_MS);
            s.setTcpNoDelay(true);
            socket = s;

            in = new BufferedInputStream(s.getInputStream());
            out = new BufferedOutputStream(s.getOutputStream());

            handshake(host, port, wsPath);

            while (!closed) {
                WsFrame f;
                try {
                    f = WsFrame.read(in);
                    idleRounds = 0;
                } catch (SocketTimeoutException te) {
                    idleRounds++;
                    if (idleRounds >= MAX_IDLE_ROUNDS) {
                        throw new IOException("连接空闲超时（已发心跳但无响应）");
                    }
                    sendControl(WsFrame.OP_PING, new byte[0]);
                    continue;
                }

                if (f.opcode == WsFrame.OP_PING) {
                    sendControl(WsFrame.OP_PONG, f.payload);
                } else if (f.opcode == WsFrame.OP_PONG) {
                    // 心跳回应，忽略
                } else if (f.opcode == WsFrame.OP_CLOSE) {
                    throw new IOException("服务端关闭了连接");
                } else if (f.opcode == WsFrame.OP_BIN) {
                    // HA 不会发二进制，忽略
                } else if (f.opcode == WsFrame.OP_TEXT) {
                    handleText(new String(f.payload, "UTF-8"));
                    if (opened) {
                        subscribedOk = true;
                    }
                }
            }
        } catch (Throwable t) {
            if (!closed) {
                listener.onError(describe(t));
            }
        } finally {
            closeQuietly();
            listener.onClose();
        }
        return subscribedOk;
    }

    private String describe(Throwable t) {
        String m = t.getMessage();
        if (m == null || m.length() == 0) {
            m = t.getClass().getSimpleName();
        }
        return m;
    }

    // ------------------------------------------------------------------
    // 握手
    // ------------------------------------------------------------------

    private void handshake(String host, int port, String wsPath) throws IOException {
        byte[] nonce = new byte[16];
        new SecureRandom().nextBytes(nonce);
        String key = Base64.encodeToString(nonce, Base64.NO_WRAP);

        String hostHeader = (port == 80 || port == 443) ? host : host + ":" + port;

        String req = "GET " + wsPath + " HTTP/1.1\r\n"
                + "Host: " + hostHeader + "\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + key + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n"
                + "\r\n";
        out.write(req.getBytes("UTF-8"));
        out.flush();

        String statusLine = readLine();
        if (statusLine == null || statusLine.indexOf(" 101") < 0) {
            throw new IOException("WebSocket 握手被拒绝：" + statusLine
                    + "（请确认地址与端口正确，HA 的 WebSocket 与网页同端口）");
        }
        String line;
        while ((line = readLine()) != null && line.length() > 0) {
            // 响应头内容用不上，丢掉
        }
    }

    private String readLine() throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream(128);
        while (true) {
            int c = in.read();
            if (c < 0) {
                return b.size() == 0 ? null : new String(b.toByteArray(), "UTF-8");
            }
            if (c == '\n') {
                byte[] a = b.toByteArray();
                int n = a.length;
                if (n > 0 && a[n - 1] == '\r') {
                    n--;
                }
                return new String(a, 0, n, "UTF-8");
            }
            b.write(c);
            if (b.size() > 8192) {
                throw new IOException("响应头异常过长");
            }
        }
    }

    // ------------------------------------------------------------------
    // 协议消息
    // ------------------------------------------------------------------

    private void handleText(String text) throws IOException {
        JSONObject m;
        try {
            m = new JSONObject(text);
        } catch (Exception e) {
            return;                     // 解析不了的报文直接忽略，不断连接
        }
        String type = m.optString("type", "");

        if ("auth_required".equals(type)) {
            sendJson("{\"type\":\"auth\",\"access_token\":\"" + esc(prefs.token) + "\"}");
        } else if ("auth_ok".equals(type)) {
            sendJson("{\"id\":" + SUBSCRIBE_ID + ",\"type\":\"subscribe_trigger\","
                    + "\"trigger\":{\"platform\":\"state\",\"entity_id\":\""
                    + esc(prefs.entityId) + "\"}}");
        } else if ("auth_invalid".equals(type)) {
            throw new IOException("令牌被拒绝：" + m.optString("message", "auth_invalid"));
        } else if ("result".equals(type)) {
            if (m.optInt("id", -1) == SUBSCRIBE_ID) {
                if (m.optBoolean("success", false)) {
                    if (!opened) {
                        opened = true;
                        listener.onOpen();
                    }
                } else {
                    throw new IOException("订阅失败：" + String.valueOf(m.opt("error")));
                }
            }
        } else if ("event".equals(type)) {
            handleEvent(m.optJSONObject("event"));
        }
    }

    /**
     * 兼容两种事件形状：
     *   A. subscribe_trigger -> event.variables.trigger.to_state
     *   B. subscribe_events  -> event.data.new_state
     * 这样万一以后要换回 subscribe_events，解析这块不用改。
     */
    private void handleEvent(JSONObject event) {
        if (event == null) {
            return;
        }
        JSONObject state = null;

        JSONObject vars = event.optJSONObject("variables");
        if (vars != null) {
            JSONObject trigger = vars.optJSONObject("trigger");
            if (trigger != null) {
                state = trigger.optJSONObject("to_state");
            }
        }
        if (state == null) {
            JSONObject data = event.optJSONObject("data");
            if (data != null) {
                String eid = data.optString("entity_id", "");
                if (eid.length() > 0 && !eid.equals(targetEntity())) {
                    return;
                }
                state = data.optJSONObject("new_state");
            }
        }
        if (state == null) {
            return;                     // 实体被删除时 new_state 为 null
        }

        String eid2 = state.optString("entity_id", "");
        if (eid2.length() > 0 && !eid2.equals(targetEntity())) {
            return;
        }
        RaceMessage msg = RaceMessage.parse(state);
        if (msg == null) {
            return;                     // 时间戳解析不出来就当没收到
        }
        listener.onState(msg);
    }

    private String targetEntity() {
        return prefs.entityId == null ? "" : prefs.entityId.trim();
    }

    private static String esc(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder b = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                b.append('\\').append(c);
            } else if (c < 0x20) {
                // 控制字符直接丢弃
            } else {
                b.append(c);
            }
        }
        return b.toString();
    }

    private void sendJson(String json) throws IOException {
        sendControl(WsFrame.OP_TEXT, json.getBytes("UTF-8"));
    }

    private synchronized void sendControl(int opcode, byte[] payload) throws IOException {
        if (closed || out == null) {
            return;
        }
        out.write(WsFrame.encode(opcode, payload));
        out.flush();
    }

    private void closeQuietly() {
        closed = true;
        Socket s = socket;
        socket = null;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // ignore
            }
        }
    }
}
