"""Local administrator recovery commands for installed consoles."""

import argparse
import secrets
from .env import getenv

from .auth import hash_password
from .database import SessionLocal
from .models import AccessRole, User


def ensure_initial_admin(db, username: str, password: str | None = None):
    if db.query(User).filter(User.role == "admin").first():
        return None

    administrator = db.query(AccessRole).filter(AccessRole.name == "Administrator").first()

    temporary_password = password or secrets.token_urlsafe(18)
    db.add(
        User(
            username=username,
            password_hash=hash_password(temporary_password),
            role="admin",
            role_id=administrator.id if administrator else None,
            enabled=True,
            must_change_password=True,
        )
    )
    db.commit()
    return temporary_password


def reset_password(db, username: str, password: str | None = None):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise ValueError(f"User '{username}' was not found")

    temporary_password = password or secrets.token_urlsafe(18)
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    user.totp_secret = None
    user.totp_enabled = False
    user.enabled = True
    db.commit()
    return temporary_password


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local Craftarr users")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser("ensure-admin", help="Create the first administrator")
    ensure.add_argument("--username", default="admin")

    reset = subparsers.add_parser("reset-password", help="Reset a user's password")
    reset.add_argument("username", nargs="?", default="admin")

    updates = subparsers.add_parser("check-updates", help="Check plugin and Paper releases (monitoring only)")
    updates.add_argument("--server", help="Server name or numeric ID; omit for all servers")
    updates.add_argument("--notify", action="store_true", help="Email newly discovered updates")
    updates.add_argument("--cached", action="store_true", help="Reuse the normal six-hour upstream cache")

    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.command == "check-updates":
            from .models import Server
            from .update_monitor import check_updates
            servers = db.query(Server).all()
            if args.server:
                servers = ([server for server in servers if server.name == args.server]
                           or [server for server in servers if str(server.id) == args.server])
                if not servers:
                    raise ValueError("Server not found")
            grouped = check_updates(db, servers, force=not args.cached, notify=args.notify)
            for server, results in grouped:
                print(f"\n{server.name}")
                print(f"{'Plugin':24} {'Installed':30} {'Latest':30} Status / Last checked")
                for result in results:
                    print(f"{result['plugin']:24} {str(result['installed_version'] or 'Unknown'):30} "
                          f"{str(result['latest_version'] or 'Unknown'):30} {result['status']} / "
                          f"{result['checked_at'] or 'Never'}")
                    if result['error']:
                        print(f"  {result['error']}")
            return 0

        if args.command == "ensure-admin":
            password = ensure_initial_admin(
                db,
                args.username,
                getenv("CRAFTARR_BOOTSTRAP_ADMIN_PASSWORD") or None,
            )
            if password:
                print(f"Administrator account created: {args.username}")
                print("Temporary administrator password (shown once):")
                print(f"  {password}")
                print("Copy it now. After signing in, Craftarr will require you to choose a new password.")
            else:
                print("An administrator account already exists; no account or password was changed.")
                print("To recover an account, run: python -m app.admin_cli reset-password <username>")
            return 0

        password = reset_password(db, args.username)
        print(f"Password reset for {args.username}.")
        print(f"Temporary password for {args.username} (shown once):")
        print(f"  {password}")
        print("Copy it now. This account must choose a new password after signing in.")
        print("Two-factor authentication was disabled for account recovery.")
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"Error: {error}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
