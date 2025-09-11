import os, re, json, smtplib, ssl, requests, time
from email.mime.text import MIMEText
from email.utils import formatdate

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

# ----------------- CONFIG -----------------
LIST_URL = os.environ.get("LIST_URL", "https://team42api.herokuapp.com/passwordreset/database_models/urgentcustomeruploadedpatent/")
LOGIN_URL = os.environ.get("LOGIN_URL")  # optional override, e.g. https://team42api.herokuapp.com/admin/login/
MODEL_PATH_SNIPPET = "/urgentcustomeruploadedpatent/"  # used to extract numeric row IDs

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
TO_EMAIL  = os.environ["TO_EMAIL"]

DJANGO_USERNAME = os.environ["DJANGO_USER"]
DJANGO_PASSWORD = os.environ["DJANGO_PASS"]

HUBSPOT_TOKEN       = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_COMPANY_ID  = os.environ["HUBSPOT_COMPANY_ID"]
HUBSPOT_OWNER_ID    = os.environ["HUBSPOT_OWNER_ID"]

STATE_FILE = "state.json"

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
def looks_like_login_html(html: str) -> bool:
    h = html.lower()
    return ("csrfmiddlewaretoken" in h) and (
        ("name=\"username\"" in h) or ("id=\"id_username\"" in h) or ("name=\"email\"" in h)
    ) and (("name=\"password\"" in h) or ("id=\"id_password\"" in h))

