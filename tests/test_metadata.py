import io
import socket
import threading

import pytest
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from PIL import Image

from patchmanager.accounts.services import ServiceError
from patchmanager.metadata import services, uploads
from patchmanager.metadata.models import Attachment, Note
from patchmanager.tenancy import rls
from tests.conftest import login


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 10), exif: bool = False) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", size, "teal")
    kwargs = {}
    if exif:
        data = Image.Exif()
        data[0x010F] = "SecretCam"  # Make
        kwargs["exif"] = data.tobytes()
    image.save(out, fmt, **kwargs)
    return out.getvalue()


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("GIF", "image/png"), ("WEBP", "image/webp")],
)
def test_images_are_reencoded(fmt, mime) -> None:
    result = uploads.clean(image_bytes(fmt))
    assert result.mime_type == mime and result.kind == "image"
    Image.open(io.BytesIO(result.data)).verify()


def test_exif_is_stripped() -> None:
    raw = image_bytes(exif=True)
    assert b"SecretCam" in raw
    assert b"SecretCam" not in uploads.clean(raw).data


def test_polyglot_payload_is_dropped() -> None:
    raw = image_bytes("PNG") + b"<script>alert(1)</script>"
    assert b"<script>" not in uploads.clean(raw).data


def test_pdf_kept_as_document() -> None:
    result = uploads.clean(b"%PDF-1.7\n%...\n")
    assert (result.mime_type, result.kind) == ("application/pdf", "document")


def test_svg_is_sanitised() -> None:
    evil = b"".join(
        [
            b'<?xml version="1.0"?>',
            b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)" viewBox="0 0 10 10">',
            b"<script>alert(2)</script>",
            b'<rect width="10" height="10" fill="url(https://evil.example/x)" onclick="x()"/>',
            b'<a href="javascript:alert(3)"><circle r="2" fill="red"/></a>',
            b"<foreignObject><p>x</p></foreignObject>",
            b'<text x="1" y="5">Label</text>',
            b'<path d="M0 0L10 10" style="background:url(x)" stroke="black"/></svg>',
        ]
    )
    clean = uploads.clean(evil).data.decode()
    for bad in (
        "onload",
        "script",
        "evil.example",
        "onclick",
        "javascript",
        "foreignObject",
        "style",
    ):
        assert bad not in clean
    assert ("Label" in clean and "<ns0:rect" in clean) or "rect" in clean


@pytest.mark.parametrize(
    "payload",
    [
        b"<!DOCTYPE svg [<!ENTITY x 'y'>]><svg xmlns='http://www.w3.org/2000/svg'>&x;</svg>",
        b"<html><body><svg></svg></body></html>",
    ],
)
def test_svg_attacks_rejected(payload) -> None:
    with pytest.raises(ServiceError):
        uploads.clean(payload)


@pytest.mark.parametrize(
    "payload", [b"MZ\x90\x00 executable", b"#!/bin/sh\nrm -rf /", b"\xff\xd8\xff broken jpeg"]
)
def test_unknown_or_broken_files_rejected(payload) -> None:
    with pytest.raises(ServiceError):
        uploads.clean(payload)


@override_settings(UPLOAD_MAX_BYTES=1024 * 1024)
def test_size_limit() -> None:
    with pytest.raises(ServiceError, match="too large"):
        uploads.clean(b"\x89PNG\r\n\x1a\n" + bytes(2 * 1024 * 1024))


def test_decompression_bomb_rejected(monkeypatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 50)
    with pytest.raises(ServiceError):
        uploads.clean(image_bytes(size=(100, 100)))


