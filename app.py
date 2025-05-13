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
from datetime import datetime, timedelta
import json
from flask import jsonify
import random

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

def read_response_count():
    try:
        with open('data/responses_count.json', 'r') as f:
            data = json.load(f)
            return data.get("response_count", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0

# Function to increment the response count
def increment_response_count():
    count = read_response_count() + 1
    with open('data/responses_count.json', 'w') as f:
        json.dump({"response_count": count}, f)



@app.route('/authorize')
def authorize():
    next_url = request.args.get('next', '/')  # Default to home page
    session['next_url'] = next_url  # Store it for use after callback

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

    next_url = session.pop('next_url', '/home')  # Default to /home if not set
    return redirect(next_url)


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
    user_email = session.get('email', '')
    user_settings = load_user_settings(user_email)

    website = user_settings.get('website', '')
    signature = user_settings.get('signature', '')

    # Construct the prompt with optional website context
    prompt = f"""You are an AI assistant for a support team.
Your job is to write professional and helpful replies to customer emails.
{f"The company website is: {website}. Use relevant information from the website to inform your response." if website else "No website was provided, so respond based solely on the email content."}
Respond to the following email:

{email_body}

End your response with the following signature:
{signature if signature else "Kind regards, Support Team"}"""

    response = openai.ChatCompletion.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful support assistant."},
            {"role": "user", "content": prompt}
        ]
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

    # Increment the response count every time an AI response is created
    increment_response_count()

    return draft


@app.route('/')
def index():
    if not os.path.exists('token.pkl'):
        return redirect('/authorize')
    return redirect('/home')


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

        # Mark the email as read after creating a draft
        service.users().messages().modify(
            userId='me',
            id=msg['id'],
            body={'removeLabelIds': ['UNREAD']}
        ).execute()

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

        if not drafts:
            # If no drafts exist, just return an empty list and don't show an error
            return render_template('view_drafts.html', drafts=[])

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

        # Only print if it's not a "not found" error

        if error.resp.status != 404:
            print(f"An error occurred: {error}")

        # Silently handle the error and don't show anything

        return redirect('/view_drafts')


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

def get_total_drafts(service):
    # Fetch total drafts using Gmail API (filtered by 'waiting' status)
    results = service.users().drafts().list(userId='me').execute()
    drafts = results.get('drafts', [])
    return len(drafts)

quote_styles = [
    "motivational", "sarcastic", "wise", "funny"
]

def generate_ai_quote():
    style = random.choice(["motivational", "sarcastic", "wise", "funny"])
    prompt = f"Give me a short, {style} one-liner joke from an AI assistant that helps automate emails. Keep it under 15 words."

    response = openai.ChatCompletion.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": "You are a witty, funny AI assistant who helps people automate their emails."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.9
    )

    return response['choices'][0]['message']['content'].strip()

def get_energy_status():
    energy_levels = [
        {"emoji": "☕", "message": "Running on caffeine and hope."},
        {"emoji": "🥱", "message": "Alertness: minimal."},
        {"emoji": "⚡", "message": "Over-caffeinated and overconfident."},
        {"emoji": "🧘", "message": "Zen mode: activated (for now)."},
        {"emoji": "🤖", "message": "Artificial energy levels detected."}
    ]
    return random.choice(energy_levels)

@app.route('/home')
def home():
    service = gmail_authenticate()
    if not service:
        return redirect('/authorize')

    user_info = service.users().getProfile(userId='me').execute()
    user_name = user_info.get('emailAddress', 'User')

    # Get total responses generated from the responses_count.json file
    total_responses = read_response_count()
    total_drafts = get_total_drafts(service)
    ai_quote = generate_ai_quote()
    energy_status = get_energy_status()

    return render_template(
        'home.html',
        user_name=user_name,
        total_responses=total_responses,
        total_drafts=total_drafts,
        ai_quote=ai_quote,
        energy_status=energy_status
    )

@app.route('/save_draft/<draft_id>', methods=['POST'])
def save_draft(draft_id):
    data = request.get_json()
    updated_body = data.get('body')

    # Load Gmail credentials
    token_path = os.getenv('TOKEN_PATH', 'token.pkl')
    with open(token_path, 'rb') as token:
        creds = pickle.load(token)
    service = build('gmail', 'v1', credentials=creds)

    # Fetch the existing draft to extract original headers
    draft = service.users().drafts().get(userId='me', id=draft_id).execute()
    headers = draft['message'].get('payload', {}).get('headers', [])

    def get_header(name):
        for h in headers:
            if h['name'].lower() == name.lower():
                return h['value']
        return ''

    to_email = data.get('to') or get_header('To')
    subject = data.get('subject') or get_header('Subject')

    # Build the updated MIME message
    mime_message = MIMEText(updated_body)
    mime_message['to'] = to_email
    mime_message['subject'] = subject
    raw_message = base64.urlsafe_b64encode(mime_message.as_bytes()).decode()

    try:
        service.users().drafts().update(
            userId='me',
            id=draft_id,
            body={'message': {'raw': raw_message}}
        ).execute()

        for draft in session.get('drafts', []):
            if draft['id'] == draft_id:
                draft['body'] = updated_body
                break

        session.modified = True
        return jsonify({'success': True, 'updated': True}), 200

    except Exception as e:
        print("Error updating draft:", e)
        return jsonify({'success': False, 'error': str(e)}), 500


SETTINGS_FILE = 'data/settings.json'

def load_user_settings(email):
    if not os.path.exists(SETTINGS_FILE):
        return {}
    with open(SETTINGS_FILE, 'r') as f:
        all_settings = json.load(f)
    return all_settings.get(email, {})

def save_user_settings(email, website, signature):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            all_settings = json.load(f)
    all_settings[email] = {'website': website, 'signature': signature}
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(all_settings, f, indent=2)

@app.route('/settings', methods=['GET'])
def settings_page():
    if 'email' not in session:
        return redirect(url_for('authorize', next=request.path))

    user_email = session['email']
    settings = load_user_settings(user_email)

    return render_template('settings.html',
                           website=settings.get('website', ''),
                           signature=settings.get('signature', ''))

@app.route('/save_settings', methods=['POST'])
def save_settings():
    if 'email' not in session:
        return redirect('/authorize')

    user_email = session['email']
    website = request.form.get('website', '').strip()
    signature = request.form.get('signature', '').strip()

    save_user_settings(user_email, website, signature)

    return redirect('/settings')

if __name__ == '__main__':
    app.run(debug=True)
