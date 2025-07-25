#!/usr/bin/env python3
# encoding: utf-8

"""Snippet representation after parsing."""

import re

import vim
import textwrap

from UltiSnips import vim_helper
from UltiSnips.error import PebkacError
from UltiSnips.indent_util import IndentUtil
from UltiSnips.position import Position
from UltiSnips.text import escape
from UltiSnips.text_objects import SnippetInstance
from UltiSnips.text_objects.python_code import SnippetUtilForAction, cached_compile


__WHITESPACE_SPLIT = re.compile(r"\s")


class _SnippetUtilCursor:
    def __init__(self, cursor):
        self._cursor = [cursor[0] - 1, cursor[1]]
        self._set = False

    def preserve(self):
        self._set = True
        self._cursor = [vim_helper.buf.cursor[0], vim_helper.buf.cursor[1]]

    def is_set(self):
        return self._set

    def set(self, line, column):
        self.__setitem__(0, line)
        self.__setitem__(1, column)

    def to_vim_cursor(self):
        return (self._cursor[0] + 1, self._cursor[1])

    def __getitem__(self, index):
        return self._cursor[index]

    def __setitem__(self, index, value):
        self._set = True
        self._cursor[index] = value

    def __len__(self):
        return 2

    def __str__(self):
        return str((self._cursor[0], self._cursor[1]))


def split_at_whitespace(string):
    """Like string.split(), but keeps empty words as empty words."""
    return re.split(__WHITESPACE_SPLIT, string)


def _words_for_line(trigger, before, num_words=None):
    """Gets the final 'num_words' words from 'before'.

    If num_words is None, then use the number of words in 'trigger'.

    """
    if num_words is None:
        num_words = len(split_at_whitespace(trigger))

    word_list = split_at_whitespace(before)
    if len(word_list) <= num_words:
        return before.strip()
    else:
        before_words = before
        for i in range(-1, -(num_words + 1), -1):
            left = before_words.rfind(word_list[i])
            before_words = before_words[:left]
        return before[len(before_words) :].strip()


