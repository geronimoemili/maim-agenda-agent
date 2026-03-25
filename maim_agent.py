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
# Assicurati che questi nomi corrispondano esattamente ai Secrets su GitHub
GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASS = os.getenv('GMAIL_PASSWORD')
GEMINI_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
RECIPIENT_ME = "g.emili@maimgroup.com"

# Inizializzazione Client
# Se i segreti mancano, il programma darà un errore chiaro qui
if not all([SUPABASE_URL, SUPABASE_KEY, GEMINI_KEY]):
    print("ERRORE: Secrets non configurati correttamente su GitHub.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

# Usiamo gemini-1.5-flash per massima compatibilità e velocità
model = genai.GenerativeModel('gemini-1.5-flash')

def get_gmail_connection():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASS)
    return mail

def fetch_new_emails():
    conn = get_gmail_connection()
    conn.select("inbox")
    # Cerchiamo solo le mail NON LETTE
    status, messages = conn.search(None, 'UNSEEN')
    texts = []
    if status == 'OK':
        for num in messages[0].split():
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
            
            # Segna come letta dopo il prelievo
            conn.store(num, '+FLAGS', '\\Seen')
    conn.logout()
    return texts

def extract_and_deduplicate(raw_texts):
    # Recuperiamo gli ultimi appuntamenti per aiutare l'IA a non duplicare
    try:
        res_db = supabase.table("appointments").select("*").order("data", desc=True).limit(30).execute()
        existing = res_db.data
    except Exception as e:
        print(f"Nota: Impossibile leggere DB, procedo senza storico: {e}")
        existing = []

    prompt = f"""
    Sei l'assistente di Maim Group. Analizza i testi delle email ed estrai esclusivamente gli appuntamenti (convegni, meeting, compleanni, lanci stampa).
    CONFRONTA con questi già esistenti per evitare duplicati: {json.dumps(existing)}
    
    Restituisci solo un array JSON con questi campi:
    - data (formato YYYY-MM-DD)
    - ora (formato HH:MM o null)
    - luogo (null o stringa)
    - descrizione (usa icone WhatsApp e grassetti per i nomi propri)
    - categoria (es. Evento, Comunicazione, Scadenza)

    REGOLE:
    1. Se l'appuntamento è già presente, ignoralo.
    2. Restituisci SOLO il codice JSON, niente introduzioni.
    
    Testi da analizzare:
    {chr(10).join(raw_texts)}
    """
    
    try:
        response = model.generate_content(prompt)
        # Pulizia rigorosa del JSON
        text_content = response.text.strip()
        if "```json" in text_content:
            text_content = text_content.split("```json")[1].split("```")[0].strip()
        elif "```" in text_content:
            text_content = text_content.split("```")[1].split("```")[0].strip()
        
        return json.loads(text_content)
    except Exception as e:
        print(f"Errore analisi IA: {e}")
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
        print(f"Mail inviata con successo: {subject}")
    except Exception as e:
        print(f"Errore invio mail: {e}")

def format_for_wa(title, events):
    if not events:
        return f"*{title}*\n_Nessun impegno in archivio per questa data._\n\n"
    
    res = f"*{title}*\n\n"
    # Ordiniamo per ora se disponibile
    for e in events:
        ora = f" 🕒 {e['ora']}" if e.get('ora') else ""
        luogo = f" 📍 {e['luogo']}" if e.get('luogo') else ""
        res += f"• {e['descrizione']}{ora}{luogo}\n"
    return res + "\n"

def main(mode):
    print(f"Avvio modalità: {mode}")
    
    if mode == "ingest":
        raw = fetch_new_emails()
        if raw:
            news = extract_and_deduplicate(raw)
            if news and isinstance(news, list):
                supabase.table("appointments").insert(news).execute()
                print(f"Inseriti {len(news)} nuovi appuntamenti.")
            else:
                print("Nessun nuovo appuntamento rilevato.")
        else:
            print("Nessuna nuova mail da leggere.")

    elif mode == "report_0700":
        today = datetime.date.today().isoformat()
        res = supabase.table("appointments").select("*").eq("data", today).execute()
        content = format_for_wa(f"MAIM - APPUNTAMENTI DI OGGI ({today})", res.data)
        send_mail(f"Maim - Appuntamenti di Oggi", content)

    elif mode == "report_1900":
        tmrw = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        res_t = supabase.table("appointments").select("*").eq("data", tmrw).execute()
        
        # Recupero anche i prossimi 7 giorni
        next_week = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
        res_w = supabase.table("appointments").select("*").gt("data", tmrw).lte("data", next_week).order("data").execute()
        
        content = format_for_wa(f"MAIM - DOMANI ({tmrw})", res_t.data)
        content += format_for_wa("PROSSIMI 7 GIORNI", res_w.data)
        send_mail(f"Maim - Appuntamenti di Domani", content)

if __name__ == "__main__":
    import sys
    # Se non viene passato un argomento, di default fa ingestion
    run_mode = sys.argv[1] if len(sys.argv) > 1 else "ingest"
    main(run_mode)