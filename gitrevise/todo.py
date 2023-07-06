from __future__ import annotations

import re
from collections import defaultdict
from enum import Enum
from typing import Iterable, List, Optional, Union

from .odb import Commit, MissingObject, Oid, Repository
from .utils import (
    cut_commit,
    decode_lossy,
    edit_commit_message,
    run_editor,
    run_sequence_editor,
    update_head,
)


class CommitAction(Enum):
    PICK = "pick"
    FIXUP = "fixup"
    SQUASH = "squash"
    REWORD = "reword"
    CUT = "cut"
    INDEX = "index"

    def __str__(self) -> str:
        return str(self.value)


class CommitlessAction(Enum):
    COMMENT = "#"
    UPDATE_REF = "update-ref"

    def __str__(self) -> str:
        return str(self.value)


def parse_action(instr: str) -> Union[CommitAction, CommitlessAction]:
    for enumeration in (CommitAction, CommitlessAction):
        for enumvalue in enumeration:
            if enumvalue.value.startswith(instr):
                # Mypy deduces too generally, to Enum instead of Union.
                return enumvalue  # type: ignore
    raise ValueError(
        f"Unrecognized action '{instr}'. Expected one of"
        " pick, fixup, squash, reword, cut, index, update-ref or #"
    )


class CommitStep:
    action: CommitAction
    commit: Commit
    message: bytes
    extrasteps: List[CommitlessStep]

    def __init__(self, action: CommitAction, commit: Commit) -> None:
        self.action = action
        self.commit = commit
        self.message = commit.message
        self.extrasteps = []

    def __str__(self) -> str:
        return f"{self.action} {self.commit.oid.short()}"

    def significant_extrasteps(self) -> Iterable[CommitlessStep]:
        return filter(lambda x: x.action != CommitlessAction.COMMENT, self.extrasteps)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CommitStep):
            return False
        return (
            self.action == other.action
            and self.commit == other.commit
            and self.message == other.message
            # Must collect into lists because comparing two filter objects is always false.
            and list(self.significant_extrasteps())
            == list(other.significant_extrasteps())
        )


class CommitlessStep:
    action: CommitlessAction
    operand: bytes

    def __init__(self, action: CommitlessAction, operand: bytes) -> None:
        self.action = action
        self.operand = operand

    def to_bytes(self) -> bytes:
        return f"{self.action} ".encode() + self.operand

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CommitlessStep):
            return False
        return self.action == other.action and self.operand == other.operand


def parse_step(repo: Repository, instr: bytes) -> Union[CommitStep, CommitlessStep]:
    parsed = re.match(
        rb"(?P<action>\S+)\s+(?P<operand>\S+)(\s+(?P<summary>.*))?$", instr
    )
    if not parsed:
        raise ValueError(
            f"todo entry '{decode_lossy(instr)}' must follow format"
            " <action> <operand> <optional summary>"
        )
    action = parse_action(decode_lossy(parsed.group("action")))
    operand = parsed.group("operand")

    if isinstance(action, CommitlessAction):
        return CommitlessStep(action, operand)

    step = CommitStep(action, repo.get_commit(decode_lossy(operand)))
    summary = parsed.group("summary")
    if summary is not None:
        step.message = step.commit.message_with_edited_summary(summary)
    return step


def build_todos(
    repo: Repository, commits: List[Commit], index: Optional[Commit]
) -> List[CommitStep]:
    steps = [CommitStep(CommitAction.PICK, commit) for commit in commits]
    if index:
        steps.append(CommitStep(CommitAction.INDEX, index))

    # Find refs for --update-refs
    commit_refs = defaultdict(list)
    for line in repo.git("show-ref", "--heads", "--tags").splitlines():
        (sha, refname) = line.split(b" ", maxsplit=1)
        commit_refs[Oid.fromhex(sha.decode())].append(refname)

    # Except those already checked out: They would require detaching.
    outchecked = set()
    for record in repo.git("worktree", "list", "--porcelain").split(b"\n\n"):
        for line in record.split(b"\n"):
            keyval = line.split(b" ", maxsplit=1)
            if keyval[0] == b"branch":
                outchecked.add(keyval[1])

    for step in steps:
        for ref in commit_refs.get(step.commit.oid, []):
            if ref.startswith(b"refs/heads/") and ref not in outchecked:
                action = CommitlessAction.UPDATE_REF
            else:
                action = CommitlessAction.COMMENT
            step.extrasteps.append(CommitlessStep(action, ref))
    return steps


