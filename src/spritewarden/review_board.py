"""Local, neutral-background direction-set review boards. Never calls a model."""
from pathlib import Path
import textwrap
from PIL import Image, ImageDraw, ImageFont
from .judge.views import direction_frame_files, GAME_ORDER
from .gates.records import load_record, stage_readiness


def build(folder):
    files=direction_frame_files(folder)
    if any(not p.resolve().is_relative_to(Path(folder).resolve()) for p in files.values()):
        raise ValueError('review images must remain inside the selected folder')
    names=[f for f in GAME_ORDER if f in files]
    if not names:raise ValueError('no named direction PNGs found')
    font=ImageFont.load_default(size=16)
    heading=ImageFont.load_default(size=20)
    width,height=440,700
    board=Image.new('RGB',(width*3,height*((len(names)+2)//3)),(232,230,221))
    draw=ImageDraw.Draw(board);report=[]
    for i,name in enumerate(names):
        path=files[name];record=load_record(path) or {};ready=stage_readiness(path,'views')
        x=(i%3)*width;y=(i//3)*height
        draw.text((x+14,y+12),name+' / '+('READY' if ready['ready'] else 'BLOCKED'),font=heading,fill=(25,45,35))
        with Image.open(path) as opened:
            if opened.width*opened.height>1024*1024:raise ValueError('review image exceeds pixel limit')
            image=opened.convert('RGBA')
            if image.width>384 or image.height>384:raise ValueError('contact sheet supports stills up to 384px; inspect larger sheets in gallery')
            # Integer nearest-neighbor zoom, never fractional art scaling.
            scale=max(1,min(384//image.width,384//image.height))
            image=image.resize((image.width*scale,image.height*scale),Image.Resampling.NEAREST)
        bg=Image.new('RGBA',(384,384),(182,181,168,255))
        bg.alpha_composite(image,((384-image.width)//2,(384-image.height)//2))
        board.paste(bg.convert('RGB'),(x+14,y+48))
        gate=(record.get('gates') or {}).get('vision_reference') or {}
        findings=gate.get('errors') or []
        summary=ready['reasons'] if not findings and not ready['ready'] else findings
        acceptance=ready.get('acceptance')
        if acceptance:
            summary=[f"Human exception by {acceptance.get('actor', 'reviewer')}: {acceptance.get('reason', '')}", *summary]
        lines=[]
        for finding in summary:lines.extend(textwrap.wrap(str(finding),width=47))
        if not lines:lines=['No unresolved readiness blockers.']
        display=lines[:13]+(['[more in JSON report]'] if len(lines)>13 else [])
        draw.multiline_text((x+14,y+445),'\n'.join(display),font=font,fill=(40,40,40),spacing=2)
        report.append({'facing':name,'path':str(path),'readiness':ready,'vision_findings':findings,'original_gate':gate})
    return board,report
