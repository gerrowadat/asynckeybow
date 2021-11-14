import time
import enum
import asyncio
import logging
import importlib
from collections import deque


class KeyInterfaceError(Exception):
    pass


class LEDInterfaceError(KeyInterfaceError):
    pass


class KeySequenceListenerError(Exception):
    pass


class KeyStateError(KeyInterfaceError):
    pass


class Implementation(enum.Enum):
    # Actually use the keybow
    KEYBOW = enum.auto()
    # Do a simulated set of things, once.
    SIMULATED = enum.auto()
    # Don't do anything
    DUMMY = enum.auto()


class KeyState(object):
    def __init__(self):
        self._pressed = False
        self._r = 0
        self._g = 0
        self._b = 0

    @property
    def pressed(self):
        return self._pressed

    @pressed.setter
    def pressed(self, var):
        if var not in (True, False):
            raise KeyStateError('pressed must be True/False, not %s' % (type(var), ))
        self._pressed = var

    def down(self):
        self._pressed = True

    def up(self):
        self.pressed = False

    @property
    def r(self):
        return self._r

    @r.setter
    def r(self, var):
        try:
            self._r = int(var)
        except TypeError:
            raise KeyStateError('color value must be an int')

    @property
    def g(self):
        return self._g

    @g.setter
    def g(self, var):
        try:
            self._g = int(var)
        except TypeError:
            raise KeyStateError('color value must be an int')

    @property
    def b(self):
        return self._b

    @b.setter
    def b(self, var):
        try:
            self._b = int(var)
        except TypeError:
            raise KeyStateError('color value must be an int')

    @property
    def colourcode(self):
        return '%s%s%s' % (format(self.r, '02x'), format(self.g, '02x'), format(self.b, '02x'))

    def set_colour(self, r, g, b):
        self.r = r
        self.g = g
        self.b = b

    def is_lit(self):
        if self.r == self.g == self.b == 0:
            return False
        return True

    def clear(self):
        self.r = 0
        self.g = 0
        self.b = 0

    def __str__(self):
        return '[%s] : %s' % ('X' if self.pressed else ' ', self.colourcode)


class KeyInterface(object):
    def __init__(self, impl=Implementation.KEYBOW):
        self._impl = impl
        self._state = {}
        self._handlers = None
        self._last_show = None
        self._keybow = None
        self._last_press = None
        self._script = []
        self._script_position = 0

        if self._impl == Implementation.KEYBOW:
            try:
                self._keybow = importlib.import_module('keybow')
            except ModuleNotFoundError:
                raise KeyInterfaceError('keybow python module not installed')

    def setup(self, keycount=3, script=None):
        if script:
            self._script = script
        for k in range(keycount):
            self._state[k] = KeyState()
        if self._impl == Implementation.KEYBOW:
            if keycount not in (3, 12):
                raise KeyInterfaceError('keycount must be 12 (or 3 for mini)')
            if keycount == 3:
                self._keybow.setup(self._keybow.MINI)

            def _handler(idx, state):
                self.key_update(idx, state)

            for idx in range(keycount):
                self._handler = self._keybow.on(index=idx, handler=_handler)

    def key_update(self, idx, state):
        self._state[idx].pressed = state
        self._last_press = (idx, state)

    def set_led(self, idx, r, g, b):
        if self._impl == Implementation.KEYBOW:
            self._keybow.set_led(idx, r, g, b)
        self._state[idx].set_colour(r, g, b)
        self.show()

    def show(self):
        if self._impl == Implementation.KEYBOW:
            self._keybow.show()
        for k in self._state:
            logging.debug('[%d %s]' % (k, str(self._state[k])))

    def clear(self):
        if self._impl == Implementation.KEYBOW:
            self._keybow.clear()
        for k in self._state:
            self._state[k].clear()

    def _from_hexcode(self, code):
        return (int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16))

    async def led_on(self, key_idx, hexcode):
        (r, g, b) = self._from_hexcode(hexcode)
        self.set_led(key_idx, r, g, b)

    async def led_toggle(self, key_idx, hexcode=None):
        if self._state[key_idx].is_lit():
            await self.led_off(key_idx)
        else:
            if not hexcode:
                raise LEDInterfaceError('cannot toggle LED %d to on without hexcode' % (key_idx, ))
            await self.led_on(key_idx, hexcode)

    async def led_off(self, key_idx):
        self.set_led(key_idx, 0, 0, 0)

    async def all_leds_off(self):
        for idx in self._state:
            self.set_led(idx, 0, 0, 0)

    async def async_wait(self):
        if self._impl == Implementation.KEYBOW:
            while True:
                last = self._last_press
                if last:
                    self._last_press = None
                    return last
                await asyncio.sleep(1.0 / 120.0)
        elif self._impl == Implementation.SIMULATED:
            # Run through the script
            if self._script_position >= len(self._script):
                logging.info('script completed')
                self.show()
                # wait a long time, which is good enough for testing purposes.
                await asyncio.sleep(99999999)

            cmd = self._script[self._script_position]
            self._script_position += 1

            logging.debug('script cmd: %s' % (cmd, ))
            (op, arg) = cmd.split()
            arg = float(arg)
            if op == 'sleep':
                await asyncio.sleep(arg)
                return await self.async_wait()
            elif op == 'down':
                self.key_update(arg, True)
                return (arg, True)
            elif op == 'up':
                self.key_update(arg, False)
                return (arg, False)


