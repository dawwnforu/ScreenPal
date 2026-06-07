"""ScreenPal core — screen capture, multi-provider AI, note generation."""

import base64
import io
import json
import os
from datetime import datetime
from pathlib import Path

import mss
import httpx
from PIL import Image

# ─── Config ───────────────────────────────────────────────────────────

class Config:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.data = self._load()

    def _load(self):
        defaults = {
            "provider": "gemini",
            "anthropic_key": os.getenv("ANTHROPIC_API_KEY", ""),
            "google_key": os.getenv("GOOGLE_API_KEY", ""),
            "claude_model": "claude-sonnet-4-6",
            "gemini_model": "gemini-2.0-flash",
            "personality": "study_buddy",
            "auto_interval": 30,
            "notes_dir": "notes",
        }
        if Path(self.config_path).exists():
            loaded = json.loads(Path(self.config_path).read_text("utf-8"))
            defaults.update(loaded)
        return defaults

    def save(self):
        Path(self.config_path).write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8"
        )

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()


# ─── Screen Capture ───────────────────────────────────────────────────

class ScreenCapture:
    def __init__(self, config: Config = None):
        self.sct = mss.mss()
        self.monitors = self.sct.monitors
        self.config = config

    def _get_region(self, monitor_index):
        """Get region from config or full monitor."""
        if self.config:
            region = self.config.get("capture_region")
            if region and region.get("active"):
                return {
                    "left": region["x"],
                    "top": region["y"],
                    "width": region["w"],
                    "height": region["h"],
                }
        if monitor_index >= len(self.monitors):
            monitor_index = 1
        return self.monitors[monitor_index]

    def capture(self, monitor_index=1):
        region = self._get_region(monitor_index)
        screenshot = self.sct.grab(region)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        img.thumbnail((1920, 1080), Image.LANCZOS)
        return img

    def to_base64(self, img):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()

    def capture_base64(self, monitor_index=1):
        img = self.capture(monitor_index)
        return self.to_base64(img), img


# ─── Region Selector (tkinter overlay) ─────────────────────────────────

def select_capture_region(config: Config):
    """Open a transparent fullscreen overlay to select capture region.

    User drags a rectangle. Coordinates saved to config.
    Returns the selected region or None if cancelled.
    """
    import tkinter as tk

    result = {"region": None}

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.35)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.config(cursor="cross")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    start_x = tk.IntVar()
    start_y = tk.IntVar()
    rect = None

    # Instructions
    canvas.create_text(
        root.winfo_screenwidth() // 2,
        40,
        text="拖拽框选课件区域 → 松手确认  |  Esc 取消  |  双击全屏",
        fill="#ffcc00",
        font=("Microsoft YaHei", 18, "bold"),
    )

    def on_press(event):
        nonlocal rect
        start_x.set(event.x)
        start_y.set(event.y)
        if rect:
            canvas.delete(rect)
        rect = canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#ffcc00", width=3, dash=(10, 4),
        )

    def on_drag(event):
        nonlocal rect
        if rect:
            canvas.coords(rect, start_x.get(), start_y.get(), event.x, event.y)

    def on_release(event):
        x1, y1 = start_x.get(), start_y.get()
        x2, y2 = event.x, event.y
        if abs(x2 - x1) < 20 and abs(y2 - y1) < 20:
            return  # too small, ignore
        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        config.set("capture_region", {
            "active": True, "x": x, "y": y, "w": w, "h": h,
        })
        result["region"] = {"x": x, "y": y, "w": w, "h": h}
        root.destroy()

    def on_double_click(event):
        config.set("capture_region", {"active": False})
        result["region"] = None
        root.destroy()

    def on_escape(event):
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Double-Button-1>", on_double_click)
    root.bind("<Escape>", on_escape)

    root.focus_force()
    root.mainloop()
    return result["region"]


# ─── AI Providers ─────────────────────────────────────────────────────

