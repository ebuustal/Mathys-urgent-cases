import os, re, json, smtplib, ssl, time, requests
from email.mime.text import MIMEText
from email.utils import formatdate

# BeautifulSoup is optional; we fall back to regex if it's not available
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
LOGIN_URL = os.environ.get("LOGIN_URL", "").strip()   # optional admin login page
# Use just the model-path segment that appears in admin URLs
MODEL_PATH_SNIPPET = os.environ.get("MODEL_PATH_SNIPPET", "/urgentcustomeruploadedpatent/")

# Gmail (use an App Password)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
TO_EMAIL  = os.environ["TO_EMAIL"]

# Django
DJANGO_USERNAME = os.environ["DJANGO_USER"]
DJANGO_PASSWORD = os.environ["DJANGO_PASS"]

# HubSpot (Private App token)
HUBSPOT_TOKEN      = os.environ["HUBSPOT_TOKEN"]
HUBSPOT_OWNER_ID   = os.environ.get("HUBSPOT_OWNER_ID", "154662807")
HUBSPOT_COMPANY_ID = os.environ.get("HUBSPOT_COMPANY_ID", "5590029115")
HUBSPOT_PORTAL_ID  = os.environ.get("HUBSPOT_PORTAL_ID", "8124475")

# Optional Slack Workflow webhook
SLACK_WORKFLOW_URL = os.environ.get("SLACK_WORKFLOW_URL", "").strip()

STATE_FILE = "state.json"
MODE = os.environ.get("MODE", "normal").lower().strip()  # "normal" or "test"

HS_BASE = "https://api.hubapi.com"

# =========================
# Utilities
# =========================
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

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_seen_id": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# =========================
# Fetch page with login support
# =========================
def looks_like_login_html(html: str) -> bool:
    h = html.lower()
    return ("csrfmiddlewaretoken" in h) and (
        ('name="username"' in h) or ('id="id_username"' in h) or ('name="email"' in h)
    ) and (('name="password"' in h) or ('id="id_password"' in h))

def perform_form_login(session: requests.Session, url: str, html: str, referer: str):
    if not BeautifulSoup:
        raise RuntimeError("Login form detected but BeautifulSoup is not available.")
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("Login form not found.")
    post_url = requests.compat.urljoin(url, form.get("action") or url)
    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")
    # map creds
    for k in list(payload.keys()):
        lk = k.lower()
        if ("user" in lk and "name" in lk) or lk in ("email", "username"):
            payload[k] = DJANGO_USERNAME
        if "pass" in lk:
            payload[k] = DJANGO_PASSWORD
    r = session.post(post_url, data=payload, headers={"Referer": referer}, timeout=30, allow_redirects=True)
    r.raise_for_status()

def login_and_fetch(session: requests.Session):
    # 1) Try explicit LOGIN_URL if provided
    if LOGIN_URL:
        try:
            r0 = session.get(LOGIN_URL, timeout=30, allow_redirects=True)
            r0.raise_for_status()
            if looks_like_login_html(r0.text):
                perform_form_login(session, r0.url, r0.text, r0.url)
        except requests.HTTPError as e:
            print(f"Warning: LOGIN_URL failed ({e}). Proceeding via LIST_URL login if needed.")
    # 2) Fetch the list; auto-login if it returns the login page
    r = session.get(LIST_URL, timeout=30, allow_redirects=True)
    r.raise_for_status()
    if "html" in r.headers.get("content-type", "").lower() and looks_like_login_html(r.text):
        perform_form_login(session, r.url, r.text, r.url)
        r = session.get(LIST_URL, timeout=30, allow_redirects=True)
        r.raise_for_status()
    return r

# =========================
# Parsers
# =========================
def parse_admin_table(html: str):
    """
    Robust parser for Django admin changelist:
    - Prefer #result_list header + rows
    - Fallback to regex for '/<model_path>/<id>/' anywhere in HTML
    Returns: list of dicts {id: int, ft_ref: Optional[str]}
    """
    rows_out = []

    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="result_list") or soup.find("table")
        if table:
            # headers
            header_cells = []
            thead = table.find("thead")
            if thead:
                header_cells = thead.find_all("th")
            if not header_cells:
                # sometimes Django renders header as first row
                first_tr = table.find("tr")
                if first_tr:
                    header_cells = first_tr.find_all(["th", "td"])
            headers = [th.get_text(strip=True).upper() for th in (header_cells or [])]

            # Which column has FT PATENT REF?
            ft_idx = None
            for i, h in enumerate(headers):
                if "FT" in h and "PATENT" in h and "REF" in h:
                    ft_idx = i
                    break

            # body rows (skip header row if we used it)
            body_rows = table.select("tbody tr") or table.find_all("tr")[1:]
            for tr in body_rows:
                # ID: take href containing the model path
                rid = None
                ft  = None

                # Any link to the change page?
                link = tr.find("a", href=True)
                if link and MODEL_PATH_SNIPPET in link["href"]:
                    m = re.search(rf"{re.escape(MODEL_PATH_SNIPPET)}(\d+)", link["href"])
                    if m:
                        rid = int(m.group(1))

                # Column text for FT ref (if we discovered index)
                if ft_idx is not None:
                    cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                    if ft_idx < len(cells):
                        ft = cells[ft_idx] or None

                if rid is not None:
                    rows_out.append({"id": rid, "ft_ref": ft})

    # Fallback: regex scan of the whole HTML for IDs, if we got nothing
    if not rows_out:
        ids = [int(m.group(1)) for m in re.finditer(rf"{re.escape(MODEL_PATH_SNIPPET)}(\d+)", html)]
        # de-dup but preserve order
        seen = set()
        uniq = []
        for i in ids:
            if i not in seen:
                uniq.append(i)
                seen.add(i)
        rows_out = [{"id": i, "ft_ref": None} for i in uniq]

    print(f"Parser: found {len(rows_out)} row(s). Sample IDs: {[r['id'] for r in rows_out[:5]]}")
    return rows_out

