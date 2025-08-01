"""
String transformers that can split and merge strings.
"""

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Literal, Optional, TypeVar, Union

from mypy_extensions import trait

from black.comments import contains_pragma_comment
from black.lines import Line, append_leaves
from black.mode import Feature, Mode
from black.nodes import (
    CLOSING_BRACKETS,
    OPENING_BRACKETS,
    STANDALONE_COMMENT,
    is_empty_lpar,
    is_empty_par,
    is_empty_rpar,
    is_part_of_annotation,
    parent_type,
    replace_child,
    syms,
)
from black.rusty import Err, Ok, Result
from black.strings import (
    assert_is_leaf_string,
    count_chars_in_width,
    get_string_prefix,
    has_triple_quotes,
    normalize_string_quotes,
    str_width,
)
from blib2to3.pgen2 import token
from blib2to3.pytree import Leaf, Node


class CannotTransform(Exception):
    """Base class for errors raised by Transformers."""


# types
T = TypeVar("T")
LN = Union[Leaf, Node]
Transformer = Callable[[Line, Collection[Feature], Mode], Iterator[Line]]
Index = int
NodeType = int
ParserState = int
StringID = int
TResult = Result[T, CannotTransform]  # (T)ransform Result
TMatchResult = TResult[list[Index]]

SPLIT_SAFE_CHARS = frozenset(["\u3001", "\u3002", "\uff0c"])  # East Asian stops


def TErr(err_msg: str) -> Err[CannotTransform]:
    """(T)ransform Err

    Convenience function used when working with the TResult type.
    """
    cant_transform = CannotTransform(err_msg)
    return Err(cant_transform)


def hug_power_op(
    line: Line, features: Collection[Feature], mode: Mode
) -> Iterator[Line]:
    """A transformer which normalizes spacing around power operators."""

    # Performance optimization to avoid unnecessary Leaf clones and other ops.
    for leaf in line.leaves:
        if leaf.type == token.DOUBLESTAR:
            break
    else:
        raise CannotTransform("No doublestar token was found in the line.")

    def is_simple_lookup(index: int, kind: Literal[1, -1]) -> bool:
        # Brackets and parentheses indicate calls, subscripts, etc. ...
        # basically stuff that doesn't count as "simple". Only a NAME lookup
        # or dotted lookup (eg. NAME.NAME) is OK.
        if kind == -1:
            return handle_is_simple_look_up_prev(line, index, {token.RPAR, token.RSQB})
        else:
            return handle_is_simple_lookup_forward(
                line, index, {token.LPAR, token.LSQB}
            )

    def is_simple_operand(index: int, kind: Literal[1, -1]) -> bool:
        # An operand is considered "simple" if's a NAME, a numeric CONSTANT, a simple
        # lookup (see above), with or without a preceding unary operator.
        start = line.leaves[index]
        if start.type in {token.NAME, token.NUMBER}:
            return is_simple_lookup(index, kind)

        if start.type in {token.PLUS, token.MINUS, token.TILDE}:
            if line.leaves[index + 1].type in {token.NAME, token.NUMBER}:
                # kind is always one as bases with a preceding unary op will be checked
                # for simplicity starting from the next token (so it'll hit the check
                # above).
                return is_simple_lookup(index + 1, kind=1)

        return False

    new_line = line.clone()
    should_hug = False
    for idx, leaf in enumerate(line.leaves):
        new_leaf = leaf.clone()
        if should_hug:
            new_leaf.prefix = ""
            should_hug = False

        should_hug = (
            (0 < idx < len(line.leaves) - 1)
            and leaf.type == token.DOUBLESTAR
            and is_simple_operand(idx - 1, kind=-1)
            and line.leaves[idx - 1].value != "lambda"
            and is_simple_operand(idx + 1, kind=1)
        )
        if should_hug:
            new_leaf.prefix = ""

        # We have to be careful to make a new line properly:
        # - bracket related metadata must be maintained (handled by Line.append)
        # - comments need to copied over, updating the leaf IDs they're attached to
        new_line.append(new_leaf, preformatted=True)
        for comment_leaf in line.comments_after(leaf):
            new_line.append(comment_leaf, preformatted=True)

    yield new_line


def handle_is_simple_look_up_prev(line: Line, index: int, disallowed: set[int]) -> bool:
    """
    Handling the determination of is_simple_lookup for the lines prior to the doublestar
    token. This is required because of the need to isolate the chained expression
    to determine the bracket or parenthesis belong to the single expression.
    """
    contains_disallowed = False
    chain = []

    while 0 <= index < len(line.leaves):
        current = line.leaves[index]
        chain.append(current)
        if not contains_disallowed and current.type in disallowed:
            contains_disallowed = True
        if not is_expression_chained(chain):
            return not contains_disallowed

        index -= 1

    return True


def handle_is_simple_lookup_forward(
    line: Line, index: int, disallowed: set[int]
) -> bool:
    """
    Handling decision is_simple_lookup for the lines behind the doublestar token.
    This function is simplified to keep consistent with the prior logic and the forward
    case are more straightforward and do not need to care about chained expressions.
    """
    while 0 <= index < len(line.leaves):
        current = line.leaves[index]
        if current.type in disallowed:
            return False
        if current.type not in {token.NAME, token.DOT} or (
            current.type == token.NAME and current.value == "for"
        ):
            # If the current token isn't disallowed, we'll assume this is simple as
            # only the disallowed tokens are semantically attached to this lookup
            # expression we're checking. Also, stop early if we hit the 'for' bit
            # of a comprehension.
            return True

        index += 1

    return True


def is_expression_chained(chained_leaves: list[Leaf]) -> bool:
    """
    Function to determine if the variable is a chained call.
    (e.g., foo.lookup, foo().lookup, (foo.lookup())) will be recognized as chained call)
    """
    if len(chained_leaves) < 2:
        return True

    current_leaf = chained_leaves[-1]
    past_leaf = chained_leaves[-2]

    if past_leaf.type == token.NAME:
        return current_leaf.type in {token.DOT}
    elif past_leaf.type in {token.RPAR, token.RSQB}:
        return current_leaf.type in {token.RSQB, token.RPAR}
    elif past_leaf.type in {token.LPAR, token.LSQB}:
        return current_leaf.type in {token.NAME, token.LPAR, token.LSQB}
    else:
        return False


class StringTransformer(ABC):
    """
    An implementation of the Transformer protocol that relies on its
    subclasses overriding the template methods `do_match(...)` and
    `do_transform(...)`.

    This Transformer works exclusively on strings (for example, by merging
    or splitting them).

    The following sections can be found among the docstrings of each concrete
    StringTransformer subclass.

    Requirements:
        Which requirements must be met of the given Line for this
        StringTransformer to be applied?

    Transformations:
        If the given Line meets all of the above requirements, which string
        transformations can you expect to be applied to it by this
        StringTransformer?

    Collaborations:
        What contractual agreements does this StringTransformer have with other
        StringTransfomers? Such collaborations should be eliminated/minimized
        as much as possible.
    """

    __name__: Final = "StringTransformer"

    # Ideally this would be a dataclass, but unfortunately mypyc breaks when used with
    # `abc.ABC`.
    def __init__(self, line_length: int, normalize_strings: bool) -> None:
        self.line_length = line_length
        self.normalize_strings = normalize_strings

    @abstractmethod
    def do_match(self, line: Line) -> TMatchResult:
        """
        Returns:
            * Ok(string_indices) such that for each index, `line.leaves[index]`
              is our target string if a match was able to be made. For
              transformers that don't result in more lines (e.g. StringMerger,
              StringParenStripper), multiple matches and transforms are done at
              once to reduce the complexity.
              OR
            * Err(CannotTransform), if no match could be made.
        """

    @abstractmethod
    def do_transform(
        self, line: Line, string_indices: list[int]
    ) -> Iterator[TResult[Line]]:
        """
        Yields:
            * Ok(new_line) where new_line is the new transformed line.
              OR
            * Err(CannotTransform) if the transformation failed for some reason. The
              `do_match(...)` template method should usually be used to reject
              the form of the given Line, but in some cases it is difficult to
              know whether or not a Line meets the StringTransformer's
              requirements until the transformation is already midway.

        Side Effects:
            This method should NOT mutate @line directly, but it MAY mutate the
            Line's underlying Node structure. (WARNING: If the underlying Node
            structure IS altered, then this method should NOT be allowed to
            yield an CannotTransform after that point.)
        """

    def __call__(
        self, line: Line, _features: Collection[Feature], _mode: Mode
    ) -> Iterator[Line]:
        """
        StringTransformer instances have a call signature that mirrors that of
        the Transformer type.

        Raises:
            CannotTransform(...) if the concrete StringTransformer class is unable
            to transform @line.
        """
        # Optimization to avoid calling `self.do_match(...)` when the line does
        # not contain any string.
        if not any(leaf.type == token.STRING for leaf in line.leaves):
            raise CannotTransform("There are no strings in this line.")

        match_result = self.do_match(line)

        if isinstance(match_result, Err):
            cant_transform = match_result.err()
            raise CannotTransform(
                f"The string transformer {self.__class__.__name__} does not recognize"
                " this line as one that it can transform."
            ) from cant_transform

        string_indices = match_result.ok()

        for line_result in self.do_transform(line, string_indices):
            if isinstance(line_result, Err):
                cant_transform = line_result.err()
                raise CannotTransform(
                    "StringTransformer failed while attempting to transform string."
                ) from cant_transform
            line = line_result.ok()
            yield line


@dataclass
class CustomSplit:
    """A custom (i.e. manual) string split.

    A single CustomSplit instance represents a single substring.

    Examples:
        Consider the following string:
        ```
        "Hi there friend."
        " This is a custom"
        f" string {split}."
        ```

        This string will correspond to the following three CustomSplit instances:
        ```
        CustomSplit(False, 16)
        CustomSplit(False, 17)
        CustomSplit(True, 16)
        ```
    """

    has_prefix: bool
    break_idx: int


