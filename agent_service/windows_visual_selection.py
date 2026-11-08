"""Local, fail-closed evidence for underlined ribbon selection and live content."""
from pathlib import Path
import struct

from . import windows_visual as visual


def readback(frame, row):
    from .windows_visual_memory import _bitmap
    raw=Path(_bitmap(frame)['bitmap']).read_bytes()
    width,height=frame['width'],frame['height']
    top_down=struct.unpack_from('<i',raw,22)[0]<0
    selected=[]
    for label,box in row:
        x,y,w,h=map(round,box)
        lines=[]
        # Include one guard row on both sides; a thick shape clipped by the
        # search band must never look like a thin selection underline.
        for yy in range(max(0,y+h-1),min(y+h+10,height)):
            points=[]
            for xx in range(max(0,x-3),min(width,x+w+4)):
                scan=yy if top_down else height-1-yy
                b,g,r,_=raw[54+(scan*width+xx)*4:58+(scan*width+xx)*4]
                if max(r,g,b)-min(r,g,b)>=55 and 35<=max(r,g,b)<=235:
                    points.append((xx,(r,g,b)))
            # Widths are integer OCR pixels. Round the 80% coverage threshold
            # to a pixel (e.g. a 26px underline under a 33px OCR box).
            if len(points)<max(12,round(w*.8)):continue
            positions=[p[0] for p in points]
            if (positions[0]<=x-3 or positions[-1]>=x+w+3
                    or positions!=list(range(positions[0],positions[-1]+1))
                    or abs((positions[0]+positions[-1])/2-(x+w/2))>4
                    or positions[-1]-positions[0]>w+6):continue
            # Windows anti-aliases the two end pixels. Require a uniform core;
            # endpoints must keep its channel ordering, not an unrelated hue.
            color=points[1][1]
            if any(max(abs(a-b) for a,b in zip(rgb,color))>20 for _,rgb in points[1:-1]):continue
            order=sorted(range(3),key=lambda i:color[i])
            if any(sorted(range(3),key=lambda i:rgb[i])!=order for _,rgb in (points[0],points[-1])):continue
            lines.append(yy)
        if (1<=len(lines)<=3 and y+h<=lines[0] and lines[-1]<y+h+9
                and lines==list(range(lines[0],lines[-1]+1))):
            selected.append(label)
    low=max((b[1]+b[3] for _,b in row),default=240)+6
    words=[v for v in frame['words'] if low<=v['box'][1] and v['box'][1]+v['box'][3]<=min(180,low+80)]
    groups=[]
    for item in words:
        x,y,w,h=item['box']
        if groups:
            text,a,b,c,d=groups[-1]
            if abs((y+h/2)-(b+d/2))<=max(h,d)*.45 and -1<=x-(a+c)<=max(h,d)*.8:
                groups[-1]=(text+item['text'],a,b,x+w-a,max(h,d))
                continue
        groups.append((item['text'],x,y,w,h))
    terms=[]
    navigation={visual.normalize(n) for n in visual.LABELS}
    for text,*_ in groups:
        text=text.strip()
        if (2<=len(text)<=40 and any(c.isalpha() for c in text)
                and visual.normalize(text) not in navigation and text not in terms):terms.append(text)
    return {'selected_labels':selected,'content_labels':terms[:32],
            'rule':'unique_colored_underline_1_to_3_rows_and_two_content_labels_v1'}
