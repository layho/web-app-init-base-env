"""Private, bounded, normalized image evidence shared by feedback and rejection."""
import base64
import binascii
import io
import json
import os
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory
from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1] / "logs" / "feedback-attachments"
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 9
ID = re.compile(r"[0-9a-f]{32}\Z")

def normalize(values):
    if len(values) > MAX_FILES:
        raise ValueError("最多上传9张截图")
    result = []
    for value in values:
        try:
            raw = base64.b64decode(value, validate=True)
            if not raw or len(raw) > MAX_BYTES:
                raise ValueError("单张截图不得超过10MB")
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in {"PNG", "JPEG", "WEBP"} or im.width * im.height > 20000000:
                    raise ValueError("仅支持PNG、JPG、WebP，且像素数不得超过2000万")
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((2560, 2560))
                out = io.BytesIO()
                im.save(out, "JPEG", quality=90)
                if out.tell() > MAX_BYTES:
                    raise ValueError("截图编码后超过限制")
                result.append(out.getvalue())
        except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("无效截图文件") from exc
    return result

def save(scope, key, images):
    if scope not in {"feedback","rejection"} or not re.fullmatch(r"[0-9a-f]{1,40}", str(key)):
        raise ValueError("Invalid attachment scope")
    folder = ROOT / scope / str(key)
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.exists():
        raise ValueError("附件记录已存在")
    with TemporaryDirectory(dir=folder.parent) as tmp:
        staging = Path(tmp) / "data"
        staging.mkdir(mode=0o700)
        for image in images:
            path = staging / (secrets.token_hex(16) + ".jpg")
            with path.open("xb") as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(image)
        os.rename(staging, folder)
    return listing(scope,key)

def listing(scope,key):
    if scope not in {"feedback","rejection"} or not re.fullmatch(r"[0-9a-f]{1,40}", str(key)):
        return []
    folder = ROOT / scope / str(key)
    if not folder.is_dir() or folder.is_symlink():
        return []
    return [{"id":p.stem, "url":f"/api/v1/attachments/{scope}/{key}/{p.stem}"}
            for p in sorted(folder.glob("*.jpg")) if ID.fullmatch(p.stem) and p.is_file() and not p.is_symlink()]

def read(scope,key,id):
    if not ID.fullmatch(id) or not any(x["id"] == id for x in listing(scope,key)):
        raise FileNotFoundError()
    path=ROOT / scope / str(key) / (id+".jpg")
    fd=os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:
        raise ValueError("附件超限")
    return raw