PERSONALITIES = {
    "study_buddy": (
        "你的名字叫小伴，你是学生的'图书馆死党'——不是冷冰冰的AI，而是一起熬夜自习的哥们/闺蜜。"
        "语气：轻松、自然、口语化。像微信语音一样说话，多说'对吧？''懂我意思吧？''你想想看~'这种生活化的表达。"
        "习惯：时不时开个无伤大雅的小玩笑，偶尔吐槽课程太难，学习很苦但陪你一起扛。"
    ),
    "serious_teacher": (
        "你是一位严谨但亲切的大学老师。你不是在讲课，是在办公室给学生做一对一辅导。"
        "说话有逻辑有条理，但不摆架子。会反问学生'这个你听懂了吗？用你自己的话复述一遍？'"
        "适当给予肯定，但也会严格指出理解偏差。"
    ),
    "encouraging_coach": (
        "你是一个超有耐心的学习教练。你的信念是：没有笨学生，只有没找到方法的学生。"
        "发现学生困惑，先说'没事没事，这个点很多人卡住的，咱们换个角度想想~'"
        "每次只讲一个点，讲透再往下。使用大量比喻、画图、举例。"
        "每一轮对话都在帮学生悄悄建立信心。"
    ),
    "socratic_guide": (
        "你是苏格拉底式的学习搭档。你从不直接给答案，而是像剥洋葱一样用问题引导思考。"
        "'你先说说你现在是怎么理解的？''如果把这个条件去掉会怎样？''你觉得核心矛盾在哪里？'"
        "但你不是在考学生——你是在陪他一起推理，让他感受'自己想出来'的爽感。"
    ),
}

SYSTEM_PROMPT = """你是 ScreenPal（屏幕学伴），也叫"小伴"。你能实时看到用户的屏幕课件内容。

## 你的核心身份
你和用户是一起在图书馆/自习室学习的**老朋友**。你不是工具，你是**学习搭子**。你们一起看屏幕上的课件，一起吐槽，一起搞懂那些折磨人的知识点。

## 核心行为准则

### 1. 主动引导，不等提问
看到屏幕上有新课件时，不要只是"汇报"内容。而是像朋友一样主动开聊：
- "哦这个！我们上次是不是在XX课上提过？"
- "这页PPT信息量有点大啊，你觉得哪个点最绕？"
- "我先说我的理解哈，你看看对不对……"

### 2. 用口语，不用"AI腔"
- 短句为主，像在说话，不是写文章
- 用"嗯""诶""哈哈""哎"等语气词自然过渡
- 每句话不超过30字——因为你的回复会被语音朗读出来
- 不要编号列表、不要markdown标题、不要长段落

### 3. 先理解学生，再给答案
当学生提问：
  第一步：先确认他的困惑点在哪——"你是卡在XX这个环节了吗？"
  第二步：用一个他能理解的类比/例子来说
  第三步：反问确认——"现在你觉得懂了吗？试着给我讲一遍？"

### 4. 觉察状态，主动切换策略
- 学生连续提问 → 说明这个点真不会 → 换个讲法，打比方
- 学生沉默很久 → 轻轻cue一下："还在吗哈哈，是不是看到公式直接眼前一黑了"
- 学生说"懂了" → 出个小测试题验证一下（友善地）
- 学生说"好难/想放弃" → 先共情再鼓励："期末呢大家都这样，我陪你慢慢啃"

### 5. 分析屏幕课件时
看一眼就抓重点，用大白话说出来：
- 这是什么课，在讲什么主题
- 这个内容**为什么重要**（考点/实用/基础）
- 你觉得哪个部分最可能把人绕晕
- 扔一个引导性问题，邀请学生开聊

## 语音交流注意事项
你的回复将被TTS朗读出来，所以：
- 每次回复3-6句话即可，不要太长
- 不用markdown
- 不用列表
- 自然断句，每句不超过25-30字
- 适当加入"嗯""这样""你看啊"等口语过渡

请始终用中文交流。像老朋友一样陪伴学习。"""

NOTE_SYSTEM = """你是一个擅长整理学习笔记的AI。根据学生的疑难点和课件内容，生成精炼的复习笔记。

笔记格式（Markdown）：
1. **知识标题** — 一句话概括
2. **核心概念** — 用最简语言解释，配合例子
3. **易错点** — 常见误解和注意事项
4. **记忆锦囊** — 口诀/类比/图像联想
5. **速测题** — 2道自测题（附答案）

用中文，保持简洁。笔记是给学生期末复习用的，要实用！"""


class AIProvider:
    """Base class for AI backends."""
    def analyze_screen(self, image_base64, system_prompt, user_text):
        raise NotImplementedError
    def chat(self, messages, system_prompt, max_tokens):
        raise NotImplementedError


