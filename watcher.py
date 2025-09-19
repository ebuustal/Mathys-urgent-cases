import os, re, json, time, smtplib, ssl, requests
from email.mime.text import MIMEText
from email.utils import formatdate

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

# =========================
# CONFIG
# =========================
LIST_URL  = os.environ.get(
    "LIST_URL",
    "https://team42api.herokuapp.com/admin/passwordreset/urgentcustomeruploadedpatent/",
)
LOGIN_URL = os.environ.get("LOGIN_URL", "")  # optional

MODEL_PATH_SNIPPET = "/admin/passwordreset/urgentcustomeruploadedpatent/"

DJANGO_USERNAME = os.environ["DJANGO_USER"]
DJANGO_PASSWORD = os.environ["DJANGO_PASS"]

# Email (Gmail App Password)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
TO_EMAIL  = os.environ["TO_EMAIL"]

# HubSpot
HS_BASE            = "https://api.hubapi.com"
HUBSPOT_TOKEN      = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_OWNER_ID   = os.environ.get("HUBSPOT_OWNER_ID", "154662807")
HUBSPOT_COMPANY_ID = os.environ.get("HUBSPOT_COMPANY_ID", "5590029115")
HUBSPOT_CONTACT_ID = os.environ.get("HUBSPOT_CONTACT_ID", "")  # optional fallback

# Slack Workflow (optional)
SLACK_WORKFLOW_URL = os.environ.get("SLACK_WORKFLOW_URL", "")
HUBSPOT_PORTAL_ID  = os.environ.get("HUBSPOT_PORTAL_ID", "")

STATE_FILE = "state.json"
MODE = os.environ.get("MODE", "normal").strip().lower()

# =========================
# helpers
# =========================
def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"]   = TO_EMAIL
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_seen_id": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def looks_like_login_html(html: str) -> bool:
    h = html.lower()
    return ("csrfmiddlewaretoken" in h) and (
        ("name=\"username\"" in h) or ("id=\"id_username\"" in h) or ("name=\"email\"" in h)
    ) and (("name=\"password\"" in h) or ("id=\"id_password\"" in h))