# =========================
# HubSpot
# =========================
def hs_create_task(ft_ref: str, row_id: int) -> str:
    url = f"{HS_BASE}/crm/v3/objects/tasks"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    subject = "Manual Upload Verification"
    body = (
        f"{ft_ref or f'ID {row_id}'}, please verify, "
        f"if case due in less than 7 days please pass task to OX to confirm we are able to renew"
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
        print("Task create error:", r.status_code, r.text)
        r.raise_for_status()
    task_id = r.json().get("id")
    print("Created Task:", task_id)
    return task_id

def hs_associate_task_to_company(task_id: str, company_id: str):
    url = f"{HS_BASE}/crm/v3/associations/tasks/companies/batch/create"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "inputs": [{
            "from": {"id": str(task_id)},
            "to":   {"id": str(company_id)},
            "type": "task_to_company"   # label-based association
        }]
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        print("Associate task→company error:", r.status_code, r.text)
        # Don't abort the whole run if association fails
    else:
        print(f"Task {task_id} associated to Company {company_id}.")

def notify_slack(task_id: str, ft_ref: str | None):
    if not SLACK_WORKFLOW_URL:
        return
    try:
        payload = {
            "task_id": task_id,
            "portal_id": str(HUBSPOT_PORTAL_ID),
            # Small convenience text if you want to show it in the workflow:
            "text": f"Manual Upload Verification — {ft_ref or task_id}"
        }
        r = requests.post(SLACK_WORKFLOW_URL, json=payload, timeout=15)
        if r.status_code >= 400:
            print("Slack workflow error:", r.status_code, r.text)
        else:
            print("Slack workflow triggered.")
    except Exception as e:
        print("Slack workflow exception:", e)

def create_hubspot_and_slack(ft_ref: str, row_id: int):
    tid = hs_create_task(ft_ref, row_id)
    hs_associate_task_to_company(tid, HUBSPOT_COMPANY_ID)
    notify_slack(tid, ft_ref)
    return tid

# =========================
# Modes
# =========================
def normal_mode():
    with requests.Session() as sess:
        resp = login_and_fetch(sess)

    ct = resp.headers.get("content-type", "").lower()
    content = resp.text

    # Save fetched page for debugging
    try:
        with open("last_page.html", "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass

    rows = parse_admin_table(content)

    if not rows:
        print("No rows found.")
        return

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
            create_hubspot_and_slack(ft, latest["id"])
        except Exception as e:
            print(f"HubSpot/Slack first-run error: {e}")
        state["last_seen_id"] = mx
        save_state(state)
        print(f"First-run alert done. Baseline set to {mx}.")
        return

    if mx > last:
        new_rows = sorted([r for r in rows if r["id"] > last], key=lambda x: x["id"])
        lines = []
        for r in new_rows:
            label = f"ID {r['id']}"
            if r.get("ft_ref"):
                label += f" — FT PATENT REF: {r['ft_ref']}"
            lines.append(label)
        body = (
            f"Detected {len(new_rows)} new entr{'y' if len(new_rows)==1 else 'ies'}.\n"
            f"Previous highest ID: {last}\nLatest ID now: {mx}\n\n" +
            "\n".join(lines) + f"\n\nOpen: {LIST_URL}"
        )
        send_email("New entries detected", body)
        for r in new_rows:
            try:
                create_hubspot_and_slack(r.get("ft_ref"), r["id"])
            except Exception as e:
                print(f"HubSpot/Slack error for row {r['id']}: {e}")
        state["last_seen_id"] = mx
        save_state(state)
    else:
        print("No new entries.")

def test_mode():
    fake_id = int(time.time()) % 1000000
    fake_ft = "TEST-FT-REF-" + str(fake_id)
    send_email(
        "TEST: New entries detected",
        f"(Test run) Simulated new entry.\nID {fake_id} — FT PATENT REF: {fake_ft}\n\nOpen: {LIST_URL}"
    )
    try:
        create_hubspot_and_slack(fake_ft, fake_id)
    except Exception as e:
        print(f"HubSpot/Slack TEST error: {e}")
    print("Test email + HubSpot + Slack workflow sent.")

# =========================
# Entry
# =========================
if __name__ == "__main__":
    if MODE == "test":
        test_mode()
    else:
        normal_mode()
