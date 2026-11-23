"""SMTP notification for the email response mode (Chapter 4.8).

Sends a structured alert when SMTP is configured in the environment; otherwise it
reports that email is not configured so the orchestrator degrades honestly to
composing the message without sending.
"""
import datetime
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from dotenv import dotenv_values

MIN_SECONDS = 20                    # a job that ends quicker than this and did not fail is not worth a letter
MODES = ("failed", "all")


def _cfg(key, default=None):
    return os.getenv(key) or dotenv_values(".env").get(key) or default


def smtp_configured() -> bool:
    return bool(_cfg("SMTP_HOST"))


def mode() -> str:
    """When the person wants a letter about a finished job: ``failed``, ``all``, or "" for never (Settings, Notifications)."""
    m = (os.getenv("ZTSA_NOTIFY_EMAIL") or "").strip().lower()
    return m if m in MODES else ""


def _seconds(job: dict) -> float | None:
    try:
        a = datetime.datetime.fromisoformat(str(job["started"]).replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(str(job["finished"]).replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds())
    except (KeyError, ValueError):
        return None


def should_send(job: dict, chosen: str | None = None) -> bool:
    """Whether a finished job gets a letter: never when the person asked for none or when SMTP is not set up; never for a job the person cancelled themselves or one the app interrupted;
    always for a failure when any letter is wanted; for a success only with ``all`` and only when it took long enough to be worth one."""
    chosen = mode() if chosen is None else chosen
    if not chosen or not smtp_configured() or job.get("status") in ("cancelled", "interrupted", "running"):
        return False
    if job.get("status") == "error":
        return True
    if (job.get('stage') == 'qualify_npm_dataset' and job.get('status') == 'done'
            and (job.get('progress') or {}).get('state') == 'waiting'):
        return False
    secs = _seconds(job)
    return chosen == "all" and secs is not None and secs >= MIN_SECONDS


def scrub(line: str) -> str:
    """A line of a job's output made fit to leave this computer in a letter: credentials masked, the user's folder and the app's folder replaced, one line of at most 240 characters."""
    from app.backend.pipeline.scan import code_rules
    out = code_rules.mask(line)
    for path, alias in ((os.getcwd(), "."), (os.path.expanduser("~"), "~")):
        if path and len(path) > 3:
            out = re.sub(re.escape(path), alias, out, flags=re.I)
            out = re.sub(re.escape(path.replace("\\", "/")), alias, out, flags=re.I)
    out = re.sub(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+", "~", out)
    return out.strip()[:240]


def describe(job: dict, tail: list[str]) -> tuple[str, str]:
    """The subject and the body of the letter. It leaves the computer, so it is short: the job, how long it took and, for a failure, the last line of the error and the last three lines
    of output. The job's parameters are not in it (they can hold paths and names), credentials are masked and the user's folders are replaced by ~ and ."""
    ok = job.get("status") == "done"
    secs = _seconds(job)
    took = f" in {int(secs // 60)} min {int(secs % 60)} s" if secs is not None and secs >= 60 else f" in {int(secs or 0)} s" if secs is not None else ""
    subject = f"[AI Vulnerability Framework] {job.get('stage', 'job').replace('_', ' ')} {'finished' if ok else 'failed'}"
    lines = [f"The job {job.get('stage')} ({job.get('id')}) {'finished' if ok else 'failed'}{took}.", ""]
    if not ok:
        last = next((l for l in reversed(str(job.get("error") or "").splitlines()) if l.strip()), "")
        if last:
            lines += ["What went wrong (the last line of the error):", scrub(last), ""]
        shown = [scrub(l) for l in (tail or [])[-3:] if l.strip()]
        if shown:
            lines += ["The last lines of its output:", *shown, ""]
    lines.append("Open the Jobs list of the app for the full output.")
    return subject, "\n".join(lines)


def email_job(job: dict, tail: list[str] | None = None) -> dict:
    """Send the letter about a finished job when the settings ask for one. Never raises: a letter that cannot be sent must not disturb the job's record."""
    try:
        if not should_send(job):
            return {"sent": False, "reason": "not wanted"}
        subject, body = describe(job, tail or [])
        return send_email(subject, body)
    except Exception as e:  # ignored: a failure to write the letter is reported to the caller, never raised into the job
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


class SmtpRefused(Exception):
    """The connection to the mail server was not made, with a sentence for the person."""


def connect(host: str, port: int, user: str | None, password: str | None):
    """An open connection to the mail server, logged in when a user and password are given. The password is sent only over an encrypted connection: a server that does not offer
    STARTTLS is used without login (a local relay) or refused when a password was given, never sent the password in the clear."""
    context = ssl.create_default_context()                  # the certificate and the host name are checked; smtplib's own default checks neither
    try:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=context) if port == 465 else smtplib.SMTP(host, port, timeout=20)
    except ssl.SSLCertVerificationError as e:
        raise SmtpRefused(f"The certificate of {host} could not be verified ({e.verify_message or 'not trusted'}), so nothing was sent.") from None
    try:
        server.ehlo()
        if port == 465:
            pass                                             # encrypted from the first byte
        elif server.has_extn("starttls"):
            try:
                server.starttls(context=context)
            except ssl.SSLCertVerificationError as e:
                raise SmtpRefused(f"The certificate of {host} could not be verified ({e.verify_message or 'not trusted'}), so the password was not sent.") from None
            server.ehlo()
        elif user and password:
            raise SmtpRefused(f"{host} does not offer an encrypted connection, so the password was not sent.")
        if user and password:
            server.login(user, password)
        return server
    except Exception:
        try:
            server.close()
        except Exception:  # ignored: closing a connection that is already broken
            pass
        raise


def send_email(subject: str, body: str) -> dict:
    if not smtp_configured():
        return {"sent": False, "reason": "SMTP not configured"}
    host = _cfg("SMTP_HOST")
    port = int(_cfg("SMTP_PORT", "587"))
    user = _cfg("SMTP_USER")
    password = _cfg("SMTP_PASSWORD")
    sender = _cfg("SMTP_FROM", user or "noreply@localhost")
    to = _cfg("SMTP_TO", user or "")
    if not to:
        return {"sent": False, "reason": "no SMTP_TO configured"}
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)
    try:
        s = connect(host, port, user, password)
        try:
            s.send_message(msg)
        finally:
            s.quit()
        return {"sent": True, "to": to}
    except Exception as e:
        return {"sent": False, "reason": str(e)}
