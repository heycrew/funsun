"""测试 pywebview move / get_window_x 是否正常"""
import webview
import json, os

class TestAPI:
    def __init__(self, config):
        self._config = config
        self._window = None
        self._x = config.get("x", 300)
        self._y = config.get("y", 200)

    def get_window_x(self): return self._x
    def get_window_y(self): return self._y

    def move_window_to(self, x, y):
        ix, iy = int(x), int(y)
        print(f"[Python] move_window_to({ix}, {iy})")
        if self._window:
            self._window.move(ix, iy)
        self._x, self._y = ix, iy

    def log(self, msg):
        print(f"[JS] {msg}")

html_test = """
<html><head><style>
body{background:#222;color:#fff;font-family:sans-serif;padding:20px}
.drag{height:40px;background:#444;cursor:move;line-height:40px;padding:0 10px;margin-bottom:20px}
button{padding:10px 20px;margin:5px;font-size:14px}
p{margin:8px 0;color:#aaa;font-size:12px}
</style></head><body>
<div class="drag" id="dragBar">=== 拖拽测试栏 (按住这里拖动) ===</div>
<p id="log"></p>
<button onclick="testMove()">测试1: 移动到 (100,100)</button>
<button onclick="testMove2()">测试2: 向右移动200px</button>
<button onclick="testRead()">测试3: 读取当前位置</button>
<button onclick="doDrag()">测试4: 模拟拖拽向右100px</button>

<script>
function log(msg) {
    document.getElementById('log').textContent += msg + '\\n';
    if (window.pywebview && window.pywebview.api) {
        try { window.pywebview.api.log(msg); } catch(e) {}
    }
}
function testMove() {
    log('调用 move_window_to(100,100)...');
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.move_window_to(100, 100);
        log('move_window_to 调用完成');
    }
}
function testMove2() {
    var x = (window.pywebview && window.pywebview.api) ? window.pywebview.api.get_window_x() : '?';
    var y = (window.pywebview && window.pywebview.api) ? window.pywebview.api.get_window_y() : '?';
    log('当前坐标: x=' + x + ' y=' + y);
    window.pywebview.api.move_window_to(Number(x) + 200, Number(y));
    log('已发送向右200px');
}
function testRead() {
    var x = window.pywebview.api ? window.pywebview.api.get_window_x() : 'N/A';
    var y = window.pywebview.api ? window.pywebview.api.get_window_y() : 'N/A';
    log('get_window_x=' + x + ' get_window_y=' + y);
}

// 模拟实际拖拽逻辑
var dragging = false, startMX = 0, startMY = 0, startWX = 0, startWY = 0, hasPos = false;
document.getElementById('dragBar').addEventListener('mousedown', function(e) {
    dragging = true; startMX = e.screenX; startMY = e.screenY; hasPos = false;
    if (window.pywebview && window.pywebview.api) {
        try {
            startWX = window.pywebview.api.get_window_x() || 0;
            startWY = window.pywebview.api.get_window_y() || 0;
            hasPos = true;
            log('mousedown: screen=(' + startMX + ',' + startMY + ') win=(' + startWX + ',' + startWY + ')');
        } catch(ex) { log('ERROR: ' + ex); }
    }
    e.preventDefault();
});
document.addEventListener('mousemove', function(e) {
    if (!dragging || !hasPos) return;
    var newX = Math.round(startWX + (e.screenX - startMX));
    var newY = Math.round(startWY + (e.screenY - startMY));
    if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.move_window_to(newX, newY);
    }
});
document.addEventListener('mouseup', function() { dragging = false; });

function doDrag() {
    log('模拟拖拽...手动按住标题栏拖动试试');
}
</script></body></html>
"""

cfg = {"x": 400, "y": 300}
api = TestAPI(cfg)
window = webview.create_window("API测试", html=html_test, width=500, height=450, x=400, y=300, frameless=True, on_top=True, js_api=api)
api._window = window
webview.start()
