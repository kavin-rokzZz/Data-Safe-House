"""Admin CLI.  Usage:  python -m app.cli create-admin [username]   |   reset-password [username]"""
from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from .database import SessionLocal, init_db
from .models import AdminUser
from .security import hash_password, validate_new_password


def _prompt_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        err = validate_new_password(pw)
        if err:
            print(err)
            continue
        if pw != getpass.getpass("Confirm: "):
            print("Passwords do not match.")
            continue
        return pw


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {"create-admin", "reset-password"}:
        sys.exit(__doc__)
    cmd, username = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "admin")
    init_db()
    with SessionLocal() as db:
        user = db.scalar(select(AdminUser).where(AdminUser.username == username))
        if cmd == "create-admin":
            if user:
                sys.exit(f"User '{username}' already exists (use reset-password).")
            db.add(AdminUser(username=username, password_hash=hash_password(_prompt_password())))
            print(f"Admin '{username}' created.")
        else:
            if not user:
                sys.exit(f"No such user '{username}'.")
            user.password_hash = hash_password(_prompt_password())
            print("Password updated (all existing sessions are now invalid).")
        db.commit()


if __name__ == "__main__":
    main()