class KeySequence(enum.Enum):
    SINGLE = 1
    HOLD = 2
    DOUBLE = 3


class KeySequenceListener(object):
    def __init__(self, interface, listen_for=[KeySequence.SINGLE]):
        """Listen for sequences of keypresses.
            listen_for: (list) list of seqwucne types to listen for.
            interface: (KeyInterface) a post-setup() KeyInterface.
        """
        self._listen_for = listen_for
        if not isinstance(interface, KeyInterface):
            raise KeySequenceListenerError('interface must be KeyInterface instance')
        self._ki = interface
        self._tl = deque([None for x in range(50)], 50)  # A timeline of events in (timestamp: event) format.

    async def produce(self, q):
        """Listen for and enqueue key sequences.
            q: (asnycio.Queue) A queue to push sequences onto
        """
        while True:
            result = []
            keypress = await self._ki.async_wait()
            # keypress is (key_index, bool) where True is on key down.
            if not keypress[1]:  # key up...
                # See if this was a press or a hold (if we care):
                key_history = [kp for kp in list(self._tl) if kp and kp[1][0] == keypress[0]]
                last_action = key_history[0]
                if last_action:
                    press_duration = (time.time() - last_action[0]) * 1000
                    if press_duration > 500 and KeySequence.HOLD in self._listen_for:
                        result = (int(keypress[0]), KeySequence.HOLD)
                    else:
                        result = (int(keypress[0]), KeySequence.SINGLE)

            self._tl.appendleft((time.time(), keypress),)
            await q.put(result)


class LEDOperation(enum.Enum):
    OFF = enum.auto()
    ON = enum.auto()
    BLINK = enum.auto()


class LEDCommand(object):
    def __init__(self, key_idx, op=LEDOperation.BLINK, r=0, g=0, b=0, blink_count=1):
        self._key_idx = key_idx
        self._op = op
        self._r = r
        self._g = g
        self._b = b
        self._blink_count = blink_count

    @property
    def key_idx(self):
        return self._key_idx

    @key_idx.setter
    def key_idx(self, val):
        self._key_idx = val

    @property
    def op(self):
        return self._op

    @op.setter
    def op(self, val):
        if val not in LEDOperation:
            raise LEDInterfaceError('value of op must be in LEDOperation')
        self._op = val

    @property
    def r(self):
        return self._r

    @r.setter
    def r(self, val):
        if not isinstance(int, val) or val < 0 or val > 255:
            raise LEDInterfaceError('value of r must be an int 0 < r < 255')
        self._r = val

    @property
    def g(self):
        return self._g

    @g.setter
    def g(self, val):
        if not isinstance(int, val) or val < 0 or val > 255:
            raise LEDInterfaceError('value of g must be an int 0 < r < 255')
        self._r = val

    @property
    def b(self):
        return self._b

    @b.setter
    def b(self, val):
        if not isinstance(int, val) or val < 0 or val > 255:
            raise LEDInterfaceError('value of b must be an int 0 < r < 255')
        self._r = val

    @property
    def blink_count(self):
        return self._blink_count

    @blink_count.setter
    def blink_count(self, val):
        self._blink_count = val
