"""FastAPI application: the API and the simple interface.

Security and cost protection: orgnr validation, per-IP rate limiting on
/analyze, optional access token, and the analysis cache so repeated
orgnr never triggers a new LLM call. Logs show the sources and model
behind each analysis.
"""
import logging
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import settings
from .llm import analyse_with_llm
from .models import Analysis, SourceStatus
from .sources import fetch_brreg, get_unstructured_source
from .store import get_analysis, save_analysis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("app.main")

app = FastAPI(title="BDO AI Case - AI opportunity analyser", version="1.0.0")


def _rate_limit_key(request: Request) -> str:
    """Per-client key for the rate limiter.

    Behind a cloud ingress every request arrives from the same internal
    proxy IP, so prefer the forwarded client IP when the edge provides
    it; fall back to the connection address otherwise.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)
app.state.limiter = limiter


def _check_access_token(request: Request) -> None:
    """If APP_ACCESS_TOKEN is set, /analyze requires the header
    X-Access-Token to match."""
    if settings.app_access_token:
        supplied = request.headers.get("x-access-token", "")
        if not secrets.compare_digest(supplied, settings.app_access_token):
            raise HTTPException(status_code=401, detail="Invalid access token")


def _validate_orgnr(orgnr: str) -> str:
    digits = (orgnr or "").replace(" ", "")
    if len(digits) != 9 or not digits.isdigit():
        raise HTTPException(status_code=400, detail="Organisation number must be 9 digits.")
    return digits


def _weight_mod11(digits9: str) -> bool:
    """Validate the Norwegian orgnr check digit (MOD11), rejecting typos
    before any external call."""
    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(digits9, weights))
    remainder = total % 11
    check = 0 if remainder == 0 else 11 - remainder
    return int(digits9[8]) == check


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index() -> HTMLResponse:
    """The simple interface: one form, one result page."""
    return HTMLResponse(INDEX_HTML)


@app.api_route("/no", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index_no() -> HTMLResponse:
    """Norsk grensesnitt: samme funksjonalitet som forsiden."""
    return HTMLResponse(NO_INDEX_HTML)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": settings.environment}


@app.post("/analyze")
@limiter.limit(f"{settings.rate_limit_per_hour}/hour")
async def analyze(request: Request, body: dict) -> dict:
    """Full pipeline for one organisation number:
    Brreg facts -> website context -> LLM assessment -> saved analysis.
    Repeated orgnr are served from the cache without a new LLM call.
    """
    _check_access_token(request)
    orgnr = _validate_orgnr(str(body.get("orgnr", "")))
    if not _weight_mod11(orgnr):
        raise HTTPException(status_code=400, detail="Invalid organisation number (check digit failed).")

    cached = get_analysis(orgnr)
    if cached:
        logger.info("cache_hit orgnr=%s", orgnr)
        return {"cached": True, **cached.model_dump()}

    sources: list[SourceStatus] = []
    facts = []
    async with httpx.AsyncClient() as client:
        brreg_facts, brreg_status = await fetch_brreg(client, orgnr)
        sources.append(brreg_status)
        facts.extend(brreg_facts)
        website_fact = next((f for f in brreg_facts if f.fact_id == "brreg:website"), None)
        site_url = (website_fact.value if website_fact and
                    website_fact.value.startswith("http") else "")
        if not site_url and website_fact and website_fact.value not in ("", "(none registered)"):
            site_url = f"https://{website_fact.value}"
        # Unstructured source. When Brreg has no registered homepage,
        # the configured search provider finds the company's site and
        # verify.py checks the URL against the registry facts before use.
        text, web_statuses, citable_id, text_fact = await get_unstructured_source(
            client, facts, site_url)
        sources.extend(web_statuses)       # statuses start with the website status (brreg already added)
        if text_fact:
            facts.append(text_fact)        # searched page text becomes a citable websearch fact

    if not facts:
        logger.info("no_facts orgnr=%s", orgnr)
        raise HTTPException(status_code=404, detail=(
            "No base facts found for this organisation number "
            f"({'; '.join(s.detail for s in sources)})."
        ))

    try:
        llm_data, model_name = await analyse_with_llm(orgnr, facts, text, citable_id)
    except RuntimeError as exc:
        logger.error("llm_failed orgnr=%s error=%s", orgnr, exc)
        detail = ("The AI service returned an unusable answer after retries. "
                  "Please try again later.")
        if settings.environment == "development":
            detail += f" [debug: {exc}]"
        raise HTTPException(status_code=502, detail=detail)

    analysis = Analysis(
        orgnr=orgnr,
        company_name=next((f.value for f in facts if f.fact_id == "brreg:name"), orgnr),
        generated_at=datetime.now(timezone.utc),
        model=model_name,
        sources=sources,
        facts=facts,
        use_cases=llm_data["use_cases"],
        limitations=llm_data["limitations"],
    )
    save_analysis(analysis)
    logger.info("analysis_saved orgnr=%s model=%s sources=%s",
                orgnr, model_name, [f"{s.source}:{s.status}" for s in sources])
    return {"cached": False, **analysis.model_dump()}


@app.get("/analysis/{orgnr}")
def retrieve(orgnr: str) -> dict:
    """Retrieve a saved analysis without a new LLM call."""
    cached = get_analysis(_validate_orgnr(orgnr))
    if not cached:
        raise HTTPException(status_code=404, detail="No saved analysis for this orgnr.")
    return {"cached": True, **cached.model_dump()}


INDEX_HTML = """<!DOCTYPE html>
<html><head><title>AI opportunity analyser</title>
<style>
 body{font-family:Segoe UI,Arial,sans-serif;max-width:760px;margin:40px auto;color:#263746}
 input{padding:8px;font-size:16px;width:220px}
 button{padding:8px 16px;font-size:16px}
 .zone{border:1px solid #ccc;border-radius:6px;padding:10px;margin:10px 0}
 .facts{background:#EAF2F5}.assess{background:#FDF3D8}.limits{background:#FBEAEA}
 h3{margin:4px 0}
 .src{color:#566874;font-size:12px}
</style></head>
<body>
<h2>AI opportunity analyser</h2>
<p>Enter a Norwegian organisation number (9 digits). The app retrieves facts
from Brreg, reads the company website, and asks an LLM for grounded AI
opportunities. Facts, sources and the AI's own assessment are shown separately.</p>
<p class="src"><a href="/no">Norsk versjon</a></p>
<form onsubmit="run();return false">
 <input id="orgnr" placeholder="e.g. 9 digits" pattern="[0-9 ]{9,11}" maxlength="11" required>
 <button>Analyse</button>
</form>
<div id="out"></div>
<script>
async function run(){
 const out=document.getElementById('out'); out.innerHTML='Working... (Brreg + website + LLM)';
 const orgnr=document.getElementById('orgnr').value;
 try{
  const r=await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({orgnr})});
  if(!r.ok){out.innerHTML='<div class="zone limits">Error '+r.status+': '+(await r.json()).detail+'</div>';return;}
  const d=await r.json();
  const esc=(v)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let html='';
  if(d.model==='mock') html+='<div class="zone limits"><b>DEMO MODE:</b> Use-case text below is template output, not generated by a real LLM. Company facts are retrieved from the live sources shown.</div>';
  html+='<h3>'+esc(d.company_name)+' ('+esc(d.orgnr)+')</h3><p class="src">Model: '+esc(d.model)+
    ' | '+esc(new Date(d.generated_at).toLocaleString())+(d.cached?' | served from cache (no new LLM call)':'')+'</p>';
  html+='<div class="zone facts"><b>Documented facts (sources)</b>';
  for(const f of d.facts){if(f.fact_id==='websearch:text')continue;html+='<p><b>'+esc(f.label)+':</b> '+esc(f.value)+' <span class="src">['+esc(f.source)+' | '+esc(new Date(f.retrieved_at).toLocaleString())+']</span></p>';}
  html+='</div><div class="zone assess"><b>AI assessment</b>';
  for(const uc of d.use_cases){html+='<p><b>'+esc(uc.title)+'</b><br>'+esc(uc.description)+'<br><i>Expected value: '+
    esc(uc.expected_value)+'</i><br><span class="src">Based on: '+uc.based_on_fact_ids.map(esc).join(', ')+'</span></p>';}
  html+='</div><div class="zone facts"><b>Sources (incl. web search)</b><ul>';
  for(const s of d.sources){if(s.status!=='ok')continue;const short=(s.url||'').replace('https://data.brreg.no/enhetsregisteret/api/enheter/','Brreg entry ').replace('https://www.','');let what = (s.detail||'');if(what.indexOf('Extracted ')===0) what='';html+='<li><b>'+esc(s.source)+'</b> | '+esc(short)+' | '+esc(s.status)+(what?' | '+esc(what):'')+'</li>';}
  html+='</ul></div>';
  html+='</div><div class="zone limits"><b>Limitations</b><ul>';
  for(const l of d.limitations){html+='<li>'+esc(l)+'</li>';}
  html+='</ul></div>';
  out.innerHTML=html;
 }catch(e){out.innerHTML='<div class="zone limits">Request failed: '+e+'</div>';}
}
</script></body></html>"""


NO_INDEX_HTML = """<!DOCTYPE html>
<html lang="no"><head><title>AI-mulighetsanalysator</title>
<style>
 body{font-family:Segoe UI,Arial,sans-serif;max-width:760px;margin:40px auto;color:#263746}
 input{padding:8px;font-size:16px;width:220px}
 button{padding:8px 16px;font-size:16px}
 .zone{border:1px solid #ccc;border-radius:6px;padding:10px;margin:10px 0}
 .facts{background:#EAF2F5}.assess{background:#FDF3D8}.limits{background:#FBEAEA}
 h3{margin:4px 0}
 .src{color:#566874;font-size:12px}
</style></head>
<body>
<h2>AI-mulighetsanalysator</h2>
<p>Skriv inn et norsk organisasjonsnummer (9 siffer). Appen henter fakta fra Brreg, leser selskapets nettside og ber en LLM om begrunnede AI-muligheter. Fakta, kilder og AIens egen vurdering vises separat.</p>
<form onsubmit="run();return false">
 <input id="orgnr" placeholder="f.eks. 9 siffer" pattern="[0-9 ]{9,11}" maxlength="11" required>
 <button>Analyser</button>
</form>
<div id="out"></div>
<p class="src"><a href="/">English version</a></p>
<script>
const NO_LABELS={'Company name':'Selskapsnavn','Industry (NACE code)':'Næringskode (Brreg)','Organisation form':'Organisasjonsform','Registered place':'Registrert sted','Registered website':'Registrert nettside','Registered employees':'Registrerte ansatte'};
function label(v){return NO_LABELS[v]||v;}
async function run(){
 const out=document.getElementById('out'); out.innerHTML='Arbeider... (Brreg + nettside + LLM)';
 const orgnr=document.getElementById('orgnr').value;
 try{
  const r=await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({orgnr})});
  if(!r.ok){out.innerHTML='<div class="zone limits">Feil '+r.status+': '+(await r.json()).detail+'</div>';return;}
  const d=await r.json();
  const esc=(v)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let html='';
  if(d.model==='mock') html+='<div class="zone limits"><b>DEMO-MODUS:</b> Brukseksempelteksten under er mal, ikke generert av en ekte LLM. Fakta er hentet fra de live kildene som vises.</div>';
  html+='<h3>'+esc(d.company_name)+' ('+esc(d.orgnr)+')</h3><p class="src">Modell: '+esc(d.model)+
    ' | '+esc(new Date(d.generated_at).toLocaleString())+(d.cached?' | hentet fra hurtigbuffer (intet nytt LLM-kall)':'')+'</p>';
  html+='<div class="zone facts"><b>Dokumenterte fakta (kilder)</b>';
  for(const f of d.facts){if(f.fact_id==='websearch:text')continue;html+='<p><b>'+esc(label(f.label))+':</b> '+esc(f.value)+' <span class="src">['+esc(f.source)+' | '+esc(new Date(f.retrieved_at).toLocaleString())+']</span></p>';}
  html+='</div><div class="zone assess"><b>AI-vurdering</b>';
  for(const uc of d.use_cases){html+='<p><b>'+esc(uc.title)+'</b><br>'+esc(uc.description)+'<br><i>Forventet verdi: '+
    esc(uc.expected_value)+'</i><br><span class="src">Bygger på: '+uc.based_on_fact_ids.map(esc).join(', ')+'</span></p>';}
  html+='</div><div class="zone facts"><b>Kilder (inkl. netsøk)</b><ul>';
  for(const s of d.sources){if(s.status!=='ok')continue;const short=(s.url||'').replace('https://data.brreg.no/enhetsregisteret/api/enheter/','Brreg entry ').replace('https://www.','');let what = (s.detail||'');if(what.indexOf('Extracted ')===0) what='';html+='<li><b>'+esc(s.source)+'</b> | '+esc(short)+' | '+esc(s.status)+(what?' | '+esc(what):'')+'</li>';}
  html+='</ul></div>';
  html+='</div><div class="zone limits"><b>Begrensninger</b><ul>';
  for(const l of d.limitations){html+='<li>'+esc(l)+'</li>';}
  html+='</ul></div>';
  out.innerHTML=html;
 }catch(e){out.innerHTML='<div class="zone limits">Forespørselen mislyktes: '+e+'</div>';}
}
</script></body></html>"""
