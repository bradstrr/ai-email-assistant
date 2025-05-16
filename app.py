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
import json
from flask import jsonify
import random
from flask import flash
from google.auth.transport.requests import Request
import firebase_admin
from firebase_admin import credentials, firestore

# Load the service account JSON string from env var
service_account_json_str = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')

# Parse JSON string into dict
service_account_info = json.loads(service_account_json_str)

# Write the JSON to a temp file
with tempfile.NamedTemporaryFile(mode='w+', suffix='.json', delete=False) as temp:
    json.dump(service_account_info, temp)
    temp.flush()  # Ensure data is written
    temp_path = temp.name

# Use the temp file path to create credentials
cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)

# Get Firestore client
db = firestore.client()

TOKEN_DIR = 'tokens'  # Make sure this directory exists
os.makedirs(TOKEN_DIR, exist_ok=True)

def get_token_path(user_email):
    """Generate token path for each user based on email"""
    safe_email = user_email.replace('@', '_at_')  # basic sanitization
    return os.path.join(TOKEN_DIR, f'{safe_email}_token.pkl')

def save_user_credentials(creds, user_email):
    """Save user's credentials to their own token file"""
    token_path = get_token_path(user_email)
    with open(token_path, 'wb') as token_file:
        pickle.dump(creds, token_file)

def load_user_credentials(user_email):
    """Load credentials for the user"""
    token_path = get_token_path(user_email)
    creds = None

    if os.path.exists(token_path):
        with open(token_path, 'rb') as token_file:
            creds = pickle.load(token_file)

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_user_credentials(creds, user_email)

    return creds

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


def gmail_authenticate(user_email):
    creds = load_user_credentials(user_email)  # Using your existing logic to load the user's token

    if not creds:
        return None

    return build('gmail', 'v1', credentials=creds)

def read_response_count(user_email):
    doc_ref = db.collection('response_counts').document(user_email)
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        return data.get("response_count", 0)
    else:
        return 0

def increment_response_count(user_email):
    doc_ref = db.collection('response_counts').document(user_email)
    doc = doc_ref.get()
    if doc.exists:
        current_count = doc.to_dict().get("response_count", 0) + 1
    else:
        current_count = 1
    doc_ref.set({"response_count": current_count})


@app.route('/authorize')
def authorize():
    # Check if the user is already authenticated (e.g., via session)
    if 'credentials' in session:  # User is already authenticated
        return redirect(url_for('home'))  # Or whatever page you want them to go to after they are logged in

    # If the user is not authenticated, start the OAuth flow
    flow = Flow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)  # This should match your Google Console redirect URI
    )

    # Generate the authorization URL
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent'
    )

    # Store the state in session to validate on callback
    session['state'] = state

    # Redirect the user to the authorization URL
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')

    if not state:
        # Handle the error where session state is missing
        return redirect(url_for('index'))  # Or redirect to an error page

    flow = Flow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )

    # Fetch the token and validate
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials

    # Build Gmail service to get user email
    service = build('gmail', 'v1', credentials=creds)
    try:
        user_info = service.users().getProfile(userId='me').execute()
        user_email = user_info['emailAddress']
    except Exception as e:
        # Handle any error during the fetching of user profile (e.g. network failure)
        return f"Failed to fetch user info: {str(e)}", 500

    # Save email in session
    session['email'] = user_email

    # Save credentials to a unique file per user
    token_path = get_token_path(user_email)
    try:
        with open(token_path, 'wb') as token_file:
            pickle.dump(creds, token_file)
    except Exception as e:
        # Handle any error saving the token
        return f"Failed to save token: {str(e)}", 500

    # Redirect to the next page (home or whatever page was stored)
    next_url = session.pop('next_url', '/home')
    return redirect(next_url)


def get_email_content(service, message_id, user_email):
    creds = load_user_credentials(user_email)  # Load user-specific credentials
    if not creds:
        # Handle missing or expired credentials
        return None

    # Now use the service with the correct user credentials
    service = build('gmail', 'v1', credentials=creds)
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

    user_email = session.get('email')
    if user_email:
        increment_response_count(user_email)

    return draft

@app.route('/start')
def index():
    user_email = session.get('email')  # Get the user email from session

    if not user_email:
        return redirect('/authorize')  # Redirect if no user is logged in

    token_path = f'tokens/{user_email}.pkl'

    # Check if the user's token file exists
    if not os.path.exists(token_path):
        return redirect('/authorize')
    return redirect('/home')

@app.route('/', methods=['GET', 'POST'])
def landing():
    if request.method == 'POST':
        email = request.form.get('email')
        if email:
            # Save to file
            with open('contact_emails.txt', 'a') as f:
                f.write(email + '\n')
            flash("Thanks! We'll be in touch.")
            return redirect('/')
    return render_template('landing.html')

@app.route('/faq')
def faq():
    return render_template('faq.html')

