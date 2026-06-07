"""ScreenPal Widget — floating companion with voice + screenshot favorites."""

import io
import queue
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from core import Config, NoteGenerator, ScreenCapture, StudyBuddy, select_capture_region

# ─── Colors ────────────────────────────────────────────────────────────

BG = "#1a1814"
SURFACE = "#231f1a"
SURFACE2 = "#2d2822"
BORDER = "#3a342c"
TEXT = "#e8e4db"
TEXT2 = "#9d9689"
AMBER = "#e8a840"
GREEN = "#7a9f7a"
RED = "#c47a6a"

# ─── TTS ───────────────────────────────────────────────────────────────

try:
    import pyttsx3
    _tts = pyttsx3.init()
    _tts.setProperty("rate", 180)
    # Try to find a Chinese voice
    voices = _tts.getProperty("voices")
    for v in voices:
        if "chinese" in v.name.lower() or "zh" in v.name.lower() or "chinese" in v.id.lower():
            _tts.setProperty("voice", v.id)
            break
    TTS_OK = True
except Exception:
    TTS_OK = False
    _tts = None


def speak(text):
    """Read text aloud. Non-blocking."""
    if not TTS_OK or not text:
        return
    clean = text.replace("**", "").replace("`", "").replace("#", "").replace("*", "").strip()
    # Truncate if too long
    if len(clean) > 300:
        clean = clean[:300] + "……后面省略"

    def _run():
        try:
            _tts.say(clean)
            _tts.runAndWait()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


# ─── STT ───────────────────────────────────────────────────────────────

try:
    import speech_recognition as sr
    _recognizer = sr.Recognizer()
    _mic = sr.Microphone()
    with _mic as source:
        _recognizer.adjust_for_ambient_noise(source, duration=0.5)
    STT_OK = True
except Exception:
    STT_OK = False
    _recognizer = None
    _mic = None


def listen(timeout=5) -> str | None:
    """Listen from mic and return transcribed text, or None."""
    if not STT_OK:
        return None
    try:
        with _mic as source:
            audio = _recognizer.listen(source, timeout=timeout, phrase_time_limit=8)
        try:
            return _recognizer.recognize_google(audio, language="zh-CN")
        except Exception:
            try:
                return _recognizer.recognize_google(audio, language="zh-CN")
            except Exception:
                return None
    except Exception:
        return None


# ─── VAD (Voice Activity Detection) ────────────────────────────────────

try:
    import sounddevice as sd
    import numpy as np
    VAD_OK = True
except Exception:
    VAD_OK = False


_vad_active = False
_vad_thread = None
_vad_callback = None  # Called when speech is detected: callback(text)


def _vad_loop():
    """Background loop: monitor mic level, trigger STT on speech."""
    sample_rate = 16000
    chunk_sec = 0.3
    threshold = 0.015
    silence_frames = 0

    stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32")
    stream.start()

    while _vad_active:
        try:
            data, overflowed = stream.read(int(chunk_sec * sample_rate))
            level = float(np.abs(data).mean())

            if level > threshold:
                silence_frames = 0
                # Speech detected — trigger STT
                text = listen(timeout=5)
                if text and _vad_callback:
                    _vad_callback(text)
                # Cooldown to avoid re-triggering on own TTS
                import time
                time.sleep(1.5)
            else:
                silence_frames += 1
        except Exception:
            import time
            time.sleep(0.1)

    stream.stop()
    stream.close()


def start_vad(callback):
    """Start voice activity detection. callback(text) when speech detected."""
    global _vad_active, _vad_thread, _vad_callback
    if not VAD_OK or not STT_OK:
        return False
    _vad_active = True
    _vad_callback = callback
    _vad_thread = threading.Thread(target=_vad_loop, daemon=True)
    _vad_thread.start()
    return True


def stop_vad():
    """Stop voice activity detection."""
    global _vad_active
    _vad_active = False


# ─── Screenshot Favorites ──────────────────────────────────────────────

SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)


