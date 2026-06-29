from pathlib import Path

APP = Path("app.py")
if not APP.exists():
    raise SystemExit("错误：当前目录没有 app.py，请先 cd ~/autodl-tmp/font_morph_web")

text = APP.read_text(encoding="utf-8", errors="ignore")

if "/skeleton_elastic_editor/{job_id}" not in text:
    raise SystemExit(
        "错误：app.py 里还没有 /skeleton_elastic_editor/{job_id} 路由。\n"
        "请先运行之前添加弹性骨架编辑器路由的补丁，再运行本脚本。"
    )

MARK = "# ===== REDIRECT_OLD_SKELETON_EDITOR_TO_ELASTIC_V1 ====="

if MARK in text:
    print("已经安装过旧骨架编辑器跳转补丁，不重复添加。")
else:
    backup = APP.with_suffix(".py.backup_redirect_skeleton_editor")
    backup.write_text(text, encoding="utf-8")
    print(f"已备份 app.py 到：{backup}")

    patch = r'''

# ===== REDIRECT_OLD_SKELETON_EDITOR_TO_ELASTIC_V1 =====
from starlette.responses import RedirectResponse as _ElasticSkeletonRedirectResponse

@app.middleware("http")
async def _redirect_old_skeleton_editor_to_elastic(request, call_next):
    """
    把旧骨架编辑器入口：
        /skeleton_editor/{job_id}

    自动替换为新弹性骨架编辑器入口：
        /skeleton_elastic_editor/{job_id}

    这样首页里原来的“打开骨架编辑器”按钮不用改前端，也会直接跳到新版页面。
    """
    path = request.url.path

    if path.startswith("/skeleton_editor/"):
        suffix = path[len("/skeleton_editor/"):]
        target = "/skeleton_elastic_editor/" + suffix

        if request.url.query:
            target += "?" + request.url.query

        return _ElasticSkeletonRedirectResponse(url=target, status_code=302)

    return await call_next(request)

# ===== END_REDIRECT_OLD_SKELETON_EDITOR_TO_ELASTIC_V1 =====
'''
    APP.write_text(text.rstrip() + "\n\n" + patch + "\n", encoding="utf-8")
    print("已完成：旧 /skeleton_editor/{job_id} 会自动跳转到 /skeleton_elastic_editor/{job_id}")