@app.route('/contact', methods=['POST'])
def contact():
    name = request.form.get('name')
    email = request.form.get('email')
    message = request.form.get('message')

    if not email:
        flash("Please enter your email.")
        return redirect('/')

    try:
        # Add a new document to the 'contacts' collection
        doc_ref = db.collection('contacts').document()
        doc_ref.set({
            'name': name,
            'email': email,
            'message': message,
        })

        flash("Thanks! We'll be in touch.")
        return redirect('/')
    except Exception as e:
        print(f"Error saving contact info: {e}")
        flash("Sorry, there was an error. Please try again later.")
        return redirect('/')


@app.route('/verify-pin', methods=['POST'])
def verify_pin():
    data = request.get_json()
    username = data.get('username')
    pin = data.get('pin')

    # Admin quick check for your master admin pin (optional)
    if username == 'ADMIN' and pin == '2606':
        return jsonify({'success': True})

    # Check Firestore for username's pin
    doc_ref = db.collection('client_pins').document(username)
    doc = doc_ref.get()

    if doc.exists:
        stored_pin = doc.to_dict().get('pin')
        if stored_pin == pin:
            return jsonify({'success': True})

    return jsonify({'success': False})

@app.route('/add-client-pin', methods=['POST'])
def add_client_pin():
    data = request.get_json()
    admin_key = data.get('admin_key')  # simple admin auth, e.g. your '2606' pin or some secret
    if admin_key != '2606':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    username = data.get('username')
    pin = data.get('pin')
    if not username or not pin:
        return jsonify({'success': False, 'error': 'Missing username or pin'}), 400

    # Add or update client pin in Firestore
    doc_ref = db.collection('client_pins').document(username)
    doc_ref.set({'pin': pin})

    return jsonify({'success': True, 'message': f'Pin set for user {username}'})



@app.route('/terms-of-service')
def terms_of_service():
    return render_template('terms-of-service.html')

@app.route('/privacy-policy')
def privacy_policy():
    return render_template('privacy-policy.html')