@trait
class CustomSplitMapMixin:
    """
    This mixin class is used to map merged strings to a sequence of
    CustomSplits, which will then be used to re-split the strings iff none of
    the resultant substrings go over the configured max line length.
    """

    _Key: ClassVar = tuple[StringID, str]
    _CUSTOM_SPLIT_MAP: ClassVar[dict[_Key, tuple[CustomSplit, ...]]] = defaultdict(
        tuple
    )

    @staticmethod
    def _get_key(string: str) -> "CustomSplitMapMixin._Key":
        """
        Returns:
            A unique identifier that is used internally to map @string to a
            group of custom splits.
        """
        return (id(string), string)

    def add_custom_splits(
        self, string: str, custom_splits: Iterable[CustomSplit]
    ) -> None:
        """Custom Split Map Setter Method

        Side Effects:
            Adds a mapping from @string to the custom splits @custom_splits.
        """
        key = self._get_key(string)
        self._CUSTOM_SPLIT_MAP[key] = tuple(custom_splits)

    def pop_custom_splits(self, string: str) -> list[CustomSplit]:
        """Custom Split Map Getter Method

        Returns:
            * A list of the custom splits that are mapped to @string, if any
              exist.
              OR
            * [], otherwise.

        Side Effects:
            Deletes the mapping between @string and its associated custom
            splits (which are returned to the caller).
        """
        key = self._get_key(string)

        custom_splits = self._CUSTOM_SPLIT_MAP[key]
        del self._CUSTOM_SPLIT_MAP[key]

        return list(custom_splits)

    def has_custom_splits(self, string: str) -> bool:
        """
        Returns:
            True iff @string is associated with a set of custom splits.
        """
        key = self._get_key(string)
        return key in self._CUSTOM_SPLIT_MAP


class StringMerger(StringTransformer, CustomSplitMapMixin):
    """StringTransformer that merges strings together.

    Requirements:
        (A) The line contains adjacent strings such that ALL of the validation checks
        listed in StringMerger._validate_msg(...)'s docstring pass.
        OR
        (B) The line contains a string which uses line continuation backslashes.

    Transformations:
        Depending on which of the two requirements above where met, either:

        (A) The string group associated with the target string is merged.
        OR
        (B) All line-continuation backslashes are removed from the target string.

    Collaborations:
        StringMerger provides custom split information to StringSplitter.
    """

    def do_match(self, line: Line) -> TMatchResult:
        LL = line.leaves

        is_valid_index = is_valid_index_factory(LL)

        string_indices = []
        idx = 0
        while is_valid_index(idx):
            leaf = LL[idx]
            if (
                leaf.type == token.STRING
                and is_valid_index(idx + 1)
                and LL[idx + 1].type == token.STRING
            ):
                # Let's check if the string group contains an inline comment
                # If we have a comment inline, we don't merge the strings
                contains_comment = False
                i = idx
                while is_valid_index(i):
                    if LL[i].type != token.STRING:
                        break
                    if line.comments_after(LL[i]):
                        contains_comment = True
                        break
                    i += 1

                if not contains_comment and not is_part_of_annotation(leaf):
                    string_indices.append(idx)

                # Advance to the next non-STRING leaf.
                idx += 2
                while is_valid_index(idx) and LL[idx].type == token.STRING:
                    idx += 1

            elif leaf.type == token.STRING and "\\\n" in leaf.value:
                string_indices.append(idx)
                # Advance to the next non-STRING leaf.
                idx += 1
                while is_valid_index(idx) and LL[idx].type == token.STRING:
                    idx += 1

            else:
                idx += 1

        if string_indices:
            return Ok(string_indices)
        else:
            return TErr("This line has no strings that need merging.")

    def do_transform(
        self, line: Line, string_indices: list[int]
    ) -> Iterator[TResult[Line]]:
        new_line = line

        rblc_result = self._remove_backslash_line_continuation_chars(
            new_line, string_indices
        )
        if isinstance(rblc_result, Ok):
            new_line = rblc_result.ok()

        msg_result = self._merge_string_group(new_line, string_indices)
        if isinstance(msg_result, Ok):
            new_line = msg_result.ok()

        if isinstance(rblc_result, Err) and isinstance(msg_result, Err):
            msg_cant_transform = msg_result.err()
            rblc_cant_transform = rblc_result.err()
            cant_transform = CannotTransform(
                "StringMerger failed to merge any strings in this line."
            )

            # Chain the errors together using `__cause__`.
            msg_cant_transform.__cause__ = rblc_cant_transform
            cant_transform.__cause__ = msg_cant_transform

            yield Err(cant_transform)
        else:
            yield Ok(new_line)

    @staticmethod
    def _remove_backslash_line_continuation_chars(
        line: Line, string_indices: list[int]
    ) -> TResult[Line]:
        """
        Merge strings that were split across multiple lines using
        line-continuation backslashes.

        Returns:
            Ok(new_line), if @line contains backslash line-continuation
            characters.
                OR
            Err(CannotTransform), otherwise.
        """
        LL = line.leaves

        indices_to_transform = []
        for string_idx in string_indices:
            string_leaf = LL[string_idx]
            if (
                string_leaf.type == token.STRING
                and "\\\n" in string_leaf.value
                and not has_triple_quotes(string_leaf.value)
            ):
                indices_to_transform.append(string_idx)

        if not indices_to_transform:
            return TErr(
                "Found no string leaves that contain backslash line continuation"
                " characters."
            )

        new_line = line.clone()
        new_line.comments = line.comments.copy()
        append_leaves(new_line, line, LL)

        for string_idx in indices_to_transform:
            new_string_leaf = new_line.leaves[string_idx]
            new_string_leaf.value = new_string_leaf.value.replace("\\\n", "")

        return Ok(new_line)

    def _merge_string_group(
        self, line: Line, string_indices: list[int]
    ) -> TResult[Line]:
        """
        Merges string groups (i.e. set of adjacent strings).

        Each index from `string_indices` designates one string group's first
        leaf in `line.leaves`.

        Returns:
            Ok(new_line), if ALL of the validation checks found in
            _validate_msg(...) pass.
                OR
            Err(CannotTransform), otherwise.
        """
        LL = line.leaves

        is_valid_index = is_valid_index_factory(LL)

        # A dict of {string_idx: tuple[num_of_strings, string_leaf]}.
        merged_string_idx_dict: dict[int, tuple[int, Leaf]] = {}
        for string_idx in string_indices:
            vresult = self._validate_msg(line, string_idx)
            if isinstance(vresult, Err):
                continue
            merged_string_idx_dict[string_idx] = self._merge_one_string_group(
                LL, string_idx, is_valid_index
            )

        if not merged_string_idx_dict:
            return TErr("No string group is merged")

        # Build the final line ('new_line') that this method will later return.
        new_line = line.clone()
        previous_merged_string_idx = -1
        previous_merged_num_of_strings = -1
        for i, leaf in enumerate(LL):
            if i in merged_string_idx_dict:
                previous_merged_string_idx = i
                previous_merged_num_of_strings, string_leaf = merged_string_idx_dict[i]
                new_line.append(string_leaf)

            if (
                previous_merged_string_idx
                <= i
                < previous_merged_string_idx + previous_merged_num_of_strings
            ):
                for comment_leaf in line.comments_after(leaf):
                    new_line.append(comment_leaf, preformatted=True)
                continue

            append_leaves(new_line, line, [leaf])

        return Ok(new_line)

    def _merge_one_string_group(
        self, LL: list[Leaf], string_idx: int, is_valid_index: Callable[[int], bool]
    ) -> tuple[int, Leaf]:
        """
        Merges one string group where the first string in the group is
        `LL[string_idx]`.

        Returns:
            A tuple of `(num_of_strings, leaf)` where `num_of_strings` is the
            number of strings merged and `leaf` is the newly merged string
            to be replaced in the new line.
        """
        # If the string group is wrapped inside an Atom node, we must make sure
        # to later replace that Atom with our new (merged) string leaf.
        atom_node = LL[string_idx].parent

        # We will place BREAK_MARK in between every two substrings that we
        # merge. We will then later go through our final result and use the
        # various instances of BREAK_MARK we find to add the right values to
        # the custom split map.
        BREAK_MARK = "@@@@@ BLACK BREAKPOINT MARKER @@@@@"

        QUOTE = LL[string_idx].value[-1]

        def make_naked(string: str, string_prefix: str) -> str:
            """Strip @string (i.e. make it a "naked" string)

            Pre-conditions:
                * assert_is_leaf_string(@string)

            Returns:
                A string that is identical to @string except that
                @string_prefix has been stripped, the surrounding QUOTE
                characters have been removed, and any remaining QUOTE
                characters have been escaped.
            """
            assert_is_leaf_string(string)
            if "f" in string_prefix:
                f_expressions = [
                    string[span[0] + 1 : span[1] - 1]  # +-1 to get rid of curly braces
                    for span in iter_fexpr_spans(string)
                ]
                debug_expressions_contain_visible_quotes = any(
                    re.search(r".*[\'\"].*(?<![!:=])={1}(?!=)(?![^\s:])", expression)
                    for expression in f_expressions
                )
                if not debug_expressions_contain_visible_quotes:
                    # We don't want to toggle visible quotes in debug f-strings, as
                    # that would modify the AST
                    string = _toggle_fexpr_quotes(string, QUOTE)
                    # After quotes toggling, quotes in expressions won't be escaped
                    # because quotes can't be reused in f-strings. So we can simply
                    # let the escaping logic below run without knowing f-string
                    # expressions.

            RE_EVEN_BACKSLASHES = r"(?:(?<!\\)(?:\\\\)*)"
            naked_string = string[len(string_prefix) + 1 : -1]
            naked_string = re.sub(
                "(" + RE_EVEN_BACKSLASHES + ")" + QUOTE, r"\1\\" + QUOTE, naked_string
            )
            return naked_string

        # Holds the CustomSplit objects that will later be added to the custom
        # split map.
        custom_splits = []

        # Temporary storage for the 'has_prefix' part of the CustomSplit objects.
        prefix_tracker = []

        # Sets the 'prefix' variable. This is the prefix that the final merged
        # string will have.
        next_str_idx = string_idx
        prefix = ""
        while (
            not prefix
            and is_valid_index(next_str_idx)
            and LL[next_str_idx].type == token.STRING
        ):
            prefix = get_string_prefix(LL[next_str_idx].value).lower()
            next_str_idx += 1

        # The next loop merges the string group. The final string will be
        # contained in 'S'.
        #
        # The following convenience variables are used:
        #
        #   S: string
        #   NS: naked string
        #   SS: next string
        #   NSS: naked next string
        S = ""
        NS = ""
        num_of_strings = 0
        next_str_idx = string_idx
        while is_valid_index(next_str_idx) and LL[next_str_idx].type == token.STRING:
            num_of_strings += 1

            SS = LL[next_str_idx].value
            next_prefix = get_string_prefix(SS).lower()

            # If this is an f-string group but this substring is not prefixed
            # with 'f'...
            if "f" in prefix and "f" not in next_prefix:
                # Then we must escape any braces contained in this substring.
                SS = re.sub(r"(\{|\})", r"\1\1", SS)

            NSS = make_naked(SS, next_prefix)

            has_prefix = bool(next_prefix)
            prefix_tracker.append(has_prefix)

            S = prefix + QUOTE + NS + NSS + BREAK_MARK + QUOTE
            NS = make_naked(S, prefix)

            next_str_idx += 1

        # Take a note on the index of the non-STRING leaf.
        non_string_idx = next_str_idx

        S_leaf = Leaf(token.STRING, S)
        if self.normalize_strings:
            S_leaf.value = normalize_string_quotes(S_leaf.value)

        # Fill the 'custom_splits' list with the appropriate CustomSplit objects.
        temp_string = S_leaf.value[len(prefix) + 1 : -1]
        for has_prefix in prefix_tracker:
            mark_idx = temp_string.find(BREAK_MARK)
            assert (
                mark_idx >= 0
            ), "Logic error while filling the custom string breakpoint cache."

            temp_string = temp_string[mark_idx + len(BREAK_MARK) :]
            breakpoint_idx = mark_idx + (len(prefix) if has_prefix else 0) + 1
            custom_splits.append(CustomSplit(has_prefix, breakpoint_idx))

        string_leaf = Leaf(token.STRING, S_leaf.value.replace(BREAK_MARK, ""))

        if atom_node is not None:
            # If not all children of the atom node are merged (this can happen
            # when there is a standalone comment in the middle) ...
            if non_string_idx - string_idx < len(atom_node.children):
                # We need to replace the old STRING leaves with the new string leaf.
                first_child_idx = LL[string_idx].remove()
                for idx in range(string_idx + 1, non_string_idx):
                    LL[idx].remove()
                if first_child_idx is not None:
                    atom_node.insert_child(first_child_idx, string_leaf)
            else:
                # Else replace the atom node with the new string leaf.
                replace_child(atom_node, string_leaf)

        self.add_custom_splits(string_leaf.value, custom_splits)
        return num_of_strings, string_leaf

    @staticmethod
    def _validate_msg(line: Line, string_idx: int) -> TResult[None]:
        """Validate (M)erge (S)tring (G)roup

        Transform-time string validation logic for _merge_string_group(...).

        Returns:
            * Ok(None), if ALL validation checks (listed below) pass.
                OR
            * Err(CannotTransform), if any of the following are true:
                - The target string group does not contain ANY stand-alone comments.
                - The target string is not in a string group (i.e. it has no
                  adjacent strings).
                - The string group has more than one inline comment.
                - The string group has an inline comment that appears to be a pragma.
                - The set of all string prefixes in the string group is of
                  length greater than one and is not equal to {"", "f"}.
                - The string group consists of raw strings.
                - The string group would merge f-strings with different quote types
                  and internal quotes.
                - The string group is stringified type annotations. We don't want to
                  process stringified type annotations since pyright doesn't support
                  them spanning multiple string values. (NOTE: mypy, pytype, pyre do
                  support them, so we can change if pyright also gains support in the
                  future. See https://github.com/microsoft/pyright/issues/4359.)
        """
        # We first check for "inner" stand-alone comments (i.e. stand-alone
        # comments that have a string leaf before them AND after them).
        for inc in [1, -1]:
            i = string_idx
            found_sa_comment = False
            is_valid_index = is_valid_index_factory(line.leaves)
            while is_valid_index(i) and line.leaves[i].type in [
                token.STRING,
                STANDALONE_COMMENT,
            ]:
                if line.leaves[i].type == STANDALONE_COMMENT:
                    found_sa_comment = True
                elif found_sa_comment:
                    return TErr(
                        "StringMerger does NOT merge string groups which contain "
                        "stand-alone comments."
                    )

                i += inc

        QUOTE = line.leaves[string_idx].value[-1]

        num_of_inline_string_comments = 0
        set_of_prefixes = set()
        num_of_strings = 0
        for leaf in line.leaves[string_idx:]:
            if leaf.type != token.STRING:
                # If the string group is trailed by a comma, we count the
                # comments trailing the comma to be one of the string group's
                # comments.
                if leaf.type == token.COMMA and id(leaf) in line.comments:
                    num_of_inline_string_comments += 1
                break

            if has_triple_quotes(leaf.value):
                return TErr("StringMerger does NOT merge multiline strings.")

            num_of_strings += 1
            prefix = get_string_prefix(leaf.value).lower()
            if "r" in prefix:
                return TErr("StringMerger does NOT merge raw strings.")

            set_of_prefixes.add(prefix)

            if (
                "f" in prefix
                and leaf.value[-1] != QUOTE
                and (
                    "'" in leaf.value[len(prefix) + 1 : -1]
                    or '"' in leaf.value[len(prefix) + 1 : -1]
                )
            ):
                return TErr(
                    "StringMerger does NOT merge f-strings with different quote types"
                    " and internal quotes."
                )

            if id(leaf) in line.comments:
                num_of_inline_string_comments += 1
                if contains_pragma_comment(line.comments[id(leaf)]):
                    return TErr("Cannot merge strings which have pragma comments.")

        if num_of_strings < 2:
            return TErr(
                f"Not enough strings to merge (num_of_strings={num_of_strings})."
            )

        if num_of_inline_string_comments > 1:
            return TErr(
                f"Too many inline string comments ({num_of_inline_string_comments})."
            )

        if len(set_of_prefixes) > 1 and set_of_prefixes != {"", "f"}:
            return TErr(f"Too many different prefixes ({set_of_prefixes}).")

        return Ok(None)


