"""One scope per panel: costs and tokens share the same source and calendar."""
from datetime import datetime,timezone,timedelta


def build_panels(store,data,config):
    offset=config.get('timezone_offset_minutes',480)
    local=config['local_device']['id']
    api=store.channel_summary('api',offset)
    mixed_local=any(x['device_id']==local and x['source']=='Codex' for x in api['source_devices'])
    panels=[]
    for device in data['devices']:
        panel=store.channel_summary('plan',offset,device['id'],'Codex')
        panel.update(device_id=device['id'],scope='Codex · '+device['name'],state='partial',
                     measured_at=device.get('seen'),cost_label='估算金额')
        if device['id']==local and not mixed_local and config.get('local_device',{}).get('codex_billing_type','plan')=='plan':
            history=data.get('token_history',{})
            panel.update(state=history.get('state','connecting'),measured_at=history.get('measured_at'))
            # Never combine the Mac snapshot's cost with multi-device log tokens.
            # A snapshot from yesterday cannot supply today's headline.
            measured=history.get('measured_at')
            same_day=False
            if measured:
                tz=timezone(timedelta(minutes=offset))
                same_day=datetime.fromisoformat(measured.replace('Z','+00:00')).astimezone(tz).date()==datetime.now(tz).date()
            zone_ok=history.get('timezone') in ({'Asia/Shanghai','Asia/Chongqing','Asia/Chungking','Asia/Hong_Kong'} if offset==480 else {'UTC','Etc/UTC'} if offset==0 else set())
            for period in ('today','history'):
                available=zone_ok and (period!='today' or same_day)
                panel[period]={'tokens':history.get(period+'_tokens') if available else None,
                    'cost_usd':history.get(period+'_cost_usd') if available else None}
            if measured and (not same_day or not zone_ok):panel['state']='stale'
        panels.append(panel)
    estimates=any(api[p]['estimates'] for p in ('today','history'))
    api.update(scope='全部已接入设备',state='ok' if api['connected'] else 'unavailable',
               cost_label='费用 · 含估算' if estimates else '费用',measured_at=data['generated_at'])
    data['panels']={'plan_devices':panels,'default_device':local,'api':api}
    return data


def select_panel(data,view='usage',device=None):
    panels=data.get('panels',{})
    if view=='api':return panels.get('api',{})
    choices=panels.get('plan_devices',[])
    wanted=device or panels.get('default_device')
    return next((p for p in choices if p['device_id']==wanted),choices[0] if choices else {})
