"""Local Windows OCR helper. Receives a bounded BMP; no input/network access."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sys

RAPID_RECIPE = 'rapidocr-onnxruntime-1.4.4-wordboxes-v2'
FORM_RECIPE = 'rapidocr-onnxruntime-1.4.4-form-2x-lanczos-v1'
WINRT_RECIPE = 'winrt-2x-v1'


def rapid_words(rows, *, scale=1):
    """Keep model-aligned word boxes, never estimate substring coordinates."""
    import math
    if scale not in (1,2) or type(scale) is not int:
        raise RuntimeError('windows_ocr_scale_invalid')
    words = []
    for row in rows or []:
        if len(row) != 6 or not math.isfinite(row[2]) or row[2] < .95:
            continue
        boxes, texts, scores = row[3:]
        if not (len(boxes) == len(texts) == len(scores)):
            raise RuntimeError('windows_ocr_response_invalid')
        # Dropping a doubtful character must not join neighbours into a new label.
        if any(not math.isfinite(score) or score < .95 for score in scores):
            continue
        for box, text, score in zip(boxes, texts, scores):
            if not text.strip():
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            box=[min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)]
            words.append({'text': text, 'box': box if scale==1 else [v/scale for v in box]})
    if len(words) > 300:
        raise RuntimeError('windows_ocr_text_limit')
    return words


def recognize_rapid(path, *, form=False):
    from importlib.metadata import version
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
    import io
    if version('rapidocr-onnxruntime') != '1.4.4':
        raise RuntimeError('windows_ocr_version_mismatch')
    raw = Path(path).read_bytes()
    if len(raw) > 5_000_000:
        raise RuntimeError('windows_visual_frame_limit')
    with Image.open(io.BytesIO(raw)) as original:
        if not 200 <= original.width <= 4096 or not 60 <= original.height <= 260:
            raise RuntimeError('windows_visual_frame_limit')
        width, height = original.size
        if form:
            if width>1600:raise RuntimeError('windows_visual_frame_limit')
            expanded=original.resize((width*2,height*2),Image.Resampling.LANCZOS)
            stream=io.BytesIO();expanded.save(stream,format='PNG');recognition_bytes=stream.getvalue()
        else:recognition_bytes=raw
    # Packaged CPU ONNX models; no network or downloads at runtime.
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    rows, _ = engine(recognition_bytes, return_word_box=True)
    return {'schema': 1, 'sha256': sha256(raw).hexdigest(), 'width': width, 'height': height,
            'language': 'ch-en', 'recipe': FORM_RECIPE if form else RAPID_RECIPE,
            'words': rapid_words(rows,scale=2 if form else 1)}


async def recognize(path):
    from PIL import Image
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError('windows_ocr_language_unavailable')
    raw = Path(path).read_bytes()
    if len(raw) > 5_000_000:
        raise RuntimeError('windows_visual_frame_limit')
    import io
    with Image.open(io.BytesIO(raw)) as original:
        if original.width > 4096 or original.height > 260:
            raise RuntimeError('windows_visual_frame_limit')
        scale = min(2, OcrEngine.max_image_dimension / original.width)
        if scale < 1:
            raise RuntimeError('windows_visual_frame_limit')
        image = original.resize((int(original.width*scale), int(original.height*scale))).convert('RGBA')
        writer = DataWriter()
        writer.write_bytes(image.tobytes('raw', 'BGRA'))
        bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width, image.height, BitmapAlphaMode.IGNORE)
        result = await engine.recognize_async(bitmap)
        words = []
        for line in result.lines:
            for word in line.words:
                r = word.bounding_rect
                words.append({'text': word.text, 'box': [round(r.x/scale, 2), round(r.y/scale, 2),
                              round(r.width/scale, 2), round(r.height/scale, 2)]})
        bitmap.close()
        writer.close()
        if len(words) > 300:
            raise RuntimeError('windows_ocr_text_limit')
        return {'schema': 1, 'sha256': sha256(raw).hexdigest(), 'width': original.width,
                'height': original.height, 'language': engine.recognizer_language.language_tag,
                'recipe': WINRT_RECIPE,
                'words': words}


if __name__ == '__main__':
    try:
        result = (recognize_rapid(sys.argv[1],form=sys.argv[2]=='rapidocr-form') if len(sys.argv)>2 and sys.argv[2] in {'rapidocr','rapidocr-form'}
                  else asyncio.run(recognize(sys.argv[1])))
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'ok': False, 'code': str(exc) if isinstance(exc, RuntimeError)
                         else 'windows_ocr_unavailable', 'type': type(exc).__name__}))
        sys.exit(1)