class StringParenStripper(StringTransformer):
    """StringTransformer that strips surrounding parentheses from strings.

    Requirements:
        The line contains a string which is surrounded by parentheses and:
            - The target string is NOT the only argument to a function call.
            - The target string is NOT a "pointless" string.
            - The target string is NOT a dictionary value.
            - If the target string contains a PERCENT, the brackets are not
              preceded or followed by an operator with higher precedence than
              PERCENT.

    Transformations:
        The parentheses mentioned in the 'Requirements' section are stripped.

    Collaborations:
        StringParenStripper has its own inherent usefulness, but it is also
        relied on to clean up the parentheses created by StringParenWrapper (in
        the event that they are no longer needed).
    """

    def do_match(self, line: Line) -> TMatchResult:
        LL = line.leaves

        is_valid_index = is_valid_index_factory(LL)

        string_indices = []

        idx = -1
        while True:
            idx += 1
            if idx >= len(LL):
                break
            leaf = LL[idx]

            # Should be a string...
            if leaf.type != token.STRING:
                continue

            # If this is a "pointless" string...
            if (
                leaf.parent
                and leaf.parent.parent
                and leaf.parent.parent.type == syms.simple_stmt
            ):
                continue

            # Should be preceded by a non-empty LPAR...
            if (
                not is_valid_index(idx - 1)
                or LL[idx - 1].type != token.LPAR
                or is_empty_lpar(LL[idx - 1])
            ):
                continue

            # That LPAR should NOT be preceded by a colon (which could be a
            # dictionary value), function name, or a closing bracket (which
            # could be a function returning a function or a list/dictionary
            # containing a function)...
            if is_valid_index(idx - 2) and (
                LL[idx - 2].type == token.COLON
                or LL[idx - 2].type == token.NAME
                or LL[idx - 2].type in CLOSING_BRACKETS
            ):
                continue

            string_idx = idx

            # Skip the string trailer, if one exists.
            string_parser = StringParser()
            next_idx = string_parser.parse(LL, string_idx)

            # if the leaves in the parsed string include a PERCENT, we need to
            # make sure the initial LPAR is NOT preceded by an operator with
            # higher or equal precedence to PERCENT
            if is_valid_index(idx - 2):
                # mypy can't quite follow unless we name this
                before_lpar = LL[idx - 2]
                if token.PERCENT in {leaf.type for leaf in LL[idx - 1 : next_idx]} and (
                    (
                        before_lpar.type
                        in {
                            token.STAR,
                            token.AT,
                            token.SLASH,
                            token.DOUBLESLASH,
                            token.PERCENT,
                            token.TILDE,
                            token.DOUBLESTAR,
                            token.AWAIT,
                            token.LSQB,
                            token.LPAR,
                        }
                    )
                    or (
                        # only unary PLUS/MINUS
                        before_lpar.parent
                        and before_lpar.parent.type == syms.factor
                        and (before_lpar.type in {token.PLUS, token.MINUS})
                    )
                ):
                    continue

            # Should be followed by a non-empty RPAR...
            if (
                is_valid_index(next_idx)
                and LL[next_idx].type == token.RPAR
                and not is_empty_rpar(LL[next_idx])
            ):
                # That RPAR should NOT be followed by anything with higher
                # precedence than PERCENT
                if is_valid_index(next_idx + 1) and LL[next_idx + 1].type in {
                    token.DOUBLESTAR,
                    token.LSQB,
                    token.LPAR,
                    token.DOT,
                }:
                    continue

                string_indices.append(string_idx)
                idx = string_idx
                while idx < len(LL) - 1 and LL[idx + 1].type == token.STRING:
                    idx += 1

        if string_indices:
            return Ok(string_indices)
        return TErr("This line has no strings wrapped in parens.")

    def do_transform(
        self, line: Line, string_indices: list[int]
    ) -> Iterator[TResult[Line]]:
        LL = line.leaves

        string_and_rpar_indices: list[int] = []
        for string_idx in string_indices:
            string_parser = StringParser()
            rpar_idx = string_parser.parse(LL, string_idx)

            should_transform = True
            for leaf in (LL[string_idx - 1], LL[rpar_idx]):
                if line.comments_after(leaf):
                    # Should not strip parentheses which have comments attached
                    # to them.
                    should_transform = False
                    break
            if should_transform:
                string_and_rpar_indices.extend((string_idx, rpar_idx))

        if string_and_rpar_indices:
            yield Ok(self._transform_to_new_line(line, string_and_rpar_indices))
        else:
            yield Err(
                CannotTransform("All string groups have comments attached to them.")
            )

    def _transform_to_new_line(
        self, line: Line, string_and_rpar_indices: list[int]
    ) -> Line:
        LL = line.leaves

        new_line = line.clone()
        new_line.comments = line.comments.copy()

        previous_idx = -1
        # We need to sort the indices, since string_idx and its matching
        # rpar_idx may not come in order, e.g. in
        # `("outer" % ("inner".join(items)))`, the "inner" string's
        # string_idx is smaller than "outer" string's rpar_idx.
        for idx in sorted(string_and_rpar_indices):
            leaf = LL[idx]
            lpar_or_rpar_idx = idx - 1 if leaf.type == token.STRING else idx
            append_leaves(new_line, line, LL[previous_idx + 1 : lpar_or_rpar_idx])
            if leaf.type == token.STRING:
                string_leaf = Leaf(token.STRING, LL[idx].value)
                LL[lpar_or_rpar_idx].remove()  # Remove lpar.
                replace_child(LL[idx], string_leaf)
                new_line.append(string_leaf)
                # replace comments
                old_comments = new_line.comments.pop(id(LL[idx]), [])
                new_line.comments.setdefault(id(string_leaf), []).extend(old_comments)
            else:
                LL[lpar_or_rpar_idx].remove()  # This is a rpar.

            previous_idx = idx

        # Append the leaves after the last idx:
        append_leaves(new_line, line, LL[idx + 1 :])

        return new_line


