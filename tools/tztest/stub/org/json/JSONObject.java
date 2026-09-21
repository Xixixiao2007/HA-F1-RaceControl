package org.json;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 桌面测试专用的 JSONObject —— 是**真的解析**，不是空桩。
 * 见 {@link JSONTokener} 里关于"为什么不能用空桩"的说明。
 */
public class JSONObject {

    private final Map<String, Object> map;

    public JSONObject() {
        this.map = new LinkedHashMap<String, Object>();
    }

    public JSONObject(String source) throws JSONException {
        Object o = new JSONTokener(source).nextValue();
        if (!(o instanceof Map)) {
            throw new JSONException("顶层不是 JSON 对象");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) o;
        this.map = m;
    }

    JSONObject(Map<String, Object> m) {
        this.map = m;
    }

    public boolean has(String name) {
        return map.containsKey(name);
    }

    public Object opt(String name) {
        return map.get(name);
    }

    public String optString(String name) {
        Object v = map.get(name);
        return v == null ? "" : String.valueOf(v);
    }

    public String optString(String name, String fallback) {
        Object v = map.get(name);
        return v == null ? fallback : String.valueOf(v);
    }

    public int optInt(String name, int fallback) {
        Object v = map.get(name);
        if (v instanceof Number) {
            return ((Number) v).intValue();
        }
        if (v instanceof String) {
            try {
                return Integer.parseInt(((String) v).trim());
            } catch (NumberFormatException ignored) {
                return fallback;
            }
        }
        return fallback;
    }

    public long optLong(String name, long fallback) {
        Object v = map.get(name);
        if (v instanceof Number) {
            return ((Number) v).longValue();
        }
        if (v instanceof String) {
            try {
                return Long.parseLong(((String) v).trim());
            } catch (NumberFormatException ignored) {
                return fallback;
            }
        }
        return fallback;
    }

    public double optDouble(String name, double fallback) {
        Object v = map.get(name);
        if (v instanceof Number) {
            return ((Number) v).doubleValue();
        }
        return fallback;
    }

    public boolean optBoolean(String name) {
        return optBoolean(name, false);
    }

    public boolean optBoolean(String name, boolean fallback) {
        Object v = map.get(name);
        if (v instanceof Boolean) {
            return ((Boolean) v).booleanValue();
        }
        if (v instanceof String) {
            String s = ((String) v).trim();
            if ("true".equalsIgnoreCase(s)) {
                return true;
            }
            if ("false".equalsIgnoreCase(s)) {
                return false;
            }
        }
        return fallback;
    }

    public JSONObject optJSONObject(String name) {
        Object v = map.get(name);
        if (v instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) v;
            return new JSONObject(m);
        }
        return null;
    }

    public JSONArray optJSONArray(String name) {
        Object v = map.get(name);
        if (v instanceof List) {
            @SuppressWarnings("unchecked")
            List<Object> l = (List<Object>) v;
            return new JSONArray(l);
        }
        return null;
    }
}
