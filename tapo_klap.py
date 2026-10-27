"""Local KLAP v2 protocol client and UDP discovery for TP-Link Tapo P110M smart plugs."""

import concurrent.futures
import hashlib
import http.client
import json
import os
import socket
import struct
import urllib.parse
import zlib
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Pure-Python AES-128-CBC fallback (used only if `cryptography` is not installed)
# ---------------------------------------------------------------------------
_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)

_INV_SBOX = tuple(_SBOX.index(i) for i in range(256))
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _expand_key_128(key: bytes) -> list[list[int]]:
    w = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = [_SBOX[temp[(j + 1) % 4]] for j in range(4)]
            temp[0] ^= _RCON[i // 4]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [[w[r * 4 + c][row] for c in range(4) for row in range(4)] for r in range(11)]


def _aes_encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    s = [block[i] ^ round_keys[0][i] for i in range(16)]
    for r in range(1, 10):
        # SubBytes & ShiftRows
        s = [
            _SBOX[s[0]], _SBOX[s[5]], _SBOX[s[10]], _SBOX[s[15]],
            _SBOX[s[4]], _SBOX[s[9]], _SBOX[s[14]], _SBOX[s[3]],
            _SBOX[s[8]], _SBOX[s[13]], _SBOX[s[2]], _SBOX[s[7]],
            _SBOX[s[12]], _SBOX[s[1]], _SBOX[s[6]], _SBOX[s[11]],
        ]
        # MixColumns & AddRoundKey
        ns = [0] * 16
        rk = round_keys[r]
        for c in range(4):
            i = c * 4
            a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
            t = a0 ^ a1 ^ a2 ^ a3
            ns[i] = a0 ^ t ^ _xtime(a0 ^ a1) ^ rk[i]
            ns[i + 1] = a1 ^ t ^ _xtime(a1 ^ a2) ^ rk[i + 1]
            ns[i + 2] = a2 ^ t ^ _xtime(a2 ^ a3) ^ rk[i + 2]
            ns[i + 3] = a3 ^ t ^ _xtime(a3 ^ a0) ^ rk[i + 3]
        s = ns
    # Final round
    s = [
        _SBOX[s[0]], _SBOX[s[5]], _SBOX[s[10]], _SBOX[s[15]],
        _SBOX[s[4]], _SBOX[s[9]], _SBOX[s[14]], _SBOX[s[3]],
        _SBOX[s[8]], _SBOX[s[13]], _SBOX[s[2]], _SBOX[s[7]],
        _SBOX[s[12]], _SBOX[s[1]], _SBOX[s[6]], _SBOX[s[11]],
    ]
    rk = round_keys[10]
    return bytes(s[i] ^ rk[i] for i in range(16))


def _aes_decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    s = [block[i] ^ round_keys[10][i] for i in range(16)]
    for r in range(9, 0, -1):
        # InvShiftRows & InvSubBytes
        s = [
            _INV_SBOX[s[0]], _INV_SBOX[s[13]], _INV_SBOX[s[10]], _INV_SBOX[s[7]],
            _INV_SBOX[s[4]], _INV_SBOX[s[1]], _INV_SBOX[s[14]], _INV_SBOX[s[11]],
            _INV_SBOX[s[8]], _INV_SBOX[s[5]], _INV_SBOX[s[2]], _INV_SBOX[s[15]],
            _INV_SBOX[s[12]], _INV_SBOX[s[9]], _INV_SBOX[s[6]], _INV_SBOX[s[3]],
        ]
        rk = round_keys[r]
        s = [s[i] ^ rk[i] for i in range(16)]
        # InvMixColumns
        ns = [0] * 16
        for c in range(4):
            i = c * 4
            a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
            ns[i] = _gmul(a0, 0x0E) ^ _gmul(a1, 0x0B) ^ _gmul(a2, 0x0D) ^ _gmul(a3, 0x09)
            ns[i + 1] = _gmul(a0, 0x09) ^ _gmul(a1, 0x0E) ^ _gmul(a2, 0x0B) ^ _gmul(a3, 0x0D)
            ns[i + 2] = _gmul(a0, 0x0D) ^ _gmul(a1, 0x09) ^ _gmul(a2, 0x0E) ^ _gmul(a3, 0x0B)
            ns[i + 3] = _gmul(a0, 0x0B) ^ _gmul(a1, 0x0D) ^ _gmul(a2, 0x09) ^ _gmul(a3, 0x0E)
        s = ns
    s = [
        _INV_SBOX[s[0]], _INV_SBOX[s[13]], _INV_SBOX[s[10]], _INV_SBOX[s[7]],
        _INV_SBOX[s[4]], _INV_SBOX[s[1]], _INV_SBOX[s[14]], _INV_SBOX[s[11]],
        _INV_SBOX[s[8]], _INV_SBOX[s[5]], _INV_SBOX[s[2]], _INV_SBOX[s[15]],
        _INV_SBOX[s[12]], _INV_SBOX[s[9]], _INV_SBOX[s[6]], _INV_SBOX[s[3]],
    ]
    rk = round_keys[0]
    return bytes(s[i] ^ rk[i] for i in range(16))


def aes128_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    rk = _expand_key_128(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(padded), 16):
        blk = bytes(padded[i + j] ^ prev[j] for j in range(16))
        enc = _aes_encrypt_block(blk, rk)
        out.extend(enc)
        prev = enc
    return bytes(out)


def aes128_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    rk = _expand_key_128(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(ciphertext), 16):
        blk = ciphertext[i : i + 16]
        dec = _aes_decrypt_block(blk, rk)
        out.extend(dec[j] ^ prev[j] for j in range(16))
        prev = blk
    pad_len = out[-1]
    if 1 <= pad_len <= 16:
        return bytes(out[:-pad_len])
    return bytes(out)


# ---------------------------------------------------------------------------
# Discovery over UDP Port 20002
# ---------------------------------------------------------------------------
def _build_discovery_packet() -> bytes:
    header = bytearray(struct.pack(">BBHII4s", 2, 0, 1, 0, 0x00000000, b"\x5a\x6b\x7c\x8d"))
    crc = zlib.crc32(header) & 0xFFFFFFFF
    header[12:16] = struct.pack(">I", crc)
    return bytes(header)


def discover_tapo_devices(subnet_prefix: str = "192.168.31", timeout: float = 0.5) -> Dict[str, Dict[str, Any]]:
    """Discovers Tapo devices on the local network via UDP port 20002."""
    found: Dict[str, Dict[str, Any]] = {}
    pkt = _build_discovery_packet()

    for bcast in ["255.255.255.255", f"{subnet_prefix}.255"]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(timeout)
                s.sendto(pkt, (bcast, 20002))
                while True:
                    data, addr = s.recvfrom(4096)
                    if len(data) > 16:
                        info = json.loads(data[16:].decode(errors="ignore")).get("result", {})
                        found[addr[0]] = info
        except Exception:
            pass

    def _probe(ip: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(pkt, (ip, 20002))
                data, _ = s.recvfrom(4096)
                if len(data) > 16:
                    return ip, json.loads(data[16:].decode(errors="ignore")).get("result", {})
        except Exception:
            pass
        return ip, None

    ips = [f"{subnet_prefix}.{i}" for i in range(1, 255) if f"{subnet_prefix}.{i}" not in found]
    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
        for ip, res in ex.map(_probe, ips):
            if res:
                found[ip] = res

    return found


# ---------------------------------------------------------------------------
# Tapo KLAP v2 Client
# ---------------------------------------------------------------------------
class TapoAuthError(Exception):
    """Raised when KLAP handshake authentication fails."""


class TapoKlapClient:
    """Communicates with a TP-Link Tapo device using the local KLAP v2 (or v1) protocol."""

    def __init__(self, host: str, username: str, password: str, port: int = 80, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

        self._cookie: Optional[str] = None
        self._key: Optional[bytes] = None
        self._iv_prefix: Optional[bytes] = None
        self._sig_key: Optional[bytes] = None
        self._seq: int = 0

    @staticmethod
    def _sha1(data: bytes) -> bytes:
        return hashlib.sha1(data).digest()

    @staticmethod
    def _sha256(data: bytes) -> bytes:
        return hashlib.sha256(data).digest()

    def _auth_hash_v2(self) -> bytes:
        return self._sha256(
            self._sha1(self.username.encode("utf-8")) + self._sha1(self.password.encode("utf-8"))
        )

    def _auth_hash_v1(self) -> bytes:
        return hashlib.md5(
            hashlib.md5(self.username.encode("utf-8")).digest()
            + hashlib.md5(self.password.encode("utf-8")).digest()
        ).digest()

    def _post_raw(self, path: str, body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"Content-Type": "application/octet-stream"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            resp_body = resp.read()
            return resp.status, resp_headers, resp_body
        finally:
            conn.close()

    def handshake(self) -> None:
        """Performs KLAP handshake1 & handshake2 with the device."""
        local_seed = os.urandom(16)
        self._cookie = None
        status, headers, body = self._post_raw("/app/handshake1", local_seed)
        if status != 200 or len(body) < 48:
            raise TapoAuthError(f"handshake1 failed on {self.host}: HTTP {status} ({len(body)} bytes)")

        set_cookie = headers.get("set-cookie", "")
        for part in set_cookie.split(";"):
            if "TP_SESSIONID=" in part:
                self._cookie = part.strip()
                break

        remote_seed = body[:16]
        server_hash = body[16:48]

        # Match KLAP v2 auth_hash (or v1 fallback)
        matched_auth_hash: Optional[bytes] = None
        for candidate in (
            self._auth_hash_v2(),
            # Also try lowercase email in case Tapo account normalized it
            self._sha256(
                self._sha1(self.username.strip().lower().encode("utf-8"))
                + self._sha1(self.password.encode("utf-8"))
            ),
            self._auth_hash_v1(),
            # Blank credential fallback for unconfigured factory state
            self._sha256(self._sha1(b"") + self._sha1(b"")),
        ):
            calc = self._sha256(local_seed + remote_seed + candidate)
            if calc == server_hash:
                matched_auth_hash = candidate
                break

        if matched_auth_hash is None:
            raise TapoAuthError(
                f"KLAP credential mismatch on {self.host}. Verify your TP-Link Tapo email/password "
                "and ensure 'Third-Party Compatibility' is enabled in the Tapo app."
            )

        client_hash = self._sha256(remote_seed + local_seed + matched_auth_hash)
        status2, _, _ = self._post_raw("/app/handshake2", client_hash)
        if status2 != 200:
            raise TapoAuthError(f"handshake2 failed on {self.host}: HTTP {status2}")

        # Derive session keys
        self._key = self._sha256(b"lsk" + local_seed + remote_seed + matched_auth_hash)[:16]
        iv_full = self._sha256(b"iv" + local_seed + remote_seed + matched_auth_hash)
        self._iv_prefix = iv_full[:12]
        self._seq = struct.unpack(">i", iv_full[-4:])[0]
        self._sig_key = self._sha256(b"ldk" + local_seed + remote_seed + matched_auth_hash)[:28]

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Sends an encrypted KLAP request and returns the decoded JSON result."""
        if self._key is None:
            self.handshake()

        for attempt in range(2):
            try:
                assert self._key and self._iv_prefix and self._sig_key
                self._seq += 1
                seq_bytes = struct.pack(">i", self._seq)
                iv = self._iv_prefix + seq_bytes

                payload_dict: Dict[str, Any] = {"method": method}
                if params is not None:
                    payload_dict["params"] = params
                plaintext = json.dumps(payload_dict).encode("utf-8")

                ciphertext = aes128_cbc_encrypt(self._key, iv, plaintext)
                signature = self._sha256(self._sig_key + seq_bytes + ciphertext)

                query = urllib.parse.urlencode({"seq": self._seq})
                status, _, resp_body = self._post_raw(f"/app/request?{query}", signature + ciphertext)
                if status != 200 or len(resp_body) <= 32:
                    raise RuntimeError(f"HTTP {status} from /app/request")

                resp_cipher = resp_body[32:]
                decrypted = aes128_cbc_decrypt(self._key, iv, resp_cipher)
                data = json.loads(decrypted.decode("utf-8"))
                err = data.get("error_code", 0)
                if err != 0:
                    raise RuntimeError(f"Tapo error_code={err} for method={method}: {data}")
                return data.get("result", {})
            except TapoAuthError:
                raise
            except Exception:
                if attempt == 0:
                    self.handshake()
                else:
                    raise
        return {}

    def get_device_info(self) -> Dict[str, Any]:
        return self.request("get_device_info")

    def get_energy_usage(self) -> Dict[str, Any]:
        return self.request("get_energy_usage")

    def get_current_power_watts(self) -> float:
        """Returns real-time power draw in Watts (W) from the P110M energy sensor."""
        energy = self.get_energy_usage()
        if "current_power" in energy:
            # `get_energy_usage.current_power` is reported in milliwatts (mW)
            return float(energy["current_power"]) / 1000.0
        power_res = self.request("get_current_power")
        return float(power_res.get("current_power", 0.0))

    def set_device_on(self, on: bool) -> None:
        self.request("set_device_info", {"device_on": bool(on)})