class BaseStringSplitter(StringTransformer):
    """
    Abstract class for StringTransformers which transform a Line's strings by splitting
    them or placing them on their own lines where necessary to avoid going over
    the configured line length.

    Requirements:
        * The target string value is responsible for the line going over the
          line length limit. It follows that after all of black's other line
          split methods have been exhausted, this line (or one of the resulting
          lines after all line splits are performed) would still be over the
          line_length limit unless we split this string.
          AND

        * The target string is NOT a "pointless" string (i.e. a string that has
          no parent or siblings).
          AND

        * The target string is not followed by an inline comment that appears
          to be a pragma.
          AND

        * The target string is not a multiline (i.e. triple-quote) string.
    """

    STRING_OPERATORS: Final = [
        token.EQEQUAL,
        token.GREATER,
        token.GREATEREQUAL,
        token.LESS,
        token.LESSEQUAL,
        token.NOTEQUAL,
        token.PERCENT,
        token.PLUS,
        token.STAR,
    ]

    @abstractmethod
    def do_splitter_match(self, line: Line) -> TMatchResult:
        """
        BaseStringSplitter asks its clients to override this method instead of
        `StringTransformer.do_match(...)`.

        Follows the same protocol as `StringTransformer.do_match(...)`.

        Refer to `help(StringTransformer.do_match)` for more information.
        """

    def do_match(self, line: Line) -> TMatchResult:
        match_result = self.do_splitter_match(line)
        if isinstance(match_result, Err):
            return match_result

        string_indices = match_result.ok()
        assert len(string_indices) == 1, (
            f"{self.__class__.__name__} should only find one match at a time, found"
            f" {len(string_indices)}"
        )
        string_idx = string_indices[0]
        vresult = self._validate(line, string_idx)
        if isinstance(vresult, Err):
            return vresult

        return match_result

    def _validate(self, line: Line, string_idx: int) -> TResult[None]:
        """
        Checks that @line meets all of the requirements listed in this classes'
        docstring. Refer to `help(BaseStringSplitter)` for a detailed
        description of those requirements.

        Returns:
            * Ok(None), if ALL of the requirements are met.
              OR
            * Err(CannotTransform), if ANY of the requirements are NOT met.
        """
        LL = line.leaves

        string_leaf = LL[string_idx]

        max_string_length = self._get_max_string_length(line, string_idx)
        if len(string_leaf.value) <= max_string_length:
            return TErr(
                "The string itself is not what is causing this line to be too long."
            )

        if not string_leaf.parent or [L.type for L in string_leaf.parent.children] == [
            token.STRING,
            token.NEWLINE,
        ]:
            return TErr(
                f"This string ({string_leaf.value}) appears to be pointless (i.e. has"
                " no parent)."
            )

        if id(line.leaves[string_idx]) in line.comments and contains_pragma_comment(
            line.comments[id(line.leaves[string_idx])]
        ):
            return TErr(
                "Line appears to end with an inline pragma comment. Splitting the line"
                " could modify the pragma's behavior."
            )

        if has_triple_quotes(string_leaf.value):
            return TErr("We cannot split multiline strings.")

        return Ok(None)

    def _get_max_string_length(self, line: Line, string_idx: int) -> int:
        """
        Calculates the max string length used when attempting to determine
        whether or not the target string is responsible for causing the line to
        go over the line length limit.

        WARNING: This method is tightly coupled to both StringSplitter and
        (especially) StringParenWrapper. There is probably a better way to
        accomplish what is being done here.

        Returns:
            max_string_length: such that `line.leaves[string_idx].value >
            max_string_length` implies that the target string IS responsible
            for causing this line to exceed the line length limit.
        """
        LL = line.leaves

        is_valid_index = is_valid_index_factory(LL)

        # We use the shorthand "WMA4" in comments to abbreviate "We must
        # account for". When giving examples, we use STRING to mean some/any
        # valid string.
        #
        # Finally, we use the following convenience variables:
        #
        #   P:  The leaf that is before the target string leaf.
        #   N:  The leaf that is after the target string leaf.
        #   NN: The leaf that is after N.

        # WMA4 the whitespace at the beginning of the line.
        offset = line.depth * 4

        if is_valid_index(string_idx - 1):
            p_idx = string_idx - 1
            if (
                LL[string_idx - 1].type == token.LPAR
                and LL[string_idx - 1].value == ""
                and string_idx >= 2
            ):
                # If the previous leaf is an empty LPAR placeholder, we should skip it.
                p_idx -= 1

            P = LL[p_idx]
            if P.type in self.STRING_OPERATORS:
                # WMA4 a space and a string operator (e.g. `+ STRING` or `== STRING`).
                offset += len(str(P)) + 1

            if P.type == token.COMMA:
                # WMA4 a space, a comma, and a closing bracket [e.g. `), STRING`].
                offset += 3

            if P.type in [token.COLON, token.EQUAL, token.PLUSEQUAL, token.NAME]:
                # This conditional branch is meant to handle dictionary keys,
                # variable assignments, 'return STRING' statement lines, and
                # 'else STRING' ternary expression lines.

                # WMA4 a single space.
                offset += 1

                # WMA4 the lengths of any leaves that came before that space,
                # but after any closing bracket before that space.
                for leaf in reversed(LL[: p_idx + 1]):
                    offset += len(str(leaf))
                    if leaf.type in CLOSING_BRACKETS:
                        break

        if is_valid_index(string_idx + 1):
            N = LL[string_idx + 1]
            if N.type == token.RPAR and N.value == "" and len(LL) > string_idx + 2:
                # If the next leaf is an empty RPAR placeholder, we should skip it.
                N = LL[string_idx + 2]

            if N.type == token.COMMA:
                # WMA4 a single comma at the end of the string (e.g `STRING,`).
                offset += 1

            if is_valid_index(string_idx + 2):
                NN = LL[string_idx + 2]

                if N.type == token.DOT and NN.type == token.NAME:
                    # This conditional branch is meant to handle method calls invoked
                    # off of a string literal up to and including the LPAR character.

                    # WMA4 the '.' character.
                    offset += 1

                    if (
                        is_valid_index(string_idx + 3)
                        and LL[string_idx + 3].type == token.LPAR
                    ):
                        # WMA4 the left parenthesis character.
                        offset += 1

                    # WMA4 the length of the method's name.
                    offset += len(NN.value)

        has_comments = False
        for comment_leaf in line.comments_after(LL[string_idx]):
            if not has_comments:
                has_comments = True
                # WMA4 two spaces before the '#' character.
                offset += 2

            # WMA4 the length of the inline comment.
            offset += len(comment_leaf.value)

        max_string_length = count_chars_in_width(str(line), self.line_length - offset)
        return max_string_length

    @staticmethod
    def _prefer_paren_wrap_match(LL: list[Leaf]) -> Optional[int]:
        """
        Returns:
            string_idx such that @LL[string_idx] is equal to our target (i.e.
            matched) string, if this line matches the "prefer paren wrap" statement
            requirements listed in the 'Requirements' section of the StringParenWrapper
            class's docstring.
                OR
            None, otherwise.
        """
        # The line must start with a string.
        if LL[0].type != token.STRING:
            return None

        matching_nodes = [
            syms.listmaker,
            syms.dictsetmaker,
            syms.testlist_gexp,
        ]
        # If the string is an immediate child of a list/set/tuple literal...
        if (
            parent_type(LL[0]) in matching_nodes
            or parent_type(LL[0].parent) in matching_nodes
        ):
            # And the string is surrounded by commas (or is the first/last child)...
            prev_sibling = LL[0].prev_sibling
            next_sibling = LL[0].next_sibling
            if (
                not prev_sibling
                and not next_sibling
                and parent_type(LL[0]) == syms.atom
            ):
                # If it's an atom string, we need to check the parent atom's siblings.
                parent = LL[0].parent
                assert parent is not None  # For type checkers.
                prev_sibling = parent.prev_sibling
                next_sibling = parent.next_sibling
            if (not prev_sibling or prev_sibling.type == token.COMMA) and (
                not next_sibling or next_sibling.type == token.COMMA
            ):
                return 0

        return None


