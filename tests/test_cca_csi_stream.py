import importlib.util
import json
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


spec = importlib.util.spec_from_file_location(
    "cca_csi_stream", Path(__file__).resolve().parents[1] / "deploy/cca/cca-csi-stream.py"
)
stream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stream)


class StreamTest(unittest.TestCase):
    def test_split_markers_and_multiple_frames(self):
        frames = [b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9"]
        data = b"noise" + b"".join(frames)
        for chunk_size in range(1, len(data) + 1):
            parser = stream.JpegParser()
            actual = []
            for offset in range(0, len(data), chunk_size):
                actual.extend(parser.feed(data[offset:offset + chunk_size]))
            self.assertEqual(actual, frames)

    def test_incomplete_frame_memory_is_bounded(self):
        parser = stream.JpegParser()
        with self.assertRaises(RuntimeError):
            list(parser.feed(b"\xff\xd8" + b"x" * (8 * 1024 * 1024)))

    def test_metadata_parser_handles_split_json_stream(self):
        parser = stream.MetadataParser()
        chunks = [b'[\n{"Lux": 12.', b'5, "ExposureTime": 30000},\n', b'{"Lux": 42.0}\n]']
        actual = []
        for chunk in chunks:
            actual.extend(parser.feed(chunk))
        self.assertEqual(actual, [
            {"Lux": 12.5, "ExposureTime": 30000},
            {"Lux": 42.0},
        ])

    def test_http_fresh_frames_stream_and_stale_camera(self):
        frames = stream.Frames(1280, 720, 15)
        server = stream.CameraHTTPServer(("127.0.0.1", 0), frames)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/snapshot", timeout=2)
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
            with urlopen(base + "/state", timeout=2) as response:
                self.assertFalse(json.load(response)["ok"])

            first = b"\xff\xd8first\xff\xd9"
            frames.publish(first)
            frames.publish_metadata({
                "Lux": 93.8,
                "ExposureTime": 30000,
                "AnalogueGain": 1.6,
                "DigitalGain": 1.0,
            })
            with urlopen(base + "/state", timeout=2) as response:
                state = json.load(response)
                self.assertTrue(state["result"]["light"]["online"])
                self.assertEqual(state["result"]["light"]["lux"], 93.8)
            with urlopen(base + "/snapshot", timeout=2) as response:
                self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                self.assertEqual(response.read(), first)
            with urlopen(base + "/stream", timeout=2) as response:
                self.assertEqual(response.readline(), b"--frame\r\n")
                self.assertEqual(response.readline(), b"Content-Type: image/jpeg\r\n")
                self.assertEqual(response.readline(), f"Content-Length: {len(first)}\r\n".encode())
                self.assertEqual(response.readline(), b"\r\n")
                self.assertEqual(response.read(len(first) + 2), first + b"\r\n")
                second = b"\xff\xd8second\xff\xd9"
                frames.publish(second)
                self.assertEqual(response.readline(), b"--frame\r\n")
                response.readline()
                response.readline()
                response.readline()
                self.assertEqual(response.read(len(second) + 2), second + b"\r\n")

            with frames.condition:
                frames.updated = time.monotonic() - 6
            with urlopen(base + "/state", timeout=2) as response:
                self.assertFalse(json.load(response)["ok"])
            for path in ("/snapshot", "/stream"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path, timeout=2)
                self.assertEqual(error.exception.code, 503)
                error.exception.close()
        finally:
            server.shutdown()
            worker.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