def login_and_fetch(session: requests.Session) -> requests.Response:
    """
    Returns the final Response for LIST_URL (not just text),
    after doing login if needed.
    """
    # If user supplied a LOGIN_URL, use that flow first
    if LOGIN_URL:
        r0 = session.get(LOGIN_URL, timeout=30, allow_redirects=True)
        r0.raise_for_status()
        html = r0.text
        if looks_like_login_html(html):
            # generic form submit
            from bs4 import BeautifulSoup as BS  # will raise if bs4 not installed
            soup = BS(html, "html.parser")
            form = soup.find("form")
            if not form: raise RuntimeError("Login form not found at LOGIN_URL.")
            action = form.get("action") or r0.url
            post_url = requests.compat.urljoin(r0.url, action)
            payload = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name: continue
                payload[name] = inp.get("value", "")
            for k in list(payload.keys()):
                low = k.lower()
                if ("user" in low and "name" in low) or low in ("email","username"):
                    payload[k] = DJANGO_USERNAME
                if "pass" in low:
                    payload[k] = DJANGO_PASSWORD
            r1 = session.post(post_url, data=payload, headers={"Referer": r0.url}, timeout=30, allow_redirects=True)
            r1.raise_for_status()
        # now fetch target
        r = session.get(LIST_URL, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return r

    # Otherwise try LIST_URL directly and see if it’s a login page
    r = session.get(LIST_URL, timeout=30, allow_redirects=True)
    r.raise_for_status()
    ct = r.headers.get("content-type", "").lower()
    if "html" in ct and looks_like_login_html(r.text):
        # submit that login form
        from bs4 import BeautifulSoup as BS
        soup = BS(r.text, "html.parser")
        form = soup.find("form")
        if not form: raise RuntimeError("Login form not found (auto).")
        action = form.get("action") or r.url
        post_url = requests.compat.urljoin(r.url, action)
        payload = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name: continue
            payload[name] = inp.get("value", "")
        for k in list(payload.keys()):
            low = k.lower()
            if ("user" in low and "name" in low) or low in ("email","username"):
                payload[k] = DJANGO_USERNAME
            if "pass" in low:
                payload[k] = DJANGO_PASSWORD
        r2 = session.post(post_url, data=payload, headers={"Referer": r.url}, timeout=30, allow_redirects=True)
        r2.raise_for_status()
        r3 = session.get(LIST_URL, timeout=30, allow_redirects=True)
        r3.raise_for_status()
        return r3
    return r

# ----------------- PARSERS -----------------
def parse_json_for_rows(text: str):
    """
    Accepts either a list of objects or an object with 'results'.
    Tries to find id and ft_ref-like field.
    """
    try:
        data = json.loads(text)
    except Exception:
        return None
    items = data if isinstance(data, list) else data.get("results") or data.get("data")
    if not isinstance(items, list):
        return None
    rows = []
    for obj in items:
        if not isinstance(obj, dict): continue
        # id
        rid = None
        if "id" in obj and str(obj["id"]).isdigit():
            rid = int(obj["id"])
        else:
            # try common keys
            for k in obj.keys():
                if k.endswith("_id") and str(obj[k]).isdigit():
                    rid = int(obj[k]); break
        if rid is None: continue
        # FT ref
        ft = None
        # prefer exact-like keys
        for key in obj.keys():
            lk = key.lower()
            if "ft" in lk and "ref" in lk:
                val = obj[key]
                if isinstance(val, (str,int)):
                    ft = str(val)
                    break
        rows.append({"id": rid, "ft_ref": ft})
    return rows or None

def parse_html_for_rows(html: str):
    if not BeautifulSoup:
        return []
    soup = BeautifulSoup(html, "html.parser")
    # prefer tables
    table = soup.find("table")
    if table:
        header_cells = table.select("thead th")
        if not header_cells:
            first_tr = table.find("tr")
            if first_tr:
                header_cells = first_tr.find_all(["th","td"])
        headers = [th.get_text(strip=True).upper() for th in (header_cells or [])]
        ft_idx = None
        for i, h in enumerate(headers):
            if "FT" in h and "PATENT" in h and "REF" in h:
                ft_idx = i; break
        body_rows = table.select("tbody tr") or table.find_all("tr")[1:]
        rows = []
        for tr in body_rows:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
            row_html = str(tr)
            m = re.search(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)(?:/|\"|')", row_html)
            if not m:
                a = tr.find("a", href=True)
                if a:
                    m = re.search(r"/(\d+)(?:/|$)", a["href"])
            if not m: continue
            rid = int(m.group(1))
            ft_ref = cells[ft_idx] if ft_idx is not None and ft_idx < len(cells) else None
            rows.append({"id": rid, "ft_ref": ft_ref or None})
        if rows:
            return rows
    # fallback: any IDs on the page
    ids = [int(m.group(1)) for m in re.finditer(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)(?:/|\"|')", html)]
    return [{"id": i, "ft_ref": None} for i in ids]

# ----------------- HUBSPOT -----------------
def get_task_company_association_type_id():
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
        for item in data.get("results", []):
            if item.get("associationCategory") == "HUBSPOT_DEFINED" and "typeId" in item:
                return item["typeId"]
    except Exception:
        pass
    return 341

def create_hubspot_task(ft_ref: str, row_id: int):
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
            "hs_timestamp": int(time.time() * 1000),
            "hs_task_type": "TODO",
            "hs_task_body": body,
        },
        "associations": [
            {
                "to": {"id": str(HUBSPOT_COMPANY_ID)},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": assoc_type_id}],
            }
        ],
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    print(f"HubSpot task created: {r.json().get('id')} (company {HUBSPOT_COMPANY_ID})")

# ----------------- MAIN -----------------
def main():
    with requests.Session() as sess:
        resp = login_and_fetch(sess)
    ct = resp.headers.get("content-type","").lower()
    content = resp.text

    # Save the fetched page so we can inspect it from Actions artifacts
    with open("last_page.html", "w", encoding="utf-8") as f:
        f.write(content)

    # Parse
    rows = None
    if "application/json" in ct:
        rows = parse_json_for_rows(content)
        print("Parser: JSON")
    if not rows:
        rows = parse_html_for_rows(content)
        print("Parser: HTML")

    if not rows:
        print("No rows found.")
        return

    state = load_state()
    last = int(state.get("last_seen_id", 0))
    mx = max(r["id"] for r in rows)

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
            if r.get("ft_ref"): label += f" — FT PATENT REF: {r['ft_ref']}"
            lines.append(label)

        email_body = (
            f"Detected {len(new_rows)} new entr{'y' if len(new_rows)==1 else 'ies'}.\n"
            f"Previous highest ID: {last}\nLatest ID now: {mx}\n\n" +
            "\n".join(lines) + f"\n\nOpen: {LIST_URL}\n"
        )
        send_email("New entries detected", email_body)

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