def _fake_clamd(reply: bytes) -> tuple[str, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        conn, _addr = server.accept()
        with conn:
            buffer = b""
            while not buffer.endswith(b"\x00\x00\x00\x00"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
            conn.sendall(reply)
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    host, port = server.getsockname()
    return str(host), int(port)


@pytest.mark.parametrize(
    ("reply", "ok"), [(b"stream: OK\0", True), (b"stream: Eicar-Signature FOUND\0", False)]
)
def test_clamd_scan(settings, reply, ok) -> None:
    settings.CLAMD_HOST, settings.CLAMD_PORT = _fake_clamd(reply)
    if ok:
        uploads.scan(b"data")
    else:
        with pytest.raises(ServiceError, match="virus"):
            uploads.scan(b"data")


def test_clamd_unreachable_fails_closed(settings) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    settings.CLAMD_HOST, settings.CLAMD_PORT = probe.getsockname()
    probe.close()
    with pytest.raises(ServiceError, match="not reachable"):
        uploads.scan(b"data")


def test_markdown_is_sanitised() -> None:
    html = services.render_markdown(
        "**bold** <script>x</script> [l](javascript:alert(1)) [ok](https://ex.test)"
    )
    assert "<strong>bold</strong>" in html
    assert "<script>" not in html
    assert 'href="javascript' not in html  # left as plain text, never a link
    assert 'href="https://ex.test"' in html and "noopener" in html


@pytest.mark.django_db
def test_attach_download_and_remove(
    admin_client: Client, make_tree, system, django_capture_on_commit_callbacks
) -> None:
    t = make_tree()
    upload = SimpleUploadedFile("Rack front.JPG", image_bytes(exif=True), content_type="text/html")
    admin_client.post(
        f"/meta/dcim.rack/{t['rack'].pk}/upload/",
        {"file": upload, "caption": "Front", "next": f"/racks/{t['rack'].pk}/"},
        secure=True,
    )
    with system():
        attachment = Attachment.objects.get()
    assert (attachment.original_name, attachment.mime_type, attachment.tenant_id) == (
        "Rack front.jpg",
        "image/jpeg",
        t["tenant"].pk,
    )
    stored = storages["attachments"].open(attachment.storage_name).read()
    assert b"\xff\xd8\xff" not in stored[:16]  # encrypted at rest
    response = admin_client.get(f"/meta/files/{attachment.pk}/", secure=True)
    assert response["Content-Type"] == "image/jpeg"
    assert "sandbox" in response["Content-Security-Policy"]
    assert response["Content-Disposition"].startswith("inline")
    assert response.content.startswith(b"\xff\xd8\xff")
    assert "Front" in admin_client.get(f"/racks/{t['rack'].pk}/", secure=True).content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/meta/files/{attachment.pk}/delete/", {"next": "/"}, secure=True)
    with system():
        assert not Attachment.objects.exists()
    assert not storages["attachments"].exists(attachment.storage_name)


@pytest.mark.django_db
def test_upload_rejections_and_notes(admin_client: Client, make_tree, system) -> None:
    t = make_tree()
    base = f"/meta/locations.room/{t['room'].pk}"
    response = admin_client.post(
        f"{base}/upload/",
        {"file": SimpleUploadedFile("x.exe", b"MZ..."), "next": "/"},
        secure=True,
        follow=True,
    )
    assert "Only JPEG" in response.content.decode()
    admin_client.post(f"{base}/upload/", {"next": "/"}, secure=True)
    assert (
        admin_client.post(
            f"/meta/accounts.user/{t['room'].pk}/upload/",
            {"file": SimpleUploadedFile("a.png", image_bytes("PNG")), "next": "/"},
            secure=True,
        ).status_code
        == 302
    )
    admin_client.post(f"{base}/note/", {"body": "Key in **box 3**", "next": "/"}, secure=True)
    admin_client.post(f"{base}/note/", {"body": "", "next": "/"}, secure=True)
    page = admin_client.get(f"/locations/room/{t['room'].pk}/", secure=True).content.decode()
    assert "<strong>box 3</strong>" in page
    with system():
        note = Note.objects.get()
    admin_client.post(
        f"/meta/notes/{note.pk}/delete/", {"next": "https://evil.example/"}, secure=True
    )
    with system():
        assert not Note.objects.exists()


@pytest.mark.django_db
def test_attachment_isolation(client: Client, make_tree, make_user, grant, super_admin) -> None:
    a, b = make_tree(), make_tree(name="Other")
    with rls.system_context():
        theirs = services.attach(
            b["request"], super_admin, b["rack"], name="b.png", data=image_bytes("PNG")
        )
        mine = services.attach(
            a["request"], super_admin, a["rack"], name="a.png", data=image_bytes("PNG")
        )
    viewer = make_user()
    grant(viewer, "viewer", a["tenant"])
    login(client, viewer)
    assert client.get(f"/meta/files/{mine.pk}/", secure=True).status_code == 200
    assert client.get(f"/meta/files/{theirs.pk}/", secure=True).status_code == 404
    response = client.post(
        f"/meta/dcim.rack/{a['rack'].pk}/upload/",
        {"file": SimpleUploadedFile("c.png", image_bytes("PNG")), "next": "/"},
        secure=True,
    )
    assert response.status_code == 403
    assert (
        client.post(f"/meta/files/{mine.pk}/delete/", {"next": "/"}, secure=True).status_code == 403
    )
