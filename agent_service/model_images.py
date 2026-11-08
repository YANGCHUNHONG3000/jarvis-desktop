"""Bounded PNG inputs for CloudClient; no files, credentials or network access.

The 65,536-token allowance per image is an application safety ceiling, not an
official token estimate. Image usage must still fit it and settle through the
shared ledger. Base64 transport bytes are never counted as text tokens.
"""
import base64
from io import BytesIO
import warnings


MAX_IMAGES = 2
MAX_IMAGE_DIMENSION = 2048
MAX_TOTAL_PNG_BYTES = 8 * 1024 * 1024
IMAGE_TOKEN_BOUND = 65_536
IMAGE_BUDGET_POLICY = 'request_png_bound_v1'
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PNG_END = b'\x00\x00\x00\x00IEND\xaeB`\x82'


def _check_container(raw):
    """Reject trailing data, incomplete chunks and APNG before Pillow decoding."""
    if (not raw.startswith(PNG_SIGNATURE) or not raw.endswith(PNG_END)
            or raw[8:16] != b'\x00\x00\x00\rIHDR'
            or any(not 1 <= int.from_bytes(raw[start:start + 4], 'big') <= MAX_IMAGE_DIMENSION
                   for start in (16, 20))):
        raise ValueError('model_image_invalid')
    offset = len(PNG_SIGNATURE)
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError('model_image_invalid')
        size = int.from_bytes(raw[offset:offset + 4], 'big')
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + size
        if (end > len(raw) or kind in {b'acTL', b'fcTL', b'fdAT'}
                or (kind == b'IHDR' and offset != 8)):
            raise ValueError('model_image_invalid')
        if kind == b'IEND' and (size or end != len(raw)):
            raise ValueError('model_image_invalid')
        offset = end


def validate_png_images(images_png):
    """Return immutable validated PNG bytes; emit only fixed error codes."""
    if images_png is None:
        return ()
    if type(images_png) not in (list, tuple) or len(images_png) > MAX_IMAGES:
        raise ValueError('model_image_invalid')
    images = tuple(images_png)
    if any(type(raw) is not bytes or not raw for raw in images):
        raise ValueError('model_image_invalid')
    if sum(map(len, images)) > MAX_TOTAL_PNG_BYTES:
        raise ValueError('model_image_bytes_limit')
    if not images:
        return images
    try:
        from PIL import Image
    except ImportError:
        raise ValueError('model_image_validation_unavailable') from None
    for raw in images:
        try:
            _check_container(raw)
            with warnings.catch_warnings():
                warnings.simplefilter('error')
                with Image.open(BytesIO(raw), formats=['PNG']) as image:
                    if (image.format != 'PNG' or getattr(image, 'is_animated', False)
                            or getattr(image, 'n_frames', 1) != 1
                            or any(not 1 <= side <= MAX_IMAGE_DIMENSION for side in image.size)):
                        raise ValueError
                    image.verify()
                # verify checks container integrity; load also checks pixel decoding.
                with Image.open(BytesIO(raw), formats=['PNG']) as image:
                    image.load()
        except (OSError, SyntaxError, ValueError, Warning, EOFError, Image.DecompressionBombError):
            raise ValueError('model_image_invalid') from None
    return images


def image_user_content(text, images):
    """Build the official Chat Completions image_url shape, only in memory."""
    return [{'type': 'text', 'text': text}] + [
        {'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii'),
            'detail': 'high'}} for raw in images]