@app.route('/dashboard')
def dashboard():
    # Use the logged-in user's email from session
    user_email = session.get('email')
    if not user_email:
        return redirect('/authorize')

    # Load user credentials based on the logged-in user's email
    creds = load_user_credentials(user_email)
    if not creds:
        return redirect('/authorize')

    service = build('gmail', 'v1', credentials=creds)

    results = service.users().messages().list(userId=user_email, labelIds=['INBOX'], q='is:unread', maxResults=10).execute()
    messages = results.get('messages', [])

    email_data = []
    for msg in messages:
        # Pass user_email to the get_email_content function
        subject, sender, body, _ = get_email_content(service, msg['id'], user_email)  # Pass user_email

        # Create draft including summary + AI response
        draft = create_draft(service, user_email, subject, sender, body)  # Pass user_email

        print(f"Draft created with ID: {draft['id']}")  # For debugging

        # Mark the email as read after creating a draft
        service.users().messages().modify(
            userId=user_email,
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
    user_email = session.get('email')
    if not user_email:
        return redirect('/authorize')  # Redirect if user email is not found

    service = gmail_authenticate(user_email)
    if not service:
        return redirect('/authorize')

    try:
        results = service.users().drafts().list(userId=user_email).execute()
        drafts = results.get('drafts', [])

        if not drafts:
            return render_template('view_drafts.html', drafts=[])

        draft_details = []
        for draft in drafts:
            draft_id = draft['id']
            draft_detail = service.users().drafts().get(userId=user_email, id=draft_id).execute()
            message = draft_detail.get('message', {})
            headers = message.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')

            body = 'No body content found.'
            parts = message.get('payload', {}).get('parts', [])

            if parts:
                for part in parts:
                    if part.get('mimeType') == 'text/plain' or part.get('mimeType') == 'text/html':
                        data = part.get('body', {}).get('data', '')
                        if data:
                            body = base64.urlsafe_b64decode(data).decode('utf-8')
                            break

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
        if error.resp.status != 404:
            print(f"An error occurred: {error}")
        return redirect('/view_drafts')


@app.route('/send_draft/<draft_id>', methods=['POST'])
def send_draft(draft_id):
    user_email = session.get('email')  # Get the user's email from the session

    if not user_email:
        # If there's no user email in the session, redirect to authorize route
        return redirect('/authorize')

    service = gmail_authenticate(user_email)
    if not service:
        return redirect('/authorize')

    try:
        # Get the draft content
        draft = service.users().drafts().get(userId=user_email, id=draft_id).execute()
        message = draft['message']

        # Send the existing draft properly
        service.users().drafts().send(userId=user_email, body={'id': draft_id}).execute()

        # Optionally delete the draft now that it's sent
        service.users().drafts().delete(userId=user_email, id=draft_id).execute()

    except Exception as e:
        print(f"Error sending draft: {e}")

    return redirect(url_for('view_drafts'))

def get_total_drafts(service):
    user_email = session.get('email')  # Get the user's email from the session

    if not user_email:
        # If there's no user email in the session, return 0 or handle as needed
        return 0

    # Fetch total drafts using Gmail API (filtered by 'waiting' status)
    results = service.users().drafts().list(userId=user_email).execute()
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
    # Retrieve the user's email from the session
    user_email = session.get('email')
    if not user_email:
        return redirect('/authorize')

    # Pass the email into gmail_authenticate
    service = gmail_authenticate(user_email)
    if not service:
        return redirect('/authorize')

    total_responses = read_response_count(user_email)
    total_drafts = get_total_drafts(service)
    ai_quote = generate_ai_quote()
    energy_status = get_energy_status()

    return render_template(
        'home.html',
        user_name=user_email,
        total_responses=total_responses,
        total_drafts=total_drafts,
        ai_quote=ai_quote,
        energy_status=energy_status
    )

@app.route('/save_draft/<draft_id>', methods=['POST'])
def save_draft(draft_id):
    data = request.get_json()
    updated_body = data.get('body')

    user_email = session.get('email')
    if not user_email:
        return jsonify({'success': False, 'error': 'User not logged in'}), 401

    # Use the same Gmail auth method as send_draft
    service = gmail_authenticate(user_email)
    if not service:
        return jsonify({'success': False, 'error': 'Failed to authenticate with Gmail'}), 403

    try:
        # Get the current draft
        draft = service.users().drafts().get(userId=user_email, id=draft_id).execute()
        headers = draft['message'].get('payload', {}).get('headers', [])

        def get_header(name):
            for h in headers:
                if h['name'].lower() == name.lower():
                    return h['value']
            return ''

        to_email = data.get('to') or get_header('To')
        subject = data.get('subject') or get_header('Subject')

        # Create new MIME message
        mime_message = MIMEText(updated_body)
        mime_message['to'] = to_email
        mime_message['subject'] = subject
        raw_message = base64.urlsafe_b64encode(mime_message.as_bytes()).decode()

        # Update the draft
        service.users().drafts().update(
            userId=user_email,
            id=draft_id,
            body={'message': {'raw': raw_message}}
        ).execute()

        # Update the session-stored draft
        for draft_item in session.get('drafts', []):
            if draft_item['id'] == draft_id:
                draft_item['body'] = updated_body
                break
        session.modified = True

        return jsonify({'success': True, 'updated': True}), 200

    except Exception as e:
        print(f"Error updating draft {draft_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def load_user_settings(email):
    # Get document for this user
    doc_ref = db.collection('user_settings').document(email)
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        return {
            'website': data.get('website', ''),
            'signature': data.get('signature', '')
        }
    else:
        return {'website': '', 'signature': ''}  # default if no doc

def save_user_settings(email, website, signature):
    # Set the user document (create or update)
    doc_ref = db.collection('user_settings').document(email)
    doc_ref.set({
        'website': website,
        'signature': signature
    })

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    # Handle form submission for website link and signature
    if request.method == 'POST':
        website_link = request.form['website_link']
        email_signature = request.form['email_signature']

        # Get the user's email from session
        user_email = session.get('email', '')

        # Save these values to session and data file
        session['website_link'] = website_link
        session['email_signature'] = email_signature

        if user_email:  # Ensure email exists in session
            save_user_settings(user_email, website_link, email_signature)  # Pass email

        flash('Settings updated successfully!', 'success')

    # Retrieve saved settings from the session or data file
    user_email = session.get('email', '')
    website_link = ''
    email_signature = ''

    if user_email:
        # Load settings specific to the user
        user_settings = load_user_settings(user_email)
        website_link = user_settings.get('website', '')
        email_signature = user_settings.get('signature', '')

    # Render the settings page with the current values
    return render_template('settings.html',
                           website_link=website_link,
                           email_signature=email_signature)


@app.route('/save_settings', methods=['POST'])
def save_settings():
    # Get the settings from the form
    website = request.form.get('website', '').strip()
    signature = request.form.get('signature', '').strip()

    print(f"Session email: {session.get('email')}")  # Debugging

    # Save these values in session
    session['website_link'] = website
    session['email_signature'] = signature

    # Get the user's email from session
    user_email = session.get('email', '')

    # Optionally, save user settings to the data file or database
    if user_email:  # Ensure email exists in session
        save_user_settings(user_email, website, signature)  # Pass email

    flash('Settings updated successfully!', 'success')
    return redirect('/settings')


@app.route('/delete_draft/<draft_id>', methods=['POST'])
def delete_draft(draft_id):
    # Get the user's email from the session
    user_email = session.get('email', '')

    if not user_email:
        return redirect('/authorize')  # Redirect if user email is not found

    service = gmail_authenticate(user_email)  # Use the user's specific authentication service

    if not service:
        return redirect('/authorize')

    try:
        service.users().drafts().delete(userId=user_email, id=draft_id).execute()
    except Exception as e:
        print(f"Failed to delete draft: {e}")
        # Optionally, flash an error message

    return redirect(url_for('view_drafts'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)