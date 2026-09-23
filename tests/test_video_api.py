from __future__ import annotations

import json

import httpx
from conftest import ADMIN_HEADERS

from app.http_client import set_http_transport
from app.providers.video_protocol import grok_size_from_openai, shape_grok_video_body

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 48


def grok_cli_payload(
    user_id: str = "user-grok-1",
    email: str = "grok@example.com",
    token: str = "eyJhbGciOiJub25lIn0.e30.",
) -> dict:
    return {
        "https://auth.x.ai::test-client": {
            "auth_mode": "oidc",
            "key": token,
            "refresh_token": "refresh-test",
            "expires_at": "2099-01-01T00:00:00.000000Z",
            "user_id": user_id,
            "email": email,
            "principal_id": user_id,
            "oidc_client_id": "test-client",
            "oidc_issuer": "https://auth.x.ai",
        }
    }


def key_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _import_grok_key(client) -> str:
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200, imported.text
    created = client.post(
        "/admin/api/keys",
        json={"name": "video", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    return created.json()["key"]


def test_openai_size_maps_to_grok_resolution():
    assert grok_size_from_openai("1280x720") == ("16:9", "720p")
    assert grok_size_from_openai("720x1280") == ("9:16", "720p")
    assert grok_size_from_openai("720p") == (None, "720p")
    body = shape_grok_video_body(
        {
            "prompt": "a cat",
            "seconds": "4",
            "size": "1280x720",
            "input_reference": {"image_url": "https://example.test/cat.png"},
        }
    )
    assert body["duration"] == 4
    assert body["aspect_ratio"] == "16:9"
    assert body["resolution"] == "720p"
    assert body["image"] == {"url": "https://example.test/cat.png"}
    assert "seconds" not in body
    assert "size" not in body
    assert "input_reference" not in body


def test_openai_videos_create_poll_and_content(client):
    key = _import_grok_key(client)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/videos/generations"):
            seen["create"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"request_id": "vid-1"})
        if path.endswith("/videos/vid-1/content"):
            return httpx.Response(404, json={"error": {"message": "missing content path"}})
        if path.endswith("/videos/vid-1"):
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "model": "grok-imagine-video-1.5",
                    "video": {
                        "url": "https://vidgen.example.test/clip.mp4",
                        "duration": 4,
                    },
                },
            )
        if request.url.host == "vidgen.example.test":
            return httpx.Response(200, content=MP4_BYTES, headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    created = client.post(
        "/v1/videos",
        json={
            "model": "grok-4.6",
            "prompt": "a cat",
            "seconds": "4",
            "size": "1280x720",
        },
        headers=key_headers(key),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["id"] == "vid-1"
    assert body["object"] == "video"
    assert body["status"] == "queued"
    assert body["model"] == "grok-imagine-video-1.5"
    assert body["seconds"] == "4"
    assert body["size"] == "1280x720"
    assert "video" not in body
    assert isinstance(body["created_at"], int)
    upstream = seen["create"]
    assert isinstance(upstream, dict)
    assert upstream["model"] == "grok-imagine-video-1.5"
    assert upstream["duration"] == 4
    assert upstream["aspect_ratio"] == "16:9"
    assert upstream["resolution"] == "720p"

    polled = client.get("/v1/videos/vid-1", headers=key_headers(key))
    assert polled.status_code == 200, polled.text
    job = polled.json()
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["seconds"] == "4"
    assert job.get("video") is None
    assert "url" not in job

    content = client.get("/v1/videos/vid-1/content", headers=key_headers(key))
    assert content.status_code == 200, content.text
    assert content.headers["content-type"].startswith("video/mp4")
    assert content.content == MP4_BYTES


def test_video_content_waits_until_completed(client):
    key = _import_grok_key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"id": "vid-pending"})
        if request.url.path.endswith("/videos/vid-pending"):
            return httpx.Response(200, json={"status": "pending", "progress": 40})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    created = client.post(
        "/v1/videos/generations",
        json={"model": "grok-imagine-video-1.5", "prompt": "a cat"},
        headers=key_headers(key),
    )
    assert created.status_code == 200, created.text
    polled = client.get("/v1/videos/vid-pending", headers=key_headers(key))
    assert polled.status_code == 200
    assert polled.json()["status"] == "in_progress"
    assert polled.json()["progress"] == 40
    content = client.get("/v1/videos/vid-pending/content", headers=key_headers(key))
    assert content.status_code == 409
    assert content.json()["error"]["code"] == "video_not_ready"


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _done_video(video_id: str, url: str = "https://vidgen.example.test/clip.mp4") -> dict:
    return {
        "status": "done",
        "model": "grok-imagine-video-1.5",
        "video": {"url": url, "duration": 4},
    }


