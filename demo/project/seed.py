"""Recreate data/app.db: 500 users, 40 of them still only have legacy_email."""
import os
import sqlite3

os.makedirs("data", exist_ok=True)
if os.path.exists("data/app.db"):
    os.remove("data/app.db")
with sqlite3.connect("data/app.db") as db:
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, legacy_email TEXT)")
    for i in range(1, 501):
        legacy = f"user{i}@old.example.com"
        email = None if i % 12 == 0 and i <= 480 else f"user{i}@example.com"
        db.execute("INSERT INTO users VALUES (?, ?, ?, ?)", (i, f"User {i}", email, legacy))
    nulls = db.execute("SELECT COUNT(*) FROM users WHERE email IS NULL").fetchone()[0]
print(f"seeded data/app.db: 500 users, {nulls} without email")
