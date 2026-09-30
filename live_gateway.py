"""Bounded RTSP/TCP gateway: real-IP quotas and immediate socket cancellation."""
import os
import re
import socket
import socketserver
import threading
from urllib.parse import urlsplit


class Gateway(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, live):
        self.live = live
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, Viewer)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # Never let signed RTSP paths or credentials reach stderr.
        self.live.cache.debug.emit('WARN', 'live_gateway_connection_error')


class Viewer(socketserver.BaseRequestHandler):
    def close(self):
        self.stop.set()
        for sock in (self.request, self.upstream):
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(self):
        self.live = self.server.live
        self.identity = self.client_address[0]
        self.room = None
        self.stop = threading.Event()
        self.upstream = None
        admitted = False
        reader = None
        try:
            self.request.settimeout(15)
            stream = self.request.makefile('rb')
            # Do not allocate an IP quota until the first real room request.
            with stream:
                while not self.stop.is_set():
                    first = stream.read(1)
                    if not first:
                        break
                    if first == b'$':
                        if not self.upstream:
                            raise ValueError('interleaved frame before SETUP')
                        header = self.exact(stream, 3)
                        packet = first + header + self.exact(stream, int.from_bytes(header[1:], 'big'))
                    else:
                        line = first + stream.readline(8192)
                        if len(line) > 8192 or not line.endswith(b'\r\n'):
                            raise ValueError('invalid RTSP request line')
                        method, uri, protocol = line.decode('ascii').strip().split(' ')
                        if protocol != 'RTSP/1.0' or method not in ('OPTIONS', 'DESCRIBE', 'SETUP', 'PLAY', 'PAUSE', 'TEARDOWN', 'GET_PARAMETER', 'SET_PARAMETER'):
                            raise ValueError('unsupported RTSP method')
                        header = bytearray(line)
                        length = 0
                        cseq = '1'
                        seen_length = False
                        while True:
                            part = stream.readline(8192)
                            header.extend(part)
                            if len(header) > 32768 or not part.endswith(b'\r\n'):
                                raise ValueError('invalid RTSP headers')
                            if part == b'\r\n':
                                break
                            name, value = part.decode('ascii').split(':', 1)
                            if name.lower() == 'content-length':
                                if seen_length:
                                    raise ValueError('duplicate content length')
                                seen_length = True
                                length = int(value.strip())
                            if name.lower() == 'cseq':
                                cseq = value.strip()
                                if not cseq.isdecimal() or len(cseq) > 12:
                                    raise ValueError('invalid CSeq')
                        if not 0 <= length <= 65536:
                            raise ValueError('RTSP body too large')
                        body = self.exact(stream, length)
                        if uri == '*' and method == 'OPTIONS' and not self.upstream:
                            self.request.sendall(('RTSP/1.0 200 OK\r\nCSeq: ' + cseq + '\r\nPublic: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n\r\n').encode())
                            continue
                        if uri != '*':
                            parsed = urlsplit(uri)
                            match = re.fullmatch(r'/live/([A-Za-z0-9_-]{32})(?:/|/trackID=\d+)?', parsed.path)
                            if parsed.scheme not in ('rtsp', 'rtspt') or not match:
                                raise PermissionError('unknown live path')
                            self.live.bind(self, match[1])
                        if self.room is None:
                            raise PermissionError('room required')
                        if not admitted:
                            # Same lock as pause/delete avoids admitting a request after a kick.
                            with self.live.lock:
                                if not self.live.allowed(self.room):
                                    raise PermissionError('room paused')
                                if not self.live.bandwidth.admit(self.identity):
                                    self.request.sendall(('RTSP/1.0 453 Not Enough Bandwidth\r\nCSeq: ' + cseq + '\r\n\r\n').encode())
                                    return
                                self.live.bandwidth.begin(self.identity)
                                admitted = True
                                self.live.connections.add(self)
                            self.upstream = socket.create_connection((os.environ.get('LIVE_MTX_RTSP_HOST', 'mediamtx'), int(os.environ.get('LIVE_MTX_RTSP_PORT', '8554'))), timeout=5)
                            self.upstream.settimeout(60)
                            self.request.settimeout(90)
                            reader = threading.Thread(target=self.receive, daemon=True)
                            reader.start()
                        packet = bytes(header) + body
                    self.upstream.sendall(packet)
        except (OSError, ValueError, UnicodeError) as error:
            self.live.cache.debug.emit('DEBUG', 'live_viewer_closed', reason=type(error).__name__)
        finally:
            self.close()
            if reader:
                reader.join(2)
            if self.upstream:
                self.upstream.close()
            if admitted:
                with self.live.lock:
                    self.live.connections.discard(self)
                    self.live.bandwidth.end(self.identity)
                    self.live.bandwidth.release(self.identity)

    @staticmethod
    def exact(stream, length):
        result = stream.read(length)
        if len(result) != length:
            raise ConnectionError('short RTSP frame')
        return result

    def receive(self):
        try:
            while not self.stop.is_set():
                data = self.upstream.recv(32768)
                if not data:
                    break
                self.live.bandwidth.acquire(self.identity, len(data), self.stop)
                self.request.sendall(data)
                with self.live.lock:
                    self.live.sent += len(data)
        except OSError:
            pass
        finally:
            self.close()
