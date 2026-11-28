import os
import re
import io
import uuid
import hashlib
import sqlite3
import math
import csv
import zipfile
import shutil
import subprocess
import base64
from xml.etree import ElementTree
from collections import Counter
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent

import streamlit as st
from dotenv import load_dotenv
from pypdf import PdfReader
from PIL import Image

load_dotenv()
DATA_DIR = BASE_DIR / "data"
RESUME_DIR = DATA_DIR / "resumes"
DB_PATH = DATA_DIR / "hr_automation.db"
for p in (DATA_DIR, RESUME_DIR):
    p.mkdir(parents=True, exist_ok=True)

st.set_page_config(
    page_title="Resume Intelligence & HR Automation",
    page_icon=":material/work:",
    layout="wide",
)

# -------------------- Shared services --------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id TEXT PRIMARY KEY,
            name TEXT, email TEXT, phone TEXT, filename TEXT,
            resume_text TEXT, skills TEXT, status TEXT DEFAULT 'Processed',
            similarity REAL, job_role TEXT, created_at TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT, event TEXT, details TEXT, created_at TEXT
        )""")
        conn.commit()

def log_event(candidate_id, event, details=""):
    with db() as conn:
        conn.execute(
            "INSERT INTO activity(candidate_id,event,details,created_at) VALUES(?,?,?,?)",
            (candidate_id, event, details, datetime.now().isoformat(timespec="seconds"))
        )
        conn.commit()

def extract_text(uploaded_file):
    data = uploaded_file.getvalue()
    suffix = Path(uploaded_file.name).suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(data))
        page_texts = []
        language = os.getenv("OCR_LANGUAGE", "eng")

        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            if len(re.sub(r"\s+", "", page_text)) >= 40:
                page_texts.append(page_text)
                continue

            images = list(page.images)
            if not images:
                page_texts.append(page_text)
                continue

            tesseract_cmd = os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
            if not tesseract_cmd:
                raise RuntimeError(
                    "This PDF contains scanned page images, but Tesseract OCR was not found. "
                    "Install Tesseract OCR and set TESSERACT_CMD in .env if it is not on PATH."
                )

            image_text = []
            for embedded_image in images:
                try:
                    image = embedded_image.image
                    if image is None:
                        with Image.open(io.BytesIO(embedded_image.data)) as opened_image:
                            image = opened_image.copy()
                    image_bytes = io.BytesIO()
                    image.convert("RGB").save(image_bytes, format="PNG")
                    try:
                        result = subprocess.run(
                            [tesseract_cmd, "stdin", "stdout", "-l", language],
                            input=image_bytes.getvalue(),
                            capture_output=True,
                            check=False,
                        )
                    except FileNotFoundError as exc:
                        raise RuntimeError(
                            f"Tesseract executable was not found at {tesseract_cmd!r}."
                        ) from exc
                    if result.returncode:
                        error_detail = result.stderr.decode("utf-8", errors="replace").strip()
                        raise RuntimeError(
                            f"Tesseract OCR failed on PDF page {page_number}: "
                            f"{error_detail or 'unknown OCR error'}"
                        )
                    image_text.append(result.stdout.decode("utf-8", errors="replace"))
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"Could not decode an embedded image on PDF page {page_number}: {exc}"
                    ) from exc

            recognized_text = "\n".join(text for text in image_text if text.strip())
            page_texts.append(
                recognized_text if len(re.sub(r"\s+", "", recognized_text)) > len(re.sub(r"\s+", "", page_text))
                else page_text
            )
        return "\n".join(page_texts).strip()
    if suffix == ".docx":
        chunks = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document_xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(document_xml)
        for paragraph in root.iter():
            if paragraph.tag.endswith("}p"):
                texts = [node.text for node in paragraph.iter() if node.tag.endswith("}t") and node.text]
                if texts:
                    chunks.append("".join(texts))
        return "\n".join(chunks).strip()
    raise ValueError("Only PDF and DOCX files are supported.")

def normalize_text(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

SKILL_TERMS = [
    "python", "sql", "machine learning", "deep learning", "pandas", "numpy",
    "scikit-learn", "streamlit", "power bi", "tableau", "excel", "java",
    "c++", "aws", "azure", "gcp", "nlp", "generative ai", "langchain",
    "chromadb", "tensorflow", "pytorch", "data analysis", "statistics",
    "fastapi", "flask", "docker", "git", "html", "css", "javascript"
]

def parse_resume(text):
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text)
    phone_match = re.search(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)", text)
    email = email_match.group(0).strip().lower() if email_match else ""
    phone = re.sub(r"\s+", " ", phone_match.group(0)).strip() if phone_match else ""
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    # Name extraction is heuristic; recruiter should verify it.
    name = "Not detected"
    for line in lines[:8]:
        if len(line) <= 55 and not re.search(r"@|https?://|resume|curriculum|phone|mobile", line, re.I):
            if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{2,}", line):
                name = line.title()
                break
    low = text.lower()
    skills = sorted({skill for skill in SKILL_TERMS if skill in low})
    return {"name": name, "email": email, "phone": phone, "skills": skills}

def stable_id(file_bytes):
    return hashlib.sha256(file_bytes).hexdigest()[:24]

def upsert_candidate(uploaded_file, progress_callback=None):
    raw = uploaded_file.getvalue()
    cid = stable_id(raw)
    text = normalize_text(extract_text(uploaded_file))
    if len(text) < 30:
        raise ValueError(
            "Very little text was extracted. The PDF may be image-only or unsupported; "
            "check OCR availability or upload a text-based PDF/DOCX."
        )
    parsed = parse_resume(text)
    if progress_callback: progress_callback("Text extracted and contact details parsed")
    if progress_callback: progress_callback("Resume text indexed for matching")
    with db() as conn:
        conn.execute("""
        INSERT INTO candidates(candidate_id,name,email,phone,filename,resume_text,skills,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(candidate_id) DO UPDATE SET
        name=excluded.name,email=excluded.email,phone=excluded.phone,
        filename=excluded.filename,resume_text=excluded.resume_text,
        skills=excluded.skills,status='Processed'
        """, (cid, parsed["name"], parsed["email"], parsed["phone"], uploaded_file.name,
              text, ", ".join(parsed["skills"]), "Processed", datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    log_event(cid, "Resume processed", uploaded_file.name)
    if progress_callback: progress_callback("Candidate profile saved to SQLite")
    return cid, parsed, text

def text_vector(text):
    terms = re.findall(r"[a-z0-9+#.-]+", text.lower())
    counts = Counter(term for term in terms if len(term) > 1)
    return {term: 1.0 + math.log(count) for term, count in counts.items()}

def cosine_similarity(left, right):
    shared = set(left) & set(right)
    if not shared:
        return 0.0
    dot = sum(left[term] * right[term] for term in shared)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

def search_candidates(job_description, top_k=50):
    job_vector = text_vector(job_description)
    rows = []
    with db() as conn:
        candidates = conn.execute(
            "SELECT candidate_id,name,email,phone,filename,skills,status,resume_text FROM candidates"
        ).fetchall()
    for candidate in candidates:
        similarity = cosine_similarity(job_vector, text_vector(candidate["resume_text"] or ""))
        rows.append({
            "candidate_id": candidate["candidate_id"],
            "name": candidate["name"],
            "email": candidate["email"],
            "phone": candidate["phone"],
            "filename": candidate["filename"],
            "skills": candidate["skills"],
            "status": candidate["status"],
            "Similarity": round(similarity * 100, 2),
        })
    rows.sort(key=lambda row: row["Similarity"], reverse=True)
    return rows[:top_k]

def get_candidate(candidate_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    return dict(row) if row else None

def update_status(candidate_id, status, role=None, similarity=None):
    with db() as conn:
        conn.execute("UPDATE candidates SET status=?, job_role=COALESCE(?,job_role), similarity=COALESCE(?,similarity) WHERE candidate_id=?",
                     (status, role, similarity, candidate_id))
        conn.commit()
    log_event(candidate_id, "Status changed", status)

# -------------------- Email / SMS / calendar --------------------
def send_email_smtp(to_email, subject, body, attachment_bytes=None, attachment_name=None):
    import smtplib
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("FROM_EMAIL", username)
    if not all([host, username, password, sender]):
        raise RuntimeError("Configure SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and FROM_EMAIL in .env")
    msg = EmailMessage()
    msg["From"] = formataddr(("HR Recruitment", sender))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment_bytes and attachment_name:
        msg.add_attachment(attachment_bytes, maintype="text", subtype="calendar",
                           filename=attachment_name, params={"method": "REQUEST"})
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)

def send_email_sendgrid(to_email, subject, body, attachment_bytes=None, attachment_name=None):
    import requests
    api_key = os.getenv("SENDGRID_API_KEY", "")
    configuration_error = sendgrid_configuration_error()
    if configuration_error:
        raise RuntimeError(configuration_error)
    sender = os.getenv("SENDGRID_FROM_EMAIL", "").strip()
    content = {"personalizations": [{"to": [{"email": to_email}]}],
               "from": {"email": sender}, "subject": subject,
               "content": [{"type": "text/plain", "value": body}]}
    if attachment_bytes and attachment_name:
        content["attachments"] = [{
            "content": base64.b64encode(attachment_bytes).decode("ascii"),
            "filename": attachment_name,
            "type": "text/calendar",
            "disposition": "attachment",
        }]
    response = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=content,
        timeout=30
    )
    if not response.ok:
        try:
            errors = response.json().get("errors", [])
            detail = "; ".join(
                error["message"] for error in errors
                if isinstance(error, dict) and error.get("message")
            )
        except (ValueError, AttributeError):
            detail = ""
        if not detail:
            detail = response.text.strip()[:1000]
        hints = {
            401: "Check that the API key is active and has Mail Send permission.",
            403: "Check the API key permissions and verify SENDGRID_FROM_EMAIL as a Sender Identity.",
            400: "Check the recipient, sender identity, and message or attachment fields.",
        }
        hint = hints.get(response.status_code, "Check the SendGrid API response and account settings.")
        raise RuntimeError(
            f"SendGrid rejected the email (HTTP {response.status_code}). "
            f"{detail or hint} {hint}"
        )

def is_e164_phone(phone):
    return bool(re.fullmatch(r"\+[1-9]\d{7,14}", phone.strip()))

def send_sms(to_phone, message):
    from twilio.rest import Client
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    api_key_sid = os.getenv("TWILIO_API_KEY_SID", "").strip()
    api_key_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    from_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
    to_phone = to_phone.strip()
    configuration_error = twilio_configuration_error()
    if configuration_error:
        raise RuntimeError(configuration_error)
    if not is_e164_phone(to_phone):
        raise RuntimeError("Candidate phone must use E.164 format, for example +1234567890.")
    if api_key_sid and api_key_secret:
        client = Client(api_key_sid, api_key_secret, account_sid=sid)
    else:
        client = Client(sid, token)
    return client.messages.create(body=message, from_=from_number, to=to_phone)

def smtp_is_configured():
    host = os.getenv("SMTP_HOST", "")
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("FROM_EMAIL", username)
    return bool(host and username and password and sender)

def is_placeholder_setting(value):
    normalized = str(value or "").strip().lower()
    return not normalized or any(marker in normalized for marker in ("your_", "xxxx", "replace_with"))

def sendgrid_is_configured():
    return not sendgrid_configuration_error()

def sendgrid_configuration_error():
    api_key = os.getenv("SENDGRID_API_KEY", "").strip()
    sender = os.getenv("SENDGRID_FROM_EMAIL", "").strip()
    if not api_key or not sender:
        return "Set SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in .env."
    if is_placeholder_setting(api_key) or is_placeholder_setting(sender):
        return "Replace the SendGrid example values in .env with your API key and verified sender email."
    if not api_key.startswith("SG."):
        return "SENDGRID_API_KEY must be a SendGrid API key starting with SG.; create one in SendGrid Settings > API Keys."
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender):
        return "SENDGRID_FROM_EMAIL must be a valid email address."
    return ""

def twilio_is_configured():
    return not twilio_configuration_error()

def twilio_configuration_error():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    api_key_sid = os.getenv("TWILIO_API_KEY_SID", "").strip()
    api_key_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    from_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
    if is_placeholder_setting(sid) or is_placeholder_setting(from_number):
        return "Set TWILIO_ACCOUNT_SID and TWILIO_PHONE_NUMBER in .env."
    if token and is_placeholder_setting(token):
        token = ""
    if bool(api_key_sid) != bool(api_key_secret):
        return "Set both TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET, or use TWILIO_AUTH_TOKEN."
    if api_key_sid and (is_placeholder_setting(api_key_sid) or is_placeholder_setting(api_key_secret)):
        return "Replace the Twilio API key example values in .env."
    if not token and not (api_key_sid and api_key_secret):
        return "Set TWILIO_AUTH_TOKEN or both Twilio API Key fields in .env."
    if api_key_sid and not api_key_sid.startswith("SK"):
        return "TWILIO_API_KEY_SID should be a Twilio API key SID beginning with SK."
    if not is_e164_phone(from_number):
        return "TWILIO_PHONE_NUMBER must use E.164 format, for example +1234567890."
    return ""

def make_ics(candidate, role, start_dt, duration_minutes, meeting_link):
    # start_dt must be timezone-aware
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    def utcstamp(dt):
        return dt.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    def escape_ics(value):
        return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    uid = f"{candidate['candidate_id']}@resume-hr-automation"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Resume HR Automation//EN",
        "METHOD:REQUEST", "BEGIN:VEVENT", f"UID:{uid}",
        f"DTSTAMP:{utcstamp(datetime.now(ZoneInfo('UTC')))}",
        f"DTSTART:{utcstamp(start_dt)}", f"DTEND:{utcstamp(end_dt)}",
        f"SUMMARY:{escape_ics(f'Interview - {role}')}",
        f"DESCRIPTION:{escape_ics(f'Interview for {role}. Meeting link: {meeting_link}')}",
        f"URL:{escape_ics(meeting_link)}",
        f"ATTENDEE;RSVP=TRUE;CN={escape_ics(candidate['name'])}:mailto:{candidate['email']}",
        "END:VEVENT", "END:VCALENDAR"
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")

def create_meeting_link():
    meeting_id = uuid.uuid4().hex[:12]
    return f"https://meet.jit.si/HR-Interview-{meeting_id}"

# -------------------- UI --------------------
init_db()
with st.container(border=True):
    brand_col, workspace_col = st.columns([3, 1], vertical_alignment="center")
    with brand_col:
        st.markdown("### :violet[TALENT] :blue[MIND]")
        st.caption("RESUME INTELLIGENCE · HR AUTOMATION")
    with workspace_col:
        st.badge("RECRUITER WORKSPACE", icon=":material/lock:", color="violet")

st.title("Recruiting workspace", icon=":material/work:", text_alignment="center")
st.caption(
    "Upload resumes, match candidates, coordinate outreach and schedule interviews.",
    text_alignment="center",
)
st.info(
    "Recruiter approval is required before any email or SMS is sent. Candidate similarity is a relevance signal, not a hiring decision.",
    title="Human review required",
    icon=":material/shield_lock:",
)

steps = [
    "1. Upload resumes", "2. Extract text", "3. Parse & normalize",
    "4. Build text index", "5. Store in SQLite", "6. Enter job description",
    "7. Rank candidates", "8. Recruiter approval", "9. Send email / SMS",
    "10. Record candidate response", "11. Schedule interview", "12. HR dashboard"
]
if "completed_steps" not in st.session_state:
    st.session_state.completed_steps = set()

tab1, tab2, tab3, tab4 = st.tabs([
    "Resume intake",
    "Candidate matching",
    "Outreach & interviews",
    "Recruiting overview",
])

with tab1:
    st.subheader("Add and review resumes", icon=":material/upload_file:")
    uploads = st.file_uploader("Upload candidate resumes (PDF / DOCX)", type=["pdf", "docx"], accept_multiple_files=True)
    if st.button(
        "Process uploaded resumes",
        type="primary",
        disabled=not uploads,
        icon=":material/playlist_add_check:",
    ):
        st.session_state.completed_steps.add(1)
        overall = st.progress(0, text="Starting resume processing...")
        for idx, file in enumerate(uploads):
            with st.status(f"Processing {file.name}", expanded=True) as status:
                try:
                    overall.progress((idx) / len(uploads), text=f"Processing {file.name}")
                    cid, parsed, text = upsert_candidate(file, lambda msg: st.write("✅", msg))
                    st.write(f"Candidate: **{parsed['name']}**")
                    st.write(f"Email: {parsed['email'] or 'Not found'} | Phone: {parsed['phone'] or 'Not found'}")
                    status.update(label=f"Completed: {file.name}", state="complete")
                except Exception as exc:
                    st.error(f"{file.name}: {exc}")
                    status.update(label=f"Failed: {file.name}", state="error")
            overall.progress((idx + 1) / len(uploads), text=f"{idx+1}/{len(uploads)} resumes processed")
        st.session_state.completed_steps.update({2, 3, 4, 5})
        st.success("Resume ingestion finished. Review extracted contact details before contacting candidates.")
    st.caption("Scanned PDFs are OCR-processed when they contain embedded page images. Install Tesseract OCR and set TESSERACT_CMD in .env if the executable is not on PATH.")

with tab2:
    st.subheader("Match candidates to a role", icon=":material/person_search:")
    role = st.text_input("Job role", placeholder="e.g. Junior Data Scientist")
    jd = st.text_area("Paste job description", height=180, placeholder="Required skills, responsibilities, experience...")
    top_k = st.slider("Maximum candidates to retrieve", 5, 100, 30)
    if st.button(
        "Match and rank candidates",
        type="primary",
        disabled=not jd.strip(),
        icon=":material/search:",
    ):
        st.session_state.completed_steps.add(6)
        with st.spinner("Indexing job description and matching stored resumes..."):
            ranked = search_candidates(jd, top_k)
        if not ranked:
            st.warning("No processed candidates found. Upload resumes first.")
        else:
            st.session_state.ranked = ranked
            st.session_state.role = role
            st.session_state.jd = jd
            st.session_state.completed_steps.add(7)
            st.success(f"Retrieved and ranked {len(ranked)} candidates.")
    if "ranked" in st.session_state:
        ranked = st.session_state.ranked
        ranking_display = [
            {key: row[key] for key in ("name", "email", "phone", "skills", "Similarity", "status")}
            for row in ranked
        ]
        st.json(ranking_display, expanded=True)
        ranking_csv = io.StringIO()
        writer = csv.DictWriter(ranking_csv, fieldnames=["candidate_id","name","email","phone","filename","skills","status","Similarity"])
        writer.writeheader()
        writer.writerows(ranked)
        st.download_button(
            "Download ranking CSV",
            ranking_csv.getvalue().encode("utf-8"),
            "candidate_ranking.csv",
            "text/csv",
            width="stretch",
        )
        st.caption("Cosine similarity is calculated from resume/JD embeddings. Review actual resume evidence and verify skills, eligibility and contact details manually.")

with tab3:
    st.subheader("Outreach and interview coordination", icon=":material/calendar_month:")
    ranked = st.session_state.get("ranked", [])
    if not ranked:
        st.info("Run candidate matching first.")
    else:
        options = {
            f"{r['name']} | {r['email'] or 'No email'} | {r['Similarity']}%": r["candidate_id"]
            for r in ranked
        }
        selected_label = st.selectbox("Select a candidate", list(options.keys()))
        cid = options[selected_label]
        candidate = get_candidate(cid)
        role = st.session_state.get("role") or "the advertised role"
        st.write(f"**Email:** {candidate['email'] or 'Not detected'}")
        candidate_phone = st.text_input(
            "Candidate phone for SMS (E.164)",
            value=candidate["phone"] or "",
            key=f"contact_phone_{cid}",
            help="Verify the number and include the country code, for example +919876543210.",
        )
        phone_is_valid = is_e164_phone(candidate_phone)
        st.write(f"**Detected skills:** {candidate['skills'] or 'None detected'}")
        st.session_state.completed_steps.add(8)

        default_body = f"""Hello {candidate['name']},

Thank you for your interest. We reviewed your resume for the {role} position and would like to know whether you are interested in proceeding to an interview.

Please reply YES or NO to this message, or contact the HR team.

Regards,
Recruitment Team"""
        body = st.text_area("Review / edit invitation message", value=default_body, height=180)
        subject = st.text_input("Email subject", value=f"Interview Interest – {role}")
        consent = st.checkbox("I have verified the recipient and have authorization to contact this candidate.")
        col1, col2 = st.columns(2)
        with col1:
            email_provider_options = ["SMTP (Gmail / Outlook)", "SendGrid"]
            default_email_provider = "SendGrid" if sendgrid_is_configured() else email_provider_options[0]
            provider = st.selectbox(
                "Email provider",
                email_provider_options,
                index=email_provider_options.index(default_email_provider),
            )
            email_configured = smtp_is_configured() if provider.startswith("SMTP") else sendgrid_is_configured()
            if not email_configured:
                provider_error = (
                    "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and FROM_EMAIL in .env."
                    if provider.startswith("SMTP")
                    else sendgrid_configuration_error()
                )
                st.warning(f"Email provider is not ready. {provider_error}")
            if st.button("Send approved email", disabled=not (consent and candidate["email"] and email_configured)):
                try:
                    if provider.startswith("SMTP"):
                        send_email_smtp(candidate["email"], subject, body)
                    else:
                        send_email_sendgrid(candidate["email"], subject, body)
                    update_status(cid, "Contacted", role)
                    st.session_state.completed_steps.add(9)
                    st.success("Email accepted by the configured provider.")
                except Exception as exc:
                    st.error(str(exc))
        with col2:
            sms_text = st.text_area("SMS message", value=f"HR: Are you interested in an interview for {role}? Reply YES or NO.")
            sms_configured = twilio_is_configured()
            if not sms_configured:
                st.warning(twilio_configuration_error())
            elif not phone_is_valid:
                st.warning("Enter a verified phone number in E.164 format before sending SMS.")
            if st.button("Send approved SMS", disabled=not (consent and phone_is_valid and sms_configured)):
                try:
                    message = send_sms(candidate_phone, sms_text)
                    update_status(cid, "Contacted", role)
                    st.session_state.completed_steps.add(9)
                    st.success(f"SMS submitted to Twilio. Message SID: {message.sid}")
                except Exception as exc:
                    st.error(str(exc))

        st.subheader("Record candidate response", icon=":material/how_to_reg:")
        response = st.segmented_control(
            "Candidate response",
            ["Pending", "YES – Interested", "NO – Not interested"],
            default="Pending",
        )
        if st.button("Save candidate response"):
            status = {"Pending": "Contacted", "YES – Interested": "Interested", "NO – Not interested": "Not Interested"}[response]
            update_status(cid, status, role)
            if response != "Pending":
                st.session_state.completed_steps.add(10)
            st.success(f"Candidate status updated: {status}")

        st.subheader("Schedule an interview", icon=":material/event:")
        if candidate["status"] != "Interested":
            st.warning("First record the candidate response as YES – Interested.")
        timezone_name = os.getenv("INTERVIEW_TIMEZONE", "Asia/Kolkata")
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            tz = ZoneInfo("Asia/Kolkata")
        interview_date = st.date_input("Interview date", value=(datetime.now(tz) + timedelta(days=1)).date())
        interview_time = st.time_input("Interview time", value=datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0).time())
        duration = st.number_input("Duration (minutes)", min_value=15, max_value=180, value=30, step=15)
        meeting_state_key = f"meeting_link_{cid}"
        if st.button("Create meeting link"):
            st.session_state[meeting_state_key] = create_meeting_link()
        meeting_link = st.text_input(
            "Meeting link",
            value=st.session_state.get(meeting_state_key) or os.getenv("DEFAULT_MEETING_LINK", ""),
            key=f"meeting_link_input_{cid}",
        )
        if meeting_link:
            st.session_state[meeting_state_key] = meeting_link
        schedule_consent = st.checkbox(
            "I authorize sending this interview invitation by email and SMS.",
            key=f"schedule_consent_{cid}",
        )
        schedule_provider_options = ["SMTP (Gmail / Outlook)", "SendGrid"]
        schedule_default_provider = "SendGrid" if sendgrid_is_configured() else schedule_provider_options[0]
        schedule_provider = st.selectbox(
            "Interview email provider",
            schedule_provider_options,
            index=schedule_provider_options.index(schedule_default_provider),
            key=f"schedule_provider_{cid}",
        )
        schedule_email_configured = (
            smtp_is_configured() if schedule_provider.startswith("SMTP") else sendgrid_is_configured()
        )
        if not schedule_email_configured:
            if schedule_provider.startswith("SMTP"):
                st.warning("Configure the selected interview email provider before sending.")
            else:
                st.warning(sendgrid_configuration_error())
        if st.button(
            "Send interview invitation",
            disabled=(
                candidate["status"] != "Interested"
                or not candidate["email"]
                or not meeting_link
                or not schedule_consent
                or not schedule_email_configured
            ),
        ):
            try:
                start = datetime.combine(interview_date, interview_time).replace(tzinfo=tz)
                if start <= datetime.now(tz):
                    raise ValueError("Choose a future interview date and time.")
                ics = make_ics(candidate, role, start, int(duration), meeting_link)
                invite_body = f"""Hello {candidate['name']},

Your interview for {role} is scheduled.

Date and time: {start.strftime('%d %B %Y, %I:%M %p %Z')}
Duration: {duration} minutes
Meeting link: {meeting_link}

The calendar invitation is attached.

Regards,
Recruitment Team"""
                if schedule_provider.startswith("SMTP"):
                    send_email_smtp(
                        candidate["email"], f"Interview Invitation – {role}",
                        invite_body, ics, "interview_invitation.ics"
                    )
                else:
                    send_email_sendgrid(
                        candidate["email"], f"Interview Invitation – {role}",
                        invite_body, ics, "interview_invitation.ics"
                    )

                sms_sent = False
                sms_error = ""
                if phone_is_valid and sms_configured:
                    sms_body = (
                        f"HR: Interview for {role} scheduled for "
                        f"{start.strftime('%d %b %Y, %I:%M %p %Z')}. "
                        f"Duration: {int(duration)} min. Join: {meeting_link}"
                    )
                    try:
                        send_sms(candidate_phone, sms_body)
                        sms_sent = True
                    except Exception as exc:
                        sms_error = str(exc)
                elif not phone_is_valid:
                    sms_error = "Enter a verified E.164 phone number to send the interview SMS."
                else:
                    sms_error = "SMS was not sent because Twilio is not configured."

                update_status(cid, "Interview Scheduled", role)
                st.session_state.completed_steps.add(11)
                message = "Interview email sent with an .ics calendar attachment."
                if sms_sent:
                    message += " SMS submitted to Twilio."
                elif sms_error:
                    message += f" SMS was not sent: {sms_error}"
                else:
                    message += " SMS was not sent because no phone number is available."
                st.success(message)
            except Exception as exc:
                st.error(str(exc))

with tab4:
    st.subheader("Recruiting overview and audit trail", icon=":material/analytics:")
    with db() as conn:
        all_candidates = [dict(row) for row in conn.execute(
            "SELECT candidate_id,name,email,phone,filename,skills,status,job_role,similarity,created_at FROM candidates ORDER BY created_at DESC"
        ).fetchall()]
        activity = [dict(row) for row in conn.execute(
            "SELECT candidate_id,event,details,created_at FROM activity ORDER BY id DESC LIMIT 200"
        ).fetchall()]
    if not all_candidates:
        st.info("No candidates have been processed yet.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total candidates", len(all_candidates), border=True)
        c2.metric("Contacted", sum(row["status"] == "Contacted" for row in all_candidates), border=True)
        c3.metric("Interested", sum(row["status"] == "Interested" for row in all_candidates), border=True)
        c4.metric("Interviews scheduled", sum(row["status"] == "Interview Scheduled" for row in all_candidates), border=True)
        display_candidates = [{key: value for key, value in row.items() if key != "candidate_id"} for row in all_candidates]
        st.json(display_candidates, expanded=True)
        report_csv = io.StringIO()
        writer = csv.DictWriter(report_csv, fieldnames=list(all_candidates[0].keys()))
        writer.writeheader()
        writer.writerows(all_candidates)
        st.download_button(
            "Export candidate report",
            report_csv.getvalue().encode(),
            "hr_candidate_report.csv",
            "text/csv",
            width="stretch",
        )
        st.markdown("**Audit trail**")
        st.json(activity, expanded=True)

with st.sidebar:
    st.header("Workflow wheel")
    done = len(st.session_state.completed_steps)
    percent = int(done / len(steps) * 100)
    st.echarts_chart(
        {
            "series": [{
                "type": "gauge",
                "startAngle": 90,
                "endAngle": -270,
                "min": 0,
                "max": 100,
                "progress": {
                    "show": True,
                    "width": 12,
                    "roundCap": True,
                    "itemStyle": {"color": "#4094FF"},
                },
                "axisLine": {
                    "lineStyle": {"width": 12, "color": [[1, "#293753"]]},
                },
                "pointer": {"show": False},
                "axisTick": {"show": False},
                "splitLine": {"show": False},
                "axisLabel": {"show": False},
                "anchor": {"show": False},
                "title": {"show": False},
                "detail": {
                    "valueAnimation": True,
                    "formatter": "{value}%",
                    "fontSize": 24,
                    "offsetCenter": [0, "0%"],
                    "color": "#F4F7FF",
                },
                "data": [{"value": percent}],
            }],
        },
        height=180,
        alt=f"Workflow progress: {done} of {len(steps)} steps completed ({percent} percent).",
    )
    st.caption(f"{done}/{len(steps)} steps completed")
    for i, label in enumerate(steps, 1):
        if i in st.session_state.completed_steps:
            st.success(f"Step {i} done — {label.split('. ', 1)[-1]}")
        else:
            st.caption(f"Step {i} pending — {label.split('. ', 1)[-1]}")
    if st.button("Reset visual progress"):
        st.session_state.completed_steps = set()
        st.rerun()
