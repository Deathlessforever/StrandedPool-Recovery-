from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
import json
import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB_PATH = Path(os.getenv("STRANDEDPOOL_DB", "strandedpool.db"))

app = FastAPI(
    title="StrandedPool Recovery",
    description="A working travel-disruption recovery MVP. Demo inventory is simulated, not live availability.",
    version="0.1.0",
)

class Booking(BaseModel):
    origin: str = Field(default="Mysuru", min_length=1, max_length=80)
    destination: str = Field(default="Mangaluru", min_length=1, max_length=80)
    departure_time: str = Field(default="2026-10-21T09:00", description="Local ISO date-time")
    arrival_deadline: str = Field(default="2026-10-21T18:00", description="Local ISO date-time")
    budget_inr: int = Field(default=1500, ge=0, le=1000000)
    travellers: int = Field(default=1, ge=1, le=100)
    disruption: Literal["cancelled", "delayed", "weather"] = "cancelled"
    include_pooling: bool = True

class Alternative(BaseModel):
    mode: str
    provider: str
    departure: str
    arrival: str
    duration_minutes: int
    cost_per_person_inr: int
    capacity: int
    available_seats: int
    simulated: bool = True
    feasible: bool
    reasons: list[str]
    score: float

class RecoveryPlan(BaseModel):
    status: str
    data_mode: str
    original_journey: dict
    disruption_summary: str
    alternatives: list[Alternative]
    pool_proposals: list[dict]
    downstream_risks: list[dict]
    action_pack: list[dict]
    note: str

class ResolutionRequest(BaseModel):
    selected_provider: str = Field(min_length=1, max_length=100)
    decision_note: str = Field(default="", max_length=1000)

def db_connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection

