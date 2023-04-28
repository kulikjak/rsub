import os
import pathlib
import socket
import socketserver
import subprocess
import tempfile
import threading

from typing import Any, Dict, Optional

import sublime
import sublime_plugin

try:
    from ScriptingBridge import SBApplication
except ImportError:
    SBApplication = None

# Problems:
# Double line breaks on Windows.


SESSIONS: Dict[int, 'Session'] = {}
server = None
session_dir = None


def say(msg: str) -> None:
    print(f"[rsub] {msg}")


class Session:
    def __init__(self, sock: socket.socket) -> None:
        self.env: Dict[str, str] = {}
        self.file = b""
        self.file_size = 0
        self.in_file = False
        self.parse_done = False
        self.socket = sock
        self.temp_path: Optional[pathlib.Path] = None
        self.temp_dir: Optional[pathlib.Path] = None

    def parse_input(self, input_line: bytes) -> None:
        if input_line.strip() == b"open" or self.parse_done is True:
            return

        if self.in_file is False:
            line = input_line.decode("utf8").strip()
            if line == "":
                return
            if line == ".":
                self.parse_file(b".\n")
                return
            key, val = line.split(":", 1)
            if key == "data":
                self.file_size = int(val)
                if len(self.env) > 1:
                    self.in_file = True
            else:
                self.env[key] = val.strip()
        else:
            self.parse_file(input_line)

    def parse_file(self, line: bytes) -> None:
        if len(self.file) >= self.file_size and line == b".\n":
            self.in_file = False
            self.parse_done = True
            sublime.set_timeout(self.on_done, 0)
        else:
            self.file += line

    def close(self) -> None:
        self.socket.send(b"close\n")
        self.socket.send(b"token: " + self.env["token"].encode("utf8") + b"\n")
        self.socket.send(b"\n")
        self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        assert self.temp_path
        self.temp_path.unlink()
        # TODO: delete dirs as well?

    def send_save(self) -> None:
        self.socket.send(b"save\n")
        self.socket.send(b"token: " + self.env["token"].encode("utf8") + b"\n")
        assert self.temp_path
        with self.temp_path.open("rb") as ifile:
            new_file = ifile.read()
        self.socket.send(b"data: " + str(len(new_file)).encode("utf8") + b"\n")
        self.socket.send(new_file)
        self.socket.send(b"\n")

    def on_done(self) -> None:
        assert session_dir

        # replicate remote path locally
        real_path = pathlib.Path(self.env["real-path"].lstrip("/")).parent
        remote, name = self.env["display-name"].split(":", 1)
        name = os.path.basename(name)
        self.temp_dir = session_dir / remote / real_path
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.temp_path = self.temp_dir / name

        with self.temp_path.open("wb+") as ofile:
            ofile.write(self.file[:self.file_size])

        # create new window if needed
        if len(sublime.windows()) == 0 or "new" in self.env:
            sublime.run_command("new_window")

        # Open it within sublime
        view = sublime.active_window().open_file(str(self.temp_path))

        # Add the file metadata to the view's settings
        # This is mostly useful to obtain the path of this file on the server
        #view.settings().set("rsub", self.env) NOTE: this is currently useless

        # close previous duplicate session if it exists
        if view.id() in SESSIONS:
            previous = SESSIONS[view.id()]
            previous.close()
            say('Closed duplicate ' + previous.env['display-name'])

        # Add the session to the global list
        SESSIONS[view.id()] = self

        # Bring sublime to front
        if sublime.platform() == "osx":
            if SBApplication:
                subl_window = SBApplication.applicationWithBundleIdentifier_("com.sublimetext.4")
                subl_window.activate()
            else:
                subprocess.run([
                    "/usr/bin/osascript", "-e",
                    "tell app \"Finder\" to set frontmost of process \"Sublime Text\" to true"
                ])
        elif sublime.platform() == "linux":
            if os.getenv("XDG_SESSION_TYPE") == "wayland":
                # Wayland doesn't have a tool like wmctrl, so this
                # oneliner (though Gnome specific) has to suffice.
                subprocess.run([
                    "gdbus", "call", "--session",
                    "--dest", "org.gnome.Shell",
                    "--object-path", "/org/gnome/Shell",
                    "--method", "org.gnome.Shell.Eval",
                    'var mw = global.get_window_actors().map(w=>w.meta_window).find(mw=>mw.get_title().includes("Sublime Text")); mw && mw.activate(0)',
                ])
            else:
                subprocess.run("wmctrl -xa 'sublime_text.sublime-text-3'", shell=True)


class ConnectionHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        say(f"New connection from {self.client_address}")

        session = Session(self.request)
        version = sublime.version().encode("utf8")
        self.request.send(b"Sublime Text " + version + b" (rsub plugin)\n")

        socket_fd = self.request.makefile("rb")
        while True:
            line = socket_fd.readline()
            if len(line) == 0:
                break
            session.parse_input(line)

        say("Connection closed")


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def start_server() -> None:
    assert server
    server.serve_forever()


def unload_handler() -> None:
    global server
    say("Killing server...")
    if server:
        server.shutdown()
        server.server_close()


class RSubEventListener(sublime_plugin.EventListener):
    def on_post_save(self, view: sublime.View) -> None:
        if view.id() in SESSIONS:
            session = SESSIONS[view.id()]
            session.send_save()
            say(f"Saved {session.env['display-name']}")

    def on_close(self, view: sublime.View) -> None:
        if view.id() in SESSIONS:
            session = SESSIONS.pop(view.id())
            session.close()
            say(f"Closed {session.env['display-name']}")


def plugin_loaded() -> None:
    global server, session_dir

    # Load settings
    settings = sublime.load_settings("rsub.sublime-settings")
    port = settings.get("port", 52698)
    host = settings.get("host", "localhost")

    # prepare a secure temporary directory for this Sublime session
    try:
        temp = tempfile.TemporaryDirectory(prefix="rsub-")
        session_dir = pathlib.Path(temp.name)
    except OSError as err:
        sublime.error_message(f"Failed to create rsub temporary directory! Error: {err}")
        return

    assert isinstance(port, int)
    assert isinstance(host, (str, bytes))

    # Start server thread
    server = TCPServer((host, port), ConnectionHandler)
    threading.Thread(target=start_server, args=[]).start()
    say(f"Server running on {host}:{port}")