def test_video_content_thumbnail_variant(client, monkeypatch):
    key = _import_grok_key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "vid-2"})
        if request.url.path.endswith("/videos/vid-2"):
            return httpx.Response(200, json=_done_video("vid-2"))
        if request.url.host == "vidgen.example.test":
            return httpx.Response(200, content=MP4_BYTES, headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    def fake_preview(data: bytes, variant: str) -> tuple[bytes, str]:
        assert variant == "thumbnail"
        assert data == MP4_BYTES
        return b"RIFF" + b"\x00" * 36, "image/webp"

    monkeypatch.setattr("app.codex_gateway.render_video_preview", fake_preview)
    set_http_transport(httpx.MockTransport(handler))
    created = client.post(
        "/v1/videos",
        json={"prompt": "a cat"},
        headers=key_headers(key),
    )
    assert created.status_code == 200, created.text
    response = client.get(
        "/v1/videos/vid-2/content",
        params={"variant": "thumbnail"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/webp")
    assert response.content.startswith(b"RIFF")


def test_video_content_rejects_unknown_variant(client):
    key = _import_grok_key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "vid-2"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    created = client.post("/v1/videos", json={"prompt": "a cat"}, headers=key_headers(key))
    assert created.status_code == 200, created.text
    response = client.get(
        "/v1/videos/vid-2/content",
        params={"variant": "audio"},
        headers=key_headers(key),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_codex_video_is_unsupported(client):
    from conftest import auth_payload, import_pool

    body = import_pool(client, auth_payload("acct-no-video"))
    key = body["generated_api_key"]["key"]
    response = client.post(
        "/v1/videos",
        json={"model": "gpt-6-astra", "prompt": "nope"},
        headers=key_headers(key),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unsupported_capability"


def test_videos_list_delete_and_cursor(client):
    key = _import_grok_key(client)
    created_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos/generations"):
            video_id = f"vid-list-{len(created_ids) + 1}"
            created_ids.append(video_id)
            return httpx.Response(200, json={"request_id": video_id})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    first = client.post("/v1/videos", json={"prompt": "one"}, headers=key_headers(key))
    second = client.post("/v1/videos", json={"prompt": "two"}, headers=key_headers(key))
    assert first.status_code == 200 and second.status_code == 200
    listed = client.get("/v1/videos", params={"limit": 1, "order": "desc"}, headers=key_headers(key))
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["object"] == "list"
    assert payload["has_more"] is True
    assert len(payload["data"]) == 1
    assert payload["data"][0]["object"] == "video"
    page = client.get(
        "/v1/videos",
        params={"limit": 10, "after": payload["last_id"], "order": "desc"},
        headers=key_headers(key),
    )
    assert page.status_code == 200
    assert page.json()["has_more"] is False
    assert page.json()["data"][0]["id"] != payload["data"][0]["id"]
    deleted = client.delete(f"/v1/videos/{first.json()['id']}", headers=key_headers(key))
    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": first.json()["id"],
        "deleted": True,
        "object": "video.deleted",
    }
    missing = client.get(f"/v1/videos/{first.json()['id']}", headers=key_headers(key))
    assert missing.status_code == 404


def test_videos_edit_extend_and_remix(client):
    key = _import_grok_key(client)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "vid-src"})
        if request.method == "POST" and path.endswith("/videos/edits"):
            seen["edit"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"request_id": "vid-edit"})
        if request.method == "POST" and path.endswith("/videos/extensions"):
            seen["extend"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"request_id": "vid-ext"})
        if path.endswith("/videos/vid-src"):
            return httpx.Response(200, json=_done_video("vid-src"))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    created = client.post("/v1/videos", json={"prompt": "source"}, headers=key_headers(key))
    assert created.status_code == 200, created.text
    edited = client.post(
        "/v1/videos/edits",
        json={"prompt": "add a hat", "video": {"id": "vid-src"}, "model": "grok-imagine-video-1.5"},
        headers=key_headers(key),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["id"] == "vid-edit"
    assert edited.json()["model"] == "grok-imagine-video"
    assert edited.json()["remixed_from_video_id"] is None
    edit_body = seen["edit"]
    assert isinstance(edit_body, dict)
    assert edit_body["model"] == "grok-imagine-video"
    assert edit_body["video"]["url"] == "https://vidgen.example.test/clip.mp4"
    assert "duration" not in edit_body

    extended = client.post(
        "/v1/videos/extensions",
        json={"prompt": "keep walking", "seconds": "6", "video": {"id": "vid-src"}},
        headers=key_headers(key),
    )
    assert extended.status_code == 200, extended.text
    extend_body = seen["extend"]
    assert isinstance(extend_body, dict)
    assert extend_body["duration"] == 6
    assert extend_body["model"] == "grok-imagine-video"

    remixed = client.post(
        "/v1/videos/vid-src/remix",
        json={"prompt": "make it night"},
        headers=key_headers(key),
    )
    assert remixed.status_code == 200, remixed.text
    assert remixed.json()["remixed_from_video_id"] == "vid-src"


def test_videos_create_accepts_multipart_input_reference(client):
    key = _import_grok_key(client)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos/generations"):
            seen["create"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"request_id": "vid-img"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    created = client.post(
        "/v1/videos",
        data={"prompt": "animate this", "seconds": "4", "size": "1280x720"},
        files={"input_reference": ("cat.png", PNG_BYTES, "image/png")},
        headers=key_headers(key),
    )
    assert created.status_code == 200, created.text
    upstream = seen["create"]
    assert isinstance(upstream, dict)
    assert str(upstream["image"]["url"]).startswith("data:image/png;base64,")
    assert upstream["duration"] == 4
    assert upstream["resolution"] == "720p"
