"""VoiceFlow — declarative FSM for multi-turn realtime voice interactions.

Skills define flows in .flow files (vocab/en-us/<name>.flow).  VoiceFlowMixin
loads those files at startup, registers each state's patterns as a realtime
pattern sub-group, and drives the state machine at runtime.

Basic usage
-----------
In the skill::

    from mycroft.skills.voice_flow import VoiceFlowMixin, FlowError

    class MySkill(VoiceFlowMixin, RealtimePatternMixin, MycroftSkill):

        def initialize(self):
            ...  # riva setup as usual — no flow boilerplate needed

        @intent_handler('matrix.define.intent')
        def handle_define(self, message):
            self.start_flow('matrix.define')  # loads lazily on first call

        def handle_get_dims_a(self, entities):
            rows = entities.get('number_rows')
            if rows is None:
                raise FlowError("I need two numbers.")
            self.flow['rows_a'] = int(rows)
            # return None → advance to next state sequentially

        def handle_get_dims_b(self, entities):
            ...
            if rows_b != self.flow['cols_a']:
                return 'compat_error'   # explicit goto
            self.flow['rows_b'] = rows_b

.flow file format
-----------------
States are defined as indented blocks.  ``prompt:`` is the spoken response on
entering the state (multiple lines = random pick).  ``patterns:`` are the
patterns the user can say (same syntax as .intent files).  ``goto:`` sets the
next state explicitly; without it, the next state in file order is used.  A
state with ``error:`` instead of ``prompt:`` is an error state — only reachable
via an explicit ``return 'state_name'`` from a handler, never by sequential
advancement.

    state: get_dims_a
      prompt:
        Specify dimensions of the first matrix.
        What size is matrix A?
      patterns:
        {number_rows} by {number_cols}
        cancel

    state: compat_error
      error:
        Incompatible. A has {cols_a} columns, B needs {cols_a} rows.
      goto: get_dims_b

    state: done
      prompt: Compatible. Ready to enter values.
      goto: END

on_cancel in flow files
-----------------------
``on_cancel: END`` (or any other state name) is an optional shortcut so the
flow file itself handles the "cancel" pattern without needing a handler function.
If ``on_cancel`` is not set, cancel falls through to the normal handler.

Context / self.flow
-------------------
``self.flow`` is a plain dict cleared when a flow exits.  Handlers write their
computed values there.  Prompt strings may interpolate context values with
``{key}`` — resolved from ``self.flow`` at speak-time.
"""

import os
import random
import re

from mycroft.messagebus.message import Message
from mycroft.util.log import LOG


# ── Exceptions ────────────────────────────────────────────────────────────────

class FlowError(Exception):
    """Raised by a flow handler to signal bad input.

    The message is spoken to the user, then the current state is re-entered
    (the user is prompted again).
    """


# ── .flow file parser ─────────────────────────────────────────────────────────

