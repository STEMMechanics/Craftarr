"""Opt-in GitHub branch builds for development servers."""
from concurrent.futures import ThreadPoolExecutor
import codecs
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit
try:
    import pwd
except ImportError:  # pragma: no cover - the managed runtime is Linux.
    pwd = None

from .database import SessionLocal
from .models import (
    DeveloperPluginUpdate,
    PendingIdleRestart,
    PluginMonitoringSetting,
    Server,
)
from .plugin_manager import install_plugin_build, list_plugins, read_plugin_yml
from .plugin_monitoring import validate_development_branch
from .processes import register_server, server_status
from .update_providers import normalize_name


logger = logging.getLogger(__name__)
BUILD_TIMEOUT_SECONDS = max(60, int(os.environ.get("CRAFTARR_PLUGIN_BUILD_TIMEOUT_SECONDS", "900")))
MAX_BUILD_LOG_BYTES = 16000
MAX_BUILD_REPORT_BYTES = 2 * 1024 * 1024
LIVE_BUILD_LOG_PERSIST_INTERVAL_SECONDS = 0.25
_worker_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="craftarr-plugin-build")
_active_jobs: set[tuple[int, str]] = set()
_active_jobs_lock = threading.Lock()
_PROJECT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_PROBLEM_REPORT_FILENAME = "problems-report.html"
_PROBLEM_REPORT_URL_PATTERN = re.compile(r"file://\S*?problems-report\.html")
_UNSET = object()


class BuildCommandError(RuntimeError):
    def __init__(self, message: str, output: str = "", *, report_html: str | None = None, report_path: str | None = None):
        self.output = output
        self.report_html = report_html
        self.report_path = report_path
        super().__init__(message)


def _github_url(project: str) -> str:
    if not isinstance(project, str) or not _PROJECT_PATTERN.fullmatch(project):
        raise ValueError("Enter a valid GitHub owner/repository")
    return f"https://github.com/{project}.git"


