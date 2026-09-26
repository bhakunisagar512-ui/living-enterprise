"""The tools agents may use. They are the ONLY way an agent can see company data or the outside world.

Security:
  * Documents can only be read from the data folder, by plain file name (no paths).
  * Document text is returned inside an UNTRUSTED block and scanned for injection attempts.
  * Maths is done in code (SLA calculator), so the AI cannot miscount.
"""
import re

from crewai.tools import tool

from . import config
from .context import active_run
from .fx import FxError, get_rate
from .security import fence, scan_injection

DOC_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}\.txt$")


def indian_rupees(amount: float) -> str:
    """Formats 2568000 as 'Rs 25,68,000'."""
    whole = str(int(round(amount)))
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        whole = f"{head},{tail}"
    return f"Rs {whole}"


def _flag_injection(source: str, text: str) -> None:
    """Raises a warning (and records it for the Validator) when outside text looks like an attack."""
    run = active_run()
    for finding in scan_injection(text):
        if run is not None:
            run.flag_security(source, finding)
        else:
            print(f"  [!] Possible prompt injection in {source}: {finding}")


def resolve_document(filename: str):
    """Returns the Path of a company document, or None if the name is not allowed / not found.
    Only plain names like 'contract.txt' inside the data folder are accepted."""
    name = str(filename).strip()
    if not DOC_NAME.fullmatch(name):
        return None
    data_dir = config.DATA_DIR.resolve()
    path = (data_dir / name).resolve()
    if path.parent != data_dir or not path.is_file():
        return None
    return path


def list_documents_text() -> str:
    lines = []
    for p in sorted(config.DATA_DIR.glob("*.txt")):
        if not DOC_NAME.fullmatch(p.name):
            continue
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        title = text.splitlines()[0].strip()[:120] if text else "(empty file)"
        lines.append(f"{p.name}  -  {title}")
    listing = "\n".join(lines) or "(no documents found)"
    _flag_injection("the document list", listing)
    return fence("document list", listing)


def read_document_text(filename: str) -> str:
    path = resolve_document(filename)
    if path is None:
        return (f"'{str(filename)[:80]}' is not an available document. Use a plain file name from "
                f"'List company documents', for example 'contract.txt'.")
    text = path.read_text(encoding="utf-8", errors="replace")
    note = ""
    if len(text) > config.MAX_DOC_CHARS:
        text = text[:config.MAX_DOC_CHARS]
        note = f"\n(Document cut at {config.MAX_DOC_CHARS} characters.)"
    _flag_injection(path.name, text)
    return fence(path.name, text) + note


def sla_credit(monthly_uptimes: str, sla_target_percent: float,
               monthly_fee: float, credit_percent: float) -> str:
    pairs = re.findall(r"([A-Za-z]{3,9})\s*[:=]?\s*(\d{2,3}(?:\.\d+)?)\s*%?", monthly_uptimes)
    if not pairs:
        return "ERROR: no 'Month value%' pairs found. Copy the uptime line exactly as written."
    target = float(sla_target_percent)
    missed = [(m, float(v)) for m, v in pairs if float(v) < target]
    met = [m for m, v in pairs if float(v) >= target]
    credit_each = float(monthly_fee) * float(credit_percent) / 100
    total = credit_each * len(missed)
    missed_txt = ", ".join(f"{m} ({v}%)" for m, v in missed) or "none"
    return (
        f"Months checked: {len(pairs)}. SLA target: {sla_target_percent}%.\n"
        f"Months BELOW target (credit owed): {missed_txt}.\n"
        f"Months at or above target (no credit): {', '.join(met) or 'none'}.\n"
        f"Credit per missed month: {indian_rupees(credit_each)}. "
        f"TOTAL CREDIT OWED: {indian_rupees(total)}."
    )


def convert(amount: float, from_currency: str, to_currency: str = "INR") -> str:
    src, dst = str(from_currency).strip().upper(), str(to_currency).strip().upper()
    if not (re.fullmatch(r"[A-Z]{3}", src) and re.fullmatch(r"[A-Z]{3}", dst)):
        return "ERROR: currencies must be 3-letter codes such as USD or INR."
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return "ERROR: amount must be a number, for example 2200."
    if not 0 <= amount <= 1e12:
        return "ERROR: amount is out of range."
    if src == dst:
        return f"{amount} {src} = {amount} {dst} (same currency, no conversion needed)."
    try:
        fx = get_rate(src, dst)
    except FxError:
        return (f"ERROR: exchange rate {src}->{dst} is unavailable (live APIs failed, no cached rate). "
                f"Report this under MISSING.")
    converted = amount * fx["rate"]
    shown = indian_rupees(converted) if dst == "INR" else f"{converted:,.2f} {dst}"
    status = ("LIVE rate" if fx["verified"] else
              "UNVERIFIED rate (live APIs were down) - any figure using it must say it is indicative")
    return (f"{amount:,.2f} {src} = {shown} at 1 {src} = {fx['rate']} {dst}.\n"
            f"Source: {fx['source']}, rates dated {fx['date']}. Status: {status}.")


# ---------- The same functions, exposed to CrewAI agents ----------
@tool("List company documents")
def list_documents() -> str:
    """Lists all company documents with the title line of each, so you know what each file contains."""
    return list_documents_text()


@tool("Read company document")
def read_document(filename: str) -> str:
    """Reads one company document by its plain file name, for example 'contract.txt'."""
    return read_document_text(filename)


@tool("SLA credit calculator")
def sla_credit_calculator(monthly_uptimes: str, sla_target_percent: float,
                          monthly_fee: float, credit_percent: float) -> str:
    """Works out which months missed the uptime SLA and the exact service credit owed.
    monthly_uptimes: copy the uptime line(s) from the document, e.g. 'Nov 99.95% | Dec 99.92% | May 99.42%'.
    sla_target_percent: e.g. 99.9. monthly_fee: fee per month in rupees, e.g. 200000.
    credit_percent: credit per missed month, e.g. 5."""
    return sla_credit(monthly_uptimes, sla_target_percent, monthly_fee, credit_percent)


@tool("Currency converter (live)")
def convert_currency(amount: float, from_currency: str, to_currency: str = "INR") -> str:
    """Converts an amount between currencies using a LIVE exchange-rate API.
    Only use it when a document shows an amount in a foreign currency (e.g. USD).
    amount: e.g. 2200. from_currency: 3-letter code, e.g. 'USD'. to_currency: e.g. 'INR'."""
    return convert(amount, from_currency, to_currency)


RETRIEVER_TOOLS = [list_documents, read_document, sla_credit_calculator, convert_currency]
