from flask import Flask, render_template, redirect, request, session, url_for
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import os
import pickle
import openai
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import base64

load_dotenv()
openai.api_key = os.getenv('OPENAI_API_KEY')

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'supersecretkey')  # Needed for session management
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# Function to save the credentials from base64 string stored in environment variable
def save_credentials_from_env():
    credentials_json = os.getenv('GOOGLE_CREDENTIALS_JSON')  # Base64-encoded creds
    if credentials_json:
        with open('credentials.json', 'wb') as f:
            f.write(base64.b64decode(credentials_json))

save_credentials_from_env()  # Save credentials on app start

def gmail_authenticate():
    creds = None
    if os.path.exists('token.pkl'):
        with open('token.pkl', 'rb') as token:
            creds = pickle.load(token)
    if not creds:
        return None
    return build('gmail', 'v1', credentials=creds)


@app.route('/authorize')
def authorize():
    flow = Flow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent'
    )
    session['state'] = state
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = session['state']
    flow = Flow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    flow.fetch_token(authorization_response=request.url)

    creds = flow.credentials
    with open('token.pkl', 'wb') as token:
        pickle.dump(creds, token)

    return redirect('/dashboard')


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
            elif part['mimeType'] == 'text/html':
                body = part['body']['data']
    body = base64.urlsafe_b64decode(body).decode('utf-8')

    return subject, sender, body, date


def generate_email_response(email_body):
    response = openai.ChatCompletion.create(
        model="gpt-4-turbo",  # Using GPT-4-turbo for the AI respons
        messages=[{"role": "system", "content": "You are a helpful assistant."},
                  {"role": "user", "content": f"Respond to the following email:\n\n{email_body}"}]
    )
    return response.choices[0].message.content


def summarize_email(email_body):
    # Request a summary from OpenAI (You can adjust the prompt to be more specific)
    summary = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "system", "content": "You are a helpful assistant."},
                  {"role": "user", "content": f"Summarize the following email:\n\n{email_body}"}]
    )
    return summary.choices[0].message.content.strip()


def create_draft(service, sender, subject, recipient, body):
    # Summarize the user's email content
    email_summary = summarize_email(body)

    # Prepare the draft body with summarized content and AI response
    visual_body = f"""
    ----- Original Message -----
    To: {recipient}
    Subject: {subject}

    Summary: {email_summary}

    ---

    {generate_email_response(body)}
    """

    # Create the message
    message = MIMEMultipart()
    message['to'] = recipient
    message['subject'] = "Re: " + subject

    msg = MIMEText(visual_body)
    message.attach(msg)

    # Encode the message to raw format
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

    # Create the draft
    draft = service.users().drafts().create(
        userId="me", body={'message': {'raw': raw_message}}).execute()
    return draft


@app.route('/')
def index():
    if not os.path.exists('token.pkl'):
        return redirect('/authorize')
    return redirect('/dashboard')


@app.route('/dashboard')
def dashboard():
    service = gmail_authenticate()
    if not service:
        return redirect('/authorize')

    results = service.users().messages().list(userId='me', labelIds=['INBOX'], q='is:unread', maxResults=10).execute()
    messages = results.get('messages', [])

    email_data = []
    for msg in messages:
        subject, sender, body, _ = get_email_content(service, msg['id'])
        # Create draft including summary + AI response
        draft = create_draft(service, 'me', subject, sender, body)

        print(f"Draft created with ID: {draft['id']}")  # For debugging

        email_data.append({
            'id': msg['id'],
            'subject': subject,
            'snippet': body[:150]
        })

    return render_template('dashboard.html', messages=email_data)


@app.route('/view_drafts')
def view_drafts():
    service = gmail_authenticate()
    if not service:
        return redirect('/authorize')

    try:
        results = service.users().drafts().list(userId='me').execute()
        drafts = results.get('drafts', [])

        draft_details = []
        for draft in drafts:
            draft_id = draft['id']
            draft_detail = service.users().drafts().get(userId='me', id=draft_id).execute()
            message = draft_detail.get('message', {})
            headers = message.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')

            # Attempt to retrieve the body from parts (handling plain-text and HTML)
            body = 'No body content found.'
            parts = message.get('payload', {}).get('parts', [])

            if parts:
                for part in parts:
                    # Check if part is text/plain or text/html
                    if part.get('mimeType') == 'text/plain' or part.get('mimeType') == 'text/html':
                        data = part.get('body', {}).get('data', '')
                        if data:
                            body = base64.urlsafe_b64decode(data).decode('utf-8')
                            break  # We found a part, no need to continue

            # If no parts found, check for body directly (for single part emails)
            if not body and 'body' in message.get('payload', {}):
                body_data = message.get('payload', {}).get('body', {}).get('data', '')
                if body_data:
                    body = base64.urlsafe_b64decode(body_data).decode('utf-8')

            draft_details.append({
                'id': draft_id,
                'subject': subject,
                'body': body,

            })

        return render_template('view_drafts.html', drafts=draft_details)

    except HttpError as error:
        print(f'An error occurred: {error}')
        return redirect('/error')  # You can define an error page here


@app.route('/send_draft/<draft_id>', methods=['POST'])
def send_draft(draft_id):
    service = gmail_authenticate()
    if not service:
        return redirect('/authorize')

    try:
        # Get the draft content
        draft = service.users().drafts().get(userId='me', id=draft_id).execute()
        message = draft['message']

        # Send the existing draft properly
        service.users().drafts().send(userId='me', body={'id': draft_id}).execute()

        # Optionally delete the draft now that it's sent
        service.users().drafts().delete(userId='me', id=draft_id).execute()

    except Exception as e:
        print(f"Error sending draft: {e}")

    return redirect(url_for('view_drafts'))


if __name__ == '__main__':
    app.run(debug=True)