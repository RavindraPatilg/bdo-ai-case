"""LLM layer: prompt construction, the call, and strict validation.

The model is given only the collected facts (each with its fact_id and
source) and must ground every suggestion in those fact IDs. The raw
JSON is validated against the schema and the known fact IDs before use.
"""
import json
import logging
import re
from pathlib import Path as _Path

from openai import AsyncOpenAI

from .config import settings
from .models import Fact, validate_llm_output

logger = logging.getLogger("app.llm")

SYSTEM_PROMPT = (
    "You are a senior AI advisor for BDO Norway. You analyse Norwegian "
    "companies and suggest 2-3 concrete, realistic AI use cases where AI "
    "can create value for the company. Rules you must follow:\n"
    "1. Ground every use case ONLY in the documented facts provided. "
    "Reference the fact_id of each fact you rely on in based_on_fact_ids. "
    "Every use case must cite at least one relevant registry fact "
    "(brreg:*) in addition to any website content (websearch:text). "
    "Do not cite brreg:orgnr or brreg:name merely to satisfy this rule; "
    "the registry fact must genuinely support the suggestion. Note that "
    "industry-level suggestions may legitimately cite brreg:nace, and "
    "you always have at least that fact, so never return an empty "
    "use_cases list.\n"
    "2. Never invent facts, never use world knowledge about the specific "
    "company beyond the given facts. If the facts are thin, say so in the "
    "limitations.\n"
    "3. In limitations, describe the evidence base accurately: name the "
    "registry facts and, when a website excerpt was provided, the website. "
    "Never claim the analysis is based solely on one source when facts "
    "from several sources were used.\n"
    "4. Stay qualitative everywhere. Never invent percentages, monetary "
    "figures, dates or targets (for example 'net-zero by 2050') that no "
    "provided fact supports, in expected_value, description or title. "
    "Unsourced specifics in an audit context destroy trust.\n"
    "5. Return STRICT JSON only, matching this schema:\n"
    '{"use_cases": [{"title": str, "description": str, '
    '"expected_value": str, "based_on_fact_ids": [str]}], '
    '"limitations": [str]}\n'
    "Your ENTIRE reply must be exactly one JSON object: it must start with "
    "'{' and end with '}'. No markdown code fences, no intro sentence, no "
    "closing sentence, no text of any kind outside the JSON object."
)


def build_user_prompt(orgnr: str, facts: list[Fact], website_text: str,
                      website_fact_id: str = "") -> str:
    fact_lines = "\n".join(
        f"- fact_id={f.fact_id} | {f.label}: {f.value} (source: {f.source})"
        for f in facts
    )
    if not website_text:
        web_section = "No website text was available. Work only from the facts above."
    elif website_fact_id == "brreg:website":
        web_section = (
            "Excerpt from the company's registered homepage (documented as "
            "fact brreg:website):\n" + website_text[:3000]
            + "\n\nWhen a suggestion draws on this homepage content, cite "
            "brreg:website in its based_on_fact_ids."
        )
    else:
        web_section = (
            "Excerpt from the company's website, found through web search "
            "and verified against the registry facts (documented as fact "
            "websearch:text):\n" + website_text[:3000]
            + "\n\nWhen a suggestion draws on this website content, cite "
            "websearch:text in its based_on_fact_ids. Never cite brreg "
            "facts for content that came from this search."
        )
    return (
        f"Organisation number: {orgnr}\n\n"
        f"Documented facts:\n{fact_lines}\n\n"
        f"{web_section}\n\n"
        "Suggest 2-3 AI use cases grounded in these facts. For each use case, "
        "list the fact_ids it builds on. Also list limitations of this analysis."
    )


async def analyse_with_llm(orgnr: str, facts: list[Fact], website_text: str,
                           website_fact_id: str = "") -> tuple[dict, str]:
    """Call the configured LLM. Returns (validated_output_dict, model_name).

    Raises RuntimeError after one retry if the output stays invalid; the
    caller turns that into a predictable error.
    """
    if settings.llm_provider.lower() == "mock":  # case-insensitive, so MOCK in .env also works
        return _mock_output(orgnr, facts), "mock"

    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )
    known_ids = {f.fact_id for f in facts}
    user_prompt = build_user_prompt(orgnr, facts, website_text, website_fact_id)
    last_error = ""
    for attempt in (1, 2):  # one structured retry on invalid output
        try:
            resp = await client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt
                     + (f"\n\nYour previous JSON failed validation: {last_error}. "
                        "Correct the specific problem. Each use case must cite at "
                        "least one relevant brreg fact that supports it, plus any "
                        "website fact it uses. Do not add a registry citation just "
                        "to pass validation; replace a website-only idea if needed. "
                        "Return ONLY the corrected JSON object, with no other text, "
                        "this skeleton: "
                        '{"use_cases": [{"title": "...", "description": "...", '
                        '"expected_value": "...", "based_on_fact_ids": ["..."]}], '
                        '"limitations": ["..."]}') if attempt == 2 else ""},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw, model_name = _extract_response(resp, settings.openai_model)
            try:
                data = json.loads(_strip_to_json(raw))
            except json.JSONDecodeError:
                # Log a snippet so the real content is visible in the terminal.
                logger.warning("llm_raw_unparseable orgnr=%s raw=%.300s", orgnr, raw)
                raise
            validated = validate_llm_output(data, known_ids)
            logger.info("llm_ok model=%s attempt=%d orgnr=%s", model_name, attempt, orgnr)
            return validated.model_dump(), model_name
        except Exception as exc:
            last_error = str(exc)[:300]
            logger.warning("llm_attempt_failed attempt=%d orgnr=%s error=%s",
                           attempt, orgnr, last_error)
            _write_debug_dump(orgnr, attempt, last_error,
                              locals().get("raw"))
    raise RuntimeError(f"LLM output invalid after retry: {last_error}")


