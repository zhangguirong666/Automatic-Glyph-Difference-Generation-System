import asyncio
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional
from urllib import request as urlrequest

import torch
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer


BACKEND = os.environ.get("LOCAL_DEEPSEEK_BACKEND", "transformers").strip().lower()
OLLAMA_URL = os.environ.get("LOCAL_DEEPSEEK_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(os.environ.get("LOCAL_DEEPSEEK_OLLAMA_TIMEOUT", "600"))
MODEL_DIR = os.environ.get(
    "LOCAL_DEEPSEEK_MODEL_DIR",
    "/root/autodl-tmp/models/deepseek-r1-distill-qwen-7b",
)
MODEL_NAME = os.environ.get(
    "LOCAL_DEEPSEEK_MODEL_NAME",
    "deepseek-r1:32b" if BACKEND == "ollama" else "DeepSeek-R1-Distill-Qwen-7B",
)
MAX_GPU_MEMORY = os.environ.get("LOCAL_DEEPSEEK_MAX_GPU_MEMORY", "17GiB")
MAX_CPU_MEMORY = os.environ.get("LOCAL_DEEPSEEK_MAX_CPU_MEMORY", "64GiB")
DIRECT_ANSWER_RULE = (
    "重要要求：只输出最终答案，不要输出思考过程、推理草稿、需求分析或自我对话。"
    "不要写“用户想要”“我需要”“首先我来分析”“接下来”等过程性句子。"
)
MONGOLIAN_QA_SYSTEM = (
    "你是传统蒙古文与蒙古族文化问答助手。主要回答传统蒙古文书写、词首/词中/词尾/独立形、"
    "Unicode/GB 编码、PUA、glyph name、显现形式、强制合体字、非强制合体字、奥云/蒙科立字体规则、"
    "蒙古族节日、纹样、礼俗、服饰、历史文化等相关问题。回答用中文，必要时补充传统蒙古文原文、"
    "拉丁转写或术语说明。遇到不确定的传统蒙古文翻译、历史事实或标准条目，要明确说不确定，不能编造。"
    "不要把回答写成 Stable Diffusion、LoRA 或绘图提示词。\n"
    "必须遵守的基础知识：传统蒙古文通常自上而下竖写，列从左向右排列；字母出现独立形、词首形、"
    "词中形、词尾形，是因为字母按前后连接环境进行字形 shaping，独立形表示左右都不连接，词首形通常只向后连接，"
    "词中形前后都连接，词尾形通常只向前连接。这些是书写/字形显现形式，不是语法时态，也不是互不相关的不同字母。"
    "解释传统蒙古文字形时，禁止说成汉字书写习惯。"
    "Unicode 主要编码抽象字符和少量控制/变体选择符，实际显示依赖字体和 OpenType shaping；中国传统蒙古文国标、"
    "字体公司的 GB/PUA/glyph name 清单更多是在描述实际可见字形和合体字。奥云和蒙科立等字体公司的 glyph 命名、"
    "PUA 映射和合体规则可能不同，不能混用同一张表。蒙古族吉祥纹样常见有乌力吉/盘长纹、云纹、犄纹、回纹等；"
    "解释文化寓意时要说明这是常见象征，不要把民俗说成唯一标准。"
)

app = FastAPI(title="Local DeepSeek Service")

_load_lock = threading.Lock()
_generate_lock = threading.Lock()
_tokenizer = None
_model = None
_load_error = ""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    prompt: Optional[str] = None
    messages: Optional[List[ChatMessage]] = None
    system: Optional[str] = None
    max_new_tokens: int = 512
    temperature: float = 0.6
    top_p: float = 0.9
    do_sample: bool = True
    strip_thinking: bool = True


def _model_loaded() -> bool:
    if BACKEND == "ollama":
        return _model is not None
    return _model is not None and _tokenizer is not None


def _ollama_json(path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        OLLAMA_URL.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urlrequest.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    return json.loads(body or "{}")


def _ollama_model_exists() -> bool:
    try:
        data = _ollama_json("/api/tags")
        models = data.get("models") or []
        names = {m.get("name") for m in models}
        return MODEL_NAME in names
    except Exception:
        return False


def _load_model() -> None:
    global _tokenizer, _model, _load_error
    if _model_loaded():
        return
    with _load_lock:
        if _model_loaded():
            return
        _load_error = ""
        if BACKEND == "ollama":
            if not _ollama_model_exists():
                _load_error = f"ollama model not found: {MODEL_NAME}"
                raise RuntimeError(_load_error)
            _model = {"backend": "ollama", "model": MODEL_NAME}
            _tokenizer = None
            return
        if not os.path.isdir(MODEL_DIR):
            _load_error = f"model directory not found: {MODEL_DIR}"
            raise RuntimeError(_load_error)
        try:
            _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, trust_remote_code=True)
            max_memory: Dict[Any, str] = {"cpu": MAX_CPU_MEMORY}
            if torch.cuda.is_available():
                max_memory[0] = MAX_GPU_MEMORY
            _model = AutoModelForCausalLM.from_pretrained(
                MODEL_DIR,
                dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                device_map="auto",
                max_memory=max_memory,
                trust_remote_code=True,
            )
            _model.eval()
        except Exception as exc:
            _load_error = str(exc)
            _tokenizer = None
            _model = None
            raise


def _normalize_messages(req: ChatRequest) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    system = (req.system or "").strip()
    system_text = MONGOLIAN_QA_SYSTEM + ("\n" + system if system else "")
    messages.append({"role": "system", "content": (DIRECT_ANSWER_RULE + "\n" + system_text).strip()})
    if req.messages:
        for msg in req.messages:
            role = msg.role if msg.role in {"system", "user", "assistant"} else "user"
            content = str(msg.content or "").strip()
            if content:
                messages.append({"role": role, "content": content})
    elif req.prompt:
        messages.append({"role": "user", "content": req.prompt})
    if not messages:
        messages.append({"role": "user", "content": "你好"})
    return messages


def _strip_thinking(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if "</think>" in text:
        text = text.split("</think>", 1)[-1].strip()
    process_markers = [
        "正向提示词",
        "反向提示词",
        "参数建议",
        "最终答案",
        "可以这样写",
        "直接可用",
    ]
    if re.match(r"^(嗯|好的|首先|我来|用户|这个需求|我们需要|接下来)", text):
        positions = [text.find(marker) for marker in process_markers if text.find(marker) > 0]
        if positions:
            text = text[min(positions):].strip(" ：:\n")
    return text


def _messages_to_ollama_raw_prompt(messages: List[Dict[str, str]]) -> str:
    system_parts: List[str] = []
    dialogue_parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            dialogue_parts.append(f"<｜Assistant｜>{content}<｜end▁of▁sentence｜>")
        else:
            dialogue_parts.append(f"<｜User｜>{content}")
    system_text = "\n".join(system_parts).strip()
    prompt = (system_text + "\n" if system_text else "") + "\n".join(dialogue_parts).strip()
    # DeepSeek-R1 spends many tokens in thinking mode. Prefilling an empty think block
    # keeps the web assistant focused on the final answer for interactive use.
    return prompt.rstrip() + "\n<｜Assistant｜><think>\n\n</think>\n\n"


def _generate(req: ChatRequest) -> Dict[str, Any]:
    _load_model()
    messages = _normalize_messages(req)
    max_new_tokens = max(16, min(int(req.max_new_tokens or 512), 2048))
    temperature = max(0.05, min(float(req.temperature or 0.6), 2.0))
    top_p = max(0.1, min(float(req.top_p or 0.9), 1.0))

    if BACKEND == "ollama":
        with _generate_lock:
            data = _ollama_json(
                "/api/generate",
                {
                    "model": MODEL_NAME,
                    "prompt": _messages_to_ollama_raw_prompt(messages),
                    "raw": True,
                    "stream": False,
                    "options": {
                        "num_predict": max_new_tokens,
                        "temperature": temperature,
                        "top_p": top_p,
                    },
                },
            )
        raw = (data.get("response") or (data.get("message") or {}).get("content") or "").strip()
        if not raw and data.get("thinking"):
            raw = str(data.get("thinking") or "").strip()
        text = _strip_thinking(raw) if req.strip_thinking else raw
        return {
            "ok": True,
            "backend": BACKEND,
            "model": MODEL_NAME,
            "text": text.strip(),
            "raw_text": raw,
            "loaded": True,
            "cuda": torch.cuda.is_available(),
        }

    assert _tokenizer is not None and _model is not None
    prompt = _tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    answer_prefix = "最终答案：\n"
    prompt += answer_prefix
    inputs = _tokenizer([prompt], return_tensors="pt")
    device = _model.device
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with _generate_lock:
        with torch.inference_mode():
            output = _model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                do_sample=bool(req.do_sample),
                pad_token_id=_tokenizer.eos_token_id,
            )
    raw = answer_prefix + _tokenizer.decode(output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    text = _strip_thinking(raw) if req.strip_thinking else raw
    return {
        "ok": True,
        "backend": BACKEND,
        "model": MODEL_NAME,
        "text": text.strip(),
        "raw_text": raw,
        "loaded": True,
        "cuda": torch.cuda.is_available(),
        "gpu_memory_allocated_gb": round(torch.cuda.memory_allocated() / 1024**3, 2) if torch.cuda.is_available() else 0,
    }


@app.get("/health")
async def health(load: int = 0):
    if load:
        try:
            await asyncio.to_thread(_load_model)
        except Exception as exc:
            return {"ok": False, "loaded": False, "model_dir": MODEL_DIR, "error": str(exc)}
    return {
        "ok": not bool(_load_error),
        "loaded": _model_loaded(),
        "backend": BACKEND,
        "model": MODEL_NAME,
        "model_dir": MODEL_DIR,
        "ollama_url": OLLAMA_URL if BACKEND == "ollama" else "",
        "cuda": torch.cuda.is_available(),
        "error": _load_error,
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        return await asyncio.to_thread(_generate, req)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "loaded": _model_loaded(), "model": MODEL_NAME}
