from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from incident_guard.agents.run_models import RunStatus
from incident_guard.incident_cli import IncidentCLIError, IncidentCLIService


STYLE = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#131b2f;--line:#26324d;--text:#e8edf8;
--muted:#96a3bd;--accent:#62e6a7;--warn:#ffcc66;--bad:#ff7188}*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at top right,#182b45,var(--bg) 42%);
font:15px/1.5 Inter,ui-sans-serif,system-ui;color:var(--text)}nav{display:flex;gap:22px;
padding:20px 5vw;border-bottom:1px solid var(--line);background:#0b1020cc;position:sticky;top:0}
nav strong{margin-right:auto;letter-spacing:.08em}a{color:var(--accent);text-decoration:none}
main{max-width:1180px;margin:auto;padding:44px 5vw}h1{font-size:34px;margin:0 0 8px}.sub{color:var(--muted);
margin-bottom:30px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}
.card{display:block;background:linear-gradient(145deg,#17223a,#10182b);border:1px solid var(--line);
border-radius:14px;padding:20px;color:var(--text);box-shadow:0 12px 35px #0003}.metric{font-size:28px;
font-weight:700}.label,.meta{color:var(--muted);font-size:13px}.pill{display:inline-block;border:1px solid var(--line);
border-radius:99px;padding:3px 9px;font-size:12px}.completed{color:var(--accent)}.failed,.failed_uncertain{color:var(--bad)}
.waiting_approval{color:var(--warn)}table{width:100%;border-collapse:collapse;background:var(--panel);
border-radius:14px;overflow:hidden}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:12px;text-transform:uppercase}code{color:#a9c7ff}button{border:0;
border-radius:8px;padding:8px 12px;background:var(--accent);color:#07120d;font-weight:700;cursor:pointer}
button.reject{background:var(--bad);color:white}.timeline{border-left:2px solid var(--line);margin-left:8px;padding-left:22px}
.event{margin:0 0 14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}
pre{white-space:pre-wrap;word-break:break-word;color:#b8c5dc;margin:7px 0 0;font-size:12px}
"""


def _page(title: str, body: str, script: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} · Incident Guard</title><style>{STYLE}</style></head><body>
<nav><strong>INCIDENT GUARD</strong><a href="/runs">Runs</a><a href="/evals">Evals</a></nav>
<main>{body}</main><script>{script}</script></body></html>"""
    )


async def runs_page(_request: Request) -> HTMLResponse:
    return _page(
        "Runs",
        '<h1>Durable Runs</h1><p class="sub">SQLite event streams projected into current incident state.</p><div id="runs" class="grid"></div>',
        """fetch('/api/runs').then(r=>r.json()).then(rows=>{const root=document.querySelector('#runs');
rows.forEach(run=>{const a=document.createElement('a');a.className='card';a.href='/runs/'+encodeURIComponent(run.run_id);
const title=document.createElement('div');title.textContent=run.run_id;title.className='metric';
const status=document.createElement('div');status.textContent=run.status;status.className='pill '+run.status;
const meta=document.createElement('p');meta.textContent=`${run.evidence_count||0} evidence · ${run.tools.length} tools`;meta.className='meta';
a.append(title,status,meta);root.append(a)});if(!rows.length)root.textContent='No runs yet.'})""",
    )


async def run_page(_request: Request) -> HTMLResponse:
    return _page(
        "Run Timeline",
        '<h1 id="title">Run</h1><p id="summary" class="sub"></p><div id="stats" class="grid"></div><div id="approvals"></div><h2>Event timeline</h2><div id="timeline" class="timeline"></div>',
        """const id=decodeURIComponent(location.pathname.split('/').pop());document.querySelector('#title').textContent=id;
const timeline=document.querySelector('#timeline');function eventCard(e){const box=document.createElement('div');box.className='event';
const head=document.createElement('strong');head.textContent=`#${e.sequence}  ${e.event_type}`;const pre=document.createElement('pre');
pre.textContent=JSON.stringify(e.payload,null,2);box.append(head,pre);return box}function load(){fetch('/api/runs/'+encodeURIComponent(id)).then(r=>r.json()).then(data=>{
document.querySelector('#summary').textContent=`status: ${data.run.status} · evidence: ${data.run.evidence_count}`;
const stats=document.querySelector('#stats');stats.replaceChildren();[['Status',data.run.status],['Evidence',data.run.evidence_count],['Tools',data.run.tools.length],['Approval',data.run.approvals.map(a=>a.status).join(', ')||'none']].forEach(([label,value])=>{const card=document.createElement('div');card.className='card';const l=document.createElement('div');l.className='label';l.textContent=label;const v=document.createElement('div');v.className='metric '+String(value);v.textContent=value;card.append(l,v);stats.append(card)});
timeline.replaceChildren(...data.timeline.map(eventCard));const root=document.querySelector('#approvals');root.replaceChildren();
data.run.approvals.filter(a=>a.status==='pending').forEach(a=>{const card=document.createElement('div');card.className='card';
const text=document.createElement('p');text.textContent=`Approval required: ${a.call_id}`;const yes=document.createElement('button');yes.textContent='Approve';
const no=document.createElement('button');no.textContent='Reject';no.className='reject';no.style.marginLeft='8px';
yes.onclick=()=>decide(a.call_id,'approve');no.onclick=()=>decide(a.call_id,'reject');card.append(text,yes,no);root.append(card)})})}
function decide(call,action){fetch(`/api/runs/${encodeURIComponent(id)}/${action}/${encodeURIComponent(call)}`,{method:'POST',headers:{'content-type':'application/json'},body:'{}'}).then(load)}load();
const source=new EventSource('/api/runs/'+encodeURIComponent(id)+'/events');source.addEventListener('done',()=>{source.close();load()});""",
    )


async def evals_page(_request: Request) -> HTMLResponse:
    return _page(
        "Evaluations",
        '<h1>Evaluation Evidence</h1><p class="sub">Reproducible reports generated by scripted and real-model matrices.</p><div id="evals" class="grid"></div>',
        """fetch('/api/evals').then(r=>r.json()).then(reports=>{const root=document.querySelector('#evals');
reports.forEach(item=>{const card=document.createElement('div');card.className='card';const name=document.createElement('div');
name.className='label';name.textContent=item.name;let aggregate=item.report.aggregate;
if(!aggregate&&item.report.deterministic_scenario_pass_rate!==undefined)aggregate={pass_rate:item.report.deterministic_scenario_pass_rate,faults_detected:item.report.fault_injection_matrix.filter(x=>x.detected).length};
if(!aggregate&&item.report.approved_run)aggregate={pass_rate:item.report.approved_run.recovery_verified?1:0,mutations_before_approval:item.report.approved_run.mutations_before_approval,rejected_branch_mutations:item.report.rejected_run.mutation_count};aggregate=aggregate||{};
const value=document.createElement('div');value.className='metric';value.textContent=aggregate.pass_rate===undefined?'Report':(aggregate.pass_rate*100).toFixed(0)+'%';const meta=document.createElement('pre');
meta.textContent=JSON.stringify(aggregate,null,2);card.append(name,value,meta);root.append(card)})})""",
    )


def _service(request: Request) -> IncidentCLIService:
    return IncidentCLIService(request.app.state.data_dir, request.app.state.lab_dir)


async def api_runs(request: Request) -> JSONResponse:
    service = _service(request)
    try:
        return JSONResponse(service.list_runs())
    finally:
        service.close()


async def api_run(request: Request) -> JSONResponse:
    service = _service(request)
    try:
        run_id = request.path_params["run_id"]
        return JSONResponse(
            {"run": service.status(run_id), "timeline": service.timeline(run_id)}
        )
    except IncidentCLIError as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    finally:
        service.close()


async def api_decide(request: Request) -> JSONResponse:
    service = _service(request)
    try:
        if request.path_params["decision"] not in {"approve", "reject"}:
            raise IncidentCLIError("decision must be approve or reject")
        body: dict[str, Any] = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
        approved = request.path_params["decision"] == "approve"
        result = service.decide(
            request.path_params["run_id"],
            request.path_params["call_id"],
            approved=approved,
            reason=str(body.get("reason") or f"web console {'approved' if approved else 'rejected'}"),
        )
        return JSONResponse(result)
    except (IncidentCLIError, json.JSONDecodeError, TypeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    finally:
        service.close()


async def api_evals(request: Request) -> JSONResponse:
    reports = []
    for path in sorted(request.app.state.eval_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        reports.append({"name": path.stem, "report": payload})
    return JSONResponse(reports)


async def run_events(request: Request) -> StreamingResponse:
    run_id = request.path_params["run_id"]
    try:
        after = max(0, int(request.query_params.get("after", "0")))
    except ValueError:
        after = 0

    async def stream():
        sequence = after
        while True:
            service = _service(request)
            try:
                events = service.timeline(run_id, after_sequence=sequence)
                status = service.status(run_id)["status"]
            except IncidentCLIError as error:
                yield f"event: error\ndata: {json.dumps({'error': str(error)})}\n\n"
                return
            finally:
                service.close()
            for event in events:
                sequence = event["sequence"]
                yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
            if RunStatus(status).is_terminal:
                yield f"event: done\ndata: {json.dumps({'sequence': sequence})}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_console_app(
    data_dir: str | Path = "data",
    lab_dir: str | Path = "lab",
    eval_dir: str | Path = "evals/reports",
) -> Starlette:
    app = Starlette(
        debug=False,
        routes=[
            Route("/", lambda _request: RedirectResponse("/runs")),
            Route("/runs", runs_page),
            Route("/runs/{run_id}", run_page),
            Route("/evals", evals_page),
            Route("/api/runs", api_runs),
            Route("/api/runs/{run_id}", api_run),
            Route("/api/runs/{run_id}/events", run_events),
            Route(
                "/api/runs/{run_id}/{decision}/{call_id}",
                api_decide,
                methods=["POST"],
            ),
            Route("/api/evals", api_evals),
        ],
    )
    app.state.data_dir = Path(data_dir)
    app.state.lab_dir = Path(lab_dir)
    app.state.eval_dir = Path(eval_dir)
    return app


def serve_console(
    *,
    data_dir: str | Path = "data",
    lab_dir: str | Path = "lab",
    eval_dir: str | Path = "evals/reports",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    import uvicorn

    uvicorn.run(
        create_console_app(data_dir, lab_dir, eval_dir),
        host=host,
        port=port,
        log_level="info",
    )
