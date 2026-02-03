import asyncio
import subprocess
import re
import time
import logging
from typing import Optional, Set

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:
    print("Please install websockets: pip install websockets")
    raise

from protocol import Output, Status, Sessions, Pong, Input, Command, to_json, from_json


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x07|\r")

def strip_ansi(text: str) -> str:
    text = ANSI_ESCAPE.sub("", text)
    box_chars = "│┃┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬─━╭╮╯╰"
    text = "".join(c for c in text if c not in box_chars)
    return text


class RelayServer:

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self._clients: Set[WebSocketServerProtocol] = set()
        self._server = None

        self._session: Optional[str] = None
        self._running = False
        self._capture_task: Optional[asyncio.Task] = None

        self._last_content: str = ""
        self._last_clean: str = ""
        self._last_emitted: str = ""
        self._last_change_time: float = 0
        self._last_emit_time: float = 0
        self._debounce_time: float = 0.5
        self._max_silence: float = 5.0

    async def start(self):
        if not self._check_tmux():
            logger.error("tmux not found. On Mac, install with: brew install tmux")
            return

        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
        )
        logger.info(f"Relay server started on ws://{self.host}:{self.port}")
        await self._server.wait_closed()

    async def stop(self):
        await self._detach()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, ws: WebSocketServerProtocol):
        logger.info(f"Client connected: {ws.remote_address}")
        self._clients.add(ws)

        try:
            await self._send_sessions(ws)
            await self._send_status(ws)

            async for message in ws:
                await self._handle_message(ws, message)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client disconnected: {ws.remote_address}")
        finally:
            self._clients.discard(ws)

    async def _handle_message(self, ws: WebSocketServerProtocol, message: str):
        try:
            msg = from_json(message)

            if isinstance(msg, Input):
                await self._send_input(msg.content, key=msg.key)

            elif isinstance(msg, Command):
                await self._handle_command(msg, ws)

        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def _handle_command(self, cmd: Command, ws: WebSocketServerProtocol):
        if cmd.action == "list":
            await self._send_sessions(ws)

        elif cmd.action == "attach":
            await self._attach(cmd.session)

        elif cmd.action == "detach":
            await self._detach()

        elif cmd.action == "create":
            await self._create_session(cmd.session, cmd.command)

        elif cmd.action == "refresh":
            await self._refresh_output()

        elif cmd.action == "ping":
            await ws.send(to_json(Pong()))

        elif cmd.action == "kill":
            await self._kill_session(cmd.session)

    async def _broadcast(self, msg):
        if not self._clients:
            return
        data = to_json(msg)
        await asyncio.gather(
            *[c.send(data) for c in self._clients],
            return_exceptions=True
        )

    async def _send_status(self, ws: WebSocketServerProtocol = None):
        is_busy = (time.time() - self._last_change_time) < self._debounce_time
        status = Status(
            connected=True,
            session=self._session,
            is_busy=is_busy if self._session else False,
        )
        if ws:
            await ws.send(to_json(status))
        else:
            await self._broadcast(status)

    async def _send_sessions(self, ws: WebSocketServerProtocol = None):
        sessions = self._list_sessions()

        if self._session and self._session not in sessions:
            await self._detach()

        msg = Sessions(sessions=sessions, active=self._session)
        if ws:
            await ws.send(to_json(msg))
        else:
            await self._broadcast(msg)

    def _check_tmux(self) -> bool:
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _list_sessions(self) -> list:
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        except:
            pass
        return []

    async def _attach(self, session_name: str):
        if not session_name or session_name not in self._list_sessions():
            await self._broadcast(Output(content=f"Session not found: {session_name}"))
            return

        await self._detach()

        self._session = session_name
        self._running = True
        self._last_change_time = time.time()

        content = self._capture_pane()
        self._last_content = content
        self._last_emitted = content

        if content:
            clean = strip_ansi(content).rstrip("\n")
            await self._broadcast(Output(content=clean))
        self._last_emit_time = time.time()

        self._capture_task = asyncio.create_task(self._capture_loop())

        await self._send_sessions()
        await self._send_status()
        logger.info(f"Attached to session: {session_name}")

    async def _detach(self):
        self._running = False

        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
            self._capture_task = None

        if self._session:
            logger.info(f"Detached from session: {self._session}")
            self._session = None

        self._last_content = ""
        self._last_clean = ""
        self._last_emitted = ""
        await self._send_status()

    async def _create_session(self, name: str, command: str = None):
        name = name or "main"
        cmd = command or "bash"

        if name in self._list_sessions():
            await self._broadcast(Output(content=f"Session already exists: {name}"))
            return

        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, cmd],
                check=True
            )
            logger.info(f"Created session: {name}")
            await self._broadcast(Output(content=f"Created session: {name}"))
            await self._attach(name)
        except subprocess.CalledProcessError as e:
            await self._broadcast(Output(content=f"Failed to create session: {e}"))

        await self._send_sessions()

    async def _kill_session(self, name: str):
        if not name:
            return

        if name == self._session:
            await self._detach()

        try:
            subprocess.run(["tmux", "kill-session", "-t", name], check=True)
            logger.info(f"Killed session: {name}")
        except:
            pass

        await self._send_sessions()

    def _capture_pane(self) -> str:
        if not self._session:
            return ""
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", self._session, "-p", "-S", "-100"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return result.stdout
        except:
            pass
        return ""

    async def _refresh_output(self):
        if not self._session:
            return
        content = self._capture_pane()
        if content.strip():
            clean = strip_ansi(content).rstrip("\n")
            await self._broadcast(Output(content=clean))
        self._last_emitted = content
        self._last_emit_time = time.time()

    async def _send_input(self, text: str, key: str = None):
        if not self._session:
            await self._broadcast(Output(content="[Error] No active session"))
            return

        if self._session not in self._list_sessions():
            await self._broadcast(Output(content=f"[Error] Session '{self._session}' no longer exists"))
            await self._detach()
            await self._send_sessions()
            return

        try:
            if key:
                subprocess.run(
                    ["tmux", "send-keys", "-t", self._session, key],
                    check=True
                )
            else:
                subprocess.run(
                    ["tmux", "send-keys", "-t", self._session, "-l", text],
                    check=True
                )
                await asyncio.sleep(0.05)
                subprocess.run(
                    ["tmux", "send-keys", "-t", self._session, "Enter"],
                    check=True
                )
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to send input: {e}")

    async def _capture_loop(self):
        while self._running and self._session:
            try:
                content = self._capture_pane()
                clean = strip_ansi(content).rstrip("\n")
                now = time.time()

                if clean != self._last_clean:
                    self._last_change_time = now
                    self._last_clean = clean

                self._last_content = content

                time_since_change = now - self._last_change_time
                time_since_emit = now - self._last_emit_time
                should_emit = (
                    (time_since_change >= self._debounce_time and content != self._last_emitted)
                    or (time_since_emit >= self._max_silence and content != self._last_emitted)
                )
                if should_emit:
                    if content.strip():
                        clean = strip_ansi(content).rstrip("\n")
                        await self._broadcast(Output(content=clean))
                        await self._send_status()
                    self._last_emitted = content
                    self._last_emit_time = now

                was_busy = (now - self._last_change_time - 0.15) < self._debounce_time
                is_busy = time_since_change < self._debounce_time
                if was_busy != is_busy:
                    await self._send_status()

                await asyncio.sleep(0.15)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Capture error: {e}")
                await asyncio.sleep(0.5)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Really Remote Relay Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on")
    args = parser.parse_args()

    server = RelayServer(host=args.host, port=args.port)

    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
