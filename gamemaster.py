import argparse
import asyncio
from datetime import datetime, timedelta
from enum import IntEnum
import http
import json
import logging
import random
import ssl
import sys
from typing import Any, Optional, Union
import requests

import aiohttp

from websockets.server import serve
from websockets.client import connect
from websockets.exceptions import ConnectionClosedError

from websockets.server import WebSocketServerProtocol

# Configure logging
logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s',
                    filename='game.log', filemode='a', level=logging.INFO)
_logger = logging.getLogger("gamemaster")

# Add console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
console_handler.setFormatter(formatter)
_logger.addHandler(console_handler)

class Unit:
    def __init__(self, ws: WebSocketServerProtocol, unit_id: int) -> None:
        self.ws = ws
        self.button_pressed = False
        self.unit_id = unit_id

        self.queue = asyncio.Queue()

        self._send_task = asyncio.create_task(self._send())

    def send(self, data: dict[str, Any]):
        self.queue.put_nowait(json.dumps(data).encode())

    async def _send(self):
        while True:
            message = await self.queue.get()
            await self.ws.send(message)

    def start_button_led(self, pattern: Union[str, tuple[int, int, int]], at: datetime):
        self.send({'type': 'BUTTON_LED', 'value': 'START', 'pattern': pattern,
                  'at': at.strftime("%Y-%m-%d %H:%M:%S.%f")})

    def start_matrix(self, pattern: Union[str, tuple[int, int, int]], at: datetime):
        self.send({'type': 'MATRIX_LED', 'value': 'START', 'pattern': pattern,
                  'at': at.strftime("%Y-%m-%d %H:%M:%S.%f")})

    def play_sound(self, filename: str, at: datetime):
        self.send({'type': 'SOUND', 'value': 'START', 'filename': filename,
                  'at': at.strftime("%Y-%m-%d %H:%M:%S.%f")})

    def stop_button_led(self, at: datetime):
        self.send({'type': 'BUTTON_LED', 'value': 'OFF',
                  'at': at.strftime("%Y-%m-%d %H:%M:%S.%f")})

    def stop_matrix(self, at: datetime):
        self.send({'type': 'MATRIX_LED', 'value': 'OFF',
                  'at': at.strftime("%Y-%m-%d %H:%M:%S.%f")})

    def stop_sound(self, at: datetime):
        self.send({'type': 'SOUND', 'value': 'STOP',
                  'at': at.strftime("%Y-%m-%d %H:%M:%S.%f")})

    def win(self, sound_path: str, at: datetime):
        self.start_button_led("colorscroll", at)
        self.start_matrix("colorscroll", at)
        self.play_sound(sound_path, at)

    def lose(self, sound_path: str, at: datetime):
        self.start_button_led("flash_red", at)
        self.start_matrix("swipe_red", at)
        self.play_sound(sound_path, at)

    def correct_pressed(self, at: datetime):
        self.start_button_led((0, 200, 0), at)
        self.start_matrix((0, 128, 0), at)
        self.play_sound(
            f"sounds/on_green_press/green-press{random.randint(1, 7)}.wav", at)

    def correct(self, at: datetime):
        self.start_button_led((0, 255, 0), at)
        self.start_matrix((0, 255, 0), at)

    def wrong(self, at: datetime):
        self.start_button_led((255, 0, 0), at)
        self.start_matrix((180, 0, 0), at)

    def stop_all(self, at: datetime):
        self.stop_button_led(at)
        self.stop_matrix(at)
        self.stop_sound(at)

    def __del__(self):
        self._send_task.cancel()

    def __repr__(self) -> str:
        return hex(self.unit_id)