def _write_debug_dump(orgnr: str, attempt: int, error: str, raw) -> None:
    """In development mode, save the failed exchange for debugging."""
    if settings.environment != "development":
        return
    try:
        raw_text = raw if isinstance(raw, str) else repr(raw)
        (_Path(settings.database_path).parent / "llm_debug_last.txt").write_text(
            "orgnr: " + orgnr + "\n"
            "attempt: " + str(attempt) + "\n"
            "error: " + error + "\n"
            "raw answer (first 2000 chars):\n"
            + (raw_text or "(empty)")[:2000],
            encoding="utf-8",
        )
    except Exception:
        logger.warning("llm_debug_dump_failed", exc_info=True)


def _strip_to_json(text: str) -> str:
    """Extract the JSON object from a model answer that may be wrapped in
    a Markdown code fence or prose. json_object mode is requested, but not
    every OpenAI-compatible provider honours it strictly.
    """
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        return t[start:end + 1]
    return t


def _extract_response(resp, default_model: str) -> tuple[str, str]:
    """Normalise the response shapes different providers return.

    The SDK normally gives a ChatCompletion object, but some providers
    surface the raw body as a plain string or dict instead; handle all
    these shapes so a provider quirk cannot break the pipeline.
    """
    if isinstance(resp, str):
        text = resp.strip()
        if text.startswith("{"):
            data = json.loads(text)
        else:
            # The body itself is the message content (plain text answer).
            return text, default_model
    elif isinstance(resp, dict):
        data = resp
        text = json.dumps(resp)
    else:
        # Standard ChatCompletion object.
        content = resp.choices[0].message.content or ""
        return content, getattr(resp, "model", None) or default_model

    if isinstance(data, dict) and data.get("choices"):
        msg = data["choices"][0].get("message") or {}
        return msg.get("content") or "", data.get("model") or default_model
    if isinstance(data, dict) and "use_cases" in data:
        # The body IS the model's JSON answer (content-type quirk).
        return text, default_model
    if isinstance(data, dict) and "error" in data:
        # The provider returned an error body: surface its own message.
        raise ValueError(f"Provider error: {data['error']}")
    raise ValueError(f"Unrecognised LLM response shape: {type(resp).__name__}")


def _mock_output(orgnr: str, facts: list[Fact]) -> dict:
    """Deterministic offline output for tests and no-key demos, grounded
    in the same fact IDs as the real prompt."""
    name = next((f.value for f in facts if f.fact_id == "brreg:name"), orgnr)
    nace = next((f.value for f in facts if f.fact_id == "brreg:nace"), "unknown industry")
    use_cases = []
    if any(f.fact_id == "brreg:nace" for f in facts):
        use_cases.append({
            "title": "Predictive maintenance on operational equipment",
            "description": (
                f"For {name} ({nace}), build anomaly-detection models on operational "
                "and sensor data to predict equipment failures before they occur."),
            "expected_value": "Fewer unplanned stoppages and lower maintenance cost.",
            "based_on_fact_ids": ["brreg:name", "brreg:nace"],
            "is_assessment": True,
        })
        use_cases.append({
            "title": "Automated document intake and summarisation",
            "description": "Use an LLM pipeline to classify and summarise incoming operational and administrative documents, reducing manual processing time.",
            "expected_value": "Hours saved per week on administrative processing.",
            "based_on_fact_ids": ["brreg:nace", "brreg:orgform"],
            "is_assessment": True,
        })
    else:
        use_cases.append({
            "title": "Document intelligence from public filings",
            "description": f"Extract and structure information from available public documents for {orgnr}.",
            "expected_value": "Faster onboarding of the company as a client.",
            "based_on_fact_ids": ["brreg:name"],
            "is_assessment": True,
        })
    return {"use_cases": use_cases,
            "limitations": ["DEMO MODE: these use cases are prewritten templates, not generated by an LLM.",
                            "This mock provider uses Brreg facts only; website text is not analysed.",
                            "Suggestions require human validation before use."]}
