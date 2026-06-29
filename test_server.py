from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

@app.get("/")
def index():
    return HTMLResponse("""
    <html>
    <head><title>Test OK</title></head>
    <body style="font-family:Arial;padding:40px;">
        <h1>服务正常</h1>
        <p>如果你能看到这个页面，说明 18082 端口和浏览器没有问题。</p>
        <p>问题在原来的 app.py 或页面 JS。</p>
    </body>
    </html>
    """)