def init_db():
    with db_connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS saved_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                selected_provider TEXT,
                decision_note TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES saved_plans(id)
            )
        """)

init_db()

def parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Times must use local ISO format, e.g. 2026-10-21T09:00")

def build_plan(req: Booking) -> RecoveryPlan:
    departure = parse_time(req.departure_time)
    deadline = parse_time(req.arrival_deadline)
    if deadline <= departure:
        raise ValueError("Arrival deadline must be after departure time")

    # Fully reproducible demo options. These are deliberately labelled simulated.
    candidates = [
        {"mode":"Bus", "provider":"Demo Express", "offset":45, "duration":360, "cost":780, "capacity":40, "seats":18},
        {"mode":"Train", "provider":"Demo Rail", "offset":90, "duration":330, "cost":520, "capacity":300, "seats":0},
        {"mode":"Shared cab", "provider":"Demo Mobility", "offset":25, "duration":300, "cost":1450, "capacity":4, "seats":3},
        {"mode":"Bus", "provider":"Demo Nightliner", "offset":180, "duration":420, "cost":620, "capacity":36, "seats":12},
        {"mode":"Private cab", "provider":"Demo Taxi", "offset":15, "duration":285, "cost":5200, "capacity":4, "seats":4},
    ]
    alternatives = []
    for c in candidates:
        dep = departure + timedelta(minutes=c["offset"])
        arr = dep + timedelta(minutes=c["duration"])
        reasons = []
        if arr > deadline:
            reasons.append("Arrives after your deadline")
        if c["cost"] > req.budget_inr:
            reasons.append("Exceeds your budget")
        if c["seats"] < req.travellers:
            reasons.append(f"Only {c['seats']} demo seat(s) for {req.travellers} traveller(s)")
        feasible = not reasons
        # Lower cost and duration improve score; hard constraints remain separate.
        score = round(max(0.0, 100 - (c["cost"] / max(req.budget_inr, 1))*35 - c["duration"]/12), 1)
        alternatives.append(Alternative(
            mode=c["mode"], provider=c["provider"], departure=dep.isoformat(timespec="minutes"),
            arrival=arr.isoformat(timespec="minutes"), duration_minutes=c["duration"],
            cost_per_person_inr=c["cost"], capacity=c["capacity"], available_seats=c["seats"],
            feasible=feasible, reasons=reasons, score=score
        ))
    alternatives.sort(key=lambda a: (not a.feasible, -a.score, a.arrival))

    feasible = [a for a in alternatives if a.feasible]
    pools = []
    if req.include_pooling:
        for a in alternatives:
            if a.mode in ("Shared cab", "Private cab") and a.available_seats >= 2:
                pools.append({
                    "mode": a.mode, "provider": a.provider,
                    "suggested_group_size": min(a.available_seats, a.capacity),
                    "cost_per_person_inr": a.cost_per_person_inr,
                    "seats_remaining_after_request": max(0, a.available_seats-req.travellers),
                    "status": "SIMULATED PROPOSAL — no passengers have been contacted or booked"
                })

    downstream = [
        {"booking":"Hotel check-in", "risk":"High" if req.disruption == "cancelled" else "Medium",
         "suggested_action":"Notify the property of a possible late arrival; confirm its policy."},
        {"booking":"Pickup / onward connection", "risk":"High",
         "suggested_action":"Reconfirm pickup time and connection after selecting a recovery option."},
    ]
    actions = [
        {"title":"Hotel late-arrival message", "text":f"Hello, my journey from {req.origin} to {req.destination} has been disrupted. I may arrive later than planned. Could you please confirm the latest check-in time and whether my booking can be held?"},
        {"title":"Pickup change request", "text":f"My travel from {req.origin} to {req.destination} has been disrupted. Please let me update my pickup time once my replacement journey is confirmed."},
        {"title":"Refund / assistance request", "text":f"My original journey from {req.origin} to {req.destination} was affected by a {req.disruption} disruption. Please advise the available rebooking or refund options for my booking."},
    ]
    summary = {
        "cancelled":"The original service is treated as cancelled for this demo.",
        "delayed":"The original service is treated as delayed for this demo.",
        "weather":"Weather disruption is simulated; verify local advisories with official sources."
    }[req.disruption]
    return RecoveryPlan(
        status="alternatives_found" if feasible else "no_feasible_demo_option",
        data_mode="SIMULATED DEMO DATA",
        original_journey={"origin":req.origin, "destination":req.destination,
                          "departure":req.departure_time, "arrival_deadline":req.arrival_deadline,
                          "travellers":req.travellers, "budget_inr":req.budget_inr},
        disruption_summary=summary,
        alternatives=alternatives,
        pool_proposals=pools,
        downstream_risks=downstream,
        action_pack=actions,
        note="This MVP does not query live schedules, prices, seats, weather or booking systems. Verify all travel details with providers. It does not book, cancel or message anyone."
    )

@app.post("/api/recovery/saved-plans")
def save_recovery_plan(req: Booking):
    """Create a demo recovery plan and persist it locally for later review."""
    try:
        plan = build_plan(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connect() as connection:
        cursor = connection.execute(
            "INSERT INTO saved_plans(created_at, status, origin, destination, plan_json) VALUES (?, 'proposed', ?, ?, ?)",
            (now, req.origin, req.destination, plan.model_dump_json())
        )
        plan_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO audit_events(plan_id, created_at, event, detail) VALUES (?, ?, 'plan_created', ?)",
            (plan_id, now, "Generated a recovery proposal using simulated transport inventory.")
        )
    return {"plan_id": plan_id, "status": "proposed", "plan": plan}

@app.get("/api/recovery/saved-plans")
def list_saved_plans():
    """Return saved demo plans, newest first."""
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT id, created_at, status, origin, destination, selected_provider, decision_note FROM saved_plans ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}

@app.get("/api/recovery/saved-plans/{plan_id}")
def get_saved_plan(plan_id: int):
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")
        events = connection.execute(
            "SELECT created_at, event, detail FROM audit_events WHERE plan_id = ? ORDER BY id",
            (plan_id,)
        ).fetchall()
    item = dict(row)
    item["plan"] = json.loads(item.pop("plan_json"))
    item["audit_events"] = [dict(event) for event in events]
    return item

@app.post("/api/recovery/saved-plans/{plan_id}/resolve")
def resolve_saved_plan(plan_id: int, request: ResolutionRequest):
    """Record the traveller's chosen demo option. This does not book transport."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connect() as connection:
        row = connection.execute("SELECT plan_json, status FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")
        plan = json.loads(row["plan_json"])
        option = next((a for a in plan["alternatives"] if a["provider"] == request.selected_provider), None)
        if option is None:
            raise HTTPException(status_code=422, detail="Provider is not an alternative in this saved plan")
        if not option["feasible"]:
            raise HTTPException(status_code=422, detail="Cannot select an alternative that fails the demo constraints")
        connection.execute(
            "UPDATE saved_plans SET status = 'decision_recorded', selected_provider = ?, decision_note = ? WHERE id = ?",
            (request.selected_provider, request.decision_note, plan_id)
        )
        detail = json.dumps({"selected_provider": request.selected_provider, "note": request.decision_note})
        connection.execute(
            "INSERT INTO audit_events(plan_id, created_at, event, detail) VALUES (?, ?, 'traveller_decision_recorded', ?)",
            (plan_id, now, detail)
        )
    return {
        "plan_id": plan_id,
        "status": "decision_recorded",
        "selected_provider": request.selected_provider,
        "note": "Decision saved locally only. No transport was booked and no provider was contacted."
    }

