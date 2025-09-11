import os, re, json, smtplib, ssl, requests, time
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.utils import formatdate

# ----------------- CONFIG (from secrets) -----------------
LIST_URL = "https://team42api.herokuapp.com/passwordreset/database_models/urgentcustomeruploadedpatent/"
MODEL_PATH_SNIPPET = "/urgentcustomeruploadedpatent/"  # used to extract numeric row IDs

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]     # Gmail address (sender)
SMTP_PASS = os.environ["SMTP_PASS"]     # Gmail App Password (16 chars)
TO_EMAIL  = os.environ["TO_EMAIL"]      # alert recipient

DJANGO_USERNAME = os.environ["DJANGO_USER"]  # site login
DJANGO_PASSWORD = os.environ["DJANGO_PASS"]

# HubSpot (kept in GitHub Secrets)
HUBSPOT_TOKEN       = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_COMPANY_ID  = os.environ["HUBSPOT_COMPANY_ID"]   # e.g., 5590029115
HUBSPOT_OWNER_ID    = os.environ["HUBSPOT_OWNER_ID"]     # e.g., 154662807

STATE_FILE = "state.json"  # saved back to repo to remember last-seen ID


# ----------------- EMAIL -----------------
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


# ----------------- STATE -----------------
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_seen_id": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ----------------- LOGIN + FETCH -----------------
def _looks_like_login(html: str) -> bool:
    h = html.lower()
    return ("csrfmiddlewaretoken" in h) and (
        ("name=\"username\"" in h) or ("id=\"id_username\"" in h) or ("name=\"email\"" in h)
    ) and (("name=\"password\"" in h) or ("id=\"id_password\"" in h))

def login_and_fetch(session: requests.Session) -> str:
    """
    Tries LIST_URL; if a login form appears, submit it generically, then refetch LIST_URL.
    """
    r = session.get(LIST_URL, timeout=30, allow_redirects=True)
    r.raise_for_status()

    if not _looks_like_login(r.text):
        return r.text  # already accessible

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("Login form not found.")
    action = form.get("action") or r.url
    login_url = requests.compat.urljoin(r.url, action)

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")

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


# ----------------- PARSE TABLE -----------------
def parse_entries(html: str):
    """
    Returns a list of dicts: [{id: int, ft_ref: str|None}]
    Finds a table, detects 'FT PATENT REF' column (case-insensitive).
    """
    soup = BeautifulSoup(html, "html.parser")
    entries = []

    table = soup.find("table")
    if table:
        # headers
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

        # rows
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

    # fallback: scan page for IDs
    ids = [int(m.group(1)) for m in re.finditer(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)(?:/|\"|')", html)]
    return [{"id": i, "ft_ref": None} for i in ids]


# ----------------- HUBSPOT -----------------
def get_task_company_association_type_id():
    """
    Queries HubSpot for the correct association type (tasks -> companies).
    Falls back to 341 if meta call fails.
    """
    url = "https://api.hubapi.com/crm/v4/associations/tasks/companies/meta"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        # try sensible pick
        for item in data.get("results", []):
            name = (item.get("name") or "").lower()
            if "task_to_company" in name or name.endswith("_to_company"):
                return item.get("typeId")
        for item in data.get("results", []):
            if item.get("associationCategory") == "HUBSPOT_DEFINED" and "typeId" in item:
                return item["typeId"]
    except Exception:
        pass
    return 341  # common default

def create_hubspot_task(ft_ref: str, row_id: int):
    """
    Creates a HubSpot Task, assigned to HUBSPOT_OWNER_ID, associated with HUBSPOT_COMPANY_ID.
    Task name/body set per your request.
    """
    assoc_type_id = get_task_company_association_type_id()
    url = "https://api.hubapi.com/crm/v3/objects/tasks"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }

    subject = "Manual Upload Verification"
    body = (
        f"{ft_ref or f'ID {row_id}'}, please verify, "
        f"if case due in less than 7 days please pass task to OX to confirm we are able to renew"
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
    created = r.json()
    print(f"HubSpot task created: {created.get('id')} (company {HUBSPOT_COMPANY_ID})")


# ----------------- MAIN -----------------
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

    # First run: baseline to avoid spamming existing rows
    if last == 0:
        state["last_seen_id"] = mx
        save_state(state)
        print(f"Baseline set to ID {mx}.")
        return

    if mx > last:
        new_rows = sorted([r for r in rows if r["id"] > last], key=lambda x: x["id"])
        # email summary
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
        send_email("New entries detected", email_body)

        # create a HubSpot task per new row
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

