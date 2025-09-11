import os, re, json, smtplib, ssl, requests, time
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.utils import formatdate

# ---------- CONFIG ----------
LIST_URL = "https://team42api.herokuapp.com/passwordreset/database_models/urgentcustomeruploadedpatent/"
MODEL_PATH_SNIPPET = "/urgentcustomeruploadedpatent/"  # used to extract numeric IDs

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]     # your Gmail address
SMTP_PASS = os.environ["SMTP_PASS"]     # your Gmail App Password
TO_EMAIL  = os.environ["TO_EMAIL"]      # where alerts go

DJANGO_USERNAME = os.environ["DJANGO_USER"]
DJANGO_PASSWORD = os.environ["DJANGO_PASS"]

HUBSPOT_TOKEN       = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_COMPANY_ID  = os.environ["HUBSPOT_COMPANY_ID"]   # e.g. 10170876313
HUBSPOT_OWNER_ID    = os.environ["HUBSPOT_OWNER_ID"]     # e.g. 154662807

STATE_FILE = "state.json"  # remembered across runs by committing it back to the repo


# ---------- HELPERS ----------
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_seen_id": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())

def _looks_like_login(html: str) -> bool:
    h = html.lower()
    return ("csrfmiddlewaretoken" in h) and (
        ("name=\"username\"" in h) or ("id=\"id_username\"" in h) or ("name=\"email\"" in h)
    ) and (("name=\"password\"" in h) or ("id=\"id_password\"" in h))

def login_and_fetch(session: requests.Session) -> str:
    """
    Fetches LIST_URL; if redirected to a login form, submits it (generic parser),
    then fetches LIST_URL again.
    """
    r = session.get(LIST_URL, timeout=30, allow_redirects=True)
    r.raise_for_status()

    if not _looks_like_login(r.text):
        return r.text  # already public / session not needed

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("Login form not found.")
    action = form.get("action") or r.url
    login_url = requests.compat.urljoin(r.url, action)

    # Collect all login form inputs (keeps CSRF + next)
    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")

    # Fill username/password into probable fields
    for key in list(payload.keys()):
        low = key.lower()
        if ("user" in low and "name" in low) or (low == "email") or (low == "username"):
            payload[key] = DJANGO_USERNAME
        if "pass" in low:
            payload[key] = DJANGO_PASSWORD

    headers = {"Referer": r.url}
    r2 = session.post(login_url, data=payload, headers=headers, timeout=30, allow_redirects=True)
    r2.raise_for_status()

    r3 = session.get(LIST_URL, timeout=30)
    r3.raise_for_status()
    return r3.text

def parse_entries(html: str):
    """
    Returns [{id: int, ft_ref: str|None}].
    Detects a table, finds the 'FT PATENT REF' column (case-insensitive).
    Falls back to IDs only if needed.
    """
    soup = BeautifulSoup(html, "html.parser")
    entries = []

    table = soup.find("table")
    if table:
        # Get headers
        header_cells = table.select("thead th")
        if not header_cells:
            first_tr = table.find("tr")
            if first_tr:
                header_cells = first_tr.find_all(["th","td"])
        headers = [th.get_text(strip=True).upper() for th in (header_cells or [])]

        ft_idx = None
        for i, h in enumerate(headers):
            if "FT" in h and "PATENT" in h and "REF" in h:
                ft_idx = i
                break

        # Body rows
        body_rows = table.select("tbody tr") or table.find_all("tr")[1:]
        for tr in body_rows:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
            row_html = str(tr)
            m = re.search(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)(?:/|\"|')", row_html)
            if not m:
                a = tr.find("a", href=True)
                if a:
                    m = re.search(r"/(\d+)(?:/|$)", a["href"])
            if not m:
                continue
            rid = int(m.group(1))
            ft_ref = None
            if ft_idx is not None and ft_idx < len(cells):
                ft_ref = cells[ft_idx] or None
            entries.append({"id": rid, "ft_ref": ft_ref})

    if entries:
        return entries

    # Fallback: scan entire page for IDs
    ids = [int(m.group(1)) for m in re.finditer(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)(?:/|\"|')", html)]
    return [{"id": i, "ft_ref": None} for i in ids]