def save_screenshot(img: Image.Image, note: str = ""):
    """Save a screenshot to the favorites folder."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"{ts}.jpg"
    img.save(path, "JPEG", quality=85)
    return path


# ─── Widget ────────────────────────────────────────────────────────────

class ScreenPalWidget:
    def __init__(self):
        self.config = Config()
        self.capture = ScreenCapture(self.config)
        self.buddy = StudyBuddy(self.config)
        self.notes = NoteGenerator(self.config, self.buddy)
        self.current_image = None
        self.current_b64 = None
        self.photo = None
        self.is_listening = False
        self.ui_queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("ScreenPal")
        self._setup_window()
        self._build_ui()
        self._load_region_status()
        self._poll_ui_queue()
        self.root.mainloop()

    # ─── Window ─────────────────────────────────────────────────────

    def _setup_window(self):
        self.root.geometry("380x580+{}+80".format(
            self.root.winfo_screenwidth() - 400))
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.root.minsize(300, 350)

        self._drag_x = 0
        self._drag_y = 0

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_motion(self, event):
        if self._drag_x and self._drag_y:
            x = self.root.winfo_x() + event.x - self._drag_x
            y = self.root.winfo_y() + event.y - self._drag_y
            self.root.geometry("+{}+{}".format(x, y))

    # ─── UI Build ───────────────────────────────────────────────────

    def _build_ui(self):
        # ── Title bar ──
        title_bar = tk.Frame(self.root, bg=SURFACE, height=30)
        title_bar.pack(fill=tk.X, side=tk.TOP)
        title_bar.pack_propagate(False)

        tk.Label(title_bar, text="ScreenPal 学伴", bg=SURFACE, fg=AMBER,
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT, padx=8)

        # Hands-free toggle
        self.hf_btn = tk.Label(title_bar, text="🎙️", bg=SURFACE, fg=TEXT2,
                                font=("Consolas", 10), cursor="hand2")
        self.hf_btn.pack(side=tk.LEFT, padx=4)
        self.hf_btn.bind("<Button-1>", lambda e: self._toggle_hands_free())
        self.hands_free_on = False

        # Voice status indicator
        self.voice_indicator = tk.Label(title_bar, text="", bg=SURFACE, fg=GREEN,
                                         font=("Microsoft YaHei", 8))
        self.voice_indicator.pack(side=tk.LEFT, padx=2)
        if TTS_OK:
            self.voice_indicator.config(text="🔊")
        if STT_OK:
            self.voice_indicator.config(text=self.voice_indicator.cget("text") + " 🎤")

        # Favorites button
        fav_btn = tk.Label(title_bar, text="📷", bg=SURFACE, fg=TEXT2,
                            font=("Consolas", 10), cursor="hand2")
        fav_btn.pack(side=tk.LEFT, padx=4)
        fav_btn.bind("<Button-1>", lambda e: self._show_favorites())

        # Mini button
        self.mini_btn = tk.Label(title_bar, text="—", bg=SURFACE, fg=TEXT2,
                                  font=("Consolas", 12), cursor="hand2")
        self.mini_btn.pack(side=tk.RIGHT, padx=4)
        self.mini_btn.bind("<Button-1>", lambda e: self._toggle_mini())

        # Close
        close_btn = tk.Label(title_bar, text="✕", bg=SURFACE, fg=TEXT2,
                              font=("Consolas", 10), cursor="hand2")
        close_btn.pack(side=tk.RIGHT, padx=2)
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=RED))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=TEXT2))

        # Drag
        for w in [title_bar] + list(title_bar.winfo_children()):
            w.bind("<Button-1>", self._on_drag_start, add="+")
            w.bind("<B1-Motion>", self._on_drag_motion, add="+")

        # ── Main ──
        self.main_frame = tk.Frame(self.root, bg=BG)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(4, 6))

        # Preview
        self.preview_frame = tk.Frame(self.main_frame, bg=SURFACE2, height=110)
        self.preview_frame.pack(fill=tk.X, pady=(0, 4))
        self.preview_frame.pack_propagate(False)

        self.preview_label = tk.Label(
            self.preview_frame, bg=SURFACE2, fg=TEXT2,
            text="点击捕获屏幕\n开始一起学习",
            font=("Microsoft YaHei", 10), justify=tk.CENTER,
        )
        self.preview_label.pack(expand=True, fill=tk.BOTH)

        # Buttons
        btn_row = tk.Frame(self.main_frame, bg=BG)
        btn_row.pack(fill=tk.X, pady=(0, 4))

        self.capture_btn = tk.Button(btn_row, text="捕获屏幕", command=self._do_capture,
                                      bg=AMBER, fg="#1a1814", font=("Microsoft YaHei", 9, "bold"),
                                      relief=tk.FLAT, cursor="hand2", padx=10, pady=2,
                                      activebackground="#c48830", activeforeground="#1a1814")
        self.capture_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        region_btn = tk.Button(btn_row, text="✂", command=self._do_select_region,
                                bg=SURFACE2, fg=TEXT, font=("Consolas", 10),
                                relief=tk.FLAT, cursor="hand2", padx=8, pady=2)
        region_btn.pack(side=tk.LEFT, padx=(4, 0))

        fav_btn2 = tk.Button(btn_row, text="★", command=self._do_favorite_current,
                              bg=SURFACE2, fg=TEXT, font=("Consolas", 10),
                              relief=tk.FLAT, cursor="hand2", padx=8, pady=2)
        fav_btn2.pack(side=tk.LEFT, padx=(4, 0))

        # Region info
        self.region_label = tk.Label(self.main_frame, bg=BG, fg=AMBER,
                                      font=("Microsoft YaHei", 8), anchor=tk.W)
        self.region_label.pack(fill=tk.X, pady=(0, 2))

        # Status
        self.status_frame = tk.Frame(self.main_frame, bg=SURFACE2)
        self.status_frame.pack(fill=tk.X, pady=(0, 6))

        self.status_dot = tk.Canvas(self.status_frame, width=8, height=8,
                                     bg=SURFACE2, highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(6, 4), pady=4)
        self._dot = self.status_dot.create_oval(1, 1, 7, 7, fill=TEXT2, outline="")

        self.status_text = tk.Label(self.status_frame, text="就绪",
                                     bg=SURFACE2, fg=TEXT2,
                                     font=("Microsoft YaHei", 8))
        self.status_text.pack(side=tk.LEFT)

        # ── Chat ──
        chat_frame = tk.Frame(self.main_frame, bg=SURFACE)
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        self.chat_text = tk.Text(chat_frame, bg=SURFACE, fg=TEXT,
                                  font=("Microsoft YaHei", 10),
                                  wrap=tk.WORD, relief=tk.FLAT,
                                  padx=10, pady=8, state=tk.DISABLED,
                                  cursor="arrow")
        self.chat_text.pack(fill=tk.BOTH, expand=True)
        self.chat_text.tag_configure("ai", foreground=TEXT, lmargin1=10, lmargin2=10,
                                      rmargin=40, spacing1=6, spacing3=4)
        self.chat_text.tag_configure("user", foreground=AMBER, lmargin1=40,
                                      lmargin2=40, rmargin=10, spacing1=6, spacing3=4)
        self.chat_text.tag_configure("system", foreground=TEXT2,
                                      font=("Microsoft YaHei", 8, "italic"),
                                      justify=tk.CENTER, spacing1=4, spacing3=2)
        self.chat_text.tag_configure("label", foreground=TEXT2,
                                      font=("Microsoft YaHei", 7))

        scroll = tk.Scrollbar(chat_frame, bg=SURFACE, troughcolor=SURFACE)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text.config(yscrollcommand=scroll.set)
        scroll.config(command=self.chat_text.yview)

        self._add_chat("system", "嘿！我是小伴，图书馆学习搭子。\n点「捕获屏幕」开始，或用语音聊天~")

        # ── Input ──
        input_frame = tk.Frame(self.main_frame, bg=BG)
        input_frame.pack(fill=tk.X)

        self.mic_btn = tk.Label(input_frame, text="🎤", bg=SURFACE2, fg=TEXT,
                                 font=("Consolas", 14), cursor="hand2",
                                 padx=6, pady=2)
        self.mic_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.mic_btn.bind("<Button-1>", lambda e: self._toggle_mic())

        self.input_var = tk.StringVar()
        self.input_entry = tk.Entry(input_frame, textvariable=self.input_var,
                                     bg=SURFACE2, fg=TEXT,
                                     font=("Microsoft YaHei", 10),
                                     relief=tk.FLAT, insertbackground=TEXT)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.input_entry.bind("<Return>", lambda e: self._do_chat())
        self.input_entry.bind("<KP_Enter>", lambda e: self._do_chat())

        send_btn = tk.Button(input_frame, text="发送", command=self._do_chat,
                              bg=AMBER, fg="#1a1814", font=("Microsoft YaHei", 9, "bold"),
                              relief=tk.FLAT, padx=12, pady=3, cursor="hand2")
        send_btn.pack(side=tk.LEFT, padx=(4, 0))

        # ── Bottom ──
        bottom = tk.Frame(self.main_frame, bg=BG)
        bottom.pack(fill=tk.X, pady=(2, 0))

        note_btn = tk.Label(bottom, text="生成笔记", bg=BG, fg=TEXT2,
                             font=("Microsoft YaHei", 8), cursor="hand2")
        note_btn.pack(side=tk.LEFT)
        note_btn.bind("<Button-1>", lambda e: self._do_generate_note())

        export_btn = tk.Label(bottom, text="导出手册", bg=BG, fg=TEXT2,
                               font=("Microsoft YaHei", 8), cursor="hand2")
        export_btn.pack(side=tk.RIGHT)
        export_btn.bind("<Button-1>", lambda e: self._do_export())

    # ─── Mini mode ───────────────────────────────────────────────────

    def _toggle_mini(self):
        is_mini = self.root.winfo_height() < 200
        if is_mini:
            self.root.geometry("380x580")
            self.mini_btn.config(text="—")
            self.main_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(4, 6))
        else:
            self.root.geometry("240x48")
            self.mini_btn.config(text="□")
            self.main_frame.pack_forget()
            self._add_chat("system", "迷你模式 — 点 — 展开")

    # ─── Chat display ────────────────────────────────────────────────

    def _add_chat(self, role, text):
        self.ui_queue.put(("chat", role, text))

    def _add_chat_now(self, role, text):
        self.chat_text.config(state=tk.NORMAL)
        if role == "ai":
            self.chat_text.insert(tk.END, "小伴\n", "label")
            self.chat_text.insert(tk.END, text + "\n\n", "ai")
        elif role == "user":
            self.chat_text.insert(tk.END, "你\n", "label")
            self.chat_text.insert(tk.END, text + "\n\n", "user")
        else:
            self.chat_text.insert(tk.END, text + "\n\n", "system")
        self.chat_text.config(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    # ─── UI Queue ────────────────────────────────────────────────────

    def _poll_ui_queue(self):
        """Process UI updates from background threads."""
        while not self.ui_queue.empty():
            msg = self.ui_queue.get_nowait()
            if msg[0] == "chat":
                self._add_chat_now(msg[1], msg[2])
            elif msg[0] == "status":
                self._set_status_now(msg[1], msg[2])
            elif msg[0] == "preview":
                self._show_preview_now(msg[1])
            elif msg[0] == "btn":
                self.capture_btn.config(text=msg[1], state=msg[2])
        self.root.after(100, self._poll_ui_queue)

    # ─── Status ──────────────────────────────────────────────────────

    def _set_status(self, text, color=TEXT2):
        self.ui_queue.put(("status", text, color))

    def _set_status_now(self, text, color=TEXT2):
        self.status_text.config(text=text, fg=color)
        self.status_dot.itemconfig(self._dot, fill=color)

    # ─── Screen Capture ──────────────────────────────────────────────

    def _do_capture(self):
        self.capture_btn.config(text="捕获中...", state=tk.DISABLED)
        self._set_status("分析中...", AMBER)

        def run():
            try:
                b64, img = self.capture.capture_base64()
                result = self.buddy.analyze_screen(b64)
                self.current_image = img
                self.current_b64 = b64
                self.ui_queue.put(("chat", "ai", result["analysis"]))
                self.ui_queue.put(("preview", img))
                self.ui_queue.put(("status", "就绪", GREEN))
                self.ui_queue.put(("btn", "捕获屏幕", tk.NORMAL))
                # Speak the AI response
                speak(result["analysis"])
            except Exception as e:
                self.ui_queue.put(("chat", "system", "捕获失败: {}".format(e)))
                self.ui_queue.put(("status", "出错", RED))
                self.ui_queue.put(("btn", "捕获屏幕", tk.NORMAL))

        threading.Thread(target=run, daemon=True).start()

    def _show_preview_now(self, img):
        preview_w = self.preview_frame.winfo_width() or 360
        preview_h = 110
        display = img.copy()
        display.thumbnail((preview_w - 8, preview_h - 8), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(display)
        self.preview_label.config(image=self.photo, text="")

    # ─── Chat ────────────────────────────────────────────────────────

    def _do_chat(self):
        msg = self.input_var.get().strip()
        if not msg:
            return
        self.input_var.set("")
        self._add_chat("user", msg)
        self._set_status("思考中...", AMBER)

        def run():
            try:
                result = self.buddy.chat(msg, voice_mode=True)
                self.ui_queue.put(("chat", "ai", result["reply"]))
                self.ui_queue.put(("status", "就绪", GREEN))
                speak(result["reply"])
                if result.get("difficulty_flagged"):
                    self.ui_queue.put(("chat", "system", "检测到疑难点 → 可生成笔记"))
            except Exception as e:
                self.ui_queue.put(("chat", "system", "发送失败: {}".format(e)))
                self.ui_queue.put(("status", "出错", RED))

        threading.Thread(target=run, daemon=True).start()

    # ─── Hands-free ─────────────────────────────────────────────────

    def _toggle_hands_free(self):
        if self.hands_free_on:
            self.hands_free_on = False
            stop_vad()
            self.hf_btn.config(fg=TEXT2, bg=SURFACE)
            self._set_status("免提已关闭", GREEN)
            self._add_chat("system", "免提模式已关闭")
        else:
            ok = start_vad(self._on_vad_speech)
            if not ok:
                self._add_chat("system", "免提模式不可用（需麦克风权限）")
                return
            self.hands_free_on = True
            self.hf_btn.config(fg="#1a1814", bg=AMBER)
            self._set_status("免提监听中... (直接说话)", AMBER)
            self._add_chat("system", "免提模式已开启 — 直接说话就行，我一直在听~")

    def _on_vad_speech(self, text):
        """Called from VAD thread when speech is detected."""
        self.ui_queue.put(("chat", "user", text))
        self.ui_queue.put(("status", "思考中...", AMBER))
        try:
            result = self.buddy.chat(text, voice_mode=True)
            self.ui_queue.put(("chat", "ai", result["reply"]))
            self.ui_queue.put(("status", "免提监听中... (直接说话)", AMBER))
            speak(result["reply"])
        except Exception as e:
            self.ui_queue.put(("chat", "system", "发送失败: {}".format(e)))
            self.ui_queue.put(("status", "免提监听中... (直接说话)", AMBER))

    # ─── Voice ───────────────────────────────────────────────────────

    def _toggle_mic(self):
        if self.is_listening:
            self.is_listening = False
            self.mic_btn.config(fg=TEXT, bg=SURFACE2)
            self._set_status("就绪", GREEN)
            return

        if not STT_OK:
            self._add_chat("system", "语音识别不可用。请打字输入。")
            return

        self.is_listening = True
        self.mic_btn.config(fg="#1a1814", bg=AMBER)
        self._set_status("聆听中... (说话后自动识别)", AMBER)

        def run():
            text = listen(timeout=6)
            if text and self.is_listening:
                self.ui_queue.put(("chat", "user", text))
                self.ui_queue.put(("status", "思考中...", AMBER))
                try:
                    result = self.buddy.chat(text, voice_mode=True)
                    self.ui_queue.put(("chat", "ai", result["reply"]))
                    self.ui_queue.put(("status", "就绪", GREEN))
                    speak(result["reply"])
                except Exception as e:
                    self.ui_queue.put(("chat", "system", "发送失败: {}".format(e)))
                    self.ui_queue.put(("status", "出错", RED))
            elif not text and self.is_listening:
                self.ui_queue.put(("chat", "system", "没听清，再试一次？"))
                self.ui_queue.put(("status", "就绪", GREEN))

            self.is_listening = False
            self.root.after(0, lambda: self.mic_btn.config(fg=TEXT, bg=SURFACE2))

        threading.Thread(target=run, daemon=True).start()

    # ─── Region ──────────────────────────────────────────────────────

    def _do_select_region(self):
        self._set_status("框选区域中...", AMBER)
        self.root.iconify()

        def run():
            region = select_capture_region(self.config)
            self.root.after(0, lambda: self._on_region_done(region))

        threading.Thread(target=run, daemon=True).start()

    def _on_region_done(self, region):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        if region:
            self._load_region_status()
            self._add_chat("system", "区域已选定: {}×{}".format(region['w'], region['h']))
        self._set_status("就绪", GREEN)

    def _load_region_status(self):
        r = self.config.get("capture_region")
        if r and r.get("active"):
            self.region_label.config(
                text="📐 已框选 ({}×{}) — 右键清除".format(r['w'], r['h']))
            self.region_label.bind("<Button-3>", lambda e: self._clear_region())
        else:
            self.region_label.config(text="")

    def _clear_region(self):
        self.config.set("capture_region", {"active": False})
        self.region_label.config(text="")
        self._add_chat("system", "已恢复全屏捕获")

    # ─── Screenshot Favorites ────────────────────────────────────────

    def _do_favorite_current(self):
        """Save current screenshot to favorites."""
        if self.current_image is None:
            self._add_chat("system", "请先捕获屏幕再收藏")
            return
        path = save_screenshot(self.current_image)
        self._add_chat("system", "截图已收藏: {}".format(path.name))

    def _show_favorites(self):
        """Open a popup showing saved screenshots."""
        files = sorted(SCREENSHOTS_DIR.glob("*.jpg"), reverse=True)
        if not files:
            self._add_chat("system", "收藏夹为空。捕获屏幕后点 ★ 收藏")
            return

        # Simple popup window
        popup = tk.Toplevel(self.root)
        popup.title("截图收藏夹")
        popup.geometry("400x500+{}+{}".format(
            self.root.winfo_x() + 50, self.root.winfo_y() + 50))
        popup.configure(bg=SURFACE)
        popup.attributes("-topmost", True)

        tk.Label(popup, text="截图收藏夹 ({} 张)".format(len(files)),
                 bg=SURFACE, fg=AMBER, font=("Microsoft YaHei", 11, "bold"),
                 ).pack(pady=(10, 5))

        canvas = tk.Canvas(popup, bg=SURFACE, highlightthickness=0)
        scrollbar = tk.Scrollbar(popup, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=SURFACE)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for f in files[:30]:  # Show last 30
            try:
                img = Image.open(f)
                img.thumbnail((360, 200), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)

                row = tk.Frame(scroll_frame, bg=SURFACE, pady=3)
                row.pack(fill=tk.X)

                lbl = tk.Label(row, image=photo, bg=SURFACE)
                lbl.image = photo  # Keep reference
                lbl.pack()

                info = tk.Label(row, text=f.stem, bg=SURFACE, fg=TEXT2,
                                 font=("Consolas", 7))
                info.pack()
            except Exception:
                pass


    # ─── Notes ───────────────────────────────────────────────────────

    def _do_generate_note(self):
        self._set_status("生成笔记中...", AMBER)

        def run():
            try:
                result = self.notes.generate()
                self.ui_queue.put(("chat", "system", "笔记已生成: {}".format(result['filename'])))
                self.ui_queue.put(("status", "就绪", GREEN))
            except Exception as e:
                self.ui_queue.put(("chat", "system", "笔记生成失败: {}".format(e)))
                self.ui_queue.put(("status", "就绪", GREEN))

        threading.Thread(target=run, daemon=True).start()

    def _do_export(self):
        def run():
            try:
                result = self.notes.export_all()
                if result:
                    self.ui_queue.put(("chat", "system", "复习手册已导出:\n{}".format(result['filepath'])))
            except Exception as e:
                self.ui_queue.put(("chat", "system", "导出失败: {}".format(e)))

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    ScreenPalWidget()
