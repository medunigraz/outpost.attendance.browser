import asyncio
import concurrent.futures
import gettext
import json
import logging
import os
from functools import partial

import click
import graypy
import websockets
from aiohttp import ClientError, ClientSession, HttpProcessingError, web
from asyncio_dispatch import Signal
from pyrc522 import RFID
from RPi import GPIO

locale = os.path.abspath(os.path.join(os.path.dirname(__file__), "locale"))
gettext.install("attendance", locale)
_ = gettext.gettext
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logging.basicConfig(level=logging.DEBUG)

sounddetection = 12
cardkey = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]


class ScreenSaver:

    enabled = Signal()
    disabled = Signal()

    async def disable(self, **kwargs):
        logger.debug("Activating display")
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/xset", "dpms", "force", "on"
        )
        await self.disabled.send()
        return await proc.wait()


class Browser:
    start = Signal()
    stop = Signal()

    def __init__(self, url):
        self.url = url

    async def run(self):
        while True:
            browser = await asyncio.create_subprocess_exec(
                "/usr/bin/chromium-browser",
                "--app={url}".format(url=self.url),
                "--start-fullscreen",
                "--kiosk",
                "--incognito",
                "--disable-pinch",
                "--overscroll-history-navigation=0",
            )
            await self.start.send()
            await browser.wait()
            await self.stop.send()


class GraylogManager:

    handler = None

    async def update(self, message, **kwargs):
        if self.handler in logger.handlers:
            logger.removeHandler(self.handler)
        if "graylog" not in message:
            return
        graylog = message.get("graylog")
        self.handler = graypy.GELFUDPHandler(
            graylog.get("hostname"), graylog.get("port")
        )
        logger.addHandler(self.handler)


class RoomManager:

    rooms = list()
    selection = Signal(message=None)
    abort = Signal(message=None)
    clock = Signal(uid=None, room=None)
    connected = asyncio.Event()
    timer = None

    def __init__(self, loop=None):
        if not loop:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop

    async def update(self, message, **kwargs):
        self.rooms = message.get("rooms")
        logger.debug("Updated rooms for terminal: {}".format(self.rooms))

    async def select(self, uid, **kwargs):
        await self.connected.wait()
        logger.debug("Preparing room selection: {}".format(tuple(self.rooms)))
        if len(self.rooms) == 1:
            room = list(self.rooms).pop()
            logger.debug("Only one room: {}".format(room))
            return await self.clock.send(uid=uid, room=room)
        self.timer = self.loop.call_later(10, self.timeout)
        await self.selection.send(
            message={"type": "rooms", "uid": uid, "rooms": tuple(self.rooms)}
        )

    async def selected(self, message, **kwargs):
        if self.timer:
            logger.debug("Canceling active room selection timer")
            self.timer.cancel()
            self.timer = None
        logger.debug("Room selected: {}".format(message))
        uid = message.get("uid")
        room = message.get("room")
        return await self.clock.send(uid=uid, room=room)

    def timeout(self):
        abort = partial(
            self.abort.send, message={"type": "rooms", "uid": None, "rooms": tuple()}
        )
        asyncio.ensure_future(abort(), loop=self.loop)


class UIDCache:

    uid = None

    def set(self, uid):
        self.uid = uid

    def equals(self, uid):
        return self.uid == uid

    def reset(self):
        self.uid = None


class CardReader:

    scanned = Signal(uid=None)

    def __init__(self, reader, loop=None):
        if not loop:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop
        self.reader = reader
        self.cache = UIDCache()

    def read(self):
        self.reader.wait_for_tag()
        (error, tag_type) = self.reader.request()
        if not error:
            (error, uid) = self.reader.anticoll()
            if not error:
                return uid

    async def run(self):
        timer = None
        with concurrent.futures.ThreadPoolExecutor() as pool:
            while True:
                uid = await self.loop.run_in_executor(pool, self.read)
                if uid and not self.cache.equals(uid):
                    if timer:
                        timer.cancel()
                        timer = None
                    logger.info("Scanned UID: {u}".format(u=uid))
                    self.cache.set(uid)
                    timer = self.loop.call_later(10, self.cache.reset)
                    await self.scanned.send(uid=tuple(uid))


class Websocket:

    clients = set()
    incoming = Signal(message=None)
    connected = Signal(client=None)

    async def connector(self, websocket, path):
        self.clients.add(websocket)
        try:
            greeting = json.dumps({"type": "ready", "service": "websocket"})
            await websocket.send(greeting)
            await self.connected.send(client=websocket)
            while True:
                message = await websocket.recv()
                data = json.loads(message)
                logger.debug("Got message from browser: {d}".format(d=data))
                await self.incoming.send(message=data)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)

    async def send(self, message, **kwargs):
        tasks = [client.send(json.dumps(message)) for client in self.clients]
        await asyncio.gather(*tasks)