def get_task_company_association_type_id():
    """
    Ask HubSpot which associationTypeId links tasks -> companies.
    Fallback to 341 if the meta call fails.
    """
    url = "https://api.hubapi.com/crm/v4/associations/tasks/companies/meta"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        for item in data.get("results", []):
            name = (item.get("name") or "").lower()
            if "task_to_company" in name or name.endswith("_to_company"):
                return item.get("typeId")
        # otherwise just return the first HUBSPOT_DEFINED typeId if present
        for item in data.get("results", []):
            if item.get("associationCategory") == "HUBSPOT_DEFINED" and "typeId" in item:
                return item["typeId"]
    except Exception:
        pass
    return 341  # common default, works in most portals

def create_hubspot_task(ft_ref: str, row_id: int):
    """
    Creates a HubSpot Task, assigns to HUBSPOT_OWNER_ID, and associates to HUBSPOT_COMPANY_ID.
    """
    assoc_type_id = get_task_company_association_type_id()
    url = "https://api.hubapi.com/crm/v3/objects/tasks"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    subject = f"New urgent patent uploaded: {ft_ref or f'ID {row_id}'}"
    body = (
        f"A new urgent customer-uploaded patent was detected.\n"
        f"System ID: {row_id}\n"
        f"FT PATENT REF: {ft_ref or 'N/A'}\n"
        f"List: {LIST_URL}\n"
    )
    payload = {
        "properties": {
            "hs_task_subject": subject,
            "hs_task_priority": "HIGH",
            "hs_task_status": "NOT_STARTED",
            "hubspot_owner_id": str(HUBSPOT_OWNER_ID),
            "hs_timestamp": int(time.time() * 1000),  # due now
            "hs_task_type": "TODO",
            "hs_task_body": body,
        },
        "associations": [
            {
                "to": {"id": str(HUBSPOT_COMPANY_ID)},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": assoc_type_id
                    }
                ],
            }
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    # Avoid printing secrets or full payloads
    created = r.json()
    print(f"HubSpot task created: {created.get('id')} (company {HUBSPOT_COMPANY_ID})")

# ---------- MAIN ----------
def main():
    with requests.Session() as sess:
        html = login_and_fetch(sess)

    rows = parse_entries(html)
    if not rows:
        print("No rows found.")
        return

    state = load_state()
    last = int(state.get("last_seen_id", 0))
    mx = max(r["id"] for r in rows)

    # First run: baseline without alert flood
    if last == 0:
        state["last_seen_id"] = mx
        save_state(state)
        print(f"Baseline set to ID {mx}.")
        return

    if mx > last:
        new_rows = sorted([r for r in rows if r["id"] > last], key=lambda x: x["id"])
        lines = []
        for r in new_rows:
            label = f"ID {r['id']}"
            if r["ft_ref"]:
                label += f" — FT PATENT REF: {r['ft_ref']}"
            lines.append(label)

        email_body = (
            f"Detected {len(new_rows)} new entr{'y' if len(new_rows)==1 else 'ies'}.\n"
            f"Previous highest ID: {last}\n"
            f"Latest ID now: {mx}\n\n" +
            "\n".join(lines) +
            f"\n\nOpen: {LIST_URL}\n"
        )
        send_email(f"New entries detected ({len(new_rows)})", email_body)

        # Create a HubSpot task per new row
        for r in new_rows:
            try:
                create_hubspot_task(r.get("ft_ref"), r["id"])
            except Exception as e:
                print(f"Failed to create task for row {r['id']}: {e}")

        state["last_seen_id"] = mx
        save_state(state)
    else:
        print("No new entries.")

if __name__ == "__main__":
    main()
