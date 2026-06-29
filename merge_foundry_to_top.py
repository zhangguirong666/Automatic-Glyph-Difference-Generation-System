from fastapi.responses import Response

MERGE_FOUNDRY_SCRIPT = r'''
<script id="merge-foundry-to-top-v1">
(function(){
  if(window.__MERGE_FOUNDRY_TO_TOP_V1__) return;
  window.__MERGE_FOUNDRY_TO_TOP_V1__ = true;

  function textOf(el){
    return (el && (el.innerText || el.textContent || "") || "").trim();
  }

  function findTopCharsetSelect(){
    const selects = Array.from(document.querySelectorAll("select"));

    for(const s of selects){
      const txt = Array.from(s.options || []).map(o => o.textContent || "").join(" ");
      if(
        txt.includes("传统蒙古文35个") ||
        txt.includes("英文 A-Z") ||
        txt.includes("中文6500") ||
        txt.includes("自定义输入字符")
      ){
        return s;
      }
    }

    return selects[0] || null;
  }

  function findFoundryPanel(){
    const candidates = Array.from(document.querySelectorAll("div,section,fieldset"));

    let best = null;

    for(const el of candidates){
      const txt = textOf(el);

      if(
        txt.includes("按字体公司规则生成") &&
        txt.includes("字体公司") &&
        txt.includes("字体文件 A") &&
        txt.includes("字体文件 B")
      ){
        if(!best || txt.length < textOf(best).length){
          best = el;
        }
      }
    }

    return best;
  }

  function findFoundryCompanySelect(panel){
    if(!panel) return null;

    const selects = Array.from(panel.querySelectorAll("select"));

    for(const s of selects){
      const txt = Array.from(s.options || []).map(o => o.textContent || "").join(" ");
      if(
        txt.includes("奥云") ||
        txt.includes("蒙科立") ||
        txt.includes("中国国标")
      ){
        return s;
      }
    }

    return selects[0] || null;
  }

  function findTopStartButton(){
    const byId = document.getElementById("startBtn");
    if(byId) return byId;

    return Array.from(document.querySelectorAll("button,input[type='submit'],input[type='button']")).find(el => {
      const t = textOf(el) || el.value || "";
      return t.includes("开始生成");
    });
  }

  function buildInlineContainer(topSelect){
    let box = document.getElementById("foundryMergedTopContainer");
    if(box) return box;

    box = document.createElement("div");
    box.id = "foundryMergedTopContainer";
    box.style.cssText = [
      "margin-top:14px",
      "padding:14px",
      "border:1px solid #d1d5db",
      "border-radius:10px",
      "background:#f8fafc",
      "display:none"
    ].join(";");

    const anchor =
      topSelect.closest("label") ||
      topSelect.parentElement ||
      topSelect;

    if(anchor && anchor.parentElement){
      anchor.parentElement.insertBefore(box, anchor.nextSibling);
    }else{
      document.body.insertBefore(box, document.body.firstChild);
    }

    return box;
  }

  function hideTopNormalControls(isFoundry){
    // 选择公司规则时，上半部分普通 Unicode 的部分参数仍然可保留；
    // 这里只把普通“开始生成”按钮隐藏，避免误点。
    const topBtn = findTopStartButton();

    if(topBtn){
      topBtn.style.display = isFoundry ? "none" : "";
    }
  }

  function addFoundryOptionsToTop(topSelect, companySelect){
    if(!topSelect || !companySelect) return;

    if(topSelect.dataset.foundryMerged === "1") return;

    const group = document.createElement("optgroup");
    group.label = "蒙古文字体公司规则";

    Array.from(companySelect.options || []).forEach(opt => {
      const o = document.createElement("option");
      o.value = "__foundry__" + opt.value;
      o.textContent = "蒙古文公司规则：" + opt.textContent.trim();
      o.dataset.companyValue = opt.value;
      group.appendChild(o);
    });

    topSelect.appendChild(group);
    topSelect.dataset.foundryMerged = "1";
  }

  function rewriteFoundryPanelTitle(panel){
    if(!panel) return;

    const headings = Array.from(panel.querySelectorAll("h1,h2,h3,b,strong,div,p"));

    for(const h of headings){
      const t = textOf(h);
      if(t.includes("按字体公司规则生成")){
        h.textContent = "蒙古文字体公司规则生成";
        return;
      }
    }
  }

  function install(){
    const topSelect = findTopCharsetSelect();
    const foundryPanel = findFoundryPanel();

    if(!topSelect || !foundryPanel) return;

    const companySelect = findFoundryCompanySelect(foundryPanel);
    if(!companySelect) return;

    addFoundryOptionsToTop(topSelect, companySelect);

    const inlineBox = buildInlineContainer(topSelect);

    if(!inlineBox.dataset.moved){
      rewriteFoundryPanelTitle(foundryPanel);
      inlineBox.appendChild(foundryPanel);
      inlineBox.dataset.moved = "1";
    }

    function sync(){
      const value = topSelect.value || "";
      const isFoundry = value.startsWith("__foundry__");

      if(isFoundry){
        const companyValue = value.replace("__foundry__", "");
        companySelect.value = companyValue;
        inlineBox.style.display = "block";
      }else{
        inlineBox.style.display = "none";
      }

      hideTopNormalControls(isFoundry);
    }

    if(!topSelect.dataset.foundryChangeBound){
      topSelect.addEventListener("change", sync);
      topSelect.dataset.foundryChangeBound = "1";
    }

    sync();
  }

  document.addEventListener("DOMContentLoaded", install);

  setTimeout(install, 500);
  setTimeout(install, 1200);
})();
</script>
'''


def install_merge_foundry_to_top(app):
    if getattr(app.state, "_merge_foundry_to_top_v1", False):
        return

    app.state._merge_foundry_to_top_v1 = True

    @app.middleware("http")
    async def merge_foundry_to_top_middleware(request, call_next):
        response = await call_next(request)

        path = request.url.path
        content_type = response.headers.get("content-type", "")

        if path not in ["/", "/unicode", "/unicode_async"]:
            return response

        if "text/html" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        text = body.decode("utf-8", errors="ignore")

        if "merge-foundry-to-top-v1" not in text:
            if "</body>" in text:
                text = text.replace("</body>", MERGE_FOUNDRY_SCRIPT + "\n</body>", 1)
            else:
                text += MERGE_FOUNDRY_SCRIPT

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html"
        )
