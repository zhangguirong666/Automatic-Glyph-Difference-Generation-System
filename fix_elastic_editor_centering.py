from pathlib import Path
import re

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_fix_centering")
backup.write_text(text, encoding="utf-8")
print(f"已备份: {backup}")

# 1) 给 state 增加 svgBounds
old_state = """  rawSvgText: "",
  rawSvgImage: null,
  pinned: new Set(),"""

new_state = """  rawSvgText: "",
  rawSvgImage: null,
  svgBounds: null,
  pinned: new Set(),"""

if old_state in text:
    text = text.replace(old_state, new_state, 1)
    print("已添加 state.svgBounds")
else:
    print("未找到 state 插入点，可能已改过。")

# 2) 在 loadGlyphData 里，加载 raw svg 时顺便解析 bounds
old_load_block = """  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    state.rawSvgImage = await svgToImage(state.rawSvgText);
  } catch (e) {
    state.rawSvgText = "";
    state.rawSvgImage = null;
  }"""

new_load_block = """  try {
    state.rawSvgText = await fetchText(`/raw_svg_variant/${JOB_ID}/${state.code}/${state.variant}`);
    state.rawSvgImage = await svgToImage(state.rawSvgText);
    state.svgBounds = parseSvgBounds(state.rawSvgText);
  } catch (e) {
    state.rawSvgText = "";
    state.rawSvgImage = null;
    state.svgBounds = null;
  }"""

if old_load_block in text:
    text = text.replace(old_load_block, new_load_block, 1)
    print("已更新 loadGlyphData 中的 SVG bounds 解析")
else:
    print("未找到 loadGlyphData 的原始代码块，可能已改过。")

# 3) 替换 fit()，支持 points 或 svgBounds 居中
old_fit = """function fit() {
  if (!state.points.length) return;
  const b = bounds(state.points);
  const r = canvas.getBoundingClientRect();
  const pad = 90;
  const sx = (r.width - pad * 2) / Math.max(1, b.width);
  const sy = (r.height - pad * 2) / Math.max(1, b.height);
  state.scale = Math.min(sx, sy);
  state.offsetX = r.width / 2 - b.cx * state.scale;
  state.offsetY = r.height / 2 - b.cy * state.scale;
}"""

new_fit = """function fit() {
  const r = canvas.getBoundingClientRect();
  const pad = 90;

  let b = null;

  if (state.points && state.points.length) {
    b = bounds(state.points);
  } else if (state.svgBounds) {
    b = state.svgBounds;
  } else {
    state.scale = 1;
    state.offsetX = r.width / 2;
    state.offsetY = r.height / 2;
    return;
  }

  const sx = (r.width - pad * 2) / Math.max(1, b.width);
  const sy = (r.height - pad * 2) / Math.max(1, b.height);
  state.scale = Math.min(sx, sy);
  state.offsetX = r.width / 2 - b.cx * state.scale;
  state.offsetY = r.height / 2 - b.cy * state.scale;
}"""

if old_fit in text:
    text = text.replace(old_fit, new_fit, 1)
    print("已替换 fit() 为居中增强版")
else:
    print("未找到旧 fit()，尝试正则替换...")
    text = re.sub(
        r"function fit\(\)\s*\{.*?\n\}",
        new_fit,
        text,
        count=1,
        flags=re.S
    )

# 4) 给 draw() 里 raw svg 的绘制增加优先使用 svgBounds
old_draw_svg = """  if (ui.showRawSvg.checked && state.rawSvgImage) {
    const b = bounds(state.basePoints);
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.drawImage(state.rawSvgImage, b.minX - 30, b.minY - 30, b.width + 60, b.height + 60);
    ctx.restore();
  }"""

new_draw_svg = """  if (ui.showRawSvg.checked && state.rawSvgImage) {
    const b = (state.basePoints && state.basePoints.length)
      ? bounds(state.basePoints)
      : (state.svgBounds || {minX:0,minY:0,width:300,height:300});

    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.drawImage(
      state.rawSvgImage,
      b.minX,
      b.minY,
      Math.max(1, b.width),
      Math.max(1, b.height)
    );
    ctx.restore();
  }"""

if old_draw_svg in text:
    text = text.replace(old_draw_svg, new_draw_svg, 1)
    print("已修复 raw svg 的绘制位置")
else:
    print("未找到旧 raw svg 绘制代码块，可能已改过。")

# 5) 插入 parseSvgBounds() 工具函数
marker = "function svgToImage(svgText) {"
if marker in text and "function parseSvgBounds(svgText)" not in text:
    insert_code = r'''
function parseSvgBounds(svgText) {
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(svgText, "image/svg+xml");
    const svg = doc.documentElement;

    const vb = svg.getAttribute("viewBox");
    if (vb) {
      const nums = vb.trim().split(/[\s,]+/).map(Number);
      if (nums.length === 4 && nums.every(n => Number.isFinite(n))) {
        const [minX, minY, width, height] = nums;
        return {
          minX,
          minY,
          maxX: minX + width,
          maxY: minY + height,
          width,
          height,
          cx: minX + width / 2,
          cy: minY + height / 2
        };
      }
    }

    const w = parseFloat((svg.getAttribute("width") || "300").replace(/[a-z%]+/ig, ""));
    const h = parseFloat((svg.getAttribute("height") || "300").replace(/[a-z%]+/ig, ""));
    if (Number.isFinite(w) && Number.isFinite(h)) {
      return {
        minX: 0,
        minY: 0,
        maxX: w,
        maxY: h,
        width: w,
        height: h,
        cx: w / 2,
        cy: h / 2
      };
    }
  } catch (e) {
    console.warn("parseSvgBounds failed:", e);
  }

  return {
    minX: 0,
    minY: 0,
    maxX: 300,
    maxY: 300,
    width: 300,
    height: 300,
    cx: 150,
    cy: 150
  };
}

'''
    text = text.replace(marker, insert_code + marker, 1)
    print("已插入 parseSvgBounds()")
else:
    print("parseSvgBounds() 已存在或未找到插入点。")

APP.write_text(text, encoding="utf-8")
print("补丁写入完成。")
