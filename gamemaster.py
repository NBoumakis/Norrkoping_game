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

import aiohttp
import websockets
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
        self.start_button_led((255, 255, 0), at)
        self.start_matrix((255, 205, 0), at)

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
                   'WaitRelease',
                   'Timeout'])  #(Bugfix) New Timeout State

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

        if self.state == Game.STATES.Timeout:  # Ignore button press during Timeout state
            _logger.info("Ignoring button press during Timeout state")
            return

        if unit_id in self.ACTIVE:
            unit = self.ACTIVE[unit_id]

            unit.button_pressed = True
            self.pressed_units.add(unit)

            if self.state in self._button_pressed_callbacks:
                self._button_pressed_callbacks[self.state](unit)

    def button_released(self, unit_id: int):
        _logger.info(f"Event: Button Released, Unit: {unit_id:#x}")

        if self.state == Game.STATES.Timeout: #(Bugfix) Set state to Timeout
            _logger.info("Ignoring button release during Timeout state")
            return

        if unit_id in self.ACTIVE:
            unit = self.ACTIVE[unit_id]

            unit.button_pressed = False
            self.pressed_units.discard(unit)

            if self.state in self._button_released_callbacks: #Multi: include condition checks for single and multi
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

    async def _control_Timeout(self):
        lose_sound = random.randint(1, 6)
        for unit in self.ACTIVE.values():
            unit.lose(f"sounds/lose/lose{lose_sound}.wav", datetime.now())

        await asyncio.sleep(4)

        for unit in self.ACTIVE.values():
            unit.stop_all(datetime.now())

        if not self.pressed_units:
            if len(self.ACTIVE) > 1:
                assert self._control_task is not None
                _logger.info(f"_control_Timeout cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(self._control_PreGameMultiple())
                self.state = Game.STATES.PreGameMultiple
            elif len(self.ACTIVE) == 1:
                assert self._control_task is not None
                _logger.info(f"_control_Timeout cancels task:{self._control_task}")
                self._control_task.cancel()
                self._control_task = asyncio.create_task(self._control_PreGameSingle())
                self.state = Game.STATES.PreGameSingle

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
        if self._control_task is not None:
            _logger.info(f"_control_PlayingAllReleased cancels task:{self._control_task}")
            self._control_task.cancel()
        self._control_task = asyncio.create_task(self._control_Timeout())
        self.state = Game.STATES.Timeout #(Bugfix) Removed logic from here and moved to the Timeout state.

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
        self.priorities = {}

    async def _get_is_gamemaster(self, session: aiohttp.ClientSession, url: str):
        try:
            async with session.get(f"http://{url}:8002/gamemaster", timeout=1) as response:
                response_text = await response.text()
                if response.status in {200, 302}:
                    priority = int(response_text.strip())
                    logging.info(f"GM found at {url} with priority: {priority}")
                    self.priorities[url] = priority
                    return url if priority < self.priority else None
                return False
        except Exception as e:
            logging.error(f"Error checking GM at {url}: {str(e)}")
            return None

    async def get_gamemaster(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
            results = await asyncio.gather(
                *(self._get_is_gamemaster(session, url) for url in self.gamemaster_urls if url != self.url),
                return_exceptions=False
            )
            active_gms = [result for result in results if result not in (False, None)]
            if active_gms:
                self.active_gamemaster = min(active_gms, key=lambda url: self.priorities[url])  # Pick the highest priority
                return self.active_gamemaster
            if all(result is None for result in results):
                return None
            return False

    async def _request_gamemaster(self, session: aiohttp.ClientSession, url: str):
        try:
            async with session.get(f"http://{url}:8002/request_gamemaster") as response:
                response_text = await response.text()
                logging.info(f"GM request to {url} got: {response.status}, Body: {response_text}")
                if response.status == 200:
                    # logging.info(f"Successfully became GM at {url}")
                    return True
                elif response.status == 302:
                    return False
        except Exception as e:
            logging.error(f"Network error on GM request at {url}: {str(e)}")
            return None

    async def request_gamemaster(self):
        print(f"Requesting GM from URLs: {self.gamemaster_urls}")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
            results = await asyncio.gather(
                *(self._request_gamemaster(session, url) for url in self.gamemaster_urls if url != self.url),
                return_exceptions=False
            )
            if all(result is True for result in results if result is not None):
                self.active_gamemaster = self.url
                logging.info(f"------- Successfully became the active Gamemaster: {self.url} -------")
                return True
            elif any(result is False for result in results if result is not None):
                return False
            return None

class GamemasterFSM():
    STATES = IntEnum('States', ['Initial', 'Intent', 'Gamemaster', 'End'])

    def __init__(self, model: Gamemaster) -> None:
        self._state = GamemasterFSM.STATES.Initial
        self.model = model
        self.waiting_intent = []
        logging.info(f"Initialized FSM in {self._state.name} state.")

    async def step(self):
        logging.info(f"FSM stepping from state {self._state.name}")
        if self._state == self.STATES.Initial:
            result = await self.model.get_gamemaster()
            if result:
                logging.info(f"FSM detected active GM at {result}. Transitioning to Intent state.")
                self._state = self.STATES.Intent
                self.waiting_intent.append((self.model.url, self.model.priority))
            else:
                logging.info("No active GM found. Attempting to become GM.")
                if await self.model.request_gamemaster():
                    self._state = self.STATES.Gamemaster
                    # logging.info("Successfully became Gamemaster.")
                else:
                    logging.error("Failed to become GM. Staying in Initial to retry.")
                    await asyncio.sleep(5)
        elif self._state == self.STATES.Intent:
            while True:
                result = await self.model.get_gamemaster()
                if result:
                    logging.info(f"------- Still detected active Gamemaster at {result}. Remaining in Intent. -------")
                    self.waiting_intent.append((self.model.url, self.model.priority))
                    await asyncio.sleep(10)
                else:
                    logging.info("No active Gamemaster on recheck. Attempting to become Gamemaster.")
                    if await self.model.request_gamemaster():
                        self._state = self.STATES.Gamemaster
                        logging.info("------- Successfully transitioned to Gamemaster from Intent state. -------")
                        break
                    await asyncio.sleep(5)
        elif self._state == self.STATES.Gamemaster:
            logging.info("Operating as Gamemaster.")
            await asyncio.sleep(10)
        elif self._state == self.STATES.End:
            logging.info("Active Gamemaster has failed. Notifying highest priority in Intent state.")
            if self.waiting_intent:
                self.waiting_intent.sort(key=lambda gm: gm[1])  # Sort by priority
                highest_priority_url = self.waiting_intent[0][0]
                try:
                    async with connect(f"ws://{highest_priority_url}:8002") as socket:
                        await socket.send(json.dumps({'type': 'GM_FAIL'}))
                except ConnectionClosedError:
                    pass
                self.waiting_intent.pop(0)
                self._state = self.STATES.Initial
                await self.step()  # Retry logic

async def handler(websocket: WebSocketServerProtocol, path: str, game: Game):
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
                    await websocket.close()
                    break
    except ConnectionClosedError as e:
        # _logger.error(f"Connection closed with error: {e}")
        if unit_id is not None:
            game.unregister(unit_id)
    except Exception as e:
        _logger.error(f"Unexpected error: {e}")
        if unit_id is not None:
            game.unregister(unit_id)
    finally:
        if unit_id is not None:
            _logger.info(f"Finalizing unit: {unit_id:#x}")
            game.unregister(unit_id)

# Helper function to decide if a request is for WebSocket or HTTP
async def is_websocket_request(request_headers):
    upgrade_header = request_headers.get('Upgrade', '').lower()
    connection_header = request_headers.get('Connection', '').lower()
    return 'websocket' in upgrade_header and 'upgrade' in connection_header

async def process_request(path, req_headers, game_params: GamemasterFSM):
    upgrade_header = req_headers.get('Upgrade', '').lower()
    connection_header = req_headers.get('Connection', '').lower()

    if 'websocket' in upgrade_header and 'upgrade' in connection_header:
        _logger.info("WebSocket upgrade request detected")
        return None

    if path == '/alive':
        if game_params._state == GamemasterFSM.STATES.Gamemaster:
            _logger.info("Handling /alive request, returning FOUND (302)")
            return http.HTTPStatus.FOUND, [], f'{game_params.model.url}\n'.encode()
        else:
            _logger.info("Handling /alive request, returning active gamemaster URL")
            return http.HTTPStatus.OK, [], f'{game_params.model.active_gamemaster}\n'.encode()

    elif path == '/gamemaster':
        if game_params._state == GamemasterFSM.STATES.Gamemaster:
            return http.HTTPStatus.FOUND, [], f'{game_params.model.priority}\n'.encode()
        else:
            return http.HTTPStatus.OK, [], f'{game_params.model.priority}\n'.encode()
    elif path == '/request_gamemaster':
        if game_params._state in {GamemasterFSM.STATES.Initial, GamemasterFSM.STATES.End}:
            return http.HTTPStatus.OK, [], f'{game_params.model.priority}\n'.encode()
        elif game_params._state == GamemasterFSM.STATES.Intent:
            return http.HTTPStatus.CONFLICT, [], f'{game_params.model.priority}\n'.encode()
        elif game_params._state == GamemasterFSM.STATES.Gamemaster:
            return http.HTTPStatus.FOUND, [], f'{game_params.model.url}\n'.encode()
    else:
        return http.HTTPStatus.NOT_FOUND, [], b'Unhandled request path'


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

    parser.add_argument('--port', type=int, default=8002, help='The port to use for the server')

    return parser.parse_args(args)

# async def start_server(url, port, game, ssl_context, process_request):
async def start_server(url, port, game, process_request):
    print(f"Starting server at {url}:{port}")
    async with serve(
        lambda ws, path: handler(ws, path, game),
        url,
        port,
        ping_interval=5,
        # ssl=ssl_context,
        process_request=process_request):
        await asyncio.Future()  # run forever

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

    port = options.port

    async with serve(lambda ws, path: handler(ws, path, game), options.url, 8002, ping_interval=5, process_request=process_wrap):
        while True:
            if gamemaster_state._state == gamemaster_state.STATES.Gamemaster:
                async with serve(lambda ws, path: handler(ws, path, game), options.url, 8001, ping_interval=5):
                    await asyncio.Future()
            elif gamemaster_state._state == gamemaster_state.STATES.End:
                gamemaster_state.waiting_intent.sort(key=lambda gm: gm[1])  # Sort by priority
                highest_priority_url = gamemaster_state.waiting_intent[0][0]
                try:
                    async with connect(f"ws://{highest_priority_url}:8002") as socket:
                        await socket.send(json.dumps({'type': 'GM_FAIL'}))
                except ConnectionClosedError:
                    pass
                gamemaster_state.waiting_intent.pop(0)
                gamemaster_state._state = gamemaster_state.STATES.Initial
                await gamemaster_state.step()
            await gamemaster_state.step()

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