class SnippetDefinition:

    """Represents a snippet as parsed from a file."""

    _INDENT = re.compile(r"^[ \t]*")
    _TABS = re.compile(r"^\t*")

    def __init__(
        self,
        priority,
        trigger,
        value,
        description,
        options,
        globals,
        location,
        context,
        actions,
    ):
        self._priority = int(priority)
        self._trigger = trigger
        self._value = value
        self._description = description
        self._opts = options
        self._matched = ""
        self._last_re = None
        self._globals = globals
        self._compiled_globals = None
        self._location = location

        # Make sure that we actually match our trigger in case we are
        # immediately expanded. At this point we don't take into
        # account a any context code
        self._context_code = None
        self.matches(self._trigger)

        self._context_code = context
        if context:
            self._compiled_context_code = cached_compile(
                "snip.context = " + context, "<context-code>", "exec"
            )
        self._context = None
        self._actions = actions or {}
        self._compiled_actions = {
            action: cached_compile(source, "<action-code>", "exec")
            for action, source in self._actions.items()
        }

    def __repr__(self):
        return "_SnippetDefinition(%r,%s,%s,%s)" % (
            self._priority,
            self._trigger,
            self._description,
            self._opts,
        )

    def _re_match(self, trigger):
        """Test if the current regex trigger matches `trigger`.

        If so, set _last_re and _matched.

        """
        for match in re.finditer(self._trigger, trigger):
            if match.end() != len(trigger):
                continue
            else:
                self._matched = trigger[match.start() : match.end()]

            self._last_re = match
            return match
        return False

    def _context_match(self, visual_content, before):
        # skip on empty buffer
        if len(vim.current.buffer) == 1 and vim.current.buffer[0] == "":
            return

        locals = {
            "context": None,
            "visual_mode": "",
            "visual_text": "",
            "last_placeholder": None,
            "before": before,
        }

        if visual_content:
            locals["visual_mode"] = visual_content.mode
            locals["visual_text"] = visual_content.text
            locals["last_placeholder"] = visual_content.placeholder

        return self._eval_code(
            "snip.context = " + self._context_code, locals, self._compiled_context_code
        ).context

    def _eval_code(self, code, additional_locals={}, compiled_code=None):
        current = vim.current

        locals = {
            "window": current.window,
            "buffer": current.buffer,
            "line": current.window.cursor[0] - 1,
            "column": current.window.cursor[1] - 1,
            "cursor": _SnippetUtilCursor(current.window.cursor),
        }

        locals.update(additional_locals)

        snip = SnippetUtilForAction(locals)

        try:
            if self._compiled_globals is None:
                self._precompile_globals()
            glob = {"snip": snip, "match": self._last_re}
            exec(self._compiled_globals, glob)
            exec(compiled_code or code, glob)
        except Exception as e:
            code = "\n".join(
                [
                    "import re, os, vim, string, random",
                    "\n".join(self._globals.get("!p", [])).replace("\r\n", "\n"),
                    code,
                ]
            )
            self._make_debug_exception(e, code)
            raise

        return snip

    def _execute_action(
        self, action, context, additional_locals={}, compiled_action=None
    ):
        mark_to_use = "`"
        with vim_helper.save_mark(mark_to_use):
            vim_helper.set_mark_from_pos(mark_to_use, vim_helper.get_cursor_pos())

            cursor_line_before = vim_helper.buf.line_till_cursor

            locals = {"context": context}

            locals.update(additional_locals)

            snip = self._eval_code(action, locals, compiled_action)

            if snip.cursor.is_set():
                vim_helper.buf.cursor = Position(
                    snip.cursor._cursor[0], snip.cursor._cursor[1]
                )
            else:
                new_mark_pos = vim_helper.get_mark_pos(mark_to_use)

                cursor_invalid = False

                if vim_helper._is_pos_zero(new_mark_pos):
                    cursor_invalid = True
                else:
                    vim_helper.set_cursor_from_pos(new_mark_pos)
                    if cursor_line_before != vim_helper.buf.line_till_cursor:
                        cursor_invalid = True

                if cursor_invalid:
                    raise PebkacError(
                        "line under the cursor was modified, but "
                        + '"snip.cursor" variable is not set; either set set '
                        + '"snip.cursor" to new cursor position, or do not '
                        + "modify cursor line"
                    )

        return snip

    def _make_debug_exception(self, e, code=""):
        e.snippet_info = textwrap.dedent(
            """
            Defined in: {}
            Trigger: {}
            Description: {}
            Context: {}
            Pre-expand: {}
            Post-expand: {}
        """
        ).format(
            self._location,
            self._trigger,
            self._description,
            self._context_code if self._context_code else "<none>",
            self._actions["pre_expand"] if "pre_expand" in self._actions else "<none>",
            self._actions["post_expand"]
            if "post_expand" in self._actions
            else "<none>",
            code,
        )

        e.snippet_code = code

    def _precompile_globals(self):
        self._compiled_globals = cached_compile(
            "\n".join(
                [
                    "import re, os, vim, string, random",
                    "\n".join(self._globals.get("!p", [])).replace("\r\n", "\n"),
                ]
            ),
            "<global-snippets>",
            "exec",
        )

    def has_option(self, opt):
        """Check if the named option is set."""
        return opt in self._opts

    @property
    def description(self):
        """Descriptive text for this snippet."""
        return ("(%s) %s" % (self._trigger, self._description)).strip()

    @property
    def priority(self):
        """The snippets priority, which defines which snippet will be preferred
        over others with the same trigger."""
        return self._priority

    @property
    def trigger(self):
        """The trigger text for the snippet."""
        return self._trigger

    @property
    def matched(self):
        """The last text that matched this snippet in match() or
        could_match()."""
        return self._matched

    @property
    def location(self):
        """Where this snippet was defined."""
        return self._location

    @property
    def context(self):
        """The matched context."""
        return self._context

    def matches(self, before, visual_content=None):
        """Returns True if this snippet matches 'before'."""
        # If user supplies both "w" and "i", it should perhaps be an
        # error, but if permitted it seems that "w" should take precedence
        # (since matching at word boundary and within a word == matching at word
        # boundary).
        self._matched = ""

        words = _words_for_line(self._trigger, before)

        if "r" in self._opts:
            try:
                match = self._re_match(before)
            except Exception as e:
                self._make_debug_exception(e)
                raise

        elif "w" in self._opts:
            words_len = len(self._trigger)
            words_prefix = words[:-words_len]
            words_suffix = words[-words_len:]
            match = words_suffix == self._trigger
            if match and words_prefix:
                # Require a word boundary between prefix and suffix.
                boundary_chars = escape(words_prefix[-1:] + words_suffix[:1], r"\"")
                match = vim_helper.eval('"%s" =~# "\\\\v.<."' % boundary_chars) != "0"
        elif "i" in self._opts:
            match = words.endswith(self._trigger)
        else:
            match = words == self._trigger

        # By default, we match the whole trigger
        if match and not self._matched:
            self._matched = self._trigger

        # Ensure the match was on a word boundry if needed
        if "b" in self._opts and match:
            text_before = before.rstrip()[: -len(self._matched)]
            if text_before.strip(" \t") != "":
                self._matched = ""
                return False

        self._context = None
        if match and self._context_code:
            self._context = self._context_match(visual_content, before)
            if not self.context:
                match = False

        return match

    def could_match(self, before):
        """Return True if this snippet could match the (partial) 'before'."""
        self._matched = ""

        # List all on whitespace.
        if before and before[-1] in (" ", "\t"):
            before = ""
        if before and before.rstrip() is not before:
            return False

        words = _words_for_line(self._trigger, before)

        if "r" in self._opts:
            # Test for full match only
            match = self._re_match(before)
        elif "w" in self._opts:
            # Trim non-empty prefix up to word boundary, if present.
            qwords = escape(words, r"\"")
            words_suffix = vim_helper.eval(
                'substitute("%s", "\\\\v^.+<(.+)", "\\\\1", "")' % qwords
            )
            match = self._trigger.startswith(words_suffix)
            self._matched = words_suffix

            # TODO: list_snippets() function cannot handle partial-trigger
            # matches yet, so for now fail if we trimmed the prefix.
            if words_suffix != words:
                match = False
        elif "i" in self._opts:
            # TODO: It is hard to define when a inword snippet could match,
            # therefore we check only for full-word trigger.
            match = self._trigger.startswith(words)
        else:
            match = self._trigger.startswith(words)

        # By default, we match the words from the trigger
        if match and not self._matched:
            self._matched = words

        # Ensure the match was on a word boundry if needed
        if "b" in self._opts and match:
            text_before = before.rstrip()[: -len(self._matched)]
            if text_before.strip(" \t") != "":
                self._matched = ""
                return False

        return match

    def instantiate(self, snippet_instance, initial_text, indent):
        """Parses the content of this snippet and brings the corresponding text
        objects alive inside of Vim."""
        raise NotImplementedError()

    def do_pre_expand(self, visual_content, snippets_stack):
        if "pre_expand" in self._actions:
            locals = {"buffer": vim_helper.buf, "visual_content": visual_content}

            snip = self._execute_action(
                self._actions["pre_expand"],
                self._context,
                locals,
                self._compiled_actions["pre_expand"],
            )
            self._context = snip.context
            return snip.cursor.is_set()
        else:
            return False

    def do_post_expand(self, start, end, snippets_stack):
        if "post_expand" in self._actions:
            locals = {
                "snippet_start": start,
                "snippet_end": end,
                "buffer": vim_helper.buf,
            }

            snip = self._execute_action(
                self._actions["post_expand"],
                snippets_stack[-1].context,
                locals,
                self._compiled_actions["post_expand"],
            )

            snippets_stack[-1].context = snip.context

            return snip.cursor.is_set()
        else:
            return False

    def do_post_jump(
        self, tabstop_number, jump_direction, snippets_stack, current_snippet
    ):
        if "post_jump" in self._actions:
            start = current_snippet.start
            end = current_snippet.end

            locals = {
                "tabstop": tabstop_number,
                "jump_direction": jump_direction,
                "tabstops": current_snippet.get_tabstops(),
                "snippet_start": start,
                "snippet_end": end,
                "buffer": vim_helper.buf,
            }

            snip = self._execute_action(
                self._actions["post_jump"],
                current_snippet.context,
                locals,
                self._compiled_actions["post_jump"],
            )

            current_snippet.context = snip.context

            return snip.cursor.is_set()
        else:
            return False

    def launch(self, text_before, visual_content, parent, start, end):
        """Launch this snippet, overwriting the text 'start' to 'end' and
        keeping the 'text_before' on the launch line.

        'Parent' is the parent snippet instance if any.

        """
        indent = self._INDENT.match(text_before).group(0)
        lines = (self._value + "\n").splitlines()
        ind_util = IndentUtil()

        # Replace leading tabs in the snippet definition via proper indenting
        initial_text = []
        for line_num, line in enumerate(lines):
            if "t" in self._opts:
                tabs = 0
            else:
                tabs = len(self._TABS.match(line).group(0))
            line_ind = ind_util.ntabs_to_proper_indent(tabs)
            if line_num != 0:
                line_ind = indent + line_ind

            result_line = line_ind + line[tabs:]
            if "m" in self._opts:
                result_line = result_line.rstrip()
            initial_text.append(result_line)
        initial_text = "\n".join(initial_text)

        if self._compiled_globals is None:
            self._precompile_globals()
        snippet_instance = SnippetInstance(
            self,
            parent,
            initial_text,
            start,
            end,
            visual_content,
            last_re=self._last_re,
            globals=self._globals,
            context=self._context,
            _compiled_globals=self._compiled_globals,
        )
        self.instantiate(snippet_instance, initial_text, indent)
        snippet_instance.replace_initial_text(vim_helper.buf)
        snippet_instance.update_textobjects(vim_helper.buf)
        return snippet_instance
