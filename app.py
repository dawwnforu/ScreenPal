"""ScreenPal — Flask web server."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from core import Config, NoteGenerator, ScreenCapture, StudyBuddy

app = Flask(__name__)

# Global singletons (single-user prototype)
config = Config()
capture = ScreenCapture(config)
buddy = StudyBuddy(config)
notes = NoteGenerator(config, buddy)

# Background auto-capture state
widget_process = None
auto_capture_running = False
auto_capture_thread = None
last_auto_analysis = ""


# ─── Page routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─── API: Screen Capture ──────────────────────────────────────────────

@app.route("/api/capture", methods=["POST"])
def api_capture():
    """Capture screen and analyze with AI."""
    try:
        monitor = request.json.get("monitor", 1) if request.is_json else 1
        b64, img = capture.capture_base64(monitor)
        result = buddy.analyze_screen(b64)

        return jsonify({
            "success": True,
            "image_base64": b64,
            "analysis": result["analysis"],
            "topic": result["topic"],
            "difficulty_count": len(buddy.difficulty_points),
            "image_width": img.width,
            "image_height": img.height,
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"截图分析失败: {e}"}), 500


# ─── API: Chat ────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Send a text message to the study buddy."""
    try:
        data = request.json
        message = data.get("message", "").strip()
        voice_mode = data.get("voice_mode", True)
        if not message:
            return jsonify({"success": False, "error": "消息不能为空"}), 400

        result = buddy.chat(message, voice_mode=voice_mode)

        return jsonify({
            "success": True,
            "reply": result["reply"],
            "difficulty_flagged": result["difficulty_flagged"],
            "difficulty_signal": result.get("difficulty_signal"),
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"对话失败: {e}"}), 500


# ─── API: Notes ───────────────────────────────────────────────────────

@app.route("/api/notes", methods=["GET"])
def api_notes_list():
    return jsonify({"success": True, "notes": notes.list_all()})


@app.route("/api/notes/generate", methods=["POST"])
def api_notes_generate():
    """Generate a review note from current session."""
    try:
        data = request.json or {}
        topic = data.get("topic", buddy.current_screen_topic)
        extra = data.get("extra", "")

        result = notes.generate(topic=topic, extra_context=extra)

        return jsonify({
            "success": True,
            "note": result,
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"生成笔记失败: {e}"}), 500


@app.route("/api/notes/<filename>", methods=["GET"])
def api_notes_get(filename):
    content = notes.get(filename)
    if content is None:
        return jsonify({"success": False, "error": "笔记不存在"}), 404
    return jsonify({"success": True, "filename": filename, "content": content})


@app.route("/api/notes/<filename>", methods=["DELETE"])
def api_notes_delete(filename):
    ok = notes.delete(filename)
    if not ok:
        return jsonify({"success": False, "error": "笔记不存在"}), 404
    return jsonify({"success": True})


@app.route("/api/notes/export/all", methods=["POST"])
def api_notes_export():
    """Export all notes as a combined study guide."""
    try:
        result = notes.export_all()
        if result is None:
            return jsonify({"success": False, "error": "没有笔记可导出"}), 404
        return jsonify({
            "success": True,
            "filepath": result["filepath"],
            "content": result["content"],
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"导出失败: {e}"}), 500


