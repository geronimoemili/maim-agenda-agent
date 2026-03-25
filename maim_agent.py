import os
import smtplib
import imaplib
import email
import json
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from supabase import create_client, Client

# --- CONFIGURAZIONE ---
GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASS = os.getenv('GMAIL_PASSWORD')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
RECIPIENT_ME = "g.emili@maimgroup.com"

# Verifica iniziale dei segreti
if not all([GMAIL_USER, GMAIL_PASS, GEMINI_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("!!! ERRORE CRITICO: Uno o più Secrets mancano su GitHub Settings.")

# Inizializzazione Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    genai.configure(api_key=GEMINI_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    print(f"!!! ERRORE INIZIALIZZAZIONE: {e}")

def get_gmail_connection():
    print(f"Tentativo connessione IMAP per: {GMAIL_USER}")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASS)
    return mail

def fetch_new_emails():
    try:
        conn = get_gmail_connection()
        conn.select("inbox")
        # Cerchiamo solo le mail NON LETTE
        status, messages = conn.search(None, 'UNSEEN')
        texts = []
        if status == 'OK':
            email_ids = messages[0].split()
            print(f"Mail non lette trovate: {len(email_ids)}")
            for num in email_ids:
                _, data = conn.fetch(num, '(RFC822)')
                msg = email.message_from_bytes(data[0][1])
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body += payload.decode(errors='ignore')
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode(errors='ignore')
                
                if body.strip():
                    texts.append(body)
                
                # Segna come letta
                conn.store(num, '+FLAGS', '\\Seen')
        conn.logout()
        return texts
    except Exception as e:
        print(f"!!! ERRORE GMAIL (fetch): {e}")
        return []

def extract_and_deduplicate(raw_texts):
    print("Invio dati a Gemini per estrazione...")
    try:
        res_db = supabase.table("appointments").select("*").order("data", desc=True).limit(20).execute()
        existing = res_db.data
    except Exception as e:
        print(f"Nota: Impossibile leggere storico DB (procedo): {e}")
        existing = []

    prompt = f"""
    Sei l'assistente di Maim Group. Analizza i testi ed estrai gli appuntamenti.
    CONFRONTA con questi già esistenti per evitare duplicati: {json.dumps(existing)}
    Restituisci solo un array JSON [{{ "data": "YYYY-MM-DD", "ora": "HH:MM", "luogo": "...", "descrizione": "...", "categoria": "..." }}]
    Testi: {chr(10).join(raw_texts)}
    """
    
    try:
        response = model.generate_content(prompt)
        text_content = response.text.strip()
        # Pulizia blocchi di codice markdown
        if "```" in text_content:
            text_content = text_content.split("```")[1].replace("json", "").strip()
        
        data = json.loads(text_content)
        print(f"Gemini ha estratto {len(data)} potenziali appuntamenti.")
        return data
    except Exception as e:
        print(f"!!! ERRORE IA/JSON: {e}")
        return []

def send_mail(subject, content):
    msg = MIMEMultipart()
    msg['From'] = GMAIL_USER
    msg['To'] = RECIPIENT_ME
    msg['Subject'] = subject
    msg.attach(MIMEText(content, 'plain'))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.send_message(msg)
        print("Report inviato via mail.")
    except Exception as e:
        print(f"!!! ERRORE INVIO MAIL: {e}")

def format_for_wa(title, events):
    if not events: return f"*{title}*\n_Nessun impegno_\n\n"
    res = f"*{title}*\n\n"
    for e in events:
        ora = f" 🕒 {e.get('ora') or ''}"
        luogo = f" 📍 {e.get('luogo') or ''}"
        res += f"• {e['descrizione']}{ora}{luogo}\n"
    return res + "\n"

def main(mode):
    print(f"--- START AGENT (Mode: {mode}) ---")
    
    if mode == "ingest":
        raw = fetch_new_emails()
        if raw:
            news = extract_and_deduplicate(raw)
            if news:
                print("Tentativo di scrittura su Supabase...")
                try:
                    res = supabase.table("appointments").insert(news).execute()
                    print(f"Successo Supabase: {res}")
                except Exception as e:
                    print(f"!!! ERRORE SCRITTURA SUPABASE: {e}")
            else:
                print("Nessun nuovo dato da inserire.")
        else:
            print("Nessuna mail 'Non Letta' trovata in Inbox.")

    elif mode == "report_0700" or mode == "report_1900":
        target_date = datetime.date.today()
        if mode == "report_1900":
            target_date += datetime.timedelta(days=1)
        
        print(f"Generazione report per data: {target_date}")
        res = supabase.table("appointments").select("*").eq("data", target_date.isoformat()).execute()
        content = format_for_wa(f"MAIM AGENDA - {target_date}", res.data)
        send_mail(f"Maim Agenda - {target_date}", content)

if __name__ == "__main__":
    import sys
    run_mode = sys.argv[1] if len(sys.argv) > 1 else "ingest"
    main(run_mode)