def iter_fexpr_spans(s: str) -> Iterator[tuple[int, int]]:
    """
    Yields spans corresponding to expressions in a given f-string.
    Spans are half-open ranges (left inclusive, right exclusive).
    Assumes the input string is a valid f-string, but will not crash if the input
    string is invalid.
    """
    stack: list[int] = []  # our curly paren stack
    i = 0
    while i < len(s):
        if s[i] == "{":
            # if we're in a string part of the f-string, ignore escaped curly braces
            if not stack and i + 1 < len(s) and s[i + 1] == "{":
                i += 2
                continue
            stack.append(i)
            i += 1
            continue

        if s[i] == "}":
            if not stack:
                i += 1
                continue
            j = stack.pop()
            # we've made it back out of the expression! yield the span
            if not stack:
                yield (j, i + 1)
            i += 1
            continue

        # if we're in an expression part of the f-string, fast-forward through strings
        # note that backslashes are not legal in the expression portion of f-strings
        if stack:
            delim = None
            if s[i : i + 3] in ("'''", '"""'):
                delim = s[i : i + 3]
            elif s[i] in ("'", '"'):
                delim = s[i]
            if delim:
                i += len(delim)
                while i < len(s) and s[i : i + len(delim)] != delim:
                    i += 1
                i += len(delim)
                continue
        i += 1


def fstring_contains_expr(s: str) -> bool:
    return any(iter_fexpr_spans(s))


def _toggle_fexpr_quotes(fstring: str, old_quote: str) -> str:
    """
    Toggles quotes used in f-string expressions that are `old_quote`.

    f-string expressions can't contain backslashes, so we need to toggle the
    quotes if the f-string itself will end up using the same quote. We can
    simply toggle without escaping because, quotes can't be reused in f-string
    expressions. They will fail to parse.

    NOTE: If PEP 701 is accepted, above statement will no longer be true.
    Though if quotes can be reused, we can simply reuse them without updates or
    escaping, once Black figures out how to parse the new grammar.
    """
    new_quote = "'" if old_quote == '"' else '"'
    parts = []
    previous_index = 0
    for start, end in iter_fexpr_spans(fstring):
        parts.append(fstring[previous_index:start])
        parts.append(fstring[start:end].replace(old_quote, new_quote))
        previous_index = end
    parts.append(fstring[previous_index:])
    return "".join(parts)


