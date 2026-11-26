"""
Optional Twilio inbound SMS webhook.
Run: uvicorn webhook:app --host 0.0.0.0 --port 8000
Configure the Twilio phone number's "A message comes in" webhook to:
https://YOUR_PUBLIC_HTTPS_URL/twilio/sms (POST)

For local testing, expose port 8000 using a trusted HTTPS tunnel.
Validate Twilio signatures before production deployment.
"""
import os
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request, Response
from twilio.twiml.messaging_response import MessagingResponse

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hr_automation.db")
app = FastAPI(title="HR Automation SMS Webhook")

@app.post("/twilio/sms")
async def inbound_sms(request: Request):
    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    body = str(form.get("Body", "")).strip().upper()
    reply = MessagingResponse()

    if body not in {"YES", "NO"}:
        reply.message("Please reply YES if interested in an interview, or NO if not interested.")
        return Response(content=str(reply), media_type="application/xml")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    candidate = conn.execute(
        "SELECT candidate_id,name FROM candidates WHERE phone=? ORDER BY created_at DESC LIMIT 1",
        (from_number,)
    ).fetchone()

    if not candidate:
        reply.message("We could not match this phone number. Please contact the HR team.")
    else:
        status = "Interested" if body == "YES" else "Not Interested"
        conn.execute("UPDATE candidates SET status=? WHERE candidate_id=?", (status, candidate["candidate_id"]))
        conn.execute(
            "INSERT INTO activity(candidate_id,event,details,created_at) VALUES(?,?,?,?)",
            (candidate["candidate_id"], "SMS response received", body, datetime.now().isoformat(timespec="seconds"))
        )
        conn.commit()
        reply.message("Thank you. Your response has been recorded. HR will contact you with next steps.")
    conn.close()
    return Response(content=str(reply), media_type="application/xml")
