package com.haf1.racecontrol;

import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.drawable.Drawable;

/**
 * 灰白棋盘格背景 —— 给格子旗用的。
 *
 * 格子旗原本被刷成了蓝色（跟蓝旗撞车了），但格子旗**字面上就是黑白方格**，
 * 画成棋盘格一眼就认得出来，比任何单色都准。
 *
 * 纯框架实现（`Drawable` + `Canvas`），不引入任何资源图片或第三方库。
 * 无状态、可复用 —— 换行复用时不会残留上一条的绘制结果。
 */
public class CheckerDrawable extends Drawable {

    private final Paint light = new Paint();
    private final Paint dark = new Paint();
    private final int cell;

    public CheckerDrawable(int cellPx, int lightColor, int darkColor) {
        this.cell = Math.max(3, cellPx);
        light.setColor(lightColor);
        light.setStyle(Paint.Style.FILL);
        dark.setColor(darkColor);
        dark.setStyle(Paint.Style.FILL);
    }

    @Override
    public void draw(Canvas canvas) {
        Rect b = getBounds();
        if (b.width() <= 0 || b.height() <= 0) {
            return;
        }
        canvas.drawColor(light.getColor());
        // 逐行错开半格，形成棋盘
        int row = 0;
        for (int y = b.top; y < b.bottom; y += cell, row++) {
            int startX = b.left + ((row % 2 == 0) ? cell : 0);
            for (int x = startX; x < b.right; x += cell * 2) {
                canvas.drawRect(
                        Math.max(x, b.left), y,
                        Math.min(x + cell, b.right), Math.min(y + cell, b.bottom),
                        dark);
            }
        }
    }

    @Override
    public void setAlpha(int alpha) {
        light.setAlpha(alpha);
        dark.setAlpha(alpha);
    }

    @Override
    public void setColorFilter(ColorFilter cf) {
        light.setColorFilter(cf);
        dark.setColorFilter(cf);
    }

    @Override
    public int getOpacity() {
        return PixelFormat.OPAQUE;
    }
}