class StringSplitter(BaseStringSplitter, CustomSplitMapMixin):
    """
    StringTransformer that splits "atom" strings (i.e. strings which exist on
    lines by themselves).

    Requirements:
        * The line consists ONLY of a single string (possibly prefixed by a
          string operator [e.g. '+' or '==']), MAYBE a string trailer, and MAYBE
          a trailing comma.
          AND
        * All of the requirements listed in BaseStringSplitter's docstring.

    Transformations:
        The string mentioned in the 'Requirements' section is split into as
        many substrings as necessary to adhere to the configured line length.

        In the final set of substrings, no substring should be smaller than
        MIN_SUBSTR_SIZE characters.

        The string will ONLY be split on spaces (i.e. each new substring should
        start with a space). Note that the string will NOT be split on a space
        which is escaped with a backslash.

        If the string is an f-string, it will NOT be split in the middle of an
        f-expression (e.g. in f"FooBar: {foo() if x else bar()}", {foo() if x
        else bar()} is an f-expression).

        If the string that is being split has an associated set of custom split
        records and those custom splits will NOT result in any line going over
        the configured line length, those custom splits are used. Otherwise the
        string is split as late as possible (from left-to-right) while still
        adhering to the transformation rules listed above.

    Collaborations:
        StringSplitter relies on StringMerger to construct the appropriate
        CustomSplit objects and add them to the custom split map.
    """

    MIN_SUBSTR_SIZE: Final = 6

    def do_splitter_match(self, line: Line) -> TMatchResult:
        LL = line.leaves

        if self._prefer_paren_wrap_match(LL) is not None:
            return TErr("Line needs to be wrapped in parens first.")

        is_valid_index = is_valid_index_factory(LL)

        idx = 0

        # The first two leaves MAY be the 'not in' keywords...
        if (
            is_valid_index(idx)
            and is_valid_index(idx + 1)
            and [LL[idx].type, LL[idx + 1].type] == [token.NAME, token.NAME]
            and str(LL[idx]) + str(LL[idx + 1]) == "not in"
        ):
            idx += 2
        # Else the first leaf MAY be a string operator symbol or the 'in' keyword...
        elif is_valid_index(idx) and (
            LL[idx].type in self.STRING_OPERATORS
            or LL[idx].type == token.NAME
            and str(LL[idx]) == "in"
        ):
            idx += 1

        # The next/first leaf MAY be an empty LPAR...
        if is_valid_index(idx) and is_empty_lpar(LL[idx]):
            idx += 1

        # The next/first leaf MUST be a string...
        if not is_valid_index(idx) or LL[idx].type != token.STRING:
            return TErr("Line does not start with a string.")

        string_idx = idx

        # Skip the string trailer, if one exists.
        string_parser = StringParser()
        idx = string_parser.parse(LL, string_idx)

        # That string MAY be followed by an empty RPAR...
        if is_valid_index(idx) and is_empty_rpar(LL[idx]):
            idx += 1

        # That string / empty RPAR leaf MAY be followed by a comma...
        if is_valid_index(idx) and LL[idx].type == token.COMMA:
            idx += 1

        # But no more leaves are allowed...
        if is_valid_index(idx):
            return TErr("This line does not end with a string.")

        return Ok([string_idx])

    def do_transform(
        self, line: Line, string_indices: list[int]
    ) -> Iterator[TResult[Line]]:
        LL = line.leaves
        assert len(string_indices) == 1, (
            f"{self.__class__.__name__} should only find one match at a time, found"
            f" {len(string_indices)}"
        )
        string_idx = string_indices[0]

        QUOTE = LL[string_idx].value[-1]

        is_valid_index = is_valid_index_factory(LL)
        insert_str_child = insert_str_child_factory(LL[string_idx])

        prefix = get_string_prefix(LL[string_idx].value).lower()

        # We MAY choose to drop the 'f' prefix from substrings that don't
        # contain any f-expressions, but ONLY if the original f-string
        # contains at least one f-expression. Otherwise, we will alter the AST
        # of the program.
        drop_pointless_f_prefix = ("f" in prefix) and fstring_contains_expr(
            LL[string_idx].value
        )

        first_string_line = True

        string_op_leaves = self._get_string_operator_leaves(LL)
        string_op_leaves_length = (
            sum(len(str(prefix_leaf)) for prefix_leaf in string_op_leaves) + 1
            if string_op_leaves
            else 0
        )

        def maybe_append_string_operators(new_line: Line) -> None:
            """
            Side Effects:
                If @line starts with a string operator and this is the first
                line we are constructing, this function appends the string
                operator to @new_line and replaces the old string operator leaf
                in the node structure. Otherwise this function does nothing.
            """
            maybe_prefix_leaves = string_op_leaves if first_string_line else []
            for i, prefix_leaf in enumerate(maybe_prefix_leaves):
                replace_child(LL[i], prefix_leaf)
                new_line.append(prefix_leaf)

        ends_with_comma = (
            is_valid_index(string_idx + 1) and LL[string_idx + 1].type == token.COMMA
        )

        def max_last_string_column() -> int:
            """
            Returns:
                The max allowed width of the string value used for the last
                line we will construct.  Note that this value means the width
                rather than the number of characters (e.g., many East Asian
                characters expand to two columns).
            """
            result = self.line_length
            result -= line.depth * 4
            result -= 1 if ends_with_comma else 0
            result -= string_op_leaves_length
            return result

        # --- Calculate Max Break Width (for string value)
        # We start with the line length limit
        max_break_width = self.line_length
        # The last index of a string of length N is N-1.
        max_break_width -= 1
        # Leading whitespace is not present in the string value (e.g. Leaf.value).
        max_break_width -= line.depth * 4
        if max_break_width < 0:
            yield TErr(
                f"Unable to split {LL[string_idx].value} at such high of a line depth:"
                f" {line.depth}"
            )
            return

        # Check if StringMerger registered any custom splits.
        custom_splits = self.pop_custom_splits(LL[string_idx].value)
        # We use them ONLY if none of them would produce lines that exceed the
        # line limit.
        use_custom_breakpoints = bool(
            custom_splits
            and all(csplit.break_idx <= max_break_width for csplit in custom_splits)
        )

        # Temporary storage for the remaining chunk of the string line that
        # can't fit onto the line currently being constructed.
        rest_value = LL[string_idx].value

        def more_splits_should_be_made() -> bool:
            """
            Returns:
                True iff `rest_value` (the remaining string value from the last
                split), should be split again.
            """
            if use_custom_breakpoints:
                return len(custom_splits) > 1
            else:
                return str_width(rest_value) > max_last_string_column()

        string_line_results: list[Ok[Line]] = []
        while more_splits_should_be_made():
            if use_custom_breakpoints:
                # Custom User Split (manual)
                csplit = custom_splits.pop(0)
                break_idx = csplit.break_idx
            else:
                # Algorithmic Split (automatic)
                max_bidx = (
                    count_chars_in_width(rest_value, max_break_width)
                    - string_op_leaves_length
                )
                maybe_break_idx = self._get_break_idx(rest_value, max_bidx)
                if maybe_break_idx is None:
                    # If we are unable to algorithmically determine a good split
                    # and this string has custom splits registered to it, we
                    # fall back to using them--which means we have to start
                    # over from the beginning.
                    if custom_splits:
                        rest_value = LL[string_idx].value
                        string_line_results = []
                        first_string_line = True
                        use_custom_breakpoints = True
                        continue

                    # Otherwise, we stop splitting here.
                    break

                break_idx = maybe_break_idx

            # --- Construct `next_value`
            next_value = rest_value[:break_idx] + QUOTE

            # HACK: The following 'if' statement is a hack to fix the custom
            # breakpoint index in the case of either: (a) substrings that were
            # f-strings but will have the 'f' prefix removed OR (b) substrings
            # that were not f-strings but will now become f-strings because of
            # redundant use of the 'f' prefix (i.e. none of the substrings
            # contain f-expressions but one or more of them had the 'f' prefix
            # anyway; in which case, we will prepend 'f' to _all_ substrings).
            #
            # There is probably a better way to accomplish what is being done
            # here...
            #
            # If this substring is an f-string, we _could_ remove the 'f'
            # prefix, and the current custom split did NOT originally use a
            # prefix...
            if (
                use_custom_breakpoints
                and not csplit.has_prefix
                and (
                    # `next_value == prefix + QUOTE` happens when the custom
                    # split is an empty string.
                    next_value == prefix + QUOTE
                    or next_value != self._normalize_f_string(next_value, prefix)
                )
            ):
                # Then `csplit.break_idx` will be off by one after removing
                # the 'f' prefix.
                break_idx += 1
                next_value = rest_value[:break_idx] + QUOTE

            if drop_pointless_f_prefix:
                next_value = self._normalize_f_string(next_value, prefix)

            # --- Construct `next_leaf`
            next_leaf = Leaf(token.STRING, next_value)
            insert_str_child(next_leaf)
            self._maybe_normalize_string_quotes(next_leaf)

            # --- Construct `next_line`
            next_line = line.clone()
            maybe_append_string_operators(next_line)
            next_line.append(next_leaf)
            string_line_results.append(Ok(next_line))

            rest_value = prefix + QUOTE + rest_value[break_idx:]
            first_string_line = False

        yield from string_line_results

        if drop_pointless_f_prefix:
            rest_value = self._normalize_f_string(rest_value, prefix)

        rest_leaf = Leaf(token.STRING, rest_value)
        insert_str_child(rest_leaf)

        # NOTE: I could not find a test case that verifies that the following
        # line is actually necessary, but it seems to be. Otherwise we risk
        # not normalizing the last substring, right?
        self._maybe_normalize_string_quotes(rest_leaf)

        last_line = line.clone()
        maybe_append_string_operators(last_line)

        # If there are any leaves to the right of the target string...
        if is_valid_index(string_idx + 1):
            # We use `temp_value` here to determine how long the last line
            # would be if we were to append all the leaves to the right of the
            # target string to the last string line.
            temp_value = rest_value
            for leaf in LL[string_idx + 1 :]:
                temp_value += str(leaf)
                if leaf.type == token.LPAR:
                    break

            # Try to fit them all on the same line with the last substring...
            if (
                str_width(temp_value) <= max_last_string_column()
                or LL[string_idx + 1].type == token.COMMA
            ):
                last_line.append(rest_leaf)
                append_leaves(last_line, line, LL[string_idx + 1 :])
                yield Ok(last_line)
            # Otherwise, place the last substring on one line and everything
            # else on a line below that...
            else:
                last_line.append(rest_leaf)
                yield Ok(last_line)

                non_string_line = line.clone()
                append_leaves(non_string_line, line, LL[string_idx + 1 :])
                yield Ok(non_string_line)
        # Else the target string was the last leaf...
        else:
            last_line.append(rest_leaf)
            last_line.comments = line.comments.copy()
            yield Ok(last_line)

    def _iter_nameescape_slices(self, string: str) -> Iterator[tuple[Index, Index]]:
        r"""
        Yields:
            All ranges of @string which, if @string were to be split there,
            would result in the splitting of an \N{...} expression (which is NOT
            allowed).
        """
        # True - the previous backslash was unescaped
        # False - the previous backslash was escaped *or* there was no backslash
        previous_was_unescaped_backslash = False
        it = iter(enumerate(string))
        for idx, c in it:
            if c == "\\":
                previous_was_unescaped_backslash = not previous_was_unescaped_backslash
                continue
            if not previous_was_unescaped_backslash or c != "N":
                previous_was_unescaped_backslash = False
                continue
            previous_was_unescaped_backslash = False

            begin = idx - 1  # the position of backslash before \N{...}
            for idx, c in it:
                if c == "}":
                    end = idx
                    break
            else:
                # malformed nameescape expression?
                # should have been detected by AST parsing earlier...
                raise RuntimeError(f"{self.__class__.__name__} LOGIC ERROR!")
            yield begin, end

    def _iter_fexpr_slices(self, string: str) -> Iterator[tuple[Index, Index]]:
        """
        Yields:
            All ranges of @string which, if @string were to be split there,
            would result in the splitting of an f-expression (which is NOT
            allowed).
        """
        if "f" not in get_string_prefix(string).lower():
            return
        yield from iter_fexpr_spans(string)

    def _get_illegal_split_indices(self, string: str) -> set[Index]:
        illegal_indices: set[Index] = set()
        iterators = [
            self._iter_fexpr_slices(string),
            self._iter_nameescape_slices(string),
        ]
        for it in iterators:
            for begin, end in it:
                illegal_indices.update(range(begin, end))
        return illegal_indices

    def _get_break_idx(self, string: str, max_break_idx: int) -> Optional[int]:
        """
        This method contains the algorithm that StringSplitter uses to
        determine which character to split each string at.

        Args:
            @string: The substring that we are attempting to split.
            @max_break_idx: The ideal break index. We will return this value if it
            meets all the necessary conditions. In the likely event that it
            doesn't we will try to find the closest index BELOW @max_break_idx
            that does. If that fails, we will expand our search by also
            considering all valid indices ABOVE @max_break_idx.

        Pre-Conditions:
            * assert_is_leaf_string(@string)
            * 0 <= @max_break_idx < len(@string)

        Returns:
            break_idx, if an index is able to be found that meets all of the
            conditions listed in the 'Transformations' section of this classes'
            docstring.
                OR
            None, otherwise.
        """
        is_valid_index = is_valid_index_factory(string)

        assert is_valid_index(max_break_idx)
        assert_is_leaf_string(string)

        _illegal_split_indices = self._get_illegal_split_indices(string)

        def breaks_unsplittable_expression(i: Index) -> bool:
            """
            Returns:
                True iff returning @i would result in the splitting of an
                unsplittable expression (which is NOT allowed).
            """
            return i in _illegal_split_indices

        def passes_all_checks(i: Index) -> bool:
            """
            Returns:
                True iff ALL of the conditions listed in the 'Transformations'
                section of this classes' docstring would be met by returning @i.
            """
            is_space = string[i] == " "
            is_split_safe = is_valid_index(i - 1) and string[i - 1] in SPLIT_SAFE_CHARS

            is_not_escaped = True
            j = i - 1
            while is_valid_index(j) and string[j] == "\\":
                is_not_escaped = not is_not_escaped
                j -= 1

            is_big_enough = (
                len(string[i:]) >= self.MIN_SUBSTR_SIZE
                and len(string[:i]) >= self.MIN_SUBSTR_SIZE
            )
            return (
                (is_space or is_split_safe)
                and is_not_escaped
                and is_big_enough
                and not breaks_unsplittable_expression(i)
            )

        # First, we check all indices BELOW @max_break_idx.
        break_idx = max_break_idx
        while is_valid_index(break_idx - 1) and not passes_all_checks(break_idx):
            break_idx -= 1

        if not passes_all_checks(break_idx):
            # If that fails, we check all indices ABOVE @max_break_idx.
            #
            # If we are able to find a valid index here, the next line is going
            # to be longer than the specified line length, but it's probably
            # better than doing nothing at all.
            break_idx = max_break_idx + 1
            while is_valid_index(break_idx + 1) and not passes_all_checks(break_idx):
                break_idx += 1

            if not is_valid_index(break_idx) or not passes_all_checks(break_idx):
                return None

        return break_idx

    def _maybe_normalize_string_quotes(self, leaf: Leaf) -> None:
        if self.normalize_strings:
            leaf.value = normalize_string_quotes(leaf.value)

    def _normalize_f_string(self, string: str, prefix: str) -> str:
        """
        Pre-Conditions:
            * assert_is_leaf_string(@string)

        Returns:
            * If @string is an f-string that contains no f-expressions, we
            return a string identical to @string except that the 'f' prefix
            has been stripped and all double braces (i.e. '{{' or '}}') have
            been normalized (i.e. turned into '{' or '}').
                OR
            * Otherwise, we return @string.
        """
        assert_is_leaf_string(string)

        if "f" in prefix and not fstring_contains_expr(string):
            new_prefix = prefix.replace("f", "")

            temp = string[len(prefix) :]
            temp = re.sub(r"\{\{", "{", temp)
            temp = re.sub(r"\}\}", "}", temp)
            new_string = temp

            return f"{new_prefix}{new_string}"
        else:
            return string

    def _get_string_operator_leaves(self, leaves: Iterable[Leaf]) -> list[Leaf]:
        LL = list(leaves)

        string_op_leaves = []
        i = 0
        while LL[i].type in self.STRING_OPERATORS + [token.NAME]:
            prefix_leaf = Leaf(LL[i].type, str(LL[i]).strip())
            string_op_leaves.append(prefix_leaf)
            i += 1
        return string_op_leaves


