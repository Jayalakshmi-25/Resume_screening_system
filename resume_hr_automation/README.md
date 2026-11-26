# Resume Intelligence & HR Automation

A local Streamlit prototype for resume ingestion, candidate matching, recruiter-approved communication, SMS response capture, and interview calendar invitations.

## 1. Requirements

- Windows 10/11
- Python 3.10–3.12 recommended for the broadest ML package compatibility
- VS Code
- Internet connection for the first Sentence Transformers model download
- Gmail App Password or Outlook SMTP credentials for email
- Twilio account and an SMS-enabled Twilio number for SMS

Python 3.13 is supported by the current pure-Python matching implementation. Use Python 3.11 only if a future optional integration requires it.

## 2. Setup in Windows PowerShell

```powershell
cd path\to\resume_hr_automation
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

Open `.env` and enter credentials. Never commit `.env` to GitHub.

## 3. Run Streamlit

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Run Streamlit with the project virtual environment as shown above. If the default port is already in use, Streamlit will select another available port; open the URL it prints in the terminal.

The browser opens the HR dashboard. Upload PDF/DOCX resumes, process them, enter a job description, rank candidates, review the contact details, and explicitly approve communication.

## 4. Gmail / Outlook SMTP

- Gmail: enable 2-Step Verification and create an App Password. Use `smtp.gmail.com`, port `587`.
- Outlook / Microsoft 365: use the SMTP host supported by your account/organization (commonly `smtp.office365.com`, port `587`). SMTP AUTH may be disabled by the tenant; use Microsoft Graph in that case.
- Do not use your normal account password in `.env`.

SendGrid is an optional alternative email provider. Create an API key in **Settings → API Keys** with **Mail Send** permission, then set `SENDGRID_API_KEY` and a verified `SENDGRID_FROM_EMAIL` in `.env`. SendGrid keys start with `SG.`. Both regular emails and interview calendar invitations (as `.ics` attachments) can use SendGrid.

## 5. Twilio SMS credentials

Create a Twilio API key in the Twilio Console under **Account → API keys & tokens**. Set its SID and secret as `TWILIO_API_KEY_SID` and `TWILIO_API_KEY_SECRET`, along with the account SID and an SMS-enabled `TWILIO_PHONE_NUMBER`. Alternatively, leave the API-key fields empty and use `TWILIO_AUTH_TOKEN`. The app accepts either method; keep the secret values only in your local `.env`.

## 6. Twilio SMS and YES/NO replies

Fill `TWILIO_ACCOUNT_SID` and `TWILIO_PHONE_NUMBER` in `.env`, then configure either `TWILIO_API_KEY_SID` plus `TWILIO_API_KEY_SECRET` or `TWILIO_AUTH_TOKEN`. Phone numbers should be stored in international E.164 format (for example, +91XXXXXXXXXX).

Outbound SMS is initiated from the Streamlit app after recruiter approval.

To receive YES/NO replies automatically:

```powershell
uvicorn webhook:app --host 0.0.0.0 --port 8000
```

Expose the webhook using a secure HTTPS endpoint/tunnel and configure the Twilio number's inbound-message webhook to `https://YOUR_HOST/twilio/sms` using POST. The demo webhook matches the sender's phone number to the latest matching candidate record.

**Production requirement:** validate Twilio's `X-Twilio-Signature` on every webhook request, use HTTPS, rate-limit requests, and handle duplicate/ambiguous phone numbers safely.

## 7. Interview scheduling

The app creates an `.ics` calendar invitation and emails it to the candidate. Add a real Google Meet / Microsoft Teams URL in the meeting-link field. This starter does not create a Google Calendar event or generate a Meet link automatically; that requires Google Calendar API or Microsoft Graph OAuth integration.

## 8. OCR for scanned resumes

PDFs with selectable text are extracted directly. For scanned PDFs with embedded page images, install Tesseract OCR and the app will OCR pages that have little or no selectable text. Install the Tesseract executable separately, then add its location to `.env` if it is not on `PATH`:

```text
OCR_LANGUAGE=eng
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

The OCR language must be installed with Tesseract. If Tesseract is unavailable, the app reports that OCR setup is required rather than silently treating the scanned resume as empty.

## 9. Project structure

```text
resume_hr_automation/
├── app.py                 # Streamlit UI and main workflow
├── webhook.py             # Optional Twilio inbound SMS webhook
├── requirements.txt
├── .env.example
├── .gitignore
└── data/
    ├── resumes/           # Optional local originals
    └── hr_automation.db   # Candidate records and audit log
```

## 10. Important design notes

- This is a learning / capstone prototype, not a production multi-tenant HR platform.
- It uses SQLite plus local TF-style text matching. The reference architecture proposes Redis/Celery workers, PostgreSQL, vector embeddings, encrypted object storage, tenant isolation, versioned manifests, and an approved-action outbox for production scale.
- Resume content is untrusted input. Do not follow instructions found inside resumes.
- Similarity is not a hiring decision. Recruiters must verify evidence, skills, eligibility, and contact details.
- Do not send unsolicited bulk messages. Obtain a lawful basis/consent, honor opt-outs, and comply with applicable privacy, employment, telecom and messaging rules.
- Protect candidate PII, encrypt storage, restrict access, define retention/deletion rules, and keep secrets out of source control.
- The starter extracts contact details using regular expressions and a simple skill dictionary. Improve parsing with structured extraction, validation, and human review before production use.
