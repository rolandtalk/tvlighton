#!/usr/bin/env python3
"""Serve rpicam-vid MJPEG on the existing CCA HTTP endpoints, without recording."""

import argparse
import json
import os
import select
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class JpegParser:
    """Split rpicam's concatenated JPEGs, including markers split across reads."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while True:
            start = self.buffer.find(b"\xff\xd8")
            if start < 0:
                self.buffer[:] = self.buffer[-1:]
                return
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > 8 * 1024 * 1024:
                    raise RuntimeError("Camera frame exceeded 8 MiB")
                return
            frame = bytes(self.buffer[:end + 2])
            del self.buffer[:end + 2]
            yield frame


class MetadataParser:
    """Parse the JSON object stream emitted by rpicam's metadata output."""

    def __init__(self):
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def feed(self, data):
        self.buffer += data.decode("utf-8", errors="replace")
        while True:
            self.buffer = self.buffer.lstrip(" \t\r\n[,")
            if not self.buffer or self.buffer.startswith("]"):
                return
            try:
                metadata, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                if len(self.buffer) > 1024 * 1024:
                    raise RuntimeError("Camera metadata buffer exceeded 1 MiB")
                return
            self.buffer = self.buffer[end:]
            if isinstance(metadata, dict):
                yield metadata


class Frames:
    def __init__(self, width, height, fps):
        self.condition = threading.Condition()
        self.frame = b""
        self.sequence = 0
        self.updated = 0.0
        self.metadata = {}
        self.metadata_updated = 0.0
        self.width, self.height, self.fps = width, height, fps

    def publish(self, frame):
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.updated = time.monotonic()
            self.condition.notify_all()

    def publish_metadata(self, metadata):
        with self.condition:
            self.metadata = metadata
            self.metadata_updated = time.monotonic()

    def fresh(self):
        return bool(self.frame) and time.monotonic() - self.updated < 5

    def metadata_fresh(self):
        return bool(self.metadata) and time.monotonic() - self.metadata_updated < 5


class CameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, frames):
        self.frames = frames
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.connection.settimeout(10)
        frames = self.server.frames
        path = urlsplit(self.path).path
        with frames.condition:
            online = frames.fresh()
            frame = frames.frame
            sequence = frames.sequence
            metadata = dict(frames.metadata)
            metadata_online = frames.metadata_fresh()
        try:
            if path == "/state":
                payload = {"ok": online, "result": {"source": {
                    "online": online, "resolution": {"width": frames.width, "height": frames.height},
                    "desired_fps": frames.fps}, "frames": sequence, "light": {
                        "online": metadata_online,
                        "lux": metadata.get("Lux"),
                        "exposure_time_us": metadata.get("ExposureTime"),
                        "analogue_gain": metadata.get("AnalogueGain"),
                        "digital_gain": metadata.get("DigitalGain"),
                        "colour_temperature_k": metadata.get("ColourTemperature"),
                        "ae_state": metadata.get("AeState"),
                    }}, "camera": "IMX290 CSI"}
                self.reply(200, "application/json", json.dumps(payload).encode())
            elif path in {"/snapshot", "/stream"}:
                if not online:
                    self.reply(503, "text/plain", b"Camera has no fresh frames\n")
                    return
                if path == "/snapshot":
                    self.reply(200, "image/jpeg", frame)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                last_sequence = -1
                while True:
                    with frames.condition:
                        frames.condition.wait_for(lambda: frames.sequence != last_sequence, timeout=5)
                        if not frames.fresh() or frames.sequence == last_sequence:
                            return
                        frame, last_sequence = frames.frame, frames.sequence
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    self.wfile.flush()
            else:
                self.reply(404, "text/plain", b"Use /state, /snapshot or /stream\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=int, default=75)
    args = parser.parse_args()
    frames = Frames(args.width, args.height, args.fps)
    server = CameraHTTPServer((args.host, args.port), frames)
    metadata_read_fd, metadata_write_fd = os.pipe()
    command = ["/usr/bin/rpicam-vid", "--nopreview", "--timeout", "0",
               "--width", str(args.width), "--height", str(args.height),
               "--framerate", str(args.fps), "--codec", "mjpeg", "--quality", str(args.quality),
               "--flicker-period", "8333us", "--buffer-count", "4", "--no-raw", "--flush",
               "--metadata", f"/dev/fd/{metadata_write_fd}", "--metadata-format", "json",
               "--output", "-"]

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    camera = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        bufsize=0,
        pass_fds=(metadata_write_fd,),
    )
    os.close(metadata_write_fd)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    metadata_parser = MetadataParser()

    def read_metadata():
        while True:
            data = os.read(metadata_read_fd, 65536)
            if not data:
                return
            for metadata in metadata_parser.feed(data):
                frames.publish_metadata(metadata)

    metadata_thread = threading.Thread(target=read_metadata, daemon=True)
    metadata_thread.start()
    jpeg_parser = JpegParser()
    last_frame = time.monotonic()
    try:
        while True:
            if time.monotonic() - last_frame > 20:
                raise RuntimeError("No camera frames for 20 seconds; restarting capture")
            ready, _, _ = select.select([camera.stdout], [], [], 1)
            if not ready:
                continue
            data = os.read(camera.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError("rpicam-vid stopped producing data")
            for frame in jpeg_parser.feed(data):
                frames.publish(frame)
                last_frame = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        if camera.poll() is None:
            camera.terminate()
        try:
            camera.wait(timeout=5)
        except subprocess.TimeoutExpired:
            camera.kill()
            camera.wait()
        camera.stdout.close()
        metadata_thread.join(timeout=2)
        os.close(metadata_read_fd)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
