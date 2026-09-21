package org.json;

/** 仅供桌面端单元测试使用的极简 JSON 桩，不是真实实现。 */
public class JSONObject {
    public JSONObject(String s) throws JSONException {
    }

    public boolean has(String name) {
        return false;
    }

    public String optString(String name) {
        return "";
    }

    public String optString(String name, String fallback) {
        return fallback;
    }

    public int optInt(String name, int fallback) {
        return fallback;
    }

    public JSONObject optJSONObject(String name) {
        return null;
    }

    public JSONArray optJSONArray(String name) {
        return null;
    }
}