def _safe_build_environment(home: Path) -> dict[str, str]:
    environment = {
        "PATH": "/opt/gradle/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    for key in ("JAVA_HOME", "LC_ALL", "TZ"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _builder_identity(root: Path):
    username = os.environ.get("CRAFTARR_PLUGIN_BUILD_USER", "").strip()
    if not username:
        return {}, _safe_build_environment(root)
    if pwd is None:
        raise RuntimeError("The configured plugin build user is supported only on Linux")
    try:
        identity = pwd.getpwnam(username)
    except KeyError as error:
        raise RuntimeError(f"Configured plugin build user {username} does not exist") from error
    os.chown(root, identity.pw_uid, identity.pw_gid)
    os.chmod(root, 0o700)
    os.chown(root / "tmp", identity.pw_uid, identity.pw_gid)
    os.chmod(root / "tmp", 0o700)
    home = Path(identity.pw_dir)
    environment = _safe_build_environment(root)
    environment["HOME"] = str(home)
    environment["GRADLE_USER_HOME"] = str(root / "gradle-cache")
    environment["MAVEN_OPTS"] = f"-Dmaven.repo.local={root / 'maven-cache'}"
    return {"user": identity.pw_uid, "group": identity.pw_gid, "extra_groups": []}, environment


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 60, env=None, identity=None, on_output=None) -> str:
    """Run a command while optionally forwarding combined output as it arrives."""
    completed = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        **(identity or {}),
    )
    output_parts = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    selector = selectors.DefaultSelector()
    selector.register(completed.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(remaining)
            if not events:
                timed_out = True
                break
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                text = decoder.decode(chunk)
                if text:
                    output_parts.append(text)
                    if on_output:
                        on_output(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            output_parts.append(tail)
            if on_output:
                on_output(tail)
        if timed_out:
            completed.kill()
            remainder, _ = completed.communicate()
            if remainder:
                if isinstance(remainder, bytes):
                    remainder = remainder.decode("utf-8", errors="replace")
                output_parts.append(remainder)
                if on_output:
                    on_output(remainder)
            raise subprocess.TimeoutExpired(command, timeout, output="".join(output_parts))
        return_code = completed.wait()
    finally:
        selector.close()
        if completed.poll() is None:
            completed.kill()
            completed.wait()

    output = "".join(output_parts).strip()
    if return_code:
        detail = output[-4000:] or f"Command exited with status {return_code}"
        raise BuildCommandError(detail, output)
    return output


def latest_branch_commit(project: str, branch: str) -> str:
    branch = validate_development_branch(branch)
    if not branch:
        raise ValueError("Enter a GitHub branch")
    with tempfile.TemporaryDirectory(prefix="craftarr-git-check-") as temporary:
        home = Path(temporary)
        (home / "tmp").mkdir()
        output = _run([
            "git", "ls-remote", "--heads", _github_url(project), f"refs/heads/{branch}",
        ], timeout=45, env=_safe_build_environment(home))
    matches = [line.split()[0] for line in output.splitlines() if line.split()]
    if len(matches) != 1 or not _COMMIT_PATTERN.fullmatch(matches[0]):
        raise RuntimeError("GitHub did not return a commit for this branch")
    return matches[0]


def list_branch_names(project: str) -> list[str]:
    """Return the valid branch refs advertised by a public GitHub repository."""
    with tempfile.TemporaryDirectory(prefix="craftarr-git-branches-") as temporary:
        home = Path(temporary)
        (home / "tmp").mkdir()
        output = _run([
            "git", "ls-remote", "--heads", _github_url(project),
        ], timeout=45, env=_safe_build_environment(home))

    branches = []
    for line in output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
            continue
        candidate = parts[1][len("refs/heads/"):]
        try:
            candidate = validate_development_branch(candidate)
        except ValueError:
            continue
        if candidate:
            branches.append(candidate)
    return sorted(set(branches), key=lambda branch: (branch.casefold(), branch))


def _build_command(source: Path) -> list[str]:
    if (source / "gradlew").is_file():
        return ["bash", "./gradlew", "--no-daemon", "build"]
    if (source / "mvnw").is_file():
        return ["bash", "./mvnw", "--batch-mode", "package"]
    if (source / "build.gradle").is_file() or (source / "build.gradle.kts").is_file():
        if not shutil_which("gradle"):
            raise RuntimeError("This repository needs its Gradle wrapper (gradlew)")
        return ["gradle", "--no-daemon", "build"]
    if (source / "pom.xml").is_file():
        if not shutil_which("mvn"):
            raise RuntimeError("This repository needs its Maven wrapper (mvnw)")
        return ["mvn", "--batch-mode", "package"]
    raise RuntimeError("No Gradle or Maven build file was found")


def shutil_which(command: str) -> str | None:
    # Keep process lookup local and predictable without invoking a shell.
    from shutil import which

    return which(command)


def _read_problem_report(source: Path, output: str | bytes | None = "") -> tuple[str | None, str | None]:
    """Retain a Gradle problems report while the temporary checkout still exists."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    try:
        source_root = source.resolve()
    except OSError:
        return None, None

    candidates = []
    for match in _PROBLEM_REPORT_URL_PATTERN.finditer(output or ""):
        try:
            parsed = urlsplit(match.group(0))
            if parsed.scheme != "file" or not parsed.path:
                continue
            candidates.append(Path(unquote(parsed.path)))
        except (TypeError, ValueError):
            continue
    try:
        candidates.extend(sorted(
            source.rglob(_PROBLEM_REPORT_FILENAME),
            key=lambda path: (len(path.parts), path.as_posix()),
        ))
    except OSError:
        pass

    seen = set()
    for candidate in candidates:
        try:
            report = candidate.resolve()
            relative = report.relative_to(source_root)
        except (OSError, ValueError):
            continue
        if relative.as_posix() in seen or report.name.casefold() != _PROBLEM_REPORT_FILENAME:
            continue
        seen.add(relative.as_posix())
        if any(part in {".git", ".gradle", "node_modules"} for part in relative.parts):
            continue
        try:
            if not report.is_file() or report.stat().st_size > MAX_BUILD_REPORT_BYTES:
                continue
            html = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if html:
            return relative.as_posix(), html
    return None, None


def _find_plugin_jar(source: Path, plugin_name: str) -> Path:
    artifacts = []
    excluded = ("-sources.jar", "-javadoc.jar", "-tests.jar", "-test.jar", "-original.jar")
    for artifact in source.rglob("*.jar"):
        if artifact.parent.name not in {"libs", "target"}:
            continue
        if not artifact.is_file() or artifact.name.casefold().endswith(excluded):
            continue
        if any(part in {".git", ".gradle", "node_modules"} for part in artifact.relative_to(source).parts):
            continue
        try:
            artifact.resolve().relative_to(source.resolve())
        except (OSError, ValueError):
            continue
        if read_plugin_yml(artifact).get("name", "").casefold() == plugin_name.casefold():
            artifacts.append(artifact)
    if len(artifacts) != 1:
        if not artifacts:
            raise RuntimeError(f"The build did not produce a JAR for {plugin_name}")
        names = ", ".join(artifact.relative_to(source).as_posix() for artifact in artifacts[:5])
        raise RuntimeError(f"The build produced more than one JAR for {plugin_name}: {names}")
    return artifacts[0]


def _attach_problem_report(error, source: Path, output: str = ""):
    report_path, report_html = _read_problem_report(source, output)
    error.report_path = report_path
    error.report_html = report_html
    return error


def build_and_install(server, plugin: dict, project: str, branch: str, expected_commit: str, *, should_install=None, on_output=None) -> tuple[str, str, dict | None, str | None, str | None]:
    """Build one checked-out branch commit and install its matching plugin JAR."""
    branch = validate_development_branch(branch)
    repo_url = _github_url(project)
    with tempfile.TemporaryDirectory(prefix="craftarr-plugin-build-") as temporary:
        root = Path(temporary)
        (root / "tmp").mkdir()
        identity, env = _builder_identity(root)
        source = root / "source"
        clone_log = _run([
            "git", "clone", "--quiet", "--depth=1", "--single-branch",
            f"--branch={branch}", repo_url, str(source),
        ], timeout=180, env=env, identity=identity)
        commit = _run(["git", "rev-parse", "HEAD"], cwd=source, timeout=20, env=env, identity=identity).strip()
        if not _COMMIT_PATTERN.fullmatch(commit):
            raise RuntimeError("Git did not return a full commit ID")
        if commit != expected_commit:
            raise RuntimeError("The branch changed while it was being downloaded; it will be checked again")
        command = _build_command(source)
        try:
            build_log = _run(command, cwd=source, timeout=BUILD_TIMEOUT_SECONDS, env=env, identity=identity, on_output=on_output)
        except (BuildCommandError, subprocess.TimeoutExpired) as error:
            _attach_problem_report(error, source, getattr(error, "output", ""))
            raise
        report_path, report_html = _read_problem_report(source, build_log)
        try:
            artifact = _find_plugin_jar(source, plugin["name"])
        except Exception as error:
            detail = str(error)
            raise BuildCommandError(
                detail,
                "\n".join(part for part in (clone_log, build_log, detail) if part),
                report_html=report_html,
                report_path=report_path,
            ) from error
        log = "\n".join(part for part in (clone_log, build_log) if part)
        if should_install is not None and not should_install():
            return commit, log[-MAX_BUILD_LOG_BYTES:], None, report_html, report_path
        try:
            result = install_plugin_build(
                server,
                plugin["filename"],
                artifact,
                expected_name=plugin["name"],
            )
        except Exception as error:
            raise BuildCommandError(
                str(error), log, report_html=report_html, report_path=report_path,
            ) from error
        return commit, log[-MAX_BUILD_LOG_BYTES:], result, report_html, report_path


def _set_state(db, state: DeveloperPluginUpdate, *, status: str, error=None, build_log=None,
               build_report_html=_UNSET, build_report_path=_UNSET):
    state.status = status
    state.error = str(error)[:1000] if error else None
    if build_log:
        state.build_log = build_log[-MAX_BUILD_LOG_BYTES:]
    if build_report_html is not _UNSET:
        state.build_report_html = build_report_html
    if build_report_path is not _UNSET:
        state.build_report_path = build_report_path
    state.updated_at = datetime.utcnow()
    db.commit()


def _process_job(server_id: int, plugin_name: str, filename: str, project: str, branch: str, expected_commit: str, source_key: str):
    key = (server_id, plugin_name)
    db = SessionLocal()
    try:
        server = db.get(Server, server_id)
        setting = db.get(PluginMonitoringSetting, (server_id, plugin_name))
        state = db.get(DeveloperPluginUpdate, (server_id, plugin_name))
        if not server or not setting or not state:
            return
        if (not server.developer_updates_enabled or setting.development_branch != branch
                or setting.project != project or state.source_key != source_key):
            _set_state(db, state, status="waiting")
            return
        installed_plugins = list_plugins(server)
        plugin = next((item for item in installed_plugins if item["filename"] == filename), None)
        active_matches = [item for item in installed_plugins
                          if normalize_name(item.get("name", "")) == plugin_name
                          and item.get("enabled") is True and not item.get("previous_version")]
        if not plugin or plugin.get("enabled") is not True or plugin.get("previous_version"):
            _set_state(db, state, status="waiting", error="The configured plugin is not enabled")
            return
        if len(active_matches) != 1:
            _set_state(db, state, status="failed", error="More than one enabled copy is installed; resolve the duplicates before building")
            return

        _set_state(db, state, status="building", error=None)
        register_server(server)
        def still_enabled():
            with SessionLocal() as check_db:
                current_server = check_db.get(Server, server_id)
                current_setting = check_db.get(PluginMonitoringSetting, (server_id, plugin_name))
                current_state = check_db.get(DeveloperPluginUpdate, (server_id, plugin_name))
                return bool(
                    current_server and current_server.developer_updates_enabled
                    and current_setting and current_setting.project == project
                    and current_setting.development_branch == branch
                    and current_state and current_state.source_key == source_key
                )

        live_log = ""
        last_live_log_commit = 0.0

        def persist_live_log(force=False):
            nonlocal last_live_log_commit
            if not live_log:
                return
            now = time.monotonic()
            if not force and now - last_live_log_commit < LIVE_BUILD_LOG_PERSIST_INTERVAL_SECONDS:
                return
            try:
                state.build_log = live_log
                state.updated_at = datetime.utcnow()
                db.commit()
            except Exception as error:
                db.rollback()
                logger.warning("Unable to persist live build output for server %s plugin %s: %s", server_id, plugin_name, error)
            finally:
                last_live_log_commit = now

        def capture_build_output(chunk):
            nonlocal live_log
            live_log = (live_log + chunk)[-MAX_BUILD_LOG_BYTES:]
            persist_live_log()

        try:
            commit, log, installed, report_html, report_path = build_and_install(
                server, plugin, project, branch, expected_commit,
                should_install=still_enabled,
                on_output=capture_build_output,
            )
        finally:
            persist_live_log(force=True)
        if installed is None:
            _set_state(
                db,
                state,
                status="waiting",
                error="Developer updates were disabled before the build completed",
                build_log=log,
                build_report_html=report_html,
                build_report_path=report_path,
            )
            return
        db.refresh(server)
        state.latest_commit = commit
        state.installed_commit = commit
        if log:
            state.build_log = log[-MAX_BUILD_LOG_BYTES:]
        state.build_report_html = report_html
        state.build_report_path = report_path
        state.error = None
        update_installed = installed["plugin"]
        try:
            running = bool(server_status(server.id).get("running"))
        except Exception as error:
            server.plugins_dirty = True
            state.status = "restart_failed"
            state.error = f"The build was installed, but server status could not be checked: {str(error)[:700]}"
            state.updated_at = datetime.utcnow()
            db.commit()
            return
        if running:
            server.plugins_dirty = True
            if server.developer_update_restart_policy == "immediate":
                db.commit()
                try:
                    from .automation import restart_managed_server
                    restart_managed_server(server, "to apply a development plugin update")
                    state.status = "restarted"
                except Exception as error:
                    server.plugins_dirty = True
                    state.status = "restart_failed"
                    state.error = f"The build was installed, but the restart failed: {str(error)[:800]}"
            elif server.developer_update_restart_policy == "when_empty":
                pending = db.get(PendingIdleRestart, server.id)
                if pending is None:
                    db.add(PendingIdleRestart(
                        server_id=server.id,
                        requested_by_user_id=None,
                        requested_by_username="Developer updates",
                        reason="to apply development plugin updates",
                    ))
                state.status = "restart_pending"
                db.commit()
                try:
                    from .automation import wake_idle_restart_monitor
                    wake_idle_restart_monitor()
                except Exception:
                    pass
            else:
                state.status = "restart_required"
            state.updated_at = datetime.utcnow()
            db.commit()
        else:
            state.status = "installed"
            state.updated_at = datetime.utcnow()
            db.commit()
        logger.info(
            "Built development plugin %s for server %s at %s (%s)",
            update_installed.get("name", plugin_name), server_id, commit, state.status,
        )
    except subprocess.TimeoutExpired as error:
        state = db.get(DeveloperPluginUpdate, (server_id, plugin_name))
        if state:
            output = error.output.decode(errors="replace") if isinstance(error.output, bytes) else error.output or ""
            _set_state(
                db,
                state,
                status="failed",
                error=f"Build timed out after {BUILD_TIMEOUT_SECONDS} seconds",
                build_log=output,
                build_report_html=getattr(error, "report_html", None),
                build_report_path=getattr(error, "report_path", None),
            )
    except Exception as error:
        state = db.get(DeveloperPluginUpdate, (server_id, plugin_name))
        if state:
            output = getattr(error, "output", None) or getattr(error, "stderr", None) or ""
            _set_state(
                db,
                state,
                status="failed",
                error=str(error)[:1000],
                build_log=str(output)[-MAX_BUILD_LOG_BYTES:],
                build_report_html=getattr(error, "report_html", None),
                build_report_path=getattr(error, "report_path", None),
            )
        logger.warning("Development plugin build failed for server %s plugin %s: %s", server_id, plugin_name, error)
    finally:
        db.close()
        with _active_jobs_lock:
            _active_jobs.discard(key)


def _queue_job(server: Server, setting: PluginMonitoringSetting, plugin: dict, commit: str, now: datetime, db):
    plugin_name = normalize_name(setting.plugin_name)
    key = (server.id, plugin_name)
    with _active_jobs_lock:
        if key in _active_jobs:
            return False
        _active_jobs.add(key)

    state = db.get(DeveloperPluginUpdate, (server.id, plugin_name))
    if state is None:
        state = DeveloperPluginUpdate(server_id=server.id, plugin_name=plugin_name)
        db.add(state)
    source_key = f"{setting.project.lower()}#{setting.development_branch}"
    source_changed = state.source_key != source_key
    if source_changed:
        state.source_key = source_key
        state.latest_commit = None
        state.installed_commit = None
        state.status = "waiting"
        state.build_log = None
    state.latest_commit = commit
    state.status = "queued"
    state.error = None
    state.build_report_html = None
    state.build_report_path = None
    state.checked_at = now
    state.updated_at = now
    try:
        db.commit()
    except Exception:
        db.rollback()
        with _active_jobs_lock:
            _active_jobs.discard(key)
        raise
    _worker_pool.submit(
        _process_job,
        server.id,
        plugin_name,
        plugin["filename"],
        setting.project,
        setting.development_branch,
        commit,
        source_key,
    )
    return True


def queue_developer_plugin_rebuild(db, server: Server, setting: PluginMonitoringSetting, plugin: dict) -> tuple[str, bool]:
    """Force a build for the currently configured branch commit."""
    plugin_name = normalize_name(setting.plugin_name)
    active_matches = [item for item in list_plugins(server)
                      if normalize_name(item.get("name", "")) == plugin_name
                      and item.get("enabled") is True
                      and not item.get("previous_version")]
    if len(active_matches) != 1 or active_matches[0].get("filename") != plugin.get("filename"):
        raise RuntimeError("More than one enabled copy is installed; resolve the duplicates before rebuilding")
    commit = latest_branch_commit(setting.project, setting.development_branch)
    queued = _queue_job(server, setting, plugin, commit, datetime.utcnow(), db)
    return commit, queued


def run_scheduled_developer_updates() -> None:
    """Queue due branch checks; builds run away from the server automation loop."""
    db = SessionLocal()
    now = datetime.utcnow()
    try:
        servers = db.query(Server).filter_by(developer_updates_enabled=True).all()
        for server in servers:
            interval = max(1, min(1440, int(server.developer_update_interval_minutes or 5)))
            plugins = list_plugins(server)
            for setting in db.query(PluginMonitoringSetting).filter_by(server_id=server.id).all():
                branch = setting.development_branch
                if not branch:
                    continue
                active_matches = [item for item in plugins
                                  if normalize_name(item.get("name", "")) == setting.plugin_name
                                  and item.get("enabled") is True
                                  and not item.get("previous_version")]
                if not active_matches:
                    continue
                plugin = active_matches[0]
                with _active_jobs_lock:
                    if (server.id, normalize_name(setting.plugin_name)) in _active_jobs:
                        continue
                state = db.get(DeveloperPluginUpdate, (server.id, setting.plugin_name))
                if state and state.source_key == f"{setting.project.lower()}#{branch}":
                    if state.checked_at and now - state.checked_at < timedelta(minutes=interval):
                        continue
                    if state.status == "failed" and state.latest_commit and state.latest_commit != state.installed_commit:
                        # Avoid rebuilding a broken commit on every polling tick. A new commit retries it.
                        try:
                            current_commit = latest_branch_commit(setting.project, branch)
                        except Exception as error:
                            state.status = "check_failed"
                            state.checked_at = now
                            state.error = str(error)[:1000]
                            state.updated_at = now
                            db.commit()
                            continue
                        if current_commit == state.latest_commit:
                            state.checked_at = now
                            db.commit()
                            continue
                if len(active_matches) != 1:
                    if state is None:
                        state = DeveloperPluginUpdate(server_id=server.id, plugin_name=setting.plugin_name)
                        db.add(state)
                    state.source_key = f"{setting.project.lower()}#{branch}"
                    state.status = "failed"
                    state.error = "More than one enabled copy is installed; resolve the duplicates before building"
                    state.checked_at = now
                    state.updated_at = now
                    db.commit()
                    continue
                try:
                    commit = latest_branch_commit(setting.project, branch)
                    if state and state.source_key == f"{setting.project.lower()}#{branch}" and state.installed_commit == commit:
                        state.latest_commit = commit
                        preserve_restart_state = server.plugins_dirty and state.status in {
                            "restart_required", "restart_pending", "restart_failed",
                        }
                        if not preserve_restart_state:
                            state.status = "current"
                            state.error = None
                        state.checked_at = now
                        state.updated_at = now
                        db.commit()
                        continue
                    _queue_job(server, setting, plugin, commit, now, db)
                except Exception as error:
                    if state is None:
                        state = DeveloperPluginUpdate(server_id=server.id, plugin_name=setting.plugin_name)
                        db.add(state)
                    state.source_key = f"{setting.project.lower()}#{branch}"
                    state.status = "check_failed"
                    state.error = str(error)[:1000]
                    state.checked_at = now
                    state.updated_at = now
                    db.commit()
                    logger.warning("Development branch check failed for server %s plugin %s: %s", server.id, setting.plugin_name, error)
    except Exception:
        db.rollback()
        logger.exception("Scheduled development plugin checks failed")
    finally:
        db.close()
