"""CLI and self-contained visual report for repair preflight."""
import argparse
import base64
import html
import json
from pathlib import Path
from .repair_preflight import inspect_plan


def render_report(result):
    def esc(value):return html.escape(str(value))
    figures=[]
    for kind,label in [('source','Original'),('candidate','Candidate')]:
        if not result.get(kind):continue
        import hashlib
        raw=Path(result[kind]).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=result['metrics'][kind+'_sha256']:raise ValueError('image changed since preflight; rerun local inspection')
        encoded=base64.b64encode(raw).decode('ascii')
        figures.append(f'<figure><img alt="{label}" src="data:image/png;base64,{encoded}"><figcaption>{label}</figcaption></figure>')
    blockers=''.join('<li>'+esc(item)+'</li>' for item in result['blockers']) or '<li>No deterministic blocker found. This is not artistic approval.</li>'
    options=''.join(f'<li><strong>{esc(action)}</strong>'+(' — '+esc(reason) if reason else ' — eligible proposal')+'</li>' for action,reason in list(result['excluded_actions'].items())+[(a,None) for a in result['eligible_actions']])
    metrics=esc(json.dumps(result['metrics'],indent=2))
    warnings=''.join('<li>'+esc(w)+'</li>' for w in result['warnings'])
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Repair preflight</title>
<style>
section{{max-width:780px;padding:16px;color:var(--foreground,#ddd)}}
h1{{font-size:22px;margin:0 0 8px}}h2{{font-size:16px;margin:18px 0 8px}}
.status{{display:inline-block;border:1px solid var(--border,#555);padding:5px 10px;border-radius:6px}}
.images{{display:flex;gap:12px;flex-wrap:wrap}}figure{{margin:0}}img{{width:240px;image-rendering:pixelated;background:#666;border:1px solid var(--border,#555)}}figcaption{{padding:5px 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--card,#222);padding:10px}}li{{margin-bottom:6px}}
.note{{color:var(--muted-foreground,#aaa)}}
</style><section><h1>Repair preflight</h1><span class="status">{esc(result['status'].upper())} · {esc(result['defect_kind'])}</span>
<p>{esc(result['goal'])}</p><div class="images">{''.join(figures)}</div>
<h2>Plan checks</h2><ul>{blockers}</ul><h2>Strategies</h2><ul>{options}</ul>
<h2>Jev recommendation</h2><p>{esc(result['decision'] or 'No model selection')} · {esc(result['decision_status'])} · model calls: {result['model_calls']}</p>
<p>Generation spend cap: ${result['spend_cap_usd']:.2f}. Quotes: {esc(result['quoted_costs'])}</p>
<h2>Measured pixels</h2><pre>{metrics}</pre><h2>Boundaries</h2><ul>{warnings}</ul>
<p class="note">Execution authorized: NO. {esc(result['notice'])}</p></section></html>'''


def main(argv=None):
    parser=argparse.ArgumentParser(prog='warden repair-plan',description='Inspect a proposed repair without changing art or executing generation')
    parser.add_argument('--project',required=True)
    parser.add_argument('--plan',required=True,help='JSON RepairPlan; see docs/repair-preflight.md')
    parser.add_argument('--html',help='Create a new self-contained visual report; will not overwrite files')
    parser.add_argument('--ask-jev',action='store_true',help='Request one advisory model selection; requires separate spend authorization')
    parser.add_argument('--authorize-decision-spend',action='store_true',help='Authorize one Jev query only, NOT generation or publication')
    args=parser.parse_args(argv)
    try:
        path=Path(args.plan)
        if path.stat().st_size>64000:raise ValueError('plan JSON exceeds 64 KB')
        if args.html and Path(args.html).exists():raise ValueError('HTML output already exists; use a new filename')
        request=json.loads(path.read_text(encoding='utf-8'))
        result=inspect_plan(args.project,request,ask_jev=args.ask_jev,authorize_decision_spend=args.authorize_decision_spend)
        if args.html:
            output=Path(args.html);output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('x',encoding='utf-8') as f:f.write(render_report(result))
            result['html_report']=str(output.resolve())
        print(json.dumps(result,indent=2))
        return 2 if result['status']=='blocked' else 0
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'error','error':str(exc),'execution_authorized':False}))
        return 2