class GeminiProvider(AIProvider):
    """Google Gemini via REST API — free tier available."""

    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key, model="gemini-2.0-flash"):
        self.api_key = api_key
        self.model = model

    def _endpoint(self, method):
        return f"{self.BASE}/models/{self.model}:{method}?key={self.api_key}"

    def _build_contents(self, messages):
        """Convert internal message format to Gemini format."""
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            parts = []

            if isinstance(msg["content"], list):
                # Multimodal: image + text
                for item in msg["content"]:
                    if item["type"] == "text":
                        parts.append({"text": item["text"]})
                    elif item["type"] == "image":
                        parts.append({
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": item["source"]["data"],
                            }
                        })
            else:
                parts.append({"text": msg["content"]})
            contents.append({"role": role, "parts": parts})
        return contents

    def analyze_screen(self, image_base64, system_prompt, user_text):
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}},
                    {"text": user_text},
                ],
            }],
            "generationConfig": {"maxOutputTokens": 512, "temperature": 0.8},
        }

        r = httpx.post(self._endpoint("generateContent"), json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def chat(self, messages, system_prompt, max_tokens):
        contents = self._build_contents(messages)

        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.8},
        }

        r = httpx.post(self._endpoint("generateContent"), json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


class ClaudeProvider(AIProvider):
    """Anthropic Claude via SDK."""

    def __init__(self, api_key, model="claude-sonnet-4-6"):
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def analyze_screen(self, image_base64, system_prompt, user_text):
        user_content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64},
            },
            {"type": "text", "text": user_text},
        ]

        response = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text

    def chat(self, messages, system_prompt, max_tokens):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text


class ZhiPuProvider(AIProvider):
    """智谱AI GLM-4V via OpenAI-compatible API — free credits for new users."""

    BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    def __init__(self, api_key, model="glm-4v"):
        self.api_key = api_key
        self.model = model

    def _call(self, messages, max_tokens):
        r = httpx.post(
            self.BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.8,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def analyze_screen(self, image_base64, system_prompt, user_text):
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ]
        return self._call(messages, 512)

    def chat(self, messages, system_prompt, max_tokens):
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        return self._call(full_messages, max_tokens)


def create_provider(config: Config) -> AIProvider:
    """Factory: create the right AI provider from config."""
    prov = config.get("provider", "zhipu")

    if prov == "gemini":
        key = config.get("google_key")
        if not key:
            raise ValueError("请先设置 Google API Key")
        model = config.get("gemini_model", "gemini-2.0-flash")
        return GeminiProvider(key, model)

    elif prov == "claude":
        key = config.get("anthropic_key")
        if not key:
            raise ValueError("请先设置 Anthropic API Key")
        model = config.get("claude_model", "claude-sonnet-4-6")
        return ClaudeProvider(key, model)

    elif prov == "zhipu":
        key = config.get("zhipu_key")
        if not key:
            raise ValueError("请先设置智谱 API Key (在设置 → 智谱 API Key)")
        model = config.get("zhipu_model", "glm-4v")
        return ZhiPuProvider(key, model)

    raise ValueError(f"未知的 AI 提供方: {prov}")


# ─── Study Buddy (provider-agnostic) ──────────────────────────────────

