"""
拍卖看板 - 桌面启动器
使用 pywebview 创建无边框、置顶的看板窗口
兼容 Windows 10 / Windows 11
"""
import json
import os
import webview

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "companion_config.json")
HTML_FILE = os.path.join(SCRIPT_DIR, "companion.html")


def load_config():
    defaults = {
        "width": 640, "height": 400,
        "x": None, "y": None,
        "always_on_top": True,
        "font_size": 36,
        "server_url": "",
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                defaults.update(json.load(f))
    except Exception:
        pass
    return defaults


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class CompanionAPI:
    """JS 桥接 API"""

    def __init__(self, config: dict):
        self._config = config
        self._window = None

    def _set_window(self, window):
        self._window = window

    def close_window(self):
        self._window.destroy()

    def set_on_top(self, on: bool):
        self._config["always_on_top"] = on
        save_config(self._config)
        self._window.on_top = on

    def get_on_top(self) -> bool:
        return self._window.on_top

    def resize_window(self, w: int, h: int):
        self._window.resize(int(w), int(h))


def main():
    cfg = load_config()

    if cfg["x"] is None or cfg["y"] is None:
        try:
            import tkinter as tk
            root = tk.Tk()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.destroy()
            cfg["x"] = sw - cfg["width"] - 40
            cfg["y"] = sh - cfg["height"] - 80
        except Exception:
            cfg["x"], cfg["y"] = 600, 400

    api = CompanionAPI(cfg)

    window = webview.create_window(
        title="拍卖看板",
        url=HTML_FILE,
        width=cfg["width"], height=cfg["height"],
        x=cfg["x"], y=cfg["y"],
        frameless=True,
        on_top=cfg["always_on_top"],
        transparent=False,
        easy_drag=True,
        resizable=True,
        min_size=(420, 260),
        background_color="#000000",
        js_api=api,
    )

    api._set_window(window)

    def on_closing():
        save_config({
            "width": window.width, "height": window.height,
            "x": window.x or cfg["x"], "y": window.y or cfg["y"],
            "always_on_top": cfg["always_on_top"],
            "font_size": cfg.get("font_size", 36),
            "server_url": cfg.get("server_url", ""),
        })

    window.events.closing += on_closing

    webview.start(debug=False)


if __name__ == "__main__":
    main()