@app.get("/api/recovery/saved-plans/{plan_id}/audit")
def get_plan_audit(plan_id: int):
    with db_connect() as connection:
        exists = connection.execute("SELECT id FROM saved_plans WHERE id = ?", (plan_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Saved recovery plan not found")
        events = connection.execute(
            "SELECT created_at, event, detail FROM audit_events WHERE plan_id = ? ORDER BY id",
            (plan_id,)
        ).fetchall()
    return {"plan_id": plan_id, "events": [dict(event) for event in events]}

@app.get("/health")
def health():
    return {"ok": True, "service": "StrandedPool Recovery", "data_mode": "SIMULATED DEMO DATA"}

@app.post("/api/recovery/plan", response_model=RecoveryPlan)
def recovery_plan(req: Booking):
    try:
        return build_plan(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StrandedPool | Travel recovery</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141d31;--muted:#a7b4ca;--line:#293752;--accent:#77e0c1;--warn:#ffd27d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0%,#1b3150 0,transparent 40%),var(--bg);font:15px/1.5 system-ui,sans-serif;color:#eef4ff}
main{max-width:1100px;margin:auto;padding:28px 18px 60px}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:12px}
h1{font-size:clamp(32px,6vw,54px);line-height:1.05;margin:10px 0}p{color:var(--muted)}.notice{border:1px solid #806b36;background:#302817;color:#ffe3a4;padding:12px 14px;border-radius:12px;margin:22px 0}
.layout{display:grid;grid-template-columns:320px 1fr;gap:18px}.panel{background:#111a2b;border:1px solid var(--line);border-radius:18px;padding:18px;min-width:0}
label{display:block;color:#c7d3e6;font-size:13px;margin:12px 0 5px}input,select{width:100%;padding:11px;border-radius:9px;border:1px solid #35435d;background:#0b1323;color:#fff}
button{margin-top:16px;width:100%;border:0;border-radius:10px;background:var(--accent);color:#09241d;font-weight:800;padding:13px;cursor:pointer}button:disabled{opacity:.6}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.option{border:1px solid var(--line);border-radius:14px;padding:14px;background:#0d1626}.option.best{border-color:var(--accent)}.tag{display:inline-block;font-size:11px;color:#ffd27d;border:1px solid #806b36;border-radius:30px;padding:3px 7px;margin:2px 0 7px}
.money{font-size:25px;font-weight:800}.muted{color:var(--muted);font-size:13px}.good{color:var(--accent)}.bad{color:#ff9d9d}.section{margin-top:20px}ul{padding-left:20px}.action{border-top:1px solid var(--line);padding:12px 0}.action button{width:auto;margin:8px 0 0;padding:8px 12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0a1220;border-radius:9px;padding:10px;color:#d7e4f7}
@media(max-width:760px){.layout{grid-template-columns:1fr}.cards{grid-template-columns:1fr}}
</style></head><body><main>
<div class="eyebrow">Predict · Pool · Recover</div><h1>Your journey changed.<br>Let's find a way forward.</h1>
<p>Travel-disruption recovery prototype with constraint-aware alternatives, group pooling ideas and ready-to-review messages.</p>
<div class="notice"><strong>Prototype mode:</strong> all fares, seats, operators and schedules below are simulated. Nothing is booked and no live travel data is queried.</div>
<div class="layout"><section class="panel"><h2>Your disrupted trip</h2>
<form id="form">
<label>From</label><input name="origin" value="Mysuru" required maxlength="80">
<label>To</label><input name="destination" value="Mangaluru" required maxlength="80">
<label>Original departure (local time)</label><input name="departure_time" type="datetime-local" value="2026-10-21T09:00" required>
<label>Must arrive by</label><input name="arrival_deadline" type="datetime-local" value="2026-10-21T18:00" required>
<label>Budget per traveller (₹)</label><input name="budget_inr" type="number" min="0" max="1000000" value="1500" required>
<label>Travellers</label><input name="travellers" type="number" min="1" max="100" value="2" required>
<label>Disruption type</label><select name="disruption"><option value="cancelled">Cancelled</option><option value="delayed">Delayed</option><option value="weather">Weather disruption</option></select>
<label><input type="checkbox" name="include_pooling" checked style="width:auto"> Include shared-ride proposals</label>
<button id="submit">Build recovery plan ↗</button><p id="error" role="alert"></p>
</form></section><section class="panel" id="results"><h2>Recovery workspace</h2><p>Enter your trip details and build a plan to see ranked alternatives, pooling proposals, downstream risks and message drafts.</p><div class="muted">No sign-in or API key required for the local demo.</div></section></div>
</main><script>
const form=document.querySelector('#form'), results=document.querySelector('#results'), err=document.querySelector('#error'), btn=document.querySelector('#submit'); let lastPayload=null;
const money=n=>'₹'+Number(n).toLocaleString('en-IN');
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
form.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';btn.disabled=true;btn.textContent='Building plan…';
const f=new FormData(form), body={origin:f.get('origin'),destination:f.get('destination'),departure_time:f.get('departure_time'),arrival_deadline:f.get('arrival_deadline'),budget_inr:Number(f.get('budget_inr')),travellers:Number(f.get('travellers')),disruption:f.get('disruption'),include_pooling:f.get('include_pooling')==='on'}; lastPayload=body;
try{const res=await fetch('/api/recovery/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await res.json();if(!res.ok)throw new Error(data.detail?.[0]?.msg||'Could not build plan');render(data)}catch(e){err.textContent=e.message||'Request failed'}finally{btn.disabled=false;btn.textContent='Build recovery plan ↗'}});
function render(d){const cards=d.alternatives.map((a,i)=>'<article class="option '+(i===0&&a.feasible?'best':'')+'"><span class="tag">'+(a.feasible?'CONSTRAINTS PASS':'NOT FEASIBLE')+'</span><h3>'+esc(a.mode)+' · '+esc(a.provider)+'</h3><div class="money">'+money(a.cost_per_person_inr)+'<span class="muted"> / person</span></div><p>'+esc(a.departure.replace('T',' '))+' → '+esc(a.arrival.replace('T',' '))+'</p><p>'+a.duration_minutes+' min · '+a.available_seats+' simulated seats</p>'+(a.reasons.length?'<ul class="bad">'+a.reasons.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'<p class="good">Fits budget, deadline and demo capacity.</p>')+'<p class="muted">Score '+a.score+'/100 · simulated inventory</p></article>').join('');
const pools=d.pool_proposals.length?d.pool_proposals.map(p=>'<div class="action"><strong>'+esc(p.mode)+' · '+esc(p.provider)+'</strong><p>Suggested group: '+p.suggested_group_size+' · '+money(p.cost_per_person_inr)+' per person</p><p class="muted">'+esc(p.status)+'</p></div>').join(''):'<p>No shared ride proposals in this scenario.</p>';
const risks=d.downstream_risks.map(r=>'<li><strong>'+esc(r.booking)+' — '+esc(r.risk)+'</strong><br>'+esc(r.suggested_action)+'</li>').join('');
const actions=d.action_pack.map((a,i)=>'<div class="action"><strong>'+esc(a.title)+'</strong><pre id="msg'+i+'">'+esc(a.text)+'</pre><button type="button" onclick="copyMsg('+i+')">Copy message</button></div>').join('');
results.innerHTML='<h2>Recovery plan</h2><p class="muted">'+esc(d.disruption_summary)+' · '+esc(d.status)+'</p><div class="tag">'+esc(d.data_mode)+'</div><h3>Ranked alternatives</h3><div class="cards">'+cards+'</div><div class="section"><h3>Pooling proposals</h3>'+pools+'</div><div class="section"><h3>Downstream booking risks</h3><ul>'+risks+'</ul></div><div class="section"><h3>Action pack</h3>'+actions+'</div><div class="notice">'+esc(d.note)+'</div><button type="button" onclick="savePlan()">Save plan to recovery history</button><button type="button" onclick="loadHistory()">View saved plans</button><div id="history"></div>'}
window.savePlan=async()=>{if(!lastPayload){alert('Build a plan first');return}try{const r=await fetch('/api/recovery/saved-plans',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lastPayload)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Save failed');alert('Saved recovery plan #'+d.plan_id);await loadHistory()}catch(e){alert(e.message||'Save failed')}};
window.loadHistory=async()=>{const target=document.querySelector('#history');if(!target)return;target.innerHTML='<p>Loading saved plans…</p>';try{const r=await fetch('/api/recovery/saved-plans');const d=await r.json();target.innerHTML='<h3>Saved recovery history</h3>'+(d.items.length?d.items.map(p=>'<div class="action"><strong>#'+p.id+' · '+esc(p.origin)+' → '+esc(p.destination)+'</strong><p>'+esc(p.status)+' · '+esc(p.created_at)+'</p>'+(p.selected_provider?'<p>Selected demo option: '+esc(p.selected_provider)+'</p>':'')+'<a href="/api/recovery/saved-plans/'+p.id+'" target="_blank" rel="noreferrer">Open plan details and audit log</a></div>').join(''):'<p>No saved plans yet.</p>')}catch(e){target.innerHTML='<p>Could not load saved history.</p>'}};
window.copyMsg=async i=>{const t=document.querySelector('#msg'+i).textContent;try{await navigator.clipboard.writeText(t);alert('Message copied')}catch(e){alert('Copy failed — select the text and copy manually')}};
</script></body></html>""")
