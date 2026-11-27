"""Generate admin hash locally; do not put plaintext passwords or hashes in Git."""
from getpass import getpass
from app.auth import hash_password
password = getpass("New admin password (12+ chars): ")
verify = getpass("Repeat password: ")
if password != verify:
    raise SystemExit("Passwords do not match")
print("APP_ADMIN_PASSWORD_HASH='" + hash_password(password) + "'")
