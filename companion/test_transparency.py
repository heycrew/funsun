"""测试 pywebview 透明度和 easy_drag 行为"""
import webview

# 测试1：纯透明窗口——没有任何 body 背景，只有文字阴影
html_transparent = """
<html><head><style>
html,body{width:100%;height:100%;margin:0;background:transparent}
h1{color:#fff;font-size:48px;text-align:center;padding-top:60px;
   text-shadow:0 0 8px #000,0 0 20px #000;}
</style></head><body>
<h1>纯透明测试</h1>
</body></html>
"""

# 测试2：easy_drag 下 range input 能否正常拖动
html_slider = """
<html><head><style>
html,body{width:100%;height:100%;margin:0;background:rgba(0,0,0,0.7)}
.container{padding:30px;}
h3{color:#fff;margin:20px 0}
input[type=range]{width:300px;height:6px;cursor:pointer}
p{color:#aaa;font-size:12px}
</style></head><body>
<div class="container">
<h3>滑块拖拽测试 — 分别拖动下面4个滑块</h3>
<p>如果滑块能拖动 → easy_drag 不拦截 range input</p>
<p>如果滑块不能拖动 → easy_drag 拦截了所有鼠标事件</p>
<input type="range" min="0" max="100" value="50"><br><br>
<input type="range" min="0" max="100" value="30"><br><br>
<input type="range" min="0" max="100" value="70"><br><br>
<input type="range" min="0" max="100" value="90">
</div>
</body></html>
"""

# 打开两个窗口
webview.create_window(
    "透明度测试(transparent+easy_drag)", html=html_transparent,
    frameless=True, on_top=True, transparent=True, easy_drag=True,
    width=400, height=250, x=100, y=100
)

webview.create_window(
    "滑块测试(easy_drag)", html=html_slider,
    frameless=True, on_top=True, transparent=True, easy_drag=True,
    width=420, height=400, x=550, y=100
)

# 也试一个不带 easy_drag 的滑条窗口
webview.create_window(
    "滑块测试(无easy_drag)", html=html_slider,
    frameless=True, on_top=True, transparent=True, easy_drag=False,
    width=420, height=400, x=550, y=550
)

webview.start()