def validate_todos(old: List[CommitStep], new: List[CommitStep]) -> None:
    """Raise an exception if the new todo list is malformed compared to the
    original todo list"""
    old_set = set(o.commit.oid for o in old)
    new_set = set(n.commit.oid for n in new)

    assert len(old_set) == len(old), "Unexpected duplicate original commit!"
    if len(new_set) != len(new):
        # XXX(nika): Perhaps print which commits are duplicates?
        raise ValueError("Unexpected duplicate commit found in todos")

    if new_set - old_set:
        # XXX(nika): Perhaps print which commits were found?
        raise ValueError("Unexpected commits not referenced in original TODO list")

    # Allow dropping empty commits and bubbles
    # (sequences that would be empty if squashed).
    dropped_list = [o.commit for o in old if o.commit.oid not in new_set]
    while dropped_list and len(dropped_list[0].parent_oids) == 1:
        parent_tree = dropped_list[0].parents()[0].tree_oid
        for index, commit in enumerate(dropped_list):
            if commit.tree_oid == parent_tree:
                dropped_list = dropped_list[index + 1 :]
                break
        else:
            break
    if dropped_list:
        raise ValueError(f"Can't drop nonempty commit {dropped_list[0].oid}")

    saw_index = False
    for step in new:
        if step.action == CommitAction.INDEX:
            saw_index = True
        elif saw_index:
            raise ValueError("'index' actions follow all non-index todo items")


def add_autosquash_step(step: CommitStep, picks: List[List[CommitStep]]) -> None:
    needle = summary = step.commit.summary()
    while needle.startswith(b"fixup! ") or needle.startswith(b"squash! "):
        needle = needle.split(maxsplit=1)[1]

    if needle != summary:
        if summary.startswith(b"fixup!"):
            new_step = CommitStep(CommitAction.FIXUP, step.commit)
        else:
            assert summary.startswith(b"squash!")
            new_step = CommitStep(CommitAction.SQUASH, step.commit)

        for seq in picks:
            if seq[0].commit.summary().startswith(needle):
                seq.append(new_step)
                return

        try:
            target = step.commit.repo.get_commit(needle.decode())
            for seq in picks:
                if any(s.commit == target for s in seq):
                    seq.append(new_step)
                    return
        except (ValueError, MissingObject):
            pass

    picks.append([step])


def autosquash_todos(todos: List[CommitStep]) -> List[CommitStep]:
    picks: List[List[CommitStep]] = []
    for step in todos:
        add_autosquash_step(step, picks)
    return [s for p in picks for s in p]


def edit_todos_msgedit(
    repo: Repository,
    todos: List[CommitStep],
    presentation_order_head_on_top: bool,
) -> List[Union[CommitStep, CommitlessStep]]:
    serialized_todos = []
    for step in todos:
        serialized_todos.append(f"{step}\n".encode() + step.commit.message)
        for extrastep in step.extrasteps:
            serialized_todos.append(extrastep.to_bytes())
    if presentation_order_head_on_top:
        serialized_todos.reverse()
    todos_text = b"".join(map(lambda x: b"++ " + x + b"\n", serialized_todos))

    # Invoke the editors to parse commit messages.
    response = run_editor(
        repo,
        "git-revise-todo",
        todos_text,
        comments=f"""\
        Interactive Revise Todos({len(todos)} commands)

        Commands:
         p, pick <commit> = use commit
         r, reword <commit> = use commit, but edit the commit message
         s, squash <commit> = use commit, but meld into previous commit
         f, fixup <commit> = like squash, but discard this commit's message
         c, cut <commit> = interactively split commit into two smaller commits
         i, index <commit> = leave commit changes staged, but uncommitted

        Each command block is prefixed by a '++' marker, followed by the command to
        run, the commit hash and after a newline the complete commit message until
        the next '++' marker or the end of the file.

        Commit messages will be reworded to match the provided message before the
        command is performed.

        These blocks are executed from top to bottom. They can be re-ordered and
        their commands can be changed, however the number of blocks must remain
        identical. If present, index blocks must be at the bottom of the list,
        i.e. they can not be followed by non-index blocks.


        If you remove everything, the revising process will be aborted.
        """,
    )

    # Minimal parsing into a common format
    polymorphic_steps = []
    for full in re.split(rb"^\+\+ ", response, flags=re.M)[1:]:
        cmd, message = full.split(b"\n", maxsplit=1)

        polystep = parse_step(repo, cmd.strip())
        if isinstance(polystep, CommitStep):
            # https://github.com/pylint-dev/pylint/issues/8900
            # pylint: disable=attribute-defined-outside-init
            polystep.message = message.strip() + b"\n"
        polymorphic_steps.append(polystep)
    return polymorphic_steps


