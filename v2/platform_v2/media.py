"""Bounded HTTPS image fetching with DNS validation and pinned TLS connections."""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit


class ImageError(ValueError):
    pass


def destination(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ImageError("invalid_image_url")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise ImageError("public_https_required")
        host = parsed.hostname.encode("idna").decode("ascii")
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise ImageError("image_host_unavailable")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if (not address.is_global or address.is_multicast or address.is_reserved
                    or getattr(address, "is_site_local", False)
                    or getattr(address, "ipv4_mapped", None) or getattr(address, "sixtofour", None) or getattr(address, "teredo", None)):
                raise ImageError("private_image_destination")
        return host, addresses[0], (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    except (UnicodeError, OSError, ValueError) as error:
        if isinstance(error, ImageError):
            raise
        raise ImageError("invalid_image_destination") from None


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated IP, while verifying the original hostname's
        # certificate and sending that hostname through TLS SNI and HTTP Host.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")): return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
    raise ImageError("unsupported_image_content")


def fetch_image(url, max_bytes=5 * 1024 * 1024, seconds=10):
    deadline = time.monotonic() + seconds
    try:
        for hop in range(4):
            host, address, path = destination(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise ImageError("image_timeout")
            connection = PinnedHTTPS(host, address, remaining)
            try:
                connection.request("GET", path, headers={"Accept": "image/png,image/jpeg,image/gif,image/webp", "Accept-Encoding": "identity", "User-Agent": "Itura-Image/2"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or hop == 3: raise ImageError("invalid_image_redirect")
                    url = urljoin(url, location)
                    continue
                if response.status != 200: raise ImageError("image_unavailable")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise ImageError("encoded_image_rejected")
                declared = response.getheader("Content-Length")
                if declared and (not declared.isdecimal() or int(declared) > max_bytes):
                    raise ImageError("image_too_large")
                content = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise ImageError("image_timeout")
                    if connection.sock: connection.sock.settimeout(remaining)
                    chunk = response.read(min(65536, max_bytes + 1 - len(content)))
                    if not chunk: break
                    content.extend(chunk)
                    if len(content) > max_bytes: raise ImageError("image_too_large")
                mime = image_type(content)
                if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != mime:
                    raise ImageError("image_type_mismatch")
                return bytes(content), mime
            finally:
                connection.close()
    except (OSError, http.client.HTTPException):
        raise ImageError("image_fetch_failed") from None
    raise ImageError("invalid_image_redirect")
