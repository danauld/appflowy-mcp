"""Bounded image input validation without retaining or echoing the payload."""
import base64
import binascii
import hashlib
from pathlib import PurePath

MAX_BYTES = 5 * 1024 * 1024


def decode_image(file_name, mime_type, image_base64):
    if len(image_base64) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError('image exceeds the 5 MiB decoded size limit')
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('image_base64 must contain valid base64 without a data URL prefix') from None
    signatures = {'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
                  'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
                  'image/gif': raw.startswith((b'GIF87a', b'GIF89a')),
                  'image/webp': raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'}
    if not raw or len(raw) > MAX_BYTES or not signatures.get(mime_type):
        raise ValueError('bytes must match mime_type: PNG, JPEG, GIF or WebP; maximum 5 MiB')
    if not file_name or PurePath(file_name).name != file_name or '\\' in file_name:
        raise ValueError('file_name must be a filename, not a path')
    return raw, hashlib.sha256(raw).hexdigest()
