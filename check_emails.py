import os
import pickle
import base64
from googleapiclient.discovery import build
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import openai

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']


def gmail_authenticate():
    creds = None
    if os.path.exists('token.pkl'):
        with open('token.pkl', 'rb') as token:
            creds = pickle.load(token)
    if not creds:
        return None
    return build('gmail', 'v1', credentials=creds)


def get_email_content(service, message_id):
    msg_detail = service.users().messages().get(userId='me', id=message_id).execute()
    payload = msg_detail['payload']
    headers = payload['headers']

    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '(No Subject)')
    sender = next((h['value'] for h in headers if h['name'] == 'From'), '(No Sender)')
    date = next((h['value'] for h in headers if h['name'] == 'Date'), '(No Date)')

    body = ''
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain':
                body = part['body']['data']
                break
            elif part['mimeType'] == 'text/html':
                body = part['body']['data']
    else:
        body = payload['body'].get('data', '')

    if body:
        body = base64.urlsafe_b64decode(body).decode('utf-8', errors='ignore')

    return subject, sender, body, date


def generate_email_response(email_body):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"Respond to the following email:\n\n{email_body}"}
        ]
    )
    return response.choices[0].message.content


def create_draft(service, sender, subject, recipient, body):
    message = MIMEMultipart()
    message['to'] = recipient
    message['subject'] = "Re: " + subject
    msg = MIMEText(body)
    message.attach(msg)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

    draft = service.users().drafts().create(
        userId="me", body={'message': {'raw': raw_message}}).execute()
    print(f"✅ Draft created for: {recipient} (ID: {draft['id']})")
    return draft


def check_unread_and_create_drafts():
    service = gmail_authenticate()
    if not service:
        print("❌ Authentication failed.")
        return

    results = service.users().messages().list(userId='me', labelIds=['INBOX'], q='is:unread', maxResults=10).execute()
    messages = results.get('messages', [])

    if not messages:
        print("📭 No unread messages found.")
        return

    for msg in messages:
        msg_id = msg['id']
        subject, sender, body, _ = get_email_content(service, msg_id)
        if not body:
            continue

        ai_response = generate_email_response(body)
        create_draft(service, 'me', subject, sender, ai_response)


if __name__ == "__main__":
    check_unread_and_create_drafts()
