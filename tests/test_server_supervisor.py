import threading

from app.server_supervisor import _echo_command, _serve_commands, _update_online_players


def test_supervisor_tracks_joining_and_leaving_players():
    players = set()

    _update_online_players("[Server thread/INFO]: Alex joined the game", players)
    _update_online_players("[Server thread/INFO]: Steve joined the game", players)
    _update_online_players("[Server thread/INFO]: Alex left the game", players)

    assert players == {"Steve"}


def test_supervisor_echoes_panel_commands_to_journal(capsys):
    _echo_command("say Server restarting")

    assert capsys.readouterr().out == "[Panel command] > say Server restarting\n"


def test_supervisor_tracks_modern_paper_login_and_disconnect_messages():
    players = set()

    _update_online_players(
        "[06:01:40 INFO]: nomadjimbob[/125.63.2.220:60595] logged in with entity id 299 at ([minecraft:overworld]-918.0, 89.0, 291.0)",
        players,
    )
    assert players == {"nomadjimbob"}

    _update_online_players(
        "[06:04:12 INFO]: nomadjimbob (/125.63.2.220:60595) lost connection: Disconnected",
        players,
    )
    assert players == set()


class ExitedProcess:
    stdin = None

    def poll(self):
        return 0


class ClosedSocket:
    def accept(self):
        raise AssertionError("accept should not run after the process exits")


def test_command_thread_exits_cleanly_after_minecraft_stops(tmp_path):
    thread = threading.Thread(
        target=_serve_commands,
        args=(ClosedSocket(), ExitedProcess(), threading.Event(), tmp_path / "status"),
    )

    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()


class ClosingSocket:
    def __init__(self, stopping):
        self.stopping = stopping

    def accept(self):
        self.stopping.set()
        raise OSError("socket closed")


class RunningProcess:
    stdin = None

    def poll(self):
        return None


def test_command_thread_ignores_socket_close_during_shutdown(tmp_path):
    stopping = threading.Event()

    _serve_commands(ClosingSocket(stopping), RunningProcess(), stopping, tmp_path / "status")
