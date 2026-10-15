from unittest.mock import MagicMock, patch

import pytest

from platform_v2.media import ImageError, PinnedHTTPS, destination, fetch_image


def records(*addresses):
    return [(2, 1, 6, "", (address, 443)) for address in addresses]


@pytest.mark.parametrize("url", ["http://images.example/a.png", "https://user:pass@images.example/a.png", "https://images.example:8443/a.png", "https://images.example/\r\nHost:x", "file:///tmp/image"])
def test_invalid_destinations_never_resolve(url):
    with patch("platform_v2.media.socket.getaddrinfo") as resolve:
        with pytest.raises(ImageError): destination(url)
        resolve.assert_not_called()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "fc00::1", "::ffff:8.8.8.8", "0.0.0.0"])
def test_private_or_transitional_destinations_rejected(address):
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records(address)):
        with pytest.raises(ImageError): destination("https://any.example/photo")


def test_all_dns_answers_checked_and_arbitrary_public_host_allowed():
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records("8.8.8.8")):
        assert destination("https://any.example/photo?q=1") == ("any.example", "8.8.8.8", "/photo?q=1")
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records("8.8.8.8", "10.0.0.1")):
        with pytest.raises(ImageError): destination("https://any.example/photo")


def test_connection_pins_address_and_verifies_original_hostname():
    context = MagicMock()
    with patch("platform_v2.media.ssl.create_default_context", return_value=context), patch("platform_v2.media.socket.create_connection") as connect:
        connection = PinnedHTTPS("any.example", "8.8.8.8", 3)
        connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 3)
        context.wrap_socket.assert_called_once_with(connect.return_value, server_hostname="any.example")


def response(status=200, data=b"\x89PNG\r\n\x1a\nsynthetic", **headers):
    result = MagicMock(status=status)
    values = {"Content-Type": "image/png", **headers}
    result.getheader.side_effect = lambda name, default=None: values.get(name, default)
    result.read.side_effect = [data, b""]
    return result


def test_public_image_fetches_without_credentials_and_closes_connection():
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records("8.8.8.8")), patch("platform_v2.media.PinnedHTTPS") as factory:
        factory.return_value.getresponse.return_value = response()
        data, mime = fetch_image("https://any.example/photo")
        assert mime == "image/png" and data.startswith(b"\x89PNG")
        assert "Authorization" not in factory.return_value.request.call_args.kwargs["headers"]
        factory.return_value.close.assert_called_once()


def test_redirect_to_private_host_blocked_before_second_connection():
    with patch("platform_v2.media.socket.getaddrinfo", side_effect=[records("8.8.8.8"), records("127.0.0.1")]), patch("platform_v2.media.PinnedHTTPS") as factory:
        factory.return_value.getresponse.return_value = response(302, Location="https://internal.example/secret")
        with pytest.raises(ImageError, match="private_image_destination"): fetch_image("https://public.example/photo")
        assert factory.call_count == 1


def test_relative_redirect_is_revalidated_and_fetched():
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records("8.8.8.8")) as resolve, patch("platform_v2.media.PinnedHTTPS") as factory:
        factory.return_value.getresponse.side_effect = [response(302, Location="/new.png"), response()]
        assert fetch_image("https://public.example/old.png")[1] == "image/png"
        assert resolve.call_count == 2
        assert factory.return_value.request.call_args.args[1] == "/new.png"


def test_redirect_loop_has_bounded_connections():
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records("8.8.8.8")), patch("platform_v2.media.PinnedHTTPS") as factory:
        factory.return_value.getresponse.return_value = response(302, Location="/again.png")
        with pytest.raises(ImageError, match="invalid_image_redirect"): fetch_image("https://public.example/photo")
        assert factory.call_count == 4


def test_tls_failure_closes_socket_without_downgrading():
    import ssl
    context = MagicMock()
    context.wrap_socket.side_effect = ssl.SSLError("synthetic certificate failure")
    with patch("platform_v2.media.ssl.create_default_context", return_value=context), patch("platform_v2.media.socket.create_connection") as connect:
        with pytest.raises(ssl.SSLError): PinnedHTTPS("any.example", "8.8.8.8", 3).connect()
        connect.return_value.close.assert_called_once()


@pytest.mark.parametrize("reply", [response(data=b"<script>bad</script>"), response(**{"Content-Type": "text/html"}), response(**{"Content-Length": "99999999"}), response(**{"Content-Encoding": "gzip"}), response(data=b"\x89PNG\r\n\x1a\n" + b"x" * 50)])
def test_unsafe_or_oversized_content_rejected(reply):
    with patch("platform_v2.media.socket.getaddrinfo", return_value=records("8.8.8.8")), patch("platform_v2.media.PinnedHTTPS") as factory:
        factory.return_value.getresponse.return_value = reply
        with pytest.raises(ImageError): fetch_image("https://any.example/photo", max_bytes=40)
        factory.return_value.close.assert_called_once()
