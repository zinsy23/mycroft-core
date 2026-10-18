import os
import random
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from mycroft.skills.voice_flow import (
    FlowError,
    VoiceFlowMixin,
    _parse_flow_file,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_flow(content: str) -> str:
    """Write a .flow file to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.flow', delete=False)
    f.write(content)
    f.close()
    return f.name


def _make_skill(flow_content: str, handlers: dict = None):
    """Build a minimal VoiceFlowMixin instance backed by a temp .flow file.

    handlers: {state_name: callable(entities) -> result}
    """
    tmp = tempfile.mkdtemp()
    locale_dir = os.path.join(tmp, 'locale', 'en-us')
    os.makedirs(locale_dir)

    flow_path = os.path.join(locale_dir, 'test.flow')
    with open(flow_path, 'w') as f:
        f.write(flow_content)

    spoken = []
    groups_enabled = []
    groups_disabled = []
    events_added = []
    bus_emits = []

    class FakeSkill(VoiceFlowMixin):
        skill_id = 'test-skill'
        log = MagicMock()

        def __init__(self):
            self._flow_defs = None
            self._flow_name = None
            self._flow_state = None
            self.flow = None
            self.bus = MagicMock()
            self.bus.emit.side_effect = self._on_bus_emit

        def _on_bus_emit(self, msg):
            bus_emits.append(msg)
            data = msg.data
            if data.get('action') == 'enable':
                groups_enabled.append(data['group'])
            elif data.get('action') == 'disable':
                groups_disabled.append(data['group'])

        def add_event(self, event_name, handler):
            events_added.append(event_name)

        def _speak_blocking(self, text):
            spoken.append(text)
            return 1.0

        def _flow_locale_dir(self):
            return locale_dir

    skill = FakeSkill()

    # Attach any per-state handlers
    if handlers:
        for state_name, fn in handlers.items():
            setattr(skill, f'handle_{state_name}', fn)

    skill._state = {
        'spoken': spoken,
        'groups_enabled': groups_enabled,
        'groups_disabled': groups_disabled,
        'events_added': events_added,
        'bus_emits': bus_emits,
    }
    return skill


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestParseFlowFile(unittest.TestCase):

    def test_single_line_prompt(self):
        path = _write_flow(
            "state: greet\n"
            "  prompt: Hello there.\n"
        )
        states, order, _ = _parse_flow_file(path)
        self.assertEqual(states['greet']['prompts'], ['Hello there.'])

    def test_multiline_prompt(self):
        path = _write_flow(
            "state: greet\n"
            "  prompt:\n"
            "    Hello there.\n"
            "    Hi, how are you?\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['greet']['prompts'], ['Hello there.', 'Hi, how are you?'])

    def test_patterns_block(self):
        path = _write_flow(
            "state: ask\n"
            "  patterns:\n"
            "    yes\n"
            "    no\n"
            "    maybe\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['ask']['patterns'], ['yes', 'no', 'maybe'])

    def test_goto(self):
        path = _write_flow(
            "state: a\n"
            "  prompt: A\n"
            "  goto: b\n"
            "state: b\n"
            "  prompt: B\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['a']['goto'], 'b')
        self.assertIsNone(states['b']['goto'])

    def test_next_as_goto_alias(self):
        path = _write_flow(
            "state: a\n"
            "  prompt: A\n"
            "  next: b\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['a']['goto'], 'b')

    def test_on_cancel(self):
        path = _write_flow(
            "state: a\n"
            "  prompt: A\n"
            "  on_cancel: END\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['a']['on_cancel'], 'END')

    def test_error_block_sets_is_error(self):
        path = _write_flow(
            "state: bad_input\n"
            "  error: That didn't work.\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertTrue(states['bad_input']['is_error'])
        self.assertEqual(states['bad_input']['error_lines'], ["That didn't work."])

    def test_error_state_true_flag(self):
        path = _write_flow(
            "state: bad_input\n"
            "  prompt: Oops.\n"
            "  error_state: true\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertTrue(states['bad_input']['is_error'])

    def test_multiline_error(self):
        path = _write_flow(
            "state: err\n"
            "  error:\n"
            "    Try again.\n"
            "    That was wrong.\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertTrue(states['err']['is_error'])
        self.assertEqual(states['err']['error_lines'], ['Try again.', 'That was wrong.'])

    def test_sequential_order(self):
        path = _write_flow(
            "state: first\n"
            "  prompt: First.\n"
            "state: second\n"
            "  prompt: Second.\n"
            "state: third\n"
            "  prompt: Third.\n"
        )
        states, order, _e = _parse_flow_file(path)
        self.assertEqual(order, ['first', 'second', 'third'])
        self.assertEqual(states['first']['index'], 0)
        self.assertEqual(states['second']['index'], 1)
        self.assertEqual(states['third']['index'], 2)

    def test_comments_and_blank_lines_ignored(self):
        path = _write_flow(
            "# This is a comment\n"
            "\n"
            "state: greet\n"
            "  # another comment\n"
            "  prompt: Hello.\n"
            "\n"
        )
        states, order, _e = _parse_flow_file(path)
        self.assertEqual(order, ['greet'])
        self.assertEqual(states['greet']['prompts'], ['Hello.'])

    def test_entry_block_parsed(self):
        path = _write_flow(
            "entry: matrix.define\n"
            "  patterns:\n"
            "    define matrices\n"
            "    define dot product matrices\n"
            "state: ask\n"
            "  prompt: Ready.\n"
        )
        states, order, entry_patterns = _parse_flow_file(path)
        self.assertEqual(order, ['ask'])
        self.assertNotIn('entry', states)
        self.assertIn('matrix.define', entry_patterns)
        self.assertEqual(entry_patterns['matrix.define'], ['define matrices', 'define dot product matrices'])

    def test_entry_block_empty_when_absent(self):
        path = _write_flow(
            "state: ask\n"
            "  prompt: Ready.\n"
        )
        states, order, entry_patterns = _parse_flow_file(path)
        self.assertEqual(entry_patterns, {})

    def test_empty_file(self):
        path = _write_flow("")
        states, order, _e = _parse_flow_file(path)
        self.assertEqual(states, {})
        self.assertEqual(order, [])

    def test_multiple_states_independent(self):
        path = _write_flow(
            "state: a\n"
            "  prompt: A.\n"
            "  patterns:\n"
            "    yes\n"
            "state: b\n"
            "  prompt: B.\n"
            "  patterns:\n"
            "    no\n"
        )
        states, order, _e = _parse_flow_file(path)
        self.assertEqual(states['a']['patterns'], ['yes'])
        self.assertEqual(states['b']['patterns'], ['no'])
        self.assertEqual(states['a']['prompts'], ['A.'])
        self.assertEqual(states['b']['prompts'], ['B.'])

    def test_state_defaults_when_fields_omitted(self):
        path = _write_flow(
            "state: minimal\n"
            "  prompt: Hi.\n"
        )
        states, _, _e = _parse_flow_file(path)
        s = states['minimal']
        self.assertIsNone(s['goto'])
        self.assertIsNone(s['on_cancel'])
        self.assertFalse(s['is_error'])
        self.assertEqual(s['patterns'], [])
        self.assertEqual(s['error_lines'], [])

    def test_goto_end_parsed(self):
        path = _write_flow(
            "state: last\n"
            "  prompt: Done.\n"
            "  goto: END\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['last']['goto'], 'END')

    def test_state_with_patterns_no_prompt(self):
        path = _write_flow(
            "state: silent\n"
            "  patterns:\n"
            "    go\n"
            "    stop\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['silent']['prompts'], [])
        self.assertEqual(states['silent']['patterns'], ['go', 'stop'])

    def test_patterns_with_entity_slots(self):
        path = _write_flow(
            "state: dims\n"
            "  prompt: Enter dimensions.\n"
            "  patterns:\n"
            "    {number_rows} by {number_cols}\n"
            "    {number_rows} rows [by|and] {number_cols} [columns|cols|]\n"
            "    cancel\n"
        )
        states, _, _e = _parse_flow_file(path)
        pats = states['dims']['patterns']
        self.assertEqual(len(pats), 3)
        self.assertIn('{number_rows} by {number_cols}', pats)
        self.assertIn('{number_rows} rows [by|and] {number_cols} [columns|cols|]', pats)
        self.assertIn('cancel', pats)

    def test_all_fields_together(self):
        path = _write_flow(
            "state: full\n"
            "  prompt:\n"
            "    Option A.\n"
            "    Option B.\n"
            "  patterns:\n"
            "    yes\n"
            "    no\n"
            "    cancel\n"
            "  goto: next_state\n"
            "  on_cancel: END\n"
        )
        states, _, _e = _parse_flow_file(path)
        s = states['full']
        self.assertEqual(s['prompts'], ['Option A.', 'Option B.'])
        self.assertEqual(s['patterns'], ['yes', 'no', 'cancel'])
        self.assertEqual(s['goto'], 'next_state')
        self.assertEqual(s['on_cancel'], 'END')
        self.assertFalse(s['is_error'])

    def test_full_realistic_flow(self):
        # Mirrors the actual matrix.define.flow
        path = _write_flow(
            "state: get_dims_a\n"
            "  prompt:\n"
            "    Specify dimensions of the first matrix.\n"
            "    What size is matrix A?\n"
            "  patterns:\n"
            "    {number_rows} by {number_cols}\n"
            "    {number_rows} rows [by|and] {number_cols} [columns|cols|]\n"
            "    cancel\n"
            "  on_cancel: END\n"
            "\n"
            "state: get_dims_b\n"
            "  prompt: Now specify dimensions of the second matrix.\n"
            "  patterns:\n"
            "    {number_rows} by {number_cols}\n"
            "    cancel\n"
            "  on_cancel: END\n"
            "\n"
            "state: compat_error\n"
            "  error:\n"
            "    Incompatible. A has {cols_a} columns, B needs {cols_a} rows.\n"
            "    Those don't work. B needs {cols_a} rows.\n"
            "  goto: get_dims_b\n"
            "\n"
            "state: done\n"
            "  prompt: Compatible. Ready to enter values.\n"
            "  goto: END\n"
        )
        states, order, _e = _parse_flow_file(path)

        self.assertEqual(order, ['get_dims_a', 'get_dims_b', 'compat_error', 'done'])

        a = states['get_dims_a']
        self.assertEqual(len(a['prompts']), 2)
        self.assertEqual(len(a['patterns']), 3)
        self.assertEqual(a['on_cancel'], 'END')
        self.assertFalse(a['is_error'])

        b = states['get_dims_b']
        self.assertEqual(b['prompts'], ['Now specify dimensions of the second matrix.'])
        self.assertEqual(len(b['patterns']), 2)

        err = states['compat_error']
        self.assertTrue(err['is_error'])
        self.assertEqual(len(err['error_lines']), 2)
        self.assertEqual(err['goto'], 'get_dims_b')
        self.assertEqual(err['patterns'], [])

        done = states['done']
        self.assertEqual(done['goto'], 'END')
        self.assertFalse(done['is_error'])

    def test_multiline_prompt_then_patterns_no_blank_line(self):
        # Regression: multiline prompt immediately followed by patterns: without blank line
        path = _write_flow(
            "state: ask\n"
            "  prompt:\n"
            "    Hello.\n"
            "    Hi there.\n"
            "  patterns:\n"
            "    yes\n"
            "    no\n"
        )
        states, _, _e = _parse_flow_file(path)
        self.assertEqual(states['ask']['prompts'], ['Hello.', 'Hi there.'])
        self.assertEqual(states['ask']['patterns'], ['yes', 'no'])

    def test_multiple_states_each_with_all_fields(self):
        path = _write_flow(
            "state: one\n"
            "  prompt:\n"
            "    One A.\n"
            "    One B.\n"
            "  patterns:\n"
            "    alpha\n"
            "    beta\n"
            "  goto: two\n"
            "\n"
            "state: two\n"
            "  prompt:\n"
            "    Two A.\n"
            "    Two B.\n"
            "  patterns:\n"
            "    gamma\n"
            "    delta\n"
            "  on_cancel: END\n"
            "  goto: three\n"
            "\n"
            "state: three\n"
            "  error:\n"
            "    Error A.\n"
            "    Error B.\n"
            "  goto: one\n"
        )
        states, order, _e = _parse_flow_file(path)
        self.assertEqual(order, ['one', 'two', 'three'])

        self.assertEqual(states['one']['prompts'], ['One A.', 'One B.'])
        self.assertEqual(states['one']['patterns'], ['alpha', 'beta'])
        self.assertEqual(states['one']['goto'], 'two')

        self.assertEqual(states['two']['prompts'], ['Two A.', 'Two B.'])
        self.assertEqual(states['two']['patterns'], ['gamma', 'delta'])
        self.assertEqual(states['two']['on_cancel'], 'END')
        self.assertEqual(states['two']['goto'], 'three')

        self.assertTrue(states['three']['is_error'])
        self.assertEqual(states['three']['error_lines'], ['Error A.', 'Error B.'])
        self.assertEqual(states['three']['goto'], 'one')


# ── State machine tests ───────────────────────────────────────────────────────

SIMPLE_FLOW = """\
state: step_one
  prompt: What is your name?
  patterns:
    {free_text}
    cancel

state: step_two
  prompt: Got it.
  patterns:
    yes
    no

state: done
  prompt: All done.
  goto: END
"""

TWO_STATE_FLOW = """\
state: first
  prompt: First prompt.
  patterns:
    continue
    cancel

state: second
  prompt: Second prompt.
  patterns:
    done
"""

ERROR_STATE_FLOW = """\
state: ask
  prompt: Give me a number.
  patterns:
    {number}
    cancel

state: bad_number
  error: That number is too small. Try again.
  goto: ask

state: done
  prompt: Perfect.
  goto: END
"""


class TestVoiceFlowMixin(unittest.TestCase):

    def test_start_flow_enters_first_state(self):
        skill = _make_skill(SIMPLE_FLOW)
        skill.start_flow('test')
        self.assertEqual(skill._flow_state, 'step_one')

    def test_start_flow_speaks_prompt(self):
        skill = _make_skill(SIMPLE_FLOW)
        skill.start_flow('test')
        self.assertEqual(skill._state['spoken'], ['What is your name?'])

    def test_start_flow_enables_first_state_group(self):
        skill = _make_skill(SIMPLE_FLOW)
        skill.start_flow('test')
        self.assertIn('__flow__test__step_one', skill._state['groups_enabled'])

    def test_start_flow_resets_context(self):
        skill = _make_skill(SIMPLE_FLOW)
        skill.start_flow('test')
        skill.flow['key'] = 'value'
        skill.start_flow('test')
        self.assertEqual(skill.flow, {})

    def test_handler_none_advances_sequentially(self):
        skill = _make_skill(TWO_STATE_FLOW, handlers={
            'first': lambda entities: None,
        })
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)
        self.assertEqual(skill._flow_state, 'second')

    def test_handler_goto_state_name(self):
        skill = _make_skill(ERROR_STATE_FLOW, handlers={
            'ask': lambda entities: 'bad_number',
        })
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {'number': 1}, 'utterance': 'one'}
        skill._handle_flow_input(msg)
        self.assertEqual(skill._flow_state, 'ask')  # error state → goto: ask

    def test_handler_end_exits_flow(self):
        skill = _make_skill(TWO_STATE_FLOW, handlers={
            'first': lambda entities: 'END',
        })
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)
        self.assertIsNone(skill._flow_state)
        self.assertIsNone(skill._flow_name)

    def test_flow_error_re_prompts_current_state(self):
        def bad_handler(entities):
            raise FlowError("That's not valid.")

        skill = _make_skill(TWO_STATE_FLOW, handlers={'first': bad_handler})
        skill.start_flow('test')
        skill._state['spoken'].clear()

        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)

        self.assertEqual(skill._flow_state, 'first')  # stayed in same state
        self.assertIn("That's not valid.", skill._state['spoken'])
        self.assertIn('First prompt.', skill._state['spoken'])  # re-prompted

    def test_error_state_speaks_error_lines(self):
        skill = _make_skill(ERROR_STATE_FLOW, handlers={
            'ask': lambda entities: 'bad_number',
        })
        skill.start_flow('test')
        skill._state['spoken'].clear()

        msg = MagicMock()
        msg.data = {'entities': {'number': 1}, 'utterance': 'one'}
        skill._handle_flow_input(msg)

        self.assertIn('That number is too small. Try again.', skill._state['spoken'])

    def test_error_state_not_reached_sequentially(self):
        # After 'ask' handler returns None, should skip 'bad_number' and go to 'done'
        skill = _make_skill(ERROR_STATE_FLOW, handlers={
            'ask': lambda entities: None,
        })
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {'number': 5}, 'utterance': 'five'}
        skill._handle_flow_input(msg)
        # done has goto: END so flow ends
        self.assertIsNone(skill._flow_state)

    def test_on_cancel_shortcut_end(self):
        flow = """\
state: ask
  prompt: Say something.
  on_cancel: END
  patterns:
    something
    cancel
"""
        skill = _make_skill(flow)
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'cancel'}
        skill._handle_flow_input(msg)
        self.assertIsNone(skill._flow_state)

    def test_on_cancel_shortcut_goto_state(self):
        flow = """\
state: ask
  prompt: Say something.
  on_cancel: fallback
  patterns:
    something
    cancel

state: fallback
  prompt: Cancelled.
  goto: END
"""
        skill = _make_skill(flow)
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'cancel'}
        skill._handle_flow_input(msg)
        # fallback has goto: END, terminal — flow exits after speaking
        self.assertIsNone(skill._flow_state)

    def test_prompt_interpolation(self):
        flow = """\
state: collect
  prompt: Give a value.
  patterns:
    go

state: confirm
  prompt: You said {name}.
  patterns:
    done
"""
        skill = _make_skill(flow, handlers={'collect': lambda e: None})
        skill.start_flow('test')
        skill.flow['name'] = 'Alice'
        skill._state['spoken'].clear()
        # Advance to confirm state
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'go'}
        skill._handle_flow_input(msg)
        self.assertIn('You said Alice.', skill._state['spoken'])

    def test_prompt_interpolation_missing_key_unchanged(self):
        flow = """\
state: collect
  prompt: Give a value.
  patterns:
    go

state: confirm
  prompt: You said {name}.
  patterns:
    done
"""
        skill = _make_skill(flow, handlers={'collect': lambda e: None})
        skill.start_flow('test')
        skill._state['spoken'].clear()
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'go'}
        skill._handle_flow_input(msg)
        self.assertIn('You said {name}.', skill._state['spoken'])

    def test_multiline_prompt_random_pick(self):
        flow = """\
state: greet
  prompt:
    Hello.
    Hi there.
    Hey.
  patterns:
    go
"""
        options = {'Hello.', 'Hi there.', 'Hey.'}
        seen = set()
        for _ in range(30):
            # Fresh skill per iteration so no spoken list bleed
            skill = _make_skill(flow)
            skill.start_flow('test')
            self.assertEqual(len(skill._state['spoken']), 1)
            seen.add(skill._state['spoken'][0])
        # With 30 tries, extremely likely to see at least 2 of the 3 options
        self.assertGreater(len(seen), 1)
        self.assertTrue(seen.issubset(options))

    def test_session_end_clears_flow(self):
        skill = _make_skill(SIMPLE_FLOW)
        skill.start_flow('test')
        self.assertIsNotNone(skill._flow_state)
        skill.handle_realtime_session_end(MagicMock())
        self.assertIsNone(skill._flow_state)
        self.assertIsNone(skill._flow_name)
        self.assertEqual(skill.flow, {})

    def test_session_end_noop_when_no_active_flow(self):
        skill = _make_skill(SIMPLE_FLOW)
        # Should not raise
        skill.handle_realtime_session_end(MagicMock())
        self.assertIsNone(skill._flow_state)

    def test_previous_state_group_disabled_on_transition(self):
        skill = _make_skill(TWO_STATE_FLOW, handlers={
            'first': lambda entities: None,
        })
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)
        self.assertIn('__flow__test__first', skill._state['groups_disabled'])

    def test_handler_string_not_state_name_re_prompts(self):
        # Returning a string that isn't a state name → speak it, re-enter same state
        skill = _make_skill(TWO_STATE_FLOW, handlers={
            'first': lambda entities: 'This is not a state.',
        })
        skill.start_flow('test')
        skill._state['spoken'].clear()
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)
        self.assertIn('This is not a state.', skill._state['spoken'])
        self.assertEqual(skill._flow_state, 'first')

    def test_no_handler_advances_sequentially(self):
        # If no handle_<state> method exists, result is None → sequential advance
        skill = _make_skill(TWO_STATE_FLOW)
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)
        self.assertEqual(skill._flow_state, 'second')

    def test_end_of_flow_auto_exits(self):
        # Last non-error state with no goto → flow exits after handler returns None
        flow = """\
state: only
  prompt: Only state.
  patterns:
    done
"""
        skill = _make_skill(flow, handlers={'only': lambda e: None})
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'done'}
        skill._handle_flow_input(msg)
        self.assertIsNone(skill._flow_state)

    def test_flow_context_persists_across_states(self):
        flow = """\
state: collect
  prompt: Give a value.
  patterns:
    {number}

state: confirm
  prompt: You gave {value}.
  goto: END
"""
        def collect_handler(entities):
            skill.flow['value'] = 42
            return None

        skill = _make_skill(flow, handlers={'collect': collect_handler})
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {'number': 42}, 'utterance': 'forty two'}
        skill._handle_flow_input(msg)
        # confirm state spoken with interpolated value
        self.assertIn('You gave 42.', skill._state['spoken'])


    def test_stray_input_ignored_when_no_active_flow(self):
        skill = _make_skill(TWO_STATE_FLOW)
        # No start_flow called — flow is inactive
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)  # should not raise
        self.assertIsNone(skill._flow_state)

    def test_non_flow_error_exception_logged_not_crashed(self):
        def crashing_handler(entities):
            raise RuntimeError("unexpected crash")

        skill = _make_skill(TWO_STATE_FLOW, handlers={'first': crashing_handler})
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)  # should not propagate
        # Flow stays in current state
        self.assertEqual(skill._flow_state, 'first')

    def test_flow_error_in_first_state_no_previous_group_to_disable(self):
        def bad_handler(entities):
            raise FlowError("Bad input.")

        skill = _make_skill(TWO_STATE_FLOW, handlers={'first': bad_handler})
        skill.start_flow('test')
        skill._state['groups_disabled'].clear()
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)  # should not raise
        self.assertEqual(skill._flow_state, 'first')
        # No groups should have been spuriously disabled
        self.assertEqual(skill._state['groups_disabled'], [])

    def test_restart_same_flow_resets_state(self):
        skill = _make_skill(TWO_STATE_FLOW, handlers={
            'first': lambda e: None,
        })
        skill.start_flow('test')
        # Advance to second state
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'continue'}
        skill._handle_flow_input(msg)
        self.assertEqual(skill._flow_state, 'second')
        skill.flow['leftover'] = 'data'

        # Restart — should be back at first, context cleared
        skill.start_flow('test')
        self.assertEqual(skill._flow_state, 'first')
        self.assertEqual(skill.flow, {})

    def test_multiple_error_states_each_routable(self):
        flow = """\
state: ask
  prompt: Give input.
  patterns:
    {number}
    go

state: error_small
  error: Too small.
  goto: ask

state: error_large
  error: Too large.
  goto: ask

state: done
  prompt: Perfect.
  goto: END
"""
        results = iter(['error_small', 'error_large', None])

        def ask_handler(entities):
            return next(results)

        skill = _make_skill(flow, handlers={'ask': ask_handler})
        skill.start_flow('test')

        # First match → error_small → speaks error, goes back to ask
        msg = MagicMock()
        msg.data = {'entities': {'number': 1}, 'utterance': 'one'}
        skill._handle_flow_input(msg)
        self.assertIn('Too small.', skill._state['spoken'])
        self.assertEqual(skill._flow_state, 'ask')

        # Second match → error_large → speaks error, goes back to ask
        skill._handle_flow_input(msg)
        self.assertIn('Too large.', skill._state['spoken'])
        self.assertEqual(skill._flow_state, 'ask')

        # Third match → None → advances to done → goto END → flow exits
        skill._handle_flow_input(msg)
        self.assertIsNone(skill._flow_state)

    def test_terminal_state_no_patterns_auto_advances(self):
        # A state with no patterns auto-advances to next on entry
        flow = """\
state: active
  prompt: Say something.
  patterns:
    go

state: silent
  prompt: Auto-advancing.

state: final
  prompt: Done.
  goto: END
"""
        skill = _make_skill(flow, handlers={'active': lambda e: None})
        skill.start_flow('test')
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'go'}
        skill._handle_flow_input(msg)
        # silent has no patterns so auto-advances to final, which has goto:END
        self.assertIsNone(skill._flow_state)
        self.assertIn('Auto-advancing.', skill._state['spoken'])
        self.assertIn('Done.', skill._state['spoken'])

    def test_state_without_prompt_no_speak_on_entry(self):
        flow = """\
state: silent_entry
  patterns:
    go

state: next
  prompt: Next state.
  goto: END
"""
        skill = _make_skill(flow, handlers={'silent_entry': lambda e: None})
        skill.start_flow('test')
        skill._state['spoken'].clear()
        msg = MagicMock()
        msg.data = {'entities': {}, 'utterance': 'go'}
        skill._handle_flow_input(msg)
        # Only the next state's prompt should be spoken
        self.assertEqual(skill._state['spoken'], ['Next state.'])


if __name__ == '__main__':
    unittest.main()