class Webservice:

    session = None
    config_task = None
    headers = {"Content-Type": "application/json"}
    connected = asyncio.Event()
    ready = Signal(message=None)
    unready = Signal(message=None)
    progress = Signal(message=None)
    configured = Signal(message=None)

    def __init__(self, base_url, terminal, username, password, loop=None):
        self.terminal = terminal
        self.username = username
        self.password = password
        if not loop:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop
        self.token_url = "{b}/auth/token/".format(b=base_url)
        self.clock_url = "{b}/v1/attendance/clock/".format(b=base_url)
        self.config_url = "{b}/v1/attendance/terminal/{t}?expand=rooms".format(
            b=base_url, t=terminal
        )

    async def connect(self):
        logger.info("Initiating new API connection")
        body = {"username": self.username, "password": self.password}
        async with ClientSession(headers=self.headers) as session:
            while True:
                if self.session:
                    await asyncio.sleep(10)
                    continue
                try:
                    async with session.post(
                        self.token_url, data=json.dumps(body), timeout=5
                    ) as resp:
                        resp.raise_for_status()
                        logger.info("Received authentication token")
                        credentials = await resp.json()
                    self.session = ClientSession(
                        headers={
                            **self.headers,
                            **{
                                "Authorization": "Token {0}".format(
                                    credentials.get("token")
                                )
                            },
                        }
                    )
                    if not self.connected.is_set():
                        self.connected.set()
                    logger.info("API is ready")
                    logger.debug("Starting periodic config pull")
                    self.config_task = asyncio.ensure_future(
                        self.config(), loop=self.loop
                    )
                except (ClientError, HttpProcessingError) as e:
                    logger.warn("Could not authenticate: {e}".format(e=e))
                    await self.reset()
                await asyncio.sleep(10)

    async def reset(self):
        if self.config_task:
            self.config_task.cancel()
        self.session = None

    async def config(self):
        while True:
            logger.debug("Fetch configuration {}".format(self.config_url))
            try:
                async with self.session.get(self.config_url, timeout=5) as resp:
                    resp.raise_for_status()
                    logger.debug("Received terminal configuration")
                    config = await resp.json()
                    await self.progress.send(message=config)
                logger.debug("Got config response: {}".format(config))
                self.configured.send(config)
            except (ClientError, HttpProcessingError) as e:
                logger.warn("Could not fetch configuration: {e}".format(e=e))
            await asyncio.sleep(300)

    async def status(self, **kwargs):
        if self.connected.is_set():
            await self.ready.send(message={"type": "ready", "service": "api"})
        else:
            await self.unready.send(message={"type": "unready", "service": "api"})

    async def clock(self, uid, room, **kwargs):
        await self.connected.wait()
        cardid = "".join([("%X" % t).zfill(2) for t in uid[:4]])
        logger.info("Clocking in card {c} for {r}".format(c=cardid, r=room))
        await self.progress.send(message={"type": "request"})
        body = {"terminal": self.terminal, "cardid": cardid, "room": room.get("id")}
        try:
            async with self.session.post(
                self.clock_url, data=json.dumps(body), timeout=5
            ) as resp:
                resp.raise_for_status()
                response = await resp.json()
                logger.debug("Got response for card: {j}".format(j=response))
                data = {"type": "response", "payload": response}
        except (ClientError, asyncio.TimeoutError) as e:
            logger.warn("Could not connect to API: {e}".format(e=e))
            data = {"type": "error", "message": _("Network error")}
        except HttpProcessingError as e:
            logger.warn("Could not send clock information: {e}".format(e=e))
            errors = {404: _("Your card is invalid")}
            data = {"type": "error", "message": errors.get(e.code, _("Network error"))}
        logger.debug("Sending webservice progress signal {c}".format(c=cardid))
        await self.progress.send(message=data)


@click.command()
@click.option("--terminal")
@click.option("--api")
@click.option("--username")
@click.option("--password")
@click.option("--http-host", default="localhost")
@click.option("--http-port", default=6788)
@click.option("--ws-host", default="localhost")
@click.option("--ws-port", default=6789)
@click.option("--app-root", default="app")
def cli(
    terminal, api, username, password, http_host, http_port, ws_host, ws_port, app_root
):
    loop = asyncio.get_event_loop()
    screensaver = ScreenSaver()
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(sounddetection, GPIO.IN)
    GPIO.add_event_detect(
        sounddetection,
        GPIO.RISING,
        lambda _: asyncio.run_coroutine_threadsafe(screensaver.disable(), loop),
        bouncetime=1000,
    )
    reader = RFID()
    cardreader = CardReader(reader, loop)
    webservice = Webservice(api, terminal, username, password, loop)
    websocket = Websocket()
    browser = Browser("http://localhost:{port}/index.html".format(port=http_port))
    rooms = RoomManager()
    graylog = GraylogManager()

    tasks = asyncio.gather(
        cardreader.scanned.connect(rooms.select),
        cardreader.scanned.connect(screensaver.disable),
        rooms.selection.connect(websocket.send),
        rooms.clock.connect(webservice.clock),
        rooms.abort.connect(websocket.send),
        websocket.incoming.connect(rooms.selected),
        websocket.connected.connect(webservice.status),
        webservice.ready.connect(websocket.send),
        webservice.unready.connect(websocket.send),
        webservice.progress.connect(websocket.send),
        webservice.configured.connect(graylog.update),
        webservice.configured.connect(rooms.update),
    )
    loop.run_until_complete(tasks)

    app = web.Application()
    app.router.add_static("/", app_root)

    tasks = asyncio.gather(
        rooms.run(),
        loop.create_server(app.make_handler(), http_host, http_port),
        webservice.connect(),
        cardreader.run(),
        websockets.serve(websocket.connector, ws_host, ws_port),
        browser.run(),
    )
    loop.run_until_complete(tasks)


def main():
    cli(auto_envvar_prefix="ATTENDANCE")


if __name__ == "__main__":
    main()
