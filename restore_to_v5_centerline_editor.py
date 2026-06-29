from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：找不到 app.py")

text = APP.read_text(encoding="utf-8", errors="ignore")
backup = APP.with_suffix(".py.backup_restore_to_v5_centerline")
backup.write_text(text, encoding="utf-8")
print(f"已备份 app.py 到：{backup}")

def disable_middleware(text, func_name):
    sig = f"async def {func_name}(request, call_next):"
    pos = text.find(sig)
    if pos == -1:
        print(f"未找到中间件：{func_name}")
        return text

    line_end = text.find("\n", pos)
    insert_pos = line_end + 1

    check_area = text[insert_pos:insert_pos + 300]
    if "DISABLED_BY_RESTORE_TO_V5_CENTERLINE" in check_area:
        print(f"已停用过：{func_name}")
        return text

    patch = (
        "    # DISABLED_BY_RESTORE_TO_V5_CENTERLINE\n"
        "    return await call_next(request)\n"
    )

    text = text[:insert_pos] + patch + text[insert_pos:]
    print(f"已停用中间件：{func_name}")
    return text

# 停用后来加的几个强制跳转补丁
for fname in [
    "redirect_old_editors_to_centerline_editor",
    "redirect_every_old_editor_to_svg_path_editor",
    "redirect_old_editors_to_warp_editor_v1",
]:
    text = disable_middleware(text, fname)

MARK = "# ===== FORCE_RESTORE_TO_V5_CENTERLINE_EDITOR ====="

if MARK not in text:
    patch = r'''

# ===== FORCE_RESTORE_TO_V5_CENTERLINE_EDITOR =====
from starlette.responses import RedirectResponse as _RestoreV5RedirectResponse

@app.middleware("http")
async def force_restore_to_v5_centerline_editor(request, call_next):
    """
    恢复到之前“有中心线”的 V5 编辑器版本。
    原来的“打开骨架编辑器”入口会进入：
        /skeleton_elastic_editor_v5/{job_id}
    """
    path = request.url.path

    redirect_prefixes = [
        "/skeleton_editor/",
        "/skeleton_elastic_editor/",
        "/glyph_warp_editor/",
        "/svg_path_editor/",
        "/centerline_editor/",
    ]

    # 注意：/skeleton_elastic_editor_v5/ 本身不跳转，直接放行
    if path.startswith("/skeleton_elastic_editor_v5/"):
        return await call_next(request)

    for prefix in redirect_prefixes:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            target = "/skeleton_elastic_editor_v5/" + suffix

            if request.url.query:
                target += "?" + request.url.query

            return _RestoreV5RedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_FORCE_RESTORE_TO_V5_CENTERLINE_EDITOR =====
'''
    text = text.rstrip() + "\n\n" + patch + "\n"
    print("已添加强制恢复到 V5 中心线编辑器的跳转。")
else:
    print("强制恢复 V5 跳转已存在，不重复添加。")

APP.write_text(text, encoding="utf-8")
print("恢复补丁写入完成。")
