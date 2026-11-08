"""Window annotation adapted from Windows-Use, MIT, Copyright 2025 CursorTouch.

Source: Jeomon/Windows-Use@1be0c217cd3fd229fe62b0578fe323f39526f888
windows_use/agent/desktop/service.py:Desktop.get_annotated_screenshot.
See THIRD_PARTY_NOTICES.md. Adaptations: supplied window-only pixels, fixed
colors, existing Jarvis IDs, clipped labels and bounded resize; no screen grab.
"""
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont


def annotate(png, labels):
    with Image.open(BytesIO(png)) as source:
        source.load()
        image = source.convert('RGB')
    original = image.size
    image.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
    sx, sy = image.width / original[0], image.height / original[1]
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('arial.ttf', 13)
    except OSError:
        font = ImageFont.load_default()
    for item in labels:
        left, top, right, bottom = item['bounds']
        box = (round(left*sx), round(top*sy), round(right*sx)-1, round(bottom*sy)-1)
        color = '#005AC8'
        draw.rectangle(box, outline=color, width=2)
        text = item['id']
        width = int(draw.textlength(text, font=font)) + 6
        x = max(0, min(image.width-width, box[0]))
        y = max(0, min(image.height-20, box[1]-20))
        draw.rectangle((x,y,x+width,y+20), fill=color)
        draw.text((x+3,y+2), text, fill='white', font=font)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