class StudyBuddy:
    def __init__(self, config: Config):
        self.config = config
        self.conversation_history = []
        self.difficulty_points = []
        self.current_screen_topic = ""
        self._provider = None

    @property
    def provider(self) -> AIProvider:
        if self._provider is None:
            self._provider = create_provider(self.config)
        return self._provider

    def reset_provider(self):
        """Force re-create provider (e.g. after settings change)."""
        self._provider = None

    @property
    def personality_prompt(self):
        return PERSONALITIES.get(
            self.config.get("personality", "study_buddy"),
            PERSONALITIES["study_buddy"],
        )

    def analyze_screen(self, image_base64):
        system = f"{SYSTEM_PROMPT}\n\n## 你当前选择的人格\n{self.personality_prompt}"

        user_text = (
            "我看到屏幕上的课件了。请像图书馆里坐在我旁边的老朋友一样，"
            "自然地聊起来。先说说这是什么内容，然后抛一个引导性的问题邀请我开口。"
            "记住用口语，像在说话不是在写文章，每句话不超过25个字。"
        )

        analysis = self.provider.analyze_screen(image_base64, system, user_text)

        self.conversation_history = [
            {"role": "assistant", "content": analysis},
        ]

        return {
            "analysis": analysis,
            "topic": self._extract_topic(analysis),
        }

    def chat(self, message, voice_mode=True):
        system = f"{SYSTEM_PROMPT}\n\n## 你当前选择的人格\n{self.personality_prompt}"

        messages = list(self.conversation_history[-12:])

        if voice_mode:
            hint = (
                "（请用自然口语回复我的问题，像朋友聊天一样。"
                "3到5句话就够，每句话不超过25个字。不用任何格式。）\n\n"
            )
            message = hint + message

        # Claude expects content as string, Gemini too for text-only
        messages.append({"role": "user", "content": message})

        max_tok = 384 if voice_mode else 1024

        reply = self.provider.chat(messages, system, max_tok)

        self.conversation_history.append({"role": "user", "content": message})
        self.conversation_history.append({"role": "assistant", "content": reply})

        difficulty = self._detect_difficulty(message)
        if difficulty:
            self.difficulty_points.append({
                "topic": self.current_screen_topic or "课程内容",
                "signal": difficulty,
                "time": datetime.now().isoformat(),
            })

        return {
            "reply": reply,
            "difficulty_flagged": bool(difficulty),
            "difficulty_signal": difficulty,
        }

    def _extract_topic(self, analysis):
        lines = analysis.strip().split("\n")
        for line in lines[:5]:
            line = line.strip().lstrip("#-•·1234567890. ")
            if len(line) > 4:
                self.current_screen_topic = line[:80]
                return line[:80]
        return "课件内容"

    def _detect_difficulty(self, text):
        signals = [
            "不懂", "不理解", "好难", "太难了", "看不懂", "不明白",
            "没听懂", "什么意思", "可以再解释", "能再说", "还是没懂",
            "confused", "what does", "how does", "why is",
            "i don't understand", "this is hard",
        ]
        for s in signals:
            if s in text.lower():
                return s
        return None

    def reset(self):
        self.conversation_history = []
        self.difficulty_points = []
        self.current_screen_topic = ""


# ─── Note Generator (provider-agnostic) ───────────────────────────────

class NoteGenerator:
    def __init__(self, config: Config, buddy: StudyBuddy):
        self.config = config
        self.buddy = buddy
        self.notes_dir = Path(self.config.get("notes_dir", "notes"))
        self.notes_dir.mkdir(exist_ok=True)

    def generate(self, topic=None, extra_context=""):
        if not topic:
            topic = self.buddy.current_screen_topic or "课程内容"

        difficulties = [d["signal"] for d in self.buddy.difficulty_points]
        diff_text = ", ".join(difficulties) if difficulties else "无明显疑难点"

        context_text = ""
        for entry in self.buddy.conversation_history[-8:]:
            role = "学生" if entry["role"] == "user" else "AI"
            content = entry["content"][:300]
            context_text += f"{role}: {content}\n"

        prompt = f"""请为以下学习内容生成一份复习笔记。

主题：{topic}
检测到的疑难点信号：{diff_text}
额外说明：{extra_context}

近期对话记录：
{context_text}

请按照标准格式生成复习笔记（Markdown）。"""

        # Use the same provider as the study buddy for note generation
        note_content = self.buddy.provider.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=NOTE_SYSTEM,
            max_tokens=2048,
        )

        safe_topic = "".join(c for c in topic[:30] if c.isalnum() or c in " _-").strip()
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_topic or 'note'}.md"
        filepath = self.notes_dir / filename

        full_note = (
            f"# 复习笔记：{topic}\n\n"
            f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{note_content}"
        )
        filepath.write_text(full_note, "utf-8")

        return {
            "filename": filename,
            "content": full_note,
            "filepath": str(filepath),
        }

    def list_all(self):
        notes = []
        for f in sorted(self.notes_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            notes.append({
                "filename": f.name,
                "created": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "preview": f.read_text("utf-8")[:200],
            })
        return notes

    def get(self, filename):
        fp = self.notes_dir / filename
        if fp.exists():
            return fp.read_text("utf-8")
        return None

    def delete(self, filename):
        fp = self.notes_dir / filename
        if fp.exists():
            fp.unlink()
            return True
        return False

    def export_all(self):
        all_content = []
        for f in sorted(self.notes_dir.glob("*.md"), key=lambda x: x.stat().st_mtime):
            all_content.append(f.read_text("utf-8"))

        if not all_content:
            return None

        guide = "# ScreenPal 复习手册\n\n"
        guide += f"> 导出于 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
        guide += "\n\n---\n\n".join(all_content)

        export_path = self.notes_dir / f"复习手册_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        export_path.write_text(guide, "utf-8")

        return {
            "content": guide,
            "filepath": str(export_path),
        }