class StringParenWrapper(BaseStringSplitter, CustomSplitMapMixin):
    """
    StringTransformer that wraps strings in parens and then splits at the LPAR.

    Requirements:
        All of the requirements listed in BaseStringSplitter's docstring in
        addition to the requirements listed below:

        * The line is a return/yield statement, which returns/yields a string.
          OR
        * The line is part of a ternary expression (e.g. `x = y if cond else
          z`) such that the line starts with `else <string>`, where <string> is
          some string.
          OR
        * The line is an assert statement, which ends with a string.
          OR
        * The line is an assignment statement (e.g. `x = <string>` or `x +=
          <string>`) such that the variable is being assigned the value of some
          string.
          OR
        * The line is a dictionary key assignment where some valid key is being
          assigned the value of some string.
          OR
        * The line is an lambda expression and the value is a string.
          OR
        * The line starts with an "atom" string that prefers to be wrapped in
          parens. It's preferred to be wrapped when it's is an immediate child of
          a list/set/tuple literal, AND the string is surrounded by commas (or is
          the first/last child).

    Transformations:
        The chosen string is wrapped in parentheses and then split at the LPAR.

        We then have one line which ends with an LPAR and another line that
        starts with the chosen string. The latter line is then split again at
        the RPAR. This results in the RPAR (and possibly a trailing comma)
        being placed on its own line.

        NOTE: If any leaves exist to the right of the chosen string (except
        for a trailing comma, which would be placed after the RPAR), those
        leaves are placed inside the parentheses.  In effect, the chosen
        string is not necessarily being "wrapped" by parentheses. We can,
        however, count on the LPAR being placed directly before the chosen
        string.

        In other words, StringParenWrapper creates "atom" strings. These
        can then be split again by StringSplitter, if necessary.

    Collaborations:
        In the event that a string line split by StringParenWrapper is
        changed such that it no longer needs to be given its own line,
        StringParenWrapper relies on StringParenStripper to clean up the
        parentheses it created.

        For "atom" strings that prefers to be wrapped in parens, it requires
        StringSplitter to hold the split until the string is wrapped in parens.
    """

    def do_splitter_match(self, line: Line) -> TMatchResult:
        LL = line.leaves

        if line.leaves[-1].type in OPENING_BRACKETS:
            return TErr(
                "Cannot wrap parens around a line that ends in an opening bracket."
            )

        string_idx = (
            self._return_match(LL)
            or self._else_match(LL)
            or self._assert_match(LL)
            or self._assign_match(LL)
            or self._dict_or_lambda_match(LL)
            or self._prefer_paren_wrap_match(LL)
        )

        if string_idx is not None:
            string_value = line.leaves[string_idx].value
            # If the string has neither spaces nor East Asian stops...
            if not any(
                char == " " or char in SPLIT_SAFE_CHARS for char in string_value
            ):
                # And will still violate the line length limit when split...
                max_string_width = self.line_length - ((line.depth + 1) * 4)
                if str_width(string_value) > max_string_width:
                    # And has no associated custom splits...
                    if not self.has_custom_splits(string_value):
                        # Then we should NOT put this string on its own line.
                        return TErr(
                            "We do not wrap long strings in parentheses when the"
                            " resultant line would still be over the specified line"
                            " length and can't be split further by StringSplitter."
                        )
            return Ok([string_idx])

        return TErr("This line does not contain any non-atomic strings.")

    @staticmethod
    def _return_match(LL: list[Leaf]) -> Optional[int]:
        """
        Returns:
            string_idx such that @LL[string_idx] is equal to our target (i.e.
            matched) string, if this line matches the return/yield statement
            requirements listed in the 'Requirements' section of this classes'
            docstring.
                OR
            None, otherwise.
        """
        # If this line is a part of a return/yield statement and the first leaf
        # contains either the "return" or "yield" keywords...
        if parent_type(LL[0]) in [syms.return_stmt, syms.yield_expr] and LL[
            0
        ].value in ["return", "yield"]:
            is_valid_index = is_valid_index_factory(LL)

            idx = 2 if is_valid_index(1) and is_empty_par(LL[1]) else 1
            # The next visible leaf MUST contain a string...
            if is_valid_index(idx) and LL[idx].type == token.STRING:
                return idx

        return None

    @staticmethod
    def _else_match(LL: list[Leaf]) -> Optional[int]:
        """
        Returns:
            string_idx such that @LL[string_idx] is equal to our target (i.e.
            matched) string, if this line matches the ternary expression
            requirements listed in the 'Requirements' section of this classes'
            docstring.
                OR
            None, otherwise.
        """
        # If this line is a part of a ternary expression and the first leaf
        # contains the "else" keyword...
        if (
            parent_type(LL[0]) == syms.test
            and LL[0].type == token.NAME
            and LL[0].value == "else"
        ):
            is_valid_index = is_valid_index_factory(LL)

            idx = 2 if is_valid_index(1) and is_empty_par(LL[1]) else 1
            # The next visible leaf MUST contain a string...
            if is_valid_index(idx) and LL[idx].type == token.STRING:
                return idx

        return None

    @staticmethod
    def _assert_match(LL: list[Leaf]) -> Optional[int]:
        """
        Returns:
            string_idx such that @LL[string_idx] is equal to our target (i.e.
            matched) string, if this line matches the assert statement
            requirements listed in the 'Requirements' section of this classes'
            docstring.
                OR
            None, otherwise.
        """
        # If this line is a part of an assert statement and the first leaf
        # contains the "assert" keyword...
        if parent_type(LL[0]) == syms.assert_stmt and LL[0].value == "assert":
            is_valid_index = is_valid_index_factory(LL)

            for i, leaf in enumerate(LL):
                # We MUST find a comma...
                if leaf.type == token.COMMA:
                    idx = i + 2 if is_empty_par(LL[i + 1]) else i + 1

                    # That comma MUST be followed by a string...
                    if is_valid_index(idx) and LL[idx].type == token.STRING:
                        string_idx = idx

                        # Skip the string trailer, if one exists.
                        string_parser = StringParser()
                        idx = string_parser.parse(LL, string_idx)

                        # But no more leaves are allowed...
                        if not is_valid_index(idx):
                            return string_idx

        return None

    @staticmethod
    def _assign_match(LL: list[Leaf]) -> Optional[int]:
        """
        Returns:
            string_idx such that @LL[string_idx] is equal to our target (i.e.
            matched) string, if this line matches the assignment statement
            requirements listed in the 'Requirements' section of this classes'
            docstring.
                OR
            None, otherwise.
        """
        # If this line is a part of an expression statement or is a function
        # argument AND the first leaf contains a variable name...
        if (
            parent_type(LL[0]) in [syms.expr_stmt, syms.argument, syms.power]
            and LL[0].type == token.NAME
        ):
            is_valid_index = is_valid_index_factory(LL)

            for i, leaf in enumerate(LL):
                # We MUST find either an '=' or '+=' symbol...
                if leaf.type in [token.EQUAL, token.PLUSEQUAL]:
                    idx = i + 2 if is_empty_par(LL[i + 1]) else i + 1

                    # That symbol MUST be followed by a string...
                    if is_valid_index(idx) and LL[idx].type == token.STRING:
                        string_idx = idx

                        # Skip the string trailer, if one exists.
                        string_parser = StringParser()
                        idx = string_parser.parse(LL, string_idx)

                        # The next leaf MAY be a comma iff this line is a part
                        # of a function argument...
                        if (
                            parent_type(LL[0]) == syms.argument
                            and is_valid_index(idx)
                            and LL[idx].type == token.COMMA
                        ):
                            idx += 1

                        # But no more leaves are allowed...
                        if not is_valid_index(idx):
                            return string_idx

        return None

    @staticmethod
    def _dict_or_lambda_match(LL: list[Leaf]) -> Optional[int]:
        """
        Returns:
            string_idx such that @LL[string_idx] is equal to our target (i.e.
            matched) string, if this line matches the dictionary key assignment
            statement or lambda expression requirements listed in the
            'Requirements' section of this classes' docstring.
                OR
            None, otherwise.
        """
        # If this line is a part of a dictionary key assignment or lambda expression...
        parent_types = [parent_type(LL[0]), parent_type(LL[0].parent)]
        if syms.dictsetmaker in parent_types or syms.lambdef in parent_types:
            is_valid_index = is_valid_index_factory(LL)

            for i, leaf in enumerate(LL):
                # We MUST find a colon, it can either be dict's or lambda's colon...
                if leaf.type == token.COLON and i < len(LL) - 1:
                    idx = i + 2 if is_empty_par(LL[i + 1]) else i + 1

                    # That colon MUST be followed by a string...
                    if is_valid_index(idx) and LL[idx].type == token.STRING:
                        string_idx = idx

                        # Skip the string trailer, if one exists.
                        string_parser = StringParser()
                        idx = string_parser.parse(LL, string_idx)

                        # That string MAY be followed by a comma...
                        if is_valid_index(idx) and LL[idx].type == token.COMMA:
                            idx += 1

                        # But no more leaves are allowed...
                        if not is_valid_index(idx):
                            return string_idx

        return None

    def do_transform(
        self, line: Line, string_indices: list[int]
    ) -> Iterator[TResult[Line]]:
        LL = line.leaves
        assert len(string_indices) == 1, (
            f"{self.__class__.__name__} should only find one match at a time, found"
            f" {len(string_indices)}"
        )
        string_idx = string_indices[0]

        is_valid_index = is_valid_index_factory(LL)
        insert_str_child = insert_str_child_factory(LL[string_idx])

        comma_idx = -1
        ends_with_comma = False
        if LL[comma_idx].type == token.COMMA:
            ends_with_comma = True

        leaves_to_steal_comments_from = [LL[string_idx]]
        if ends_with_comma:
            leaves_to_steal_comments_from.append(LL[comma_idx])

        # --- First Line
        first_line = line.clone()
        left_leaves = LL[:string_idx]

        # We have to remember to account for (possibly invisible) LPAR and RPAR
        # leaves that already wrapped the target string. If these leaves do
        # exist, we will replace them with our own LPAR and RPAR leaves.
        old_parens_exist = False
        if left_leaves and left_leaves[-1].type == token.LPAR:
            old_parens_exist = True
            leaves_to_steal_comments_from.append(left_leaves[-1])
            left_leaves.pop()

        append_leaves(first_line, line, left_leaves)

        lpar_leaf = Leaf(token.LPAR, "(")
        if old_parens_exist:
            replace_child(LL[string_idx - 1], lpar_leaf)
        else:
            insert_str_child(lpar_leaf)
        first_line.append(lpar_leaf)

        # We throw inline comments that were originally to the right of the
        # target string to the top line. They will now be shown to the right of
        # the LPAR.
        for leaf in leaves_to_steal_comments_from:
            for comment_leaf in line.comments_after(leaf):
                first_line.append(comment_leaf, preformatted=True)

        yield Ok(first_line)

        # --- Middle (String) Line
        # We only need to yield one (possibly too long) string line, since the
        # `StringSplitter` will break it down further if necessary.
        string_value = LL[string_idx].value
        string_line = Line(
            mode=line.mode,
            depth=line.depth + 1,
            inside_brackets=True,
            should_split_rhs=line.should_split_rhs,
            magic_trailing_comma=line.magic_trailing_comma,
        )
        string_leaf = Leaf(token.STRING, string_value)
        insert_str_child(string_leaf)
        string_line.append(string_leaf)

        old_rpar_leaf = None
        if is_valid_index(string_idx + 1):
            right_leaves = LL[string_idx + 1 :]
            if ends_with_comma:
                right_leaves.pop()

            if old_parens_exist:
                assert right_leaves and right_leaves[-1].type == token.RPAR, (
                    "Apparently, old parentheses do NOT exist?!"
                    f" (left_leaves={left_leaves}, right_leaves={right_leaves})"
                )
                old_rpar_leaf = right_leaves.pop()
            elif right_leaves and right_leaves[-1].type == token.RPAR:
                # Special case for lambda expressions as dict's value, e.g.:
                #     my_dict = {
                #        "key": lambda x: f"formatted: {x}",
                #     }
                # After wrapping the dict's value with parentheses, the string is
                # followed by a RPAR but its opening bracket is lambda's, not
                # the string's:
                #        "key": (lambda x: f"formatted: {x}"),
                opening_bracket = right_leaves[-1].opening_bracket
                if opening_bracket is not None and opening_bracket in left_leaves:
                    index = left_leaves.index(opening_bracket)
                    if (
                        0 < index < len(left_leaves) - 1
                        and left_leaves[index - 1].type == token.COLON
                        and left_leaves[index + 1].value == "lambda"
                    ):
                        right_leaves.pop()

            append_leaves(string_line, line, right_leaves)

        yield Ok(string_line)

        # --- Last Line
        last_line = line.clone()
        last_line.bracket_tracker = first_line.bracket_tracker

        new_rpar_leaf = Leaf(token.RPAR, ")")
        if old_rpar_leaf is not None:
            replace_child(old_rpar_leaf, new_rpar_leaf)
        else:
            insert_str_child(new_rpar_leaf)
        last_line.append(new_rpar_leaf)

        # If the target string ended with a comma, we place this comma to the
        # right of the RPAR on the last line.
        if ends_with_comma:
            comma_leaf = Leaf(token.COMMA, ",")
            replace_child(LL[comma_idx], comma_leaf)
            last_line.append(comma_leaf)

        yield Ok(last_line)