# ─── API: Settings ────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    ak = config.get("anthropic_key", "")
    gk = config.get("google_key", "")
    zk = config.get("zhipu_key", "")
    return jsonify({
        "success": True,
        "settings": {
            "provider": config.get("provider", "zhipu"),
            "anthropic_key": "***" + ak[-4:] if ak else "",
            "google_key": "***" + gk[-4:] if gk else "",
            "zhipu_key": "***" + zk[-4:] if zk else "",
            "claude_model": config.get("claude_model", "claude-sonnet-4-6"),
            "gemini_model": config.get("gemini_model", "gemini-2.0-flash"),
            "zhipu_model": config.get("zhipu_model", "glm-4v"),
            "personality": config.get("personality", "study_buddy"),
            "auto_interval": config.get("auto_interval", 30),
        },
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    try:
        data = request.json
        for key in ["provider", "anthropic_key", "google_key", "zhipu_key",
                     "claude_model", "gemini_model", "zhipu_model",
                     "personality", "auto_interval"]:
            if key in data and data[key]:
                config.set(key, data[key])

        # Force re-create provider with new keys
        buddy.reset_provider()

        return jsonify({"success": True, "message": "设置已保存"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/providers", methods=["GET"])
def api_providers():
    return jsonify({
        "success": True,
        "providers": {
            "zhipu": "智谱 GLM-4V — 免费额度，国内直连，多模态",
            "gemini": "Google Gemini — 免费额度，需VPN",
            "claude": "Anthropic Claude — 付费，理解力最强",
        },
        "models": {
            "zhipu": ["glm-4v", "glm-4v-plus", "glm-4v-flash"],
            "gemini": ["gemini-2.0-flash", "gemini-2.5-flash"],
            "claude": ["claude-sonnet-4-6", "claude-opus-4-7"],
        },
        "current": config.get("provider", "zhipu"),
    })


# ─── API: Region Selection ──────────────────────────────────────────────

@app.route("/api/region/select", methods=["POST"])
def api_region_select():
    """Open a transparent overlay to select capture region."""
    from core import select_capture_region
    import threading

    result = {"done": False, "region": None}

    def run():
        result["region"] = select_capture_region(config)
        result["done"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()

    return jsonify({
        "success": True,
        "message": "区域选择窗口已打开，在屏幕上拖拽框选",
    })


@app.route("/api/region", methods=["GET"])
def api_region_get():
    r = config.get("capture_region")
    return jsonify({
        "success": True,
        "region": r,
        "active": r.get("active", False) if r else False,
    })


@app.route("/api/region/clear", methods=["POST"])
def api_region_clear():
    config.set("capture_region", {"active": False})
    return jsonify({"success": True, "message": "已恢复全屏捕获"})


# ─── API: Widget Launcher ──────────────────────────────────────────────

@app.route("/api/widget/launch", methods=["POST"])
def api_widget_launch():
    """Launch the native tkinter widget in a new process."""
    global widget_process
    widget_path = Path(__file__).parent / "widget.py"

    try:
        widget_process = subprocess.Popen(
            [sys.executable, str(widget_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"success": True, "message": "组件已启动", "pid": widget_process.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/widget/status", methods=["GET"])
def api_widget_status():
    """Check if widget is running."""
    global widget_process
    running = widget_process is not None and widget_process.poll() is None
    return jsonify({"success": True, "running": running, "pid": widget_process.pid if running else None})


# ─── API: Session ─────────────────────────────────────────────────────

@app.route("/api/session", methods=["GET"])
def api_session():
    return jsonify({
        "success": True,
        "topic": buddy.current_screen_topic,
        "difficulty_points": buddy.difficulty_points,
        "message_count": len(buddy.conversation_history),
        "auto_capture_active": auto_capture_running,
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    buddy.reset()
    global last_auto_analysis
    last_auto_analysis = ""
    return jsonify({"success": True, "message": "会话已重置"})


# ─── API: Auto Capture ────────────────────────────────────────────────

@app.route("/api/auto/start", methods=["POST"])
def api_auto_start():
    global auto_capture_running, auto_capture_thread
    if auto_capture_running:
        return jsonify({"success": False, "error": "自动监控已在运行"})

    interval = request.json.get("interval", config.get("auto_interval", 30)) if request.is_json else 30

    def auto_loop():
        global last_auto_analysis
        while auto_capture_running:
            try:
                b64, _ = capture.capture_base64()
                result = buddy.analyze_screen(b64)
                last_auto_analysis = result["analysis"]
            except Exception:
                pass
            time.sleep(interval)

    auto_capture_running = True
    auto_capture_thread = threading.Thread(target=auto_loop, daemon=True)
    auto_capture_thread.start()

    return jsonify({"success": True, "message": f"自动监控已启动，间隔 {interval} 秒"})


@app.route("/api/auto/stop", methods=["POST"])
def api_auto_stop():
    global auto_capture_running
    auto_capture_running = False
    return jsonify({"success": True, "message": "自动监控已停止"})


@app.route("/api/auto/status", methods=["GET"])
def api_auto_status():
    return jsonify({
        "success": True,
        "active": auto_capture_running,
        "last_analysis": last_auto_analysis[:500] if last_auto_analysis else "",
    })


# ─── Personality info ─────────────────────────────────────────────────

@app.route("/api/personalities", methods=["GET"])
def api_personalities():
    from core import PERSONALITIES
    return jsonify({
        "success": True,
        "personalities": {
            "serious_teacher": "严谨教师 — 学术语言，结构化讲解",
            "study_buddy": "学霸同学 — 轻松日常，类比丰富",
            "encouraging_coach": "温暖教练 — 分步拆解，鼓励为主",
            "socratic_guide": "苏格拉底导师 — 引导式提问，培养独立思考",
        },
        "current": config.get("personality", "study_buddy"),
    })


# ─── Main ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  ScreenPal - Screen Study Companion")
    print("  ====================================")
    print(f"  Open: http://127.0.0.1:5000")
    print(f"  Notes dir: {Path(config.get('notes_dir')).absolute()}")
    print()
    app.run(host="127.0.0.1", port=5000, debug=True)
