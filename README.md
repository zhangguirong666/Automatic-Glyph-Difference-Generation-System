# Font Morph Web

多文字字体中间差值生成与传统蒙古文规则保留系统。项目以 FastAPI 提供网页入口，可上传两套字体文件，生成指定步数的中间变化字体，并提供 SVG/TTF 预览、下载与传统蒙古文奥云/蒙科立规则化处理。

## 主要功能

- 通用字体插值：支持中文、英文、日文、韩文、德文、传统蒙古文等字符的中间变化生成。
- 传统蒙古文厂商规则：奥云与蒙科立分公司处理，保留 `cmap / GSUB / GDEF / glyf / hmtx / vhea / vmtx` 等关键字体表。
- 蒙古文整词排版支持：生成字体时同步上下文形、合体字形和实际 shaping 会调用的 glyph，避免只能显示零散字母。
- 多格式输出：可生成 TTF、SVG 预览、ZIP 包与字形清单。
- 可变字体与预览：对兼容结果可进一步生成真实可变字体，并通过滑杆预览。
- 扩展模块：LoRA 字形风格化、节日贺卡、音乐动态字体、传统蒙古文知识问答等作为可选功能保留。

## 快速运行

Linux / AutoDL:

```bash
cd font_morph_web
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 18083
```

Windows:

```powershell
cd font_morph_web
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 18083
```

浏览器访问：

```text
http://127.0.0.1:18083
```

## 字体文件说明

本仓库不包含商业字体、训练模型、生成结果和用户上传文件。运行时请在网页中上传自己的 `.ttf/.otf` 字体文件，或将授权字体放入 `input/fonts/` 后再使用。

传统蒙古文字体请按公司选择对应入口：

- 奥云字体：选择「奥云 | 中国国标版」。
- 蒙科立字体：选择「蒙科立 | 中国国标版」。

不要混用两家字体公司的规则表和字体文件。

## 项目结构

```text
app.py                         # FastAPI 主应用
unicode_async_api.py           # 通用异步生成接口
gb_morph_algorithm.py          # 字体轮廓差值核心算法
sync_mongolian_shaping_glyphs.py # 传统蒙古文 shaping glyph 同步
features/                      # 贺卡、音乐动态字体、可变字体等功能模块
scripts/                       # 奥云/蒙科立 GB 字形生成与可变字体脚本
tools/                         # LoRA/SD/模型辅助工具
data/                          # 国标、厂商映射和运行规则表
input/                         # 本地授权字体放置目录，默认空
jobs/ output/ runtime_jobs/     # 运行时生成目录，已加入 .gitignore
```

## GitHub 发布注意

1. 上传仓库前不要提交商业字体、模型权重、用户上传素材或生成出的 TTF。
2. 若公开发布由商业字体生成的差值字体，需要确认原字体授权允许派生与再分发。
3. LoRA、Stable Diffusion、DeepSeek 本地模型属于可选模块，默认依赖不强制安装。
4. 建议先运行 `python -m py_compile app.py` 和 `uvicorn app:app --port 18083` 做本地烟测。

## 许可证

代码默认按本仓库 `LICENSE` 文件约束。字体、模型和第三方数据遵循各自原始授权。
