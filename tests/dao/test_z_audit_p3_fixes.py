# -*- coding: utf-8 -*-
"""B-20260911-01 批 R 审计残留收口 7 项 专项用例（TOCTOU/兄弟目录穿越/超限上传/放大解压）。"""
import threading
import zlib

import pytest

from conftest import iso_data_dir


def _run(coro):
    import asyncio
    return asyncio.run(coro)
  # noqa: F401  —— pytest 注入


# ==================== G1-1 · TOCTOU ====================

def test_g1_1_toctou_concurrent_register_no_duplicate_card(iso_data_dir):
    data_dir, shared_dir = iso_data_dir
    import library
    import assets_index

    rel = "uploads/test_toctou.png"
    target = shared_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(assets_index.register_file(rel))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 2
    actions = sorted(r["action"] for r in results)
    assert ("new" in actions or "version" in actions) and "skip" in actions, actions

    refs = library.list_references()
    matches = [r for r in refs if r.get("path") == rel
               or (r.get("images") and r["images"] and r["images"][0].get("path") == rel)]
    assert len(matches) == 1, f"期望 1 张卡，实得 {len(matches)}：{[r.get('id') for r in matches]}"


# ==================== G4-2 · 兄弟目录穿越 ====================

def test_g4_2_asset_file_evil_prefix_blocked(iso_data_dir):
    data_dir, shared_dir = iso_data_dir
    import library

    evil = shared_dir.parent / f"{shared_dir.name}_evil"
    evil.mkdir(parents=True, exist_ok=True)
    (evil / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    malicious_rel = f"../{shared_dir.name}_evil/secret.png"

    import main  # noqa: F401
    from fastapi import HTTPException
    try:
        main.api_asset_file(malicious_rel)
    except HTTPException as e:
        assert e.status_code == 404, f"期望 404，得到 {e.status_code}：{e.detail}"
    else:
        pytest.fail("未抛 HTTPException，兄弟目录前缀可命中 = 漏洞未修")

    (evil / "secret.png").unlink()
    evil.rmdir()


# ==================== G4-1 · 超限上传 ====================

class _FakeUploadFile:
    def __init__(self, data: bytes, chunk: int = 1024 * 1024):
        self._buf = data
        self._pos = 0
        self._chunk = chunk

    async def read(self, n: int = -1) -> bytes:
        if n <= 0:
            n = self._chunk
        end = min(self._pos + n, len(self._buf))
        out = self._buf[self._pos:end]
        self._pos = end
        return out


class _FakeRequest:
    def __init__(self, content_length):
        self.headers = {"content-length": str(content_length)}


def test_g4_1_oversize_content_length_rejected(iso_data_dir):
    import main
    from fastapi import HTTPException
    # 声明 52MB（> 50MB + 1MB slack → 必拒；51MB 恰在 slack 内合法，因 multipart 边界开销需留余量）
    fake_req = _FakeRequest(content_length=52 * 1024 * 1024)
    fake_file = _FakeUploadFile(b"")
    with pytest.raises(HTTPException) as ei:
        _run(main._read_upload_capped(fake_file, fake_req))
    assert ei.value.status_code == 413
    assert fake_file._pos == 0


def test_g4_1_oversize_streaming_rejected(iso_data_dir):
    import main
    from fastapi import HTTPException
    big = b"x" * (60 * 1024 * 1024)
    fake_req = _FakeRequest(content_length=10)
    fake_file = _FakeUploadFile(big)
    with pytest.raises(HTTPException) as ei:
        _run(main._read_upload_capped(fake_file, fake_req))
    assert ei.value.status_code == 413


def test_g4_1_under_limit_ok(iso_data_dir):
    import main
    small = b"abc" * 100
    fake_req = _FakeRequest(content_length=len(small))
    fake_file = _FakeUploadFile(small)
    out = _run(main._read_upload_capped(fake_file, fake_req))
    assert out == small


# ==================== G4-3 · 放大解压 ====================

def test_g4_3_zlib_max_length_caps_decompression(iso_data_dir):
    import main
    big = b"AAAAAAAAAA" * 1_700_000
    s = zlib.compress(big)
    assert len(s) < main._PDF_STREAM_MAX_BYTES

    raw_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
        + b"stream\r\n" + s + b"\r\nendstream\n"
        + b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    text = main._extract_pdf_text(raw_pdf)
    assert isinstance(text, str)
    assert len(text.encode("utf-8", errors="replace")) <= main._PDF_STREAM_MAX_BYTES + 1024


def test_g4_3_zlib_normal_stream_unaffected(iso_data_dir):
    import main
    payload = b"BT /F1 12 Tf (Hello World) Tj ET"
    s = zlib.compress(payload)
    raw = (
        b"%PDF-1.4\n"
        + b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        + b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        + b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
        + b"stream\r\n" + s + b"\r\nendstream\n"
        + b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    text = main._extract_pdf_text(raw)
    assert "Hello" in text or "World" in text, f"正常流应能解出文本，实得：{text!r}"
