import base64

# Replace 'credentials.json' with the actual path to your file
file_path = 'credentials.json'

# Read the credentials.json file and encode it
try:
    with open(file_path, 'rb') as f:
        credentials = f.read()

    # Encode to base64 and decode it to a string
    encoded_credentials = base64.b64encode(credentials).decode('utf-8')

    # Print the base64 encoded string
    print(encoded_credentials)

except FileNotFoundError:
    print(f"The file '{file_path}' was not found. Please check the path.")
except Exception as e:
    print(f"An error occurred: {e}")