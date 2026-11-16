import sqlite3

DB = "data/app.db"


def user_email(user_id: int) -> str | None:
    with sqlite3.connect(DB) as db:
        row = db.execute("SELECT email, legacy_email FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    email, legacy = row
    return email or legacy  # TODO(migration v2): stop falling back to legacy_email


if __name__ == "__main__":
    print(user_email(1))
