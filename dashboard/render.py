"""Native-resolution, high-contrast layouts for a six-inch Paperwhite screen."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .display import Layout, TABS
from .panels import select_panel
import re


def quota_status(provider):
    if provider['state'] == 'stale':
        return '旧数据'
    if provider['state'] == 'ok':
        return ''
    return {'timeout':'查询超时', 'network':'连接失败', 'auth_required':'需登录',
            'permission':'需授权', 'invalid_data':'数据异常'}.get(provider.get('error_code'), '暂无额度')


def reset_countdown(value):
    if not value:
        return '重置时间未知'
    seconds = (datetime.fromisoformat(value)-datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return '已到期，待刷新'
    minutes = max(1, int((seconds+59)//60))
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    return (f'{days}天 ' if days else '')+(f'{hours}小时 ' if hours else '')+f'{minutes}分后重置'


@lru_cache(maxsize=128)
def font(size, bold=False, numeric=False):
    paths = []
    if numeric:
        paths += ['C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',
                  '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf']
    if bold:
        paths += ['C:/Windows/Fonts/msyhbd.ttc', '/System/Library/Fonts/STHeiti Medium.ttc']
    paths += ['C:/Windows/Fonts/msyh.ttc', '/System/Library/Fonts/Hiragino Sans GB.ttc',
              '/System/Library/Fonts/PingFang.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf']
    for path in paths:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def compact(n):
    for divisor, suffix in ((10**9,'B'),(10**6,'M'),(10**3,'K')):
        if n >= divisor:
            return f'{n/divisor:.2f}'.rstrip('0').rstrip('.')+suffix
    return str(n)


def local_time(value, offset=480, fmt='%m-%d %H:%M'):
    if not value:
        return '—'
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(
        timezone(timedelta(minutes=offset))).strftime(fmt)


def history_metric(data):
    history = data.get('token_history')
    if history is None:
        return '日志累计 Tokens',compact(data['totals']['all_tokens'])
    label = '本机近30天 Tokens'
    if history.get('state') in ('stale','partial'):
        label = '近30天 · '+('旧数据' if history['state']=='stale' else '部分记录')
    value = history.get('history_tokens')
    return label, compact(value) if value is not None else '—'


class Canvas:
    """Coordinates are logical, but fonts and shapes are rasterized at output size."""
    def __init__(self, width, height):
        self.layout = Layout(width, height)
        self.scale = self.layout.scale
        self.image = Image.new('L', (width,height), 255)
        self.draw = ImageDraw.Draw(self.image)
        self.left = self.layout.margin
        self.right = self.layout.logical_width-self.left
        self.width = self.right-self.left
        self.bottom = self.layout.logical_height-46

    def px(self, n):
        return round(n*self.scale)

    def text(self, x, y, value, size=22, *, bold=False, numeric=False, shade=0, width=None, align='left', anchor='lt', center_y=False):
        text = str(value)
        face = font(self.px(size), bold, numeric)
        limit = self.px(width if width is not None else self.right-x)
        if self.draw.textlength(text,font=face) > limit:
            while text and self.draw.textlength(text+'…',font=face)>limit:
                text = text[:-1]
            text += '…'
        actual_x = self.px(x)
        if align == 'right':
            actual_x += limit-self.draw.textlength(text,font=face)
        elif align == 'center':
            ink=face.getbbox(text,anchor='ls')
            actual_x += (limit-ink[0]-ink[2])/2
        # Most blocks use glyph-top positioning; mixed text rows share a baseline.
        actual_y=self.px(y)
        if center_y:
            box=face.getbbox(text,anchor='ls')
            actual_y-=(box[1]+box[3])/2
            anchor='ls'
        self.draw.text((actual_x,actual_y),text,font=face,fill=shade,anchor=anchor)

    def line(self, y, x=None, end=None, shade=0, weight=1):
        self.draw.line((self.px(self.left if x is None else x),self.px(y),
                        self.px(self.right if end is None else end),self.px(y)),fill=shade,width=max(1,self.px(weight)))

    def rect(self, x, y, w, h, fill=None, outline=None, weight=1):
        self.draw.rectangle((self.px(x),self.px(y),self.px(x+w),self.px(y+h)),
                            fill=fill,outline=outline,width=max(1,self.px(weight)))

    def bar(self, x, y, w, ratio):
        self.rect(x,y,w,12,outline=0,weight=1.2)
        fill_width = max(0,min(w,w*ratio))
        if fill_width:
            self.rect(x,y,fill_width,12,fill=0)


def header(c, data, view):
    offset=data.get('timezone_offset_minutes',480)
    x,y,w,h=c.layout.device_button()
    center=y+h/2
    c.text(c.left,center,'AI USAGE',28,bold=True,numeric=True,width=c.width-210,center_y=True)
    c.text(x-132,center,local_time(data['generated_at'],offset,'%H:%M'),24,
           numeric=True,width=108,align='right',center_y=True)
    c.rect(x,y,w,h,fill=0 if view=='devices' else 255,outline=0)
    c.text(x,center,'设备',18,width=w,align='center',shade=255 if view=='devices' else 0,center_y=True)
    c.text(c.left,72,local_time(data['generated_at'],offset,'%Y.%m.%d'),17,numeric=True,shade=68)
    for i,(label,key) in enumerate(TABS):
        x,y,w,h=c.layout.tab_rect(i)
        active=view==key or view=='quota' and key=='usage'
        c.rect(x,y,w,h,fill=0 if active else 255,outline=0)
        c.text(x,y+h/2,label,21,bold=active,numeric=True,width=w,align='center',
               shade=255 if active else 0,center_y=True)


def money(value):
    if value is None:return '—'
    if value>=10000:return '$'+compact(value)
    if 0<value<0.01:return '<$0.01'
    return '$'+format(value,'.2f')


def metric_number(c,x,y,value,width,size=40):
    # Keep the row baseline fixed even if a long value needs a smaller font.
    reference=font(c.px(size),True,True)
    baseline=(c.px(y)-reference.getbbox('0123456789',anchor='ls')[1])/c.scale
    match=re.fullmatch(r'(\$?)([0-9.]+)([KMB%]?)',value)
    if not match:
        c.text(x,baseline,value,size,numeric=True,width=width,anchor='ls')
        return
    prefix,number,unit=match.groups()
    face=font(c.px(size),True,True)
    while size>24 and c.draw.textlength(number,font=face)/c.scale+(20 if prefix else 0)+(30 if unit else 0)>width:
        size-=2;face=font(c.px(size),True,True)
    if prefix:
        c.text(x,baseline,prefix,24,numeric=True,width=20,anchor='ls');x+=20
    c.text(x,baseline,number,size,bold=True,numeric=True,width=width-(20 if prefix else 0),anchor='ls')
    if unit:
        nx=x+c.draw.textlength(number,font=face)/c.scale+5
        c.text(nx,baseline,unit,24,numeric=True,width=30,anchor='ls')


def footer(c,data,view,panel=None):
    c.line(c.bottom+8,shade=136)
    measured=(panel or {}).get('measured_at')
    if view=='devices':measured=max((d['seen'] for d in data['devices'] if d['seen']),default=None)
    state=(panel or {}).get('state')
    label='更新 '+local_time(measured,data.get('timezone_offset_minutes',480),'%H:%M') if measured else '等待数据'
    if state=='stale':label+=' · 旧数据'
    elif state=='partial':label+=' · 部分记录'
    c.text(c.left,c.bottom+17,label,15,shade=68,width=c.width,align='right')


def quota_rows(c,data,x,y,width,limit=2):
    provider=next((p for p in data.get('quotas',{}).get('providers',[]) if p['id']=='codex'),None)
    face=font(c.px(21),True)
    left=face.getbbox('Codex',anchor='ls')
    right=face.getbbox('剩余额度',anchor='ls')
    baseline=c.px(y)-min(left[1],right[1])
    gap=c.px(10)
    radius=max(1,c.scale*1.5)
    dot_x=c.px(x)+left[2]+gap+radius
    dot_y=baseline+(min(left[1],right[1])+max(left[3],right[3]))/2
    # Space from the visible glyph edges, not the mixed font's space/dot widths.
    c.text(x,baseline/c.scale,'Codex',21,bold=True,anchor='ls')
    c.draw.ellipse((dot_x-radius,dot_y-radius,dot_x+radius,dot_y+radius),fill=0)
    label_x=(dot_x+radius+gap-right[0])/c.scale
    c.text(label_x,baseline/c.scale,'剩余额度',21,bold=True,anchor='ls',width=width-110-(label_x-x))
    if not provider or not provider.get('windows'):
        c.text(x,y+43,quota_status(provider) if provider else '暂无额度',20,shade=68,width=width)
        return
    if provider.get('stale'):c.text(x+width-100,y,'旧数据',18,width=100,align='right')
    offset=data.get('timezone_offset_minutes',480)
    for i,w in enumerate(provider['windows'][:limit]):
        row=y+32+i*48
        baseline=row+20
        label={'session':'时段剩余','weekly':'本周剩余'}.get(w.get('kind'),w['label'])
        c.text(x,baseline,label,19,width=width*0.32,anchor='ls')
        c.text(x+width*0.34,baseline,f"{w['remaining_percent']:g}%",22,bold=True,numeric=True,
               width=width*0.2,align='right',anchor='ls')
        reset=w.get('reset_at')
        caption=local_time(reset,offset,'%H:%M' if w.get('kind')=='session' else '%m/%d %H:%M')+' 重置' if reset else '重置时间未知'
        if w.get('expired'):caption='已到期 · 待更新'
        c.text(x+width*0.58,baseline,caption,18 if width>=480 else 16,
               shade=68,width=width*0.42,align='right',anchor='ls')
        c.rect(x,row+30,width,7,outline=0)
        fill=width*w['remaining_percent']/100
        if fill:c.rect(x,row+30,fill,7,fill=0)


def conversations(c,panel,x,y,width,limit=3,api=False):
    c.text(x,y,'最近调用' if api else '最近对话',21,bold=True,width=width-145)
    c.text(x+width-145,y+3,'Tokens' if api else '累计 Tokens',16,shade=68,width=145,align='right')
    rows=panel.get('conversations',[])[:limit]
    if not rows:c.text(x,y+43,'暂无记录',20,shade=68,width=width)
    for i,row in enumerate(rows):
        top=y+37+i*55
        if top+42>c.bottom:break
        c.text(x,top,row.get('title') or row['model']+' 调用',21,width=width-130)
        c.text(x+width-125,top,compact(row['tokens']),24,bold=True,numeric=True,width=125,align='right')
        meta=row['source']+' · '+row['device_name']
        c.text(x,top+30,meta,16,shade=68,width=width-110)
        c.text(x+width-105,top+30,local_time(row['last_usage'],fmt='%H:%M'),16,numeric=True,shade=68,width=105,align='right')


def usage(c,data,view='usage',device=None):
    panel=select_panel(data,view,device)
    api=view=='api'
    label=panel.get('cost_label','费用' if api else '估算金额')
    c.text(c.left,170,panel.get('scope','全部已接入设备' if api else 'Codex'),18,shade=68,width=c.width-115)
    if api and not panel.get('connected'):c.text(c.right-110,170,'未接入',18,width=110,align='right')
    elif panel.get('state') in ('stale','partial'):c.text(c.right-110,170,'旧数据' if panel['state']=='stale' else '部分记录',17,width=110,align='right')
    cell=c.width/2
    for i,(period,prefix) in enumerate((('today','今日'),('history','近30天'))):
        y=207+i*94
        bucket=panel.get(period,{})
        c.text(c.left,y,prefix+label,18,width=cell-12)
        c.text(c.left+cell+20,y,prefix+' Tokens',18,width=cell-20)
        metric_number(c,c.left,y+31,money(bucket.get('cost_usd')),cell-16)
        tokens=bucket.get('tokens')
        metric_number(c,c.left+cell+20,y+31,compact(tokens) if tokens is not None else '—',cell-20)
    c.line(391,shade=136)
    if api:
        c.text(c.left,416,'API 费用',21,bold=True)
        c.text(c.left,458,'余额未接入' if panel.get('connected') else '接入后显示费用与用量',20,shade=68)
    else:quota_rows(c,data,c.left,415,c.width)
    c.line(552,shade=136)
    conversations(c,panel,c.left,571,c.width,api=api)
    return panel


def quota(c, data):
    y = 190
    providers = data.get('quotas',{}).get('providers',[])
    if not providers:
        c.text(c.left,y,'暂无额度',24)
        return
    for p in providers:
        if y+40>c.bottom:
            break
        c.text(c.left,y,p['name'],28,bold=True,numeric=True,width=c.width-150)
        status = quota_status(p)
        if status:
            c.text(c.right-150,y+4,status,20,width=150,align='right')
        y += 50
        for w in p['windows']:
            if y+94>c.bottom:
                c.text(c.left,y,'更多窗口…',20,shade=64)
                return
            c.text(c.left,y,w['label'],23,width=c.width-160)
            value = ('上次剩余 ' if p.get('stale') else '剩余 ')+f"{w['remaining_percent']:g}%"
            c.text(c.right-160,y,value,23,width=160,align='right')
            c.bar(c.left,y+36,c.width,w['remaining_percent']/100)
            c.text(c.left,y+61,reset_countdown(w['reset_at']),20,shade=64)
            y += 103
        if p.get('stale') and p.get('measured_at') and y+20<c.bottom:
            c.text(c.left,y,'更新 '+local_time(p['measured_at'],data.get('timezone_offset_minutes',480)),18,shade=64)
            y += 34
        if y+35<c.bottom:
            c.line(y,shade=96)
            y += 28


def device_page(c,data):
    c.text(c.left,165,'设备与用量来源',18,shade=68)
    for index,dev in enumerate(data['devices']):
        x,y,w,h=c.layout.device_row(index)
        if y+h>c.bottom:break
        status='在线' if dev['online'] else '离线' if dev['seen'] else '待连接'
        if dev['online'] and dev['status'].get('errors'):status='采集异常'
        c.text(x,y+15,dev['name'],25,bold=True,width=w-130)
        c.text(x+w-125,y+18,status,20,width=125,align='right')
        value=compact(dev['today_tokens']) if dev['seen'] and dev['status'].get('files') else '—'
        c.text(x,y+61,'今日日志 '+value+' Tokens',23,width=w)
        c.text(x,y+103,'查看 Coding Plan →',17,shade=68,width=w)
        c.line(y+h,shade=136)


def landscape_usage(c,data,view,device):
    panel=select_panel(data,view,device)
    split=c.left+c.width*0.46
    left=split-c.left-22;right=c.right-split-22
    c.text(c.left,171,panel.get('scope',''),18,shade=68,width=c.width)
    for i,(period,prefix) in enumerate((('today','今日'),('history','近30天'))):
        y=218+i*133
        bucket=panel.get(period,{})
        c.text(c.left,y,prefix+panel.get('cost_label','估算金额'),18,width=left/2-8)
        c.text(c.left+left/2+8,y,prefix+' Tokens',18,width=left/2-8)
        metric_number(c,c.left,y+35,money(bucket.get('cost_usd')),left/2-8,32)
        tokens=bucket.get('tokens')
        metric_number(c,c.left+left/2+8,y+35,compact(tokens) if tokens is not None else '—',left/2-8,32)
    if view!='api':quota_rows(c,data,split+22,214,right)
    else:c.text(split+22,214,'API · '+('已接入' if panel.get('connected') else '未接入'),22,width=right)
    conversations(c,panel,split+22,365,right,limit=2,api=view=='api')
    return panel


def render_dashboard(width,height,data,view='usage',device=None):
    c=Canvas(width,height)
    header(c,data,view)
    panel=None
    if view=='devices':device_page(c,data)
    elif view=='quota':quota(c,data)
    elif c.layout.landscape:panel=landscape_usage(c,data,view,device)
    else:panel=usage(c,data,view,device)
    footer(c,data,view,panel)
    return c.image.point(lambda value: round(value/17)*17)