def edit_todos_linewise(
    repo: Repository,
    todos: List[CommitStep],
    presentation_order_head_on_top: bool,
) -> List[Union[CommitStep, CommitlessStep]]:
    serialized_todos = []
    for step in todos:
        serialized_todos.append(f"{step} ".encode() + step.commit.summary())
        for extrastep in step.extrasteps:
            serialized_todos.append(extrastep.to_bytes())
    if presentation_order_head_on_top:
        serialized_todos.reverse()
    todos_text = b"".join(map(lambda x: x + b"\n", serialized_todos))

    response = run_sequence_editor(
        repo,
        "git-revise-todo",
        todos_text,
        comments=f"""\
        Interactive Revise Todos ({len(todos)} commands)

        Commands:
         p, pick <commit> = use commit
         r, reword <commit> = use commit, but edit the commit message
         s, squash <commit> = use commit, but meld into previous commit
         f, fixup <commit> = like squash, but discard this commit's log message
         c, cut <commit> = interactively split commit into two smaller commits
         i, index <commit> = leave commit changes staged, but uncommitted

        These lines are executed from top to bottom. They can be re-ordered and
        their commands can be changed, however the number of lines must remain
        identical. If present, index lines must be at the bottom of the list,
        i.e. they can not be followed by non-index lines.

        If you remove everything, the revising process will be aborted.
        """,
    )

    # Minimal parsing into a common format
    polymorphic_steps = []
    for line in response.splitlines():
        if line.isspace():
            continue
        polymorphic_steps.append(parse_step(repo, line.strip()))
    return polymorphic_steps


def edit_todos(
    repo: Repository,
    todos: List[CommitStep],
    msgedit: bool,
) -> List[CommitStep]:
    presentation_order_head_on_top = repo.bool_config(
        "sequence.presentation-order-head-on-top", default=False
    )

    if msgedit:
        polymorphic_steps = edit_todos_msgedit(
            repo, todos, presentation_order_head_on_top
        )
    else:
        polymorphic_steps = edit_todos_linewise(
            repo, todos, presentation_order_head_on_top
        )

    if presentation_order_head_on_top:
        polymorphic_steps.reverse()

    result = []
    for polystep in polymorphic_steps:
        if isinstance(polystep, CommitStep):
            result.append(polystep)
        elif isinstance(polystep, CommitlessStep):
            if len(result) == 0:
                raise ValueError("Actions can not be applied to the base commit")
            result[-1].extrasteps.append(polystep)
        else:
            raise ValueError("Unhandled Step type")

    validate_todos(todos, result)

    return result


def apply_todos(
    repo: Repository,
    current: Optional[Commit],
    todos_original: List[CommitStep],
    todos_edited: List[CommitStep],
    reauthor: bool = False,
) -> Commit:
    applied_old_commits = set()
    applied_new_commits = set()

    for known_state, step in zip(todos_original, todos_edited):
        # Avoid making the user resolve the same conflict twice:
        # When reordering commits, the final state is known.
        applied_old_commits.add(known_state.commit.oid)
        applied_new_commits.add(step.commit.oid)
        deja_vu = applied_new_commits == applied_old_commits
        tree_to_keep = known_state.commit.tree() if deja_vu else None

        rebased = step.commit.rebase(current, tree_to_keep).update(message=step.message)

        if step.action == CommitAction.PICK:
            current = rebased
        elif step.action == CommitAction.FIXUP:
            if current is None:
                raise ValueError("Cannot apply fixup as first commit")
            current = current.update(tree=rebased.tree())
        elif step.action == CommitAction.REWORD:
            current = edit_commit_message(rebased)
        elif step.action == CommitAction.SQUASH:
            if current is None:
                raise ValueError("Cannot apply squash as first commit")
            fused = current.message + b"\n\n" + rebased.message
            current = current.update(tree=rebased.tree(), message=fused)
            current = edit_commit_message(current)
        elif step.action == CommitAction.CUT:
            current = cut_commit(rebased)
        elif step.action == CommitAction.INDEX:
            break
        else:
            raise ValueError(f"Unhandled action: {step.action}")

        if reauthor:
            current = current.update(author=current.repo.default_author)

        for extrastep in step.significant_extrasteps():
            if extrastep.action == CommitlessAction.UPDATE_REF:
                update_head(repo.get_commit_ref(extrastep.operand), current, None)
            else:
                raise ValueError(f"Unhandled action: {extrastep.action}")

        print(
            f"{step.action.value:6} {current.oid.short()}  {decode_lossy(current.summary())}"
        )

    if current is None:
        raise ValueError("No commits introduced on top of root commit")

    return current