class Game:
    STATES = IntEnum(
        'States', ['NoUnits',
                   'PreGameSingle',
                   'PreGameMultiple',
                   'Playing',
                   'PlayingAllReleased',
                   'Lose',
                   'Win',
                   'WaitRelease'])

    def __init__(self) -> None:
        self._state = Game.STATES.NoUnits
        self.ACTIVE: dict[int, Unit] = {}

        self.previous_correct: set[int] = set()
        self.unit_list: list[int] = []
        self.correct: Optional[int] = None
        self.wrong: Optional[int] = None

        self.pressed_units: set[Unit] = set()

        self._button_pressed_callbacks = {
            Game.STATES.PreGameSingle: self._button_pressed_PreGameSingle,
            Game.STATES.PreGameMultiple: self._button_pressed_PreGameMultiple,
            Game.STATES.Playing: self._button_pressed_Playing,
            Game.STATES.PlayingAllReleased: self._button_pressed_PlayingAllReleased,
            Game.STATES.Lose: self._button_pressed_Lose,
            Game.STATES.Win: self._button_pressed_Win,
            Game.STATES.WaitRelease: self._button_pressed_WaitRelease
        }

        self._button_released_callbacks = {
            Game.STATES.PreGameSingle: self._button_released_PreGameSingle,
            Game.STATES.PreGameMultiple: self._button_released_PreGameMultiple,
            Game.STATES.Playing: self._button_released_Playing,
            Game.STATES.PlayingAllReleased: self._button_released_PlayingAllReleased,
            Game.STATES.Lose: self._button_released_Lose,
            Game.STATES.Win: self._button_released_Win,
            Game.STATES.WaitRelease: self._button_released_WaitRelease
        }

        self._register_callbacks = {
            Game.STATES.NoUnits: self._register_NoUnits,
            Game.STATES.PreGameSingle: self._register_PreGameSingle
        }

        self._control_task: Optional[asyncio.Task] = None

    def __repr__(self):
        return f"""Game:
            Active:             {self.ACTIVE.values()}
            State:              {str(self.state)}
            Correct:            {self.correct}
            Upcoming list:      {self.unit_list}
            Wrong:              {self.wrong}
            Previous Correct:   {self.previous_correct}
            Pressed Units:      {self.pressed_units}
"""

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, next_state: STATES):
        _logger.info(self)
        _logger.info(f"Transition {self.state.name}->{next_state.name}")
        self._state = next_state

    def button_pressed(self, unit_id: int):
        _logger.info(f"Event: Button Pressed, Unit: {unit_id:#x}")

        if unit_id in self.ACTIVE:
            unit = self.ACTIVE[unit_id]

            unit.button_pressed = True
            self.pressed_units.add(unit)

            self._button_pressed_callbacks[self.state](unit)

    def button_released(self, unit_id: int):
        _logger.info(f"Event: Button Released, Unit: {unit_id:#x}")

        if unit_id in self.ACTIVE:
            unit = self.ACTIVE[unit_id]

            unit.button_pressed = False
            self.pressed_units.discard(unit)

            self._button_released_callbacks[self.state](unit)

    def register(self, unit_id: int, unit: Unit):
        _logger.info(f"Event: Unit Register, Unit: {unit}")

        self.ACTIVE[unit_id] = unit

        if self.state in (Game.STATES.NoUnits, Game.STATES.PreGameSingle):
            self._register_callbacks[self.state](unit)

        timestamp = datetime.now() + \
            timedelta(seconds=0.1) + \
            timedelta(seconds=unit.ws.latency)

        unit.stop_all(timestamp)

    def unregister(self, unit_id: int):
        _logger.info(f"Event: Unit Unregister, Unit: {unit_id:#x}")

        self.ACTIVE.pop(unit_id, None)
        self.previous_correct.discard(unit_id)

        if unit_id in self.unit_list:
            self.unit_list.remove(unit_id)
        elif unit_id == self.correct:
            self._next_correct()
            self._next_wrong()

        if unit_id == self.wrong:
            self._next_wrong()

        if len(self.ACTIVE) == 0:
            assert (self._control_task is not None)
            self._control_task.cancel()
            self._control_task = None

            self.state = Game.STATES.NoUnits
        elif self.state == Game.STATES.PreGameMultiple and len(self.ACTIVE) == 1:
            assert (self._control_task is not None)
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameSingle())

            self.state = Game.STATES.PreGameSingle
        elif self.state == Game.STATES.Playing and len(self.ACTIVE) == 1:
            assert (self._control_task is not None)
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_Win())

            self.state = Game.STATES.Win

    def _register_NoUnits(self, unit: Unit):
        assert (self._control_task is None)
        self._control_task = asyncio.create_task(self._control_PreGameSingle())

        self.state = Game.STATES.PreGameSingle

    def _register_PreGameSingle(self, unit: Unit):
        if len(self.ACTIVE) > 1:
            assert (self._control_task is not None)
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameMultiple())

            self.state = Game.STATES.PreGameMultiple
        elif len(self.ACTIVE) == 1:
            assert (self._control_task is not None)
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameSingle())

            self.state = Game.STATES.PreGameSingle

    def _button_pressed_PreGameSingle(self, unit: Unit):
        unit.win(f"sounds/win/win{random.randint(1, 8)}.wav",
                 datetime.now() +
                 timedelta(seconds=0.1) +
                 timedelta(seconds=unit.ws.latency)
                 )

        assert (self._control_task is not None)
        self._control_task.cancel()
        self._control_task = asyncio.create_task(self._control_Win())

        self.state = Game.STATES.Win

    def _button_pressed_PreGameMultiple(self, unit: Unit):
        if unit.unit_id == self.correct:
            unit.correct_pressed(datetime.now() +
                                 timedelta(seconds=0.1) +
                                 timedelta(seconds=unit.ws.latency)
                                 )

            self.previous_correct = set()
            self.previous_correct.add(unit.unit_id)

            self._setup_game()
            self.unit_list.remove(self.correct)

            self._next_correct()
            self._next_wrong()

            assert (self._control_task is not None)
            self._control_task.cancel()
            self._control_task = asyncio.create_task(self._control_Playing())

            self.state = Game.STATES.Playing

    def _button_pressed_Playing(self, unit: Unit):
        if unit.unit_id in self.previous_correct:
            unit.correct_pressed(
                datetime.now() +
                timedelta(seconds=0.1) +
                timedelta(seconds=unit.ws.latency)
            )
        elif unit.unit_id == self.wrong:
            latency = max(unit.ws.latency for unit in self.pressed_units)

            lose_sound = random.randint(1, 6)
            for unit in self.ACTIVE.values():
                unit.lose(
                    f"sounds/lose/lose{lose_sound}.wav",
                    datetime.now() +
                    timedelta(seconds=0.1) +
                    timedelta(seconds=latency)
                )

            assert (self._control_task is not None)
            _logger.info(f"_button_pressed_Playing cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_Lose())

            self.state = Game.STATES.Lose
        elif unit.unit_id == self.correct:
            if not self.unit_list:
                assert (self._control_task is not None)
                _logger.info(f"_button_pressed_Playing cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_Win())

                self.state = Game.STATES.Win
            else:
                unit.correct_pressed(
                    datetime.now() +
                    timedelta(seconds=0.1) +
                    timedelta(seconds=unit.ws.latency)
                )

                self.previous_correct.add(unit.unit_id)

                self._next_correct()
                self._next_wrong()

    def _button_pressed_PlayingAllReleased(self, unit: Unit):
        if unit.unit_id in self.previous_correct:
            unit.correct_pressed(
                datetime.now() +
                timedelta(seconds=0.1) +
                timedelta(seconds=unit.ws.latency)
            )

            self.state = Game.STATES.Playing
        elif unit.unit_id == self.wrong:
            latency = max(unit.ws.latency for unit in self.pressed_units)

            lose_sound = random.randint(1, 6)
            for pressed_unit in self.pressed_units:
                pressed_unit.lose(
                    f"sounds/lose/lose{lose_sound}.wav",
                    datetime.now() +
                    timedelta(seconds=0.1) +
                    timedelta(seconds=latency)
                )

            assert (self._control_task is not None)
            _logger.info(f"_button_pressed_PlayingAllReleased cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_Lose())

            self.state = Game.STATES.Lose
        elif unit.unit_id == self.correct:
            self.previous_correct.add(unit.unit_id)

            if not self.unit_list:
                assert (self._control_task is not None)
                _logger.info(f"_button_pressed_PlayingAllReleased cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_Win())

                self.state = Game.STATES.Win
            else:
                unit.correct_pressed(
                    datetime.now() +
                    timedelta(seconds=0.1) +
                    timedelta(seconds=unit.ws.latency)
                )

                self._next_correct()
                self._next_wrong()

                assert (self._control_task is not None)
                _logger.info(f"_button_pressed_PlayingAllReleased cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_Playing())

                self.state = Game.STATES.Playing

    def _button_pressed_WaitRelease(self, unit: Unit):
        unit.start_button_led((0xFF, 0xA5, 0x00),
                              datetime.now() +
                              timedelta(seconds=0.1) +
                              timedelta(seconds=unit.ws.latency)
                              )

    def _button_pressed_Lose(self, unit: Unit):
        pass
    _button_pressed_Win = _button_pressed_Lose

    def _button_released_PreGameSingle(self, unit: Unit):
        pass
    _button_released_PreGameMultiple = _button_released_PreGameSingle
    _button_released_PlayingAllReleased = _button_released_PreGameSingle
    _button_released_Lose = _button_released_PreGameSingle
    _button_released_Win = _button_released_PreGameSingle

    def _button_released_Playing(self, unit: Unit):
        timestamp = datetime.now() + \
            timedelta(seconds=0.1) + \
            timedelta(seconds=unit.ws.latency)

        if not self.pressed_units:
            assert (self._control_task is not None)
            _logger.info(f"_button_released_PreGameSingle cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PlayingAllReleased())

            self.state = Game.STATES.PlayingAllReleased

    def _button_released_WaitRelease(self, unit: Unit):
        timestamp = datetime.now() +\
            timedelta(seconds=0.1) + \
            timedelta(seconds=unit.ws.latency)
        unit.stop_all(timestamp)

        self.previous_correct.discard(unit.unit_id)

        if not self.pressed_units:
            if len(self.ACTIVE) > 1:
                assert (self._control_task is not None)
                _logger.info(f"_button_released_WaitRelease cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_PreGameMultiple())

                self.state = Game.STATES.PreGameMultiple
            elif len(self.ACTIVE) == 1:
                assert (self._control_task is not None)
                _logger.info(f"_button_released_WaitRelease cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_PreGameSingle())

                self.state = Game.STATES.PreGameSingle

    def _setup_game(self):
        self.unit_list = list(self.ACTIVE.keys())
        random.shuffle(self.unit_list)

        _logger.info(f"Game: Setup, Order: {self.unit_list}")

    def _next_correct(self):
        _logger.info("Picking next correct")
        if self.unit_list:
            self.correct = self.unit_list.pop(0)

            correct_unit = self.ACTIVE[self.correct]
            correct_unit.correct(
                datetime.now() +
                timedelta(seconds=0.1) +
                timedelta(seconds=correct_unit.ws.latency)
            )

            _logger.info(f"Game: Next correct, Unit: {self.correct:#x}")
        else:
            self.correct = None

            _logger.info(f"Game: Next correct, Unit: None")

    def _next_wrong(self):
        _logger.info("Picking next wrong")
        if self.unit_list:
            if self.wrong is not None and self.wrong != self.correct:
                self.ACTIVE[self.wrong].stop_all(datetime.now())
            self.wrong = random.choice(self.unit_list)
            wrong_unit = self.ACTIVE[self.wrong]
            wrong_unit.wrong(
                datetime.now() +
                timedelta(seconds=0.1) +
                timedelta(seconds=wrong_unit.ws.latency)
            )
            _logger.info(f"Game: Next wrong, Unit: {self.wrong:#x}")
        else:
            self.wrong = None
            _logger.info(f"Game: Next wrong, Unit: None")

    async def _control_PreGameSingle(self):
        if self.correct is not None:
            correct_unit = self.ACTIVE[self.correct]

            timestamp = datetime.now() +\
                timedelta(seconds=0.1) + \
                timedelta(seconds=correct_unit.ws.latency)

            correct_unit.stop_all(timestamp)

        self.correct = random.choice(list(self.ACTIVE.keys()))
        assert self.correct is not None
        correct_unit = self.ACTIVE[self.correct]

        correct_unit.correct(
            datetime.now() +
            timedelta(seconds=0.1) +
            timedelta(seconds=correct_unit.ws.latency)
        )

        _logger.info(f"Game: Next correct, Unit: {self.correct:#x}")

    async def _control_PreGameMultiple(self):
        while True:
            if self.correct is not None:
                correct_unit = self.ACTIVE[self.correct]

                correct_unit.stop_all(
                    datetime.now() +
                    timedelta(seconds=0.1) +
                    timedelta(seconds=correct_unit.ws.latency)
                )
            while self.correct == (next_unit := random.choice(list(self.ACTIVE.keys()))):
                pass

            self.correct = next_unit
            correct_unit = self.ACTIVE[self.correct]

            correct_unit.correct(
                datetime.now() +
                timedelta(seconds=0.1) +
                timedelta(seconds=correct_unit.ws.latency)
            )

            _logger.info(f"Game: Next correct, Unit: {self.correct:#x}")

            await asyncio.sleep(10)

    async def _control_WaitRelease(self):
        await asyncio.sleep(10)
        for unit in self.pressed_units:
            unit.start_button_led(
                "flash_blue",
                datetime.now() +
                timedelta(seconds=0.1) +
                timedelta(seconds=unit.ws.latency)
            )

            _logger.info(f"Event: Button held, Units: {self.pressed_units}")

    async def _control_Playing(self):
        pass

    async def _control_PlayingAllReleased(self):
        await asyncio.sleep(15)

        lose_sound = random.randint(1, 6)
        for unit in self.ACTIVE.values():
            unit.lose(f"sounds/lose/lose{lose_sound}.wav", datetime.now())

        await asyncio.sleep(4)

        for unit in self.ACTIVE.values():
            unit.stop_all(datetime.now())

        if not self.pressed_units:
            if len(self.ACTIVE) > 1:
                assert (self._control_task is not None)
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_PreGameMultiple())

                self.state = Game.STATES.PreGameMultiple
            elif len(self.ACTIVE) == 1:
                assert (self._control_task is not None)
                self._control_task.cancel()
                self._control_task = asyncio.create_task(
                    self._control_PreGameSingle())

                self.state = Game.STATES.PreGameSingle

    async def _control_Lose(self):
        lose_sound = random.randint(1, 6)
        for unit in self.ACTIVE.values():
            unit.lose(
                f"sounds/lose/lose{lose_sound}.wav",
                datetime.now())

        await asyncio.sleep(10)

        for unit in self.ACTIVE.values():
            unit.stop_all(datetime.now())

        await asyncio.sleep(10)
        if len(self.ACTIVE) > 1:
            assert (self._control_task is not None)
            _logger.info(f"_control_Lose cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameMultiple())

            self.previous_correct = set()
            self.state = Game.STATES.PreGameMultiple
        elif len(self.ACTIVE) == 1:
            assert (self._control_task is not None)
            _logger.info(f"_control_Lose cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameSingle())

            self.previous_correct = set()
            self.state = Game.STATES.PreGameSingle

    async def _control_Win(self):
        win_sound = random.randint(1, 8)
        for unit in self.ACTIVE.values():
            unit.win(f"sounds/win/win{win_sound}.wav", datetime.now())

        await asyncio.sleep(10)

        for unit in self.ACTIVE.values():
            unit.stop_all(datetime.now())

        await asyncio.sleep(10)

        if len(self.ACTIVE) > 1:
            assert (self._control_task is not None)
            _logger.info(f"_control_Win cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameMultiple())

            self.previous_correct = set()
            self.state = Game.STATES.PreGameMultiple
        elif len(self.ACTIVE) == 1:
            assert (self._control_task is not None)
            _logger.info(f"_control_Win cancels task:{self._control_task}")
            self._control_task.cancel()
            self._control_task = asyncio.create_task(
                self._control_PreGameSingle())

            self.previous_correct = set()
            self.state = Game.STATES.PreGameSingle


class Gamemaster():
    def __init__(self, url: str, priority: int, gamemaster_urls: list[str], ssl: ssl.SSLContext):
        self.gamemaster_urls = gamemaster_urls
        self.ca_certificate = ssl

        self.url = url
        self.priority = priority

        self.active_gamemaster = ''

    async def _get_is_gamemaster(self, session: aiohttp.ClientSession, url: str):
        try:
            async with session.get(f"https://{url}:8002/gamemaster",
                                   ssl=self.ca_certificate,
                                   timeout=1) as response:
                if response.status == http.HTTPStatus.FOUND:
                    self.active_gamemaster = await response.text()
                    return True
        except (aiohttp.TooManyRedirects, aiohttp.ClientConnectionError):
            pass

        return False

    async def get_gamemaster(self):
        async with aiohttp.ClientSession(timeout=1) as session:
            return any(await asyncio.gather(
                *(self._get_is_gamemaster(session, url)
                  for url in self.gamemaster_urls if url != self.url))
            )

    async def _request_gamemaster(self, session: aiohttp.ClientSession, url: str):
        try:
            async with session.get(f"https://{url}:8002/request_gamemaster",
                                   ssl=self.ca_certificate) as response:
                if response.status == http.HTTPStatus.OK:
                    return True
                elif response.status == http.HTTPStatus.CONFLICT:
                    if int(await response.text()) > self.priority:
                        self.active_gamemaster = url
                        return False
                    else:
                        return True
                elif response.status == http.HTTPStatus.FOUND:
                    self.active_gamemaster = url
                    return False
        except (aiohttp.TooManyRedirects, aiohttp.ClientConnectionError):
            pass

        return True

    async def request_gamemaster(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(1)) as session:
            return all(await asyncio.gather(
                *(self._request_gamemaster(session, url)
                    for url in self.gamemaster_urls if url != self.url)))


class GamemasterFSM():
    STATES = IntEnum('States', ['Initial', 'Intent', 'Gamemaster', 'End'])

    def __init__(self, model: Gamemaster) -> None:
        self._state = GamemasterFSM.STATES.Initial
        self.actions = {
            GamemasterFSM.STATES.Initial: self._initial_action,
            GamemasterFSM.STATES.Intent: self._intent_action,
            GamemasterFSM.STATES.Gamemaster: self._gamemaster_action,
            GamemasterFSM.STATES.End: self._end_action,
        }
        self.model = model

    async def step(self):
        await self.actions[self._state]()

    async def _initial_action(self):
        if await self.model.get_gamemaster():
            print("Found GM Enter End")
            self._state = GamemasterFSM.STATES.End
        else:
            print("Not Found GM Enter intent")
            self._state = GamemasterFSM.STATES.Intent

    async def _intent_action(self):
        if await self.model.request_gamemaster():
            print("Become GM")
            self._state = GamemasterFSM.STATES.Gamemaster
        else:
            print("Found GM Enter End")
            self._state = GamemasterFSM.STATES.End

    async def _gamemaster_action(self):
        print("Pass")
        pass

    async def _end_action(self):
        print("Broken")
        self._state = GamemasterFSM.STATES.Initial


async def handler(websocket: WebSocketServerProtocol, game: Game):
    unit_id = None
    try:
        async for msg in websocket:
            _logger.info(f"Received WebSocket message: {msg}")

            if not msg:
                _logger.warning("Received empty message")
                continue

            try:
                decoded = json.loads(msg)
            except json.JSONDecodeError as e:
                _logger.error(f"JSON decode error: {e}")
                await websocket.close(code=1002, reason='Invalid JSON format')
                return

            if decoded['type'] == 'REGISTER':
                await websocket.ping()
                unit_id = int(decoded['id'], 16)
                game.register(unit_id, Unit(websocket, unit_id))
            elif decoded['type'] == 'BUTTON_PRESSED':
                if unit_id is not None:
                    game.button_pressed(unit_id)
            elif decoded['type'] == 'BUTTON_RELEASED':
                if unit_id is not None:
                    game.button_released(unit_id)
            elif decoded['type'] == 'UNREGISTER':
                if unit_id is not None:
                    game.unregister(unit_id)
                    break
    except ConnectionClosedError as e:
        if unit_id is not None:
            _logger.info(f"Finalizing unit: {unit_id:#x}")
            game.unregister(unit_id)


async def process_request(path, req_headers, game_params: GamemasterFSM):
    if path == '/alive':
        if game_params._state == GamemasterFSM.STATES.Gamemaster:
            _logger.info("Handling /alive request, returning FOUND (302)")
            return http.HTTPStatus.FOUND, [], f'{game_params.model.url}\n'.encode()
        else:
            _logger.info("Handling /alive request, returning active gamemaster URL")
            return http.HTTPStatus.OK, [], f'{game_params.model.active_gamemaster}\n'.encode()
    elif path == '/gamemaster':
        if game_params._state == GamemasterFSM.STATES.Gamemaster:
            return http.HTTPStatus.FOUND, [], f'{game_params.model.url}\n'.encode()
        else:
            return http.HTTPStatus.OK, [], f'{game_params.model.active_gamemaster}\n'.encode()
    elif path == '/request_gamemaster':
        if game_params._state in {GamemasterFSM.STATES.Initial, GamemasterFSM.STATES.End}:
            return http.HTTPStatus.OK, [], b''
        elif game_params._state == GamemasterFSM.STATES.Intent:
            return http.HTTPStatus.CONFLICT, [], f'{game_params.model.priority}\n'.encode()
        elif game_params._state == GamemasterFSM.STATES.Gamemaster:
            return http.HTTPStatus.FOUND, [], f'{game_params.model.url}\n'.encode()


def parse_arguments(args: list[str]):
    parser = argparse.ArgumentParser()

    parser.add_argument('-u', '--url', required=True)

    parser.add_argument('-p', '--priority', type=int, required=True)

    parser.add_argument('-k', '--key',
                        metavar='path',
                        help='The path to the gamemaster key', required=True)

    parser.add_argument('-r', '--certificate',
                        metavar='path',
                        help='The path to the gamemaster certificate', required=True)

    parser.add_argument('-g', '--gamemaster-urls',
                        action='append', required=True)

    parser.add_argument('-ca', '--ca-certificate',
                        metavar='path',
                        help='The path to the CA certificate', required=True)

    return parser.parse_args(args)


async def main(args: list[str]):
    options = parse_arguments(args)

    game = Game()

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(options.certificate, options.key)

    gamemaster_params = Gamemaster(
        options.url,
        options.priority,
        options.gamemaster_urls,
        ssl_context)
    gamemaster_state = GamemasterFSM(gamemaster_params)

    async def process_wrap(path, req_h):
        return await process_request(path, req_h, gamemaster_state)

    # Refactor: Push everything to the same server port
    async with serve(lambda x: handler(x, Game()), options.url, 8002, ping_interval=5, ssl=ssl_context, process_request=process_wrap):
        while True:
            if gamemaster_state._state == gamemaster_state.STATES.Gamemaster:
                async with serve(lambda x: handler(x, game), options.url, 8001, ping_interval=5, ssl=ssl_context):
                    await asyncio.Future()  # run forever
            elif gamemaster_state._state == gamemaster_state.STATES.End:
                try:
                    async with connect(f"wss://{gamemaster_params.active_gamemaster}:8002", ssl=ssl_context) as socket:
                        await asyncio.Future()
                except ConnectionClosedError:
                    pass
            await gamemaster_state.step()

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