def submit_login_from_html(session: requests.Session, page_html: str, page_url: str):
    """Parse a login form from page_html and submit credentials."""
    from bs4 import BeautifulSoup as BS
    soup = BS(page_html, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("Login form not found.")
    post_url = requests.compat.urljoin(page_url, form.get("action") or page_url)
    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")
    for k in list(payload.keys()):
        lk = k.lower()
        if ("user" in lk and "name" in lk) or lk in ("email", "username"):
            payload[k] = DJANGO_USERNAME
        if "pass" in lk:
            payload[k] = DJANGO_PASSWORD
    r = session.post(post_url, data=payload, headers={"Referer": page_url}, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r

def fetch_admin_list(session: requests.Session):
    """
    Robust flow:
      - Try LOGIN_URL (if provided). If it 404s, warn and continue.
      - Fetch LIST_URL.
      - If LIST_URL returns login page, submit login from that page and refetch LIST_URL.
    """
    if LOGIN_URL:
        try:
            r0 = session.get(LOGIN_URL, timeout=30, allow_redirects=True)
            r0.raise_for_status()
            if looks_like_login_html(r0.text):
                submit_login_from_html(session, r0.text, r0.url)
        except requests.HTTPError as e:
            print(f"Warning: LOGIN_URL failed ({e}). Proceeding via LIST_URL login if needed.")

    r1 = session.get(LIST_URL, timeout=30, allow_redirects=True)
    r1.raise_for_status()

    if looks_like_login_html(r1.text):
        submit_login_from_html(session, r1.text, r1.url)
        r2 = session.get(LIST_URL, timeout=30, allow_redirects=True)
        r2.raise_for_status()
        return r2
    return r1

def parse_html_for_rows(html: str):
    if not BeautifulSoup:
        return []
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="result_list") or soup.find("table")
    rows = []
    ft_idx = None
    if table:
        # headers
        head = table.find("thead")
        headers = []
        if head:
            headers = [th.get_text(strip=True).upper() for th in head.find_all("th")]
        else:
            first_tr = table.find("tr")
            if first_tr:
                headers = [th.get_text(strip=True).upper() for th in first_tr.find_all(["th","td"])]
        for i, h in enumerate(headers):
            if "FT" in h and "PATENT" in h and "REF" in h:
                ft_idx = i
                break
        # body
        body_rows = table.select("tbody tr") or table.find_all("tr")[1:]
        for tr in body_rows:
            a = tr.find("a", href=True)
            rid = None
            if a:
                m = re.search(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)/", a["href"])
                if m: rid = int(m.group(1))
            if rid is None:
                m = re.search(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)/", str(tr))
                if m: rid = int(m.group(1))
            if rid is None:
                continue
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
            ft = cells[ft_idx] if ft_idx is not None and ft_idx < len(cells) else None
            rows.append({"id": rid, "ft_ref": ft or None})
    if not rows:
        ids = [int(m.group(1)) for m in re.finditer(re.escape(MODEL_PATH_SNIPPET) + r"(\d+)/", html)]
        rows = [{"id": i, "ft_ref": None} for i in ids]
    return rows

# ---------------- HubSpot ----------------
def hs_create_task(ft_ref: str, row_id: int) -> str:
    url = f"{HS_BASE}/crm/v3/objects/tasks"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    subject = "Manual Upload Verification"
    body = (
        f"{ft_ref or f'ID {row_id}'}, please review. "
        f"If case due in < 7 days, pass to OX to confirm we can renew."
    )
    payload = {
        "properties": {
            "hs_task_subject": subject,
            "hs_task_body": body,
            "hs_task_priority": "HIGH",
            "hs_task_status": "NOT_STARTED",
            "hubspot_owner_id": str(HUBSPOT_OWNER_ID),
            "hs_timestamp": int(time.time() * 1000),
            "hs_task_type": "TODO",
        }
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        print("Task create error:", r.status_code, r.text); r.raise_for_status()
    task_id = r.json().get("id")
    print("Created Task:", task_id)
    return task_id

def hs_associate_task_to_company(task_id: str, company_id: str):
    url = f"{HS_BASE}/crm/v3/associations/tasks/companies/batch/create"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    payload = {"inputs": [{"from": {"id": str(task_id)}, "to": {"id": str(company_id)}, "type": "task_to_company"}]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        print("Associate task→company error:", r.status_code, r.text); r.raise_for_status()
    print(f"Task {task_id} associated to Company {company_id}.")

def hs_associate_task_to_contact(task_id: str, contact_id: str):
    url = f"{HS_BASE}/crm/v3/associations/tasks/contacts/batch/create"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    payload = {"inputs": [{"from": {"id": str(task_id)}, "to": {"id": str(contact_id)}, "type": "task_to_contact"}]}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        print("Associate task→contact error:", r.status_code, r.text); r.raise_for_status()
    print(f"Task {task_id} associated to Contact {contact_id}.")

def create_hubspot_task_and_link(ft_ref: str, row_id: int) -> str:
    tid = hs_create_task(ft_ref, row_id)
    try:
        hs_associate_task_to_company(tid, HUBSPOT_COMPANY_ID)
    except Exception as e:
        print(f"Company association failed: {e}")
        if HUBSPOT_CONTACT_ID:
            try:
                hs_associate_task_to_contact(tid, HUBSPOT_CONTACT_ID)
            except Exception as e2:
                print(f"Contact association also failed: {e2}")
    return tid

def send_slack_workflow(task_id: str):
    if not SLACK_WORKFLOW_URL:
        print("No SLACK_WORKFLOW_URL set; skipping Slack."); return
    payload = {
        "task_id": str(task_id),
        "portal_id": str(HUBSPOT_PORTAL_ID or ""),
        "company_id": str(HUBSPOT_COMPANY_ID or ""),
    }
    try:
        r = requests.post(SLACK_WORKFLOW_URL, json=payload, timeout=15)
        if r.status_code >= 400: print("Slack workflow error:", r.status_code, r.text)
        else: print("Slack workflow triggered.")
    except Exception as e:
        print(f"Slack workflow exception: {e}")

# =========================
# modes
# =========================
def normal_mode():
    with requests.Session() as sess:
        resp = fetch_admin_list(sess)

    content = resp.text
    try:
        with open("last_page.html", "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass

    rows = parse_html_for_rows(content)
    if not rows:
        print("No rows found."); return

    state = load_state()
    last = int(state.get("last_seen_id", 0))
    mx = max(r["id"] for r in rows)

    if last == 0:
        latest = max(rows, key=lambda r: r["id"])
        ft = latest.get("ft_ref")
        send_email(
            "New entry detected (first run)",
            f"First non-empty detection.\nLatest ID: {latest['id']}\nFT PATENT REF: {ft or 'N/A'}\n\nOpen: {LIST_URL}"
        )
        try:
            tid = create_hubspot_task_and_link(ft, latest["id"])
            send_slack_workflow(tid)
        except Exception as e:
            print(f"HubSpot/Slack first-run error: {e}")
        state["last_seen_id"] = mx; save_state(state)
        print(f"First-run alert done. Baseline set to {mx}."); return

    if mx > last:
        new_rows = sorted([r for r in rows if r["id"] > last], key=lambda x: x["id"])
        lines = []
        for r in new_rows:
            label = f"ID {r['id']}"
            if r.get("ft_ref"): label += f" — FT PATENT REF: {r['ft_ref']}"
            lines.append(label)
        send_email(
            "New entries detected",
            f"Detected {len(new_rows)} new entr{'y' if len(new_rows)==1 else 'ies'}.\n"
            f"Previous highest ID: {last}\nLatest ID now: {mx}\n\n" + "\n".join(lines) + f"\n\nOpen: {LIST_URL}"
        )
        for r in new_rows:
            try:
                tid = create_hubspot_task_and_link(r.get("ft_ref"), r["id"])
                send_slack_workflow(tid)
            except Exception as e:
                print(f"HubSpot/Slack error for row {r['id']}: {e}")
        state["last_seen_id"] = mx; save_state(state)
    else:
        print("No new entries.")

def test_mode():
    fake_id = int(time.time()) % 1000000
    fake_ft = f"TEST-FT-REF-{fake_id}"
    send_email(
        "TEST: New entries detected",
        f"(Test run) Simulated new entry.\nID {fake_id} — FT PATENT REF: {fake_ft}\n\nOpen: {LIST_URL}"
    )
    try:
        tid = create_hubspot_task_and_link(fake_ft, fake_id)
        send_slack_workflow(tid)
    except Exception as e:
        print(f"HubSpot/Slack TEST error: {e}")
    print("Test email + HubSpot task + Slack sent.")

if __name__ == "__main__":
    if MODE == "test":
        test_mode()
    else:
        normal_mode()