def _parse_flow_file(path: str) -> dict:
    """Parse a .flow file into a dict of {state_name: state_dict}.

    State dict keys:
        prompts      list[str]  — spoken on entry (random pick); empty for error states
        error_lines  list[str]  — spoken on entry of error states (random pick)
        patterns     list[str]  — pattern strings for this state
        goto         str|None   — explicit next state ('END' or state name); None = sequential
        on_cancel    str|None   — state to go to when "cancel" is matched
        is_error     bool       — True for error states (not reachable sequentially)
        index        int        — position in file, used for sequential advancement
    """
    states = {}
    order = []
    entry_patterns = {}  # flow_name -> list[str]

    with open(path, 'r') as f:
        lines = f.readlines()

    current_state = None
    current_entry = None   # flow name if inside an entry: block
    current_section = None  # 'prompt' | 'error' | 'patterns'

    def _make_state(name, index):
        return {
            'prompts': [],
            'error_lines': [],
            'patterns': [],
            'goto': None,
            'on_cancel': None,
            'is_error': False,
            'index': index,
        }

    for raw_line in lines:
        line = raw_line.rstrip().expandtabs(4)
        stripped = line.strip()

        # Skip comments and blank lines between states
        if not stripped or stripped.startswith('#'):
            current_section = None
            continue

        indent = len(line) - len(line.lstrip())

        if indent == 0:
            # Top-level directive
            current_section = None
            current_entry = None
            if stripped.startswith('state:'):
                name = stripped[len('state:'):].strip()
                current_state = name
                states[name] = _make_state(name, len(order))
                order.append(name)
            elif stripped.startswith('entry:'):
                flow_name = stripped[len('entry:'):].strip()
                current_entry = flow_name
                current_state = None
                if flow_name not in entry_patterns:
                    entry_patterns[flow_name] = []
            continue

        # Inside an entry: block — collect patterns
        if current_entry is not None and current_state is None:
            if stripped == 'patterns:':
                current_section = 'entry_patterns'
            elif current_section == 'entry_patterns':
                entry_patterns[current_entry].append(stripped)
            continue

        if current_state is None:
            continue

        state = states[current_state]

        if indent >= 2:
            # If we're inside a multi-line section, check if this line is a new key
            # (a directive like 'patterns:' or 'goto:') rather than section content.
            is_key = (
                current_section is not None
                and any(stripped.startswith(k) for k in (
                    'prompt:', 'error:', 'patterns:', 'goto:', 'next:',
                    'on_cancel:', 'error_state:',
                ))
            )
            if is_key:
                current_section = None  # fall through to key handling below

            # Content under a section heading
            if current_section == 'prompt':
                state['prompts'].append(stripped)
            elif current_section == 'error':
                state['error_lines'].append(stripped)
            elif current_section == 'patterns':
                state['patterns'].append(stripped)
            elif current_section is None:
                # Inline directives (single-line keys under the state)
                if stripped.startswith('prompt:'):
                    val = stripped[len('prompt:'):].strip()
                    if val:
                        state['prompts'].append(val)
                    else:
                        current_section = 'prompt'
                elif stripped.startswith('error:'):
                    val = stripped[len('error:'):].strip()
                    state['is_error'] = True
                    if val:
                        state['error_lines'].append(val)
                    else:
                        current_section = 'error'
                elif stripped.startswith('patterns:'):
                    val = stripped[len('patterns:'):].strip()
                    if val:
                        state['patterns'].append(val)
                    else:
                        current_section = 'patterns'
                elif stripped.startswith('goto:'):
                    state['goto'] = stripped[len('goto:'):].strip()
                elif stripped.startswith('on_cancel:'):
                    state['on_cancel'] = stripped[len('on_cancel:'):].strip()
                elif stripped.startswith('next:'):
                    # 'next:' as alias for 'goto:' (fork used this syntax)
                    state['goto'] = stripped[len('next:'):].strip()
                elif stripped.startswith('error_state:'):
                    val = stripped[len('error_state:'):].strip().lower()
                    state['is_error'] = val in ('true', '1', 'yes')

    return states, order, entry_patterns


# ── Mixin ─────────────────────────────────────────────────────────────────────