class StringParser:
    """
    A state machine that aids in parsing a string's "trailer", which can be
    either non-existent, an old-style formatting sequence (e.g. `% varX` or `%
    (varX, varY)`), or a method-call / attribute access (e.g. `.format(varX,
    varY)`).

    NOTE: A new StringParser object MUST be instantiated for each string
    trailer we need to parse.

    Examples:
        We shall assume that `line` equals the `Line` object that corresponds
        to the following line of python code:
        ```
        x = "Some {}.".format("String") + some_other_string
        ```

        Furthermore, we will assume that `string_idx` is some index such that:
        ```
        assert line.leaves[string_idx].value == "Some {}."
        ```

        The following code snippet then holds:
        ```
        string_parser = StringParser()
        idx = string_parser.parse(line.leaves, string_idx)
        assert line.leaves[idx].type == token.PLUS
        ```
    """

    DEFAULT_TOKEN: Final = 20210605

    # String Parser States
    START: Final = 1
    DOT: Final = 2
    NAME: Final = 3
    PERCENT: Final = 4
    SINGLE_FMT_ARG: Final = 5
    LPAR: Final = 6
    RPAR: Final = 7
    DONE: Final = 8

    # Lookup Table for Next State
    _goto: Final[dict[tuple[ParserState, NodeType], ParserState]] = {
        # A string trailer may start with '.' OR '%'.
        (START, token.DOT): DOT,
        (START, token.PERCENT): PERCENT,
        (START, DEFAULT_TOKEN): DONE,
        # A '.' MUST be followed by an attribute or method name.
        (DOT, token.NAME): NAME,
        # A method name MUST be followed by an '(', whereas an attribute name
        # is the last symbol in the string trailer.
        (NAME, token.LPAR): LPAR,
        (NAME, DEFAULT_TOKEN): DONE,
        # A '%' symbol can be followed by an '(' or a single argument (e.g. a
        # string or variable name).
        (PERCENT, token.LPAR): LPAR,
        (PERCENT, DEFAULT_TOKEN): SINGLE_FMT_ARG,
        # If a '%' symbol is followed by a single argument, that argument is
        # the last leaf in the string trailer.
        (SINGLE_FMT_ARG, DEFAULT_TOKEN): DONE,
        # If present, a ')' symbol is the last symbol in a string trailer.
        # (NOTE: LPARS and nested RPARS are not included in this lookup table,
        # since they are treated as a special case by the parsing logic in this
        # classes' implementation.)
        (RPAR, DEFAULT_TOKEN): DONE,
    }

    def __init__(self) -> None:
        self._state = self.START
        self._unmatched_lpars = 0

    def parse(self, leaves: list[Leaf], string_idx: int) -> int:
        """
        Pre-conditions:
            * @leaves[@string_idx].type == token.STRING

        Returns:
            The index directly after the last leaf which is a part of the string
            trailer, if a "trailer" exists.
            OR
            @string_idx + 1, if no string "trailer" exists.
        """
        assert leaves[string_idx].type == token.STRING

        idx = string_idx + 1
        while idx < len(leaves) and self._next_state(leaves[idx]):
            idx += 1
        return idx

    def _next_state(self, leaf: Leaf) -> bool:
        """
        Pre-conditions:
            * On the first call to this function, @leaf MUST be the leaf that
              was directly after the string leaf in question (e.g. if our target
              string is `line.leaves[i]` then the first call to this method must
              be `line.leaves[i + 1]`).
            * On the next call to this function, the leaf parameter passed in
              MUST be the leaf directly following @leaf.

        Returns:
            True iff @leaf is a part of the string's trailer.
        """
        # We ignore empty LPAR or RPAR leaves.
        if is_empty_par(leaf):
            return True

        next_token = leaf.type
        if next_token == token.LPAR:
            self._unmatched_lpars += 1

        current_state = self._state

        # The LPAR parser state is a special case. We will return True until we
        # find the matching RPAR token.
        if current_state == self.LPAR:
            if next_token == token.RPAR:
                self._unmatched_lpars -= 1
                if self._unmatched_lpars == 0:
                    self._state = self.RPAR
        # Otherwise, we use a lookup table to determine the next state.
        else:
            # If the lookup table matches the current state to the next
            # token, we use the lookup table.
            if (current_state, next_token) in self._goto:
                self._state = self._goto[current_state, next_token]
            else:
                # Otherwise, we check if a the current state was assigned a
                # default.
                if (current_state, self.DEFAULT_TOKEN) in self._goto:
                    self._state = self._goto[current_state, self.DEFAULT_TOKEN]
                # If no default has been assigned, then this parser has a logic
                # error.
                else:
                    raise RuntimeError(f"{self.__class__.__name__} LOGIC ERROR!")

            if self._state == self.DONE:
                return False

        return True


def insert_str_child_factory(string_leaf: Leaf) -> Callable[[LN], None]:
    """
    Factory for a convenience function that is used to orphan @string_leaf
    and then insert multiple new leaves into the same part of the node
    structure that @string_leaf had originally occupied.

    Examples:
        Let `string_leaf = Leaf(token.STRING, '"foo"')` and `N =
        string_leaf.parent`. Assume the node `N` has the following
        original structure:

        Node(
            expr_stmt, [
                Leaf(NAME, 'x'),
                Leaf(EQUAL, '='),
                Leaf(STRING, '"foo"'),
            ]
        )

        We then run the code snippet shown below.
        ```
        insert_str_child = insert_str_child_factory(string_leaf)

        lpar = Leaf(token.LPAR, '(')
        insert_str_child(lpar)

        bar = Leaf(token.STRING, '"bar"')
        insert_str_child(bar)

        rpar = Leaf(token.RPAR, ')')
        insert_str_child(rpar)
        ```

        After which point, it follows that `string_leaf.parent is None` and
        the node `N` now has the following structure:

        Node(
            expr_stmt, [
                Leaf(NAME, 'x'),
                Leaf(EQUAL, '='),
                Leaf(LPAR, '('),
                Leaf(STRING, '"bar"'),
                Leaf(RPAR, ')'),
            ]
        )
    """
    string_parent = string_leaf.parent
    string_child_idx = string_leaf.remove()

    def insert_str_child(child: LN) -> None:
        nonlocal string_child_idx

        assert string_parent is not None
        assert string_child_idx is not None

        string_parent.insert_child(string_child_idx, child)
        string_child_idx += 1

    return insert_str_child


def is_valid_index_factory(seq: Sequence[Any]) -> Callable[[int], bool]:
    """
    Examples:
        ```
        my_list = [1, 2, 3]

        is_valid_index = is_valid_index_factory(my_list)

        assert is_valid_index(0)
        assert is_valid_index(2)

        assert not is_valid_index(3)
        assert not is_valid_index(-1)
        ```
    """

    def is_valid_index(idx: int) -> bool:
        """
        Returns:
            True iff @idx is positive AND seq[@idx] does NOT raise an
            IndexError.
        """
        return 0 <= idx < len(seq)

    return is_valid_index
