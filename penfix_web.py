#!/usr/bin/env python3
"""penfix 的局域网 Web 控制台（零第三方依赖）。

只监听 127.0.0.1，避免暴露到网络。

API
---
GET  /                → webui.html
GET  /api/state       → {config, status}
GET  /api/status      → 状态（供前端每秒轮询）
POST /api/config      → 保存配置并热重载守护进程
POST /api/learn       → 等待按下一个物理键，返回 {code, name}
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import penfix_config as C

# 由主程序注册的运行时句柄
STATE = {
    'get_status': None,     # callable -> dict
    'reload': None,         # callable(config) -> None
    'learn': None,          # callable(timeout) -> dict|None
    'log': print,
}

WEB_PATH = os.path.join(HERE, 'webui.html')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        STATE['log'](f'[web] {self.address_string()} {fmt % args}')

    # -------------------------------------------------- 工具

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def _err(self, msg, code=400):
        self._json({'error': str(msg)}, code)

    def _read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ValueError(f'JSON 解析失败: {e}')

    # -------------------------------------------------- 路由

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path in ('/', '/index.html'):
                self._send_file()
            elif path == '/api/state':
                self._json({'config': C.load(), 'status': self._status()})
            elif path == '/api/status':
                self._json(self._status())
            else:
                self._err('not found', 404)
        except Exception as e:
            self._err(e, 500)

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            body = self._read_json()
            if path == '/api/config':
                cfg = C.normalize(body)
                saved = C.save(cfg)
                if STATE['reload']:
                    STATE['reload'](cfg)
                self._json({'ok': True, 'path': saved, 'config': cfg})
            elif path == '/api/learn':
                timeout = float(body.get('timeout', 12))
                timeout = max(1.0, min(60.0, timeout))
                if not STATE['learn']:
                    return self._err('学习模式不可用（守护进程未就绪）', 503)
                res = STATE['learn'](timeout)
                if not res:
                    return self._err('超时：没有检测到按键', 408)
                self._json(res)
            else:
                self._err('not found', 404)
        except ValueError as e:
            self._err(e, 400)
        except Exception as e:
            self._err(e, 500)

    # -------------------------------------------------- 辅助

    def _status(self):
        st = {'config_path': C.write_path()}
        try:
            if STATE['get_status']:
                st.update(STATE['get_status']() or {})
        except Exception as e:
            st['status_error'] = str(e)
        return st

    def _send_file(self):
        try:
            with open(WEB_PATH, 'rb') as f:
                data = f.read()
        except OSError as e:
            return self._err(f'缺少 webui.html: {e}', 500)
        self._send(200, data, 'text/html; charset=utf-8')


class Server:
    def __init__(self, host='127.0.0.1', port=0):
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()
        return self.port

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def main():
    import argparse
    import webbrowser
    ap = argparse.ArgumentParser(description='penfix Web 控制台')
    ap.add_argument('--port', type=int, default=8848)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()

    srv = Server(args.host, args.port)
    port = srv.start()
    url = f'http://{args.host}:{port}/'
    print(f'控制台: {url}')
    print('（当前为独立模式，只能查看/编辑配置，无法实时读取设备状态）')
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    srv.stop()


if __name__ == '__main__':
    main()