class VoiceFlowMixin:
    """Mixin that adds declarative multi-turn voice flow support to a skill.

    Compose with RealtimePatternMixin and MycroftSkill::

        class MySkill(VoiceFlowMixin, RealtimePatternMixin, MycroftSkill):
            ...

    The mixin uses the same realtime:skill_patterns bus message API that
    RealtimePatternMixin's group methods use, so no changes to the realtime
    loop or __main__.py are needed.
    """

    # Per-instance state — initialised by _init_flow_state()
    flow: dict = None           # context dict for the active flow
    _flow_name: str = None      # active flow name
    _flow_state: str = None     # active state name
    _flow_defs: dict = None     # loaded flow definitions {flow_name: (states, order)}

    def _init_flow_state(self):
        """Initialise flow instance vars if not already set. Safe to call multiple times."""
        if self._flow_defs is None:
            self._flow_defs = {}
        if self.flow is None:
            self.flow = {}
        # _flow_name and _flow_state left alone — only reset by _end_flow / start_flow

    def bind(self, bus):
        """Wire all flow entry and state handlers immediately after bus connection.

        The skill loader sets skill_id then calls bind(bus) with a real bus
        before initialize() runs — so entry intent handlers are in place before
        any utterance can arrive.  MycroftSkill.__init__ also calls bind(None)
        during construction; the `if bus` guard skips flow loading in that case.
        """
        super().bind(bus)
        if bus:
            self._load_all_flows()

    def _flow_locale_dir(self) -> str:
        """Return the skill's locale/en-us directory."""
        skill_dir = os.path.dirname(os.path.abspath(
            __import__('sys').modules[self.__module__].__file__
        ))
        return os.path.join(skill_dir, 'locale', 'en-us')

    def _load_all_flows(self):
        """Auto-discover and load all .flow files in locale/en-us/.

        Called from bind() so handlers are wired before initialize() runs.
        Pattern registration is handled by the realtime pattern loader at
        startup — this only parses the files and wires bus event handlers.
        """
        self._init_flow_state()
        locale_dir = self._flow_locale_dir()
        if not os.path.isdir(locale_dir):
            return
        for fname in os.listdir(locale_dir):
            if fname.endswith('.flow'):
                self._load_flow(fname[:-5])

    def _load_flow(self, flow_name: str):
        """Parse one .flow file and register bus handlers for each state group.

        Pattern registration is done by the realtime loader at startup.
        This only needs to run once per skill start so the mixin knows the
        flow structure and can route _handle_flow_input correctly.
        """
        locale_dir = self._flow_locale_dir()
        path = os.path.join(locale_dir, f'{flow_name}.flow')
        if not os.path.exists(path):
            LOG.error(f"VoiceFlow: .flow file not found: {path}")
            return

        try:
            states, order, entry_patterns = _parse_flow_file(path)
        except Exception as e:
            LOG.error(f"VoiceFlow: failed to parse {path}: {e}")
            return

        self._flow_defs[flow_name] = (states, order)

        # Wire bus event handlers for each state group (patterns already registered
        # by the realtime loader at startup — we just need to listen for matches).
        for state_name in states:
            group = self._flow_group_name(flow_name, state_name)
            event_name = f'{self.skill_id}:__group__:{group}'
            self.add_event(event_name, self._handle_flow_input)

        # Wire entry intent handler — entry patterns are registered as a live
        # deterministic intent by load_flow_files in pattern_loader at startup.
        # The intent name mirrors how handle_utterance dispatches: skill_id:flow_name.
        if flow_name in entry_patterns and entry_patterns[flow_name]:
            entry_event = f'{self.skill_id}:{flow_name}'
            # capture flow_name in closure
            def _make_entry_handler(fn):
                def _handler(msg):
                    self.start_flow(fn)
                return _handler
            self.add_event(entry_event, _make_entry_handler(flow_name))
            LOG.info(f"VoiceFlow: wired entry handler for '{flow_name}' → {entry_event}")

        LOG.info(f"VoiceFlow: loaded '{flow_name}' ({len(states)} states)")

    def _flow_group_name(self, flow_name: str, state_name: str) -> str:
        return f'__flow__{flow_name}__{state_name}'

    def start_flow(self, flow_name: str, initial_state: str = None):
        """Start a voice flow, entering the first (or specified) state.

        Lazily loads the flow definition and wires bus handlers on first call.
        No explicit load_voice_flow() call needed in the skill's initialize().

        Args:
            flow_name:     Base name of the .flow file, e.g. 'matrix.define'.
            initial_state: State to start in; defaults to the first state in file order.
        """
        self._init_flow_state()

        # Lazy load: parse .flow file and wire handlers if not done yet
        if flow_name not in self._flow_defs:
            self._load_flow(flow_name)
        if flow_name not in self._flow_defs:
            return  # parse failed, already logged

        # Wire session_end cleanup once (idempotent via Mycroft's add_event)
        self.add_event('mycroft.realtime.session_end', self.handle_realtime_session_end)

        states, order = self._flow_defs[flow_name]
        # Skip error states for the default start
        non_error = [s for s in order if not states[s]['is_error']]
        if not non_error:
            LOG.error(f"VoiceFlow: flow '{flow_name}' has no non-error states")
            return

        start = initial_state or non_error[0]
        if start not in states:
            LOG.error(f"VoiceFlow: state '{start}' not found in flow '{flow_name}'")
            return

        self._flow_name = flow_name
        self.flow = {}
        self._enter_state(start)

    def _enter_state(self, state_name: str):
        """Activate a state: disable the previous group, enable the new one, speak prompt."""
        flow_name = self._flow_name
        states, order = self._flow_defs[flow_name]

        if state_name == 'END':
            self._end_flow()
            return

        if state_name not in states:
            LOG.error(f"VoiceFlow: unknown state '{state_name}' in flow '{flow_name}'")
            self._end_flow()
            return

        # Disable the previous state's group
        if self._flow_state and self._flow_state != state_name:
            prev_group = self._flow_group_name(flow_name, self._flow_state)
            self.bus.emit(Message('realtime:skill_patterns', {
                'action': 'disable',
                'skill_prefix': self.skill_id,
                'group': prev_group,
            }))

        self._flow_state = state_name
        state = states[state_name]

        # Enable this state's pattern group
        if state['patterns']:
            group = self._flow_group_name(flow_name, state_name)
            self.bus.emit(Message('realtime:skill_patterns', {
                'action': 'enable',
                'skill_prefix': self.skill_id,
                'group': group,
            }))

        # Speak the prompt (random pick if multiple lines)
        prompt_lines = state['prompts'] if not state['is_error'] else state['error_lines']
        if prompt_lines:
            prompt = random.choice(prompt_lines)
            prompt = self._interpolate_flow(prompt)
            self._speak_blocking(prompt)

        # If no patterns (terminal state), advance automatically
        if not state['patterns']:
            next_state = self._next_state_for(state_name)
            if next_state:
                self._enter_state(next_state)
            else:
                self._end_flow()

    def _handle_flow_input(self, message):
        """Dispatched when a flow state's pattern group matches."""
        if not self._flow_name or not self._flow_state:
            return

        flow_name = self._flow_name
        states, order = self._flow_defs[flow_name]
        state = states.get(self._flow_state)
        if state is None:
            return

        entities = message.data.get('entities', {})
        utterance = message.data.get('utterance', '')

        # on_cancel shortcut
        if state['on_cancel'] and self._is_cancel(utterance, entities):
            target = state['on_cancel']
            if target == 'END':
                self._end_flow()
            else:
                self._enter_state(target)
            return

        # Find and call the handler
        handler_name = f'handle_{self._flow_state.replace("-", "_")}'
        handler = getattr(self, handler_name, None)

        result = None
        if handler is not None:
            try:
                result = handler(entities)
            except FlowError as e:
                # Re-prompt the current state with the error message
                self._speak_blocking(str(e))
                self._enter_state(self._flow_state)
                return
            except Exception as e:
                import traceback
                self.log.error(
                    f"VoiceFlow: handler '{handler_name}' raised: {e}\n"
                    f"{traceback.format_exc()}"
                )
                return

        # Interpret result
        if result == 'END':
            self._end_flow()
        elif isinstance(result, str):
            # Explicit goto — could be a state name or a spoken override + state name
            # Format: plain state name  OR  "spoken text" (just speak, stay)
            # We treat any string that matches a state name as a goto
            if result in states:
                self._enter_state(result)
            else:
                # Treat as a spoken message then re-enter current state
                self._speak_blocking(self._interpolate_flow(result))
                self._enter_state(self._flow_state)
        elif result is None:
            # Sequential advancement
            next_state = self._next_state_for(self._flow_state)
            if next_state:
                self._enter_state(next_state)
            else:
                self._end_flow()

    def _next_state_for(self, state_name: str) -> str | None:
        """Return the next state after state_name, respecting goto and skipping error states."""
        flow_name = self._flow_name
        states, order = self._flow_defs[flow_name]
        state = states[state_name]

        # Explicit goto overrides sequential
        if state['goto']:
            target = state['goto']
            return None if target == 'END' else target

        # Sequential: next non-error state in file order
        idx = states[state_name]['index']
        for name in order[idx + 1:]:
            if not states[name]['is_error']:
                return name

        return None  # end of flow

    def _end_flow(self):
        """Disable the active state group and reset flow state."""
        if self._flow_name and self._flow_state:
            group = self._flow_group_name(self._flow_name, self._flow_state)
            self.bus.emit(Message('realtime:skill_patterns', {
                'action': 'disable',
                'skill_prefix': self.skill_id,
                'group': group,
            }))
        self._flow_name = None
        self._flow_state = None
        self.flow = {}
        LOG.debug(f"VoiceFlow: flow ended, state reset")

    def _interpolate_flow(self, text: str) -> str:
        """Replace {key} placeholders in text with values from self.flow."""
        def _replace(m):
            key = m.group(1)
            val = self.flow.get(key)
            return str(val) if val is not None else m.group(0)
        return re.sub(r'\{(\w+)\}', _replace, text)

    @staticmethod
    def _is_cancel(utterance: str, entities: dict) -> bool:
        return 'cancel' in utterance.lower()

    def handle_realtime_session_end(self, message):
        """Pause the active flow on session end — disables the state group and clears
        the active state/flow name, but preserves self.flow context so accumulated
        values survive across sessions until the flow is restarted via start_flow()."""
        if self._flow_state is not None:
            LOG.info(f"VoiceFlow: realtime session ended — pausing flow '{self._flow_name}' at '{self._flow_state}'")
            if self._flow_name and self._flow_state:
                group = self._flow_group_name(self._flow_name, self._flow_state)
                self.bus.emit(Message('realtime:skill_patterns', {
                    'action': 'disable',
                    'skill_prefix': self.skill_id,
                    'group': group,
                }))
            self._flow_name = None
            self._flow_state = None
            # self.flow intentionally preserved — values persist until start_flow() resets them
