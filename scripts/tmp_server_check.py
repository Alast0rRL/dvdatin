import subprocess, json
out = subprocess.run(
    ["sqlite3", "-header", "/opt/dvai/data/database.db",
     "SELECT id, profile_id, action, text, chat_id, telegram_message_id, sent_at FROM sent_messages ORDER BY id DESC LIMIT 10;"],
    capture_output=True, text=True).stdout
print("---SENT---")
print(out)
out = subprocess.run(
    ["sqlite3", "-header", "/opt/dvai/data/database.db",
     "SELECT profile_id, name, telegram_username, chat_id, telegram_message_id, responded_at FROM match_responses;"],
    capture_output=True, text=True).stdout
print("---MATCH---")
print(out)