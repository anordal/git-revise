from subprocess import CalledProcessError
from typing import TYPE_CHECKING, List, Optional, Sequence

import pytest

from gitrevise.odb import Commit, Repository

from .conftest import bash, editor_main

if TYPE_CHECKING:
    from _typeshed import StrPath


def interactive_reorder_helper(repo: Repository, cwd: "StrPath") -> None:
    bash(
        """
        echo "hello, world" > file1
        git add file1
        git commit -m "commit one"

        echo "second file" > file2
        git add file2
        git commit -m "commit two"

        echo "new line!" >> file1
        git add file1
        git commit -m "commit three"
        """
    )

    prev = repo.get_commit("HEAD")
    prev_u = prev.parent()
    prev_uu = prev_u.parent()

    with editor_main(["-i", "HEAD~~"], cwd=cwd) as ed:
        with ed.next_file() as f:
            assert f.startswith_dedent(
                f"""\
                pick {prev.parent().oid.short()} commit two
                pick {prev.oid.short()} commit three
                """
            )
            f.replace_dedent(
                f"""\
                pick {prev.oid.short()} commit three
                pick {prev.parent().oid.short()} commit two
                """
            )

    curr = repo.get_commit("HEAD")
    curr_u = curr.parent()
    curr_uu = curr_u.parent()

    assert curr != prev
    assert curr.tree() == prev.tree()
    assert curr_u.message == prev.message
    assert curr.message == prev_u.message
    assert curr_uu == prev_uu

    assert b"file2" in prev_u.tree().entries
    assert b"file2" not in curr_u.tree().entries

    assert prev_u.tree().entries[b"file2"] == curr.tree().entries[b"file2"]
    assert prev_u.tree().entries[b"file1"] == curr_uu.tree().entries[b"file1"]
    assert prev.tree().entries[b"file1"] == curr_u.tree().entries[b"file1"]


def test_interactive_reorder(repo: Repository) -> None:
    interactive_reorder_helper(repo, cwd=repo.cwd)


def test_interactive_reorder_subdir(repo: Repository) -> None:
    bash("mkdir subdir")
    interactive_reorder_helper(repo, cwd=repo.cwd / "subdir")


def test_interactive_on_root(repo: Repository) -> None:
    bash(
        """
        echo "hello, world" > file1
        git add file1
        git commit -m "commit one"

        echo "second file" > file2
        git add file2
        git commit -m "commit two"

        echo "new line!" >> file1
        git add file1
        git commit -m "commit three"
        """
    )

    orig_commit3 = prev = repo.get_commit("HEAD")
    orig_commit2 = prev_u = prev.parent()
    orig_commit1 = prev_u.parent()

    index_tree = repo.index.tree()

    with editor_main(["-i", "--root"]) as ed:
        with ed.next_file() as f:
            assert f.startswith_dedent(
                f"""\
                pick {prev.parent().parent().oid.short()} commit one
                pick {prev.parent().oid.short()} commit two
                pick {prev.oid.short()} commit three
                """
            )
            f.replace_dedent(
                f"""\
                pick {prev.parent().oid.short()} commit two
                pick {prev.parent().parent().oid.short()} commit one
                pick {prev.oid.short()} commit three
                """
            )

    new_commit3 = curr = repo.get_commit("HEAD")
    new_commit1 = curr_u = curr.parent()
    new_commit2 = curr_u.parent()

    assert curr != prev
    assert curr.tree() == index_tree
    assert new_commit1.message == orig_commit1.message
    assert new_commit2.message == orig_commit2.message
    assert new_commit3.message == orig_commit3.message

    assert new_commit2.is_root
    assert new_commit1.parent() == new_commit2
    assert new_commit3.parent() == new_commit1

    assert new_commit1.tree().entries[b"file1"] == orig_commit1.tree().entries[b"file1"]
    assert new_commit2.tree().entries[b"file2"] == orig_commit2.tree().entries[b"file2"]
    assert new_commit3.tree() == orig_commit3.tree()


def test_interactive_fixup(repo: Repository) -> None:
    bash(
        """
        echo "hello, world" > file1
        git add file1
        git commit -m "commit one"

        echo "second file" > file2
        git add file2
        git commit -m "commit two"

        echo "new line!" >> file1
        git add file1
        git commit -m "commit three"

        echo "extra" >> file3
        git add file3
        """
    )

    prev = repo.get_commit("HEAD")
    prev_u = prev.parent()
    prev_uu = prev_u.parent()

    index_tree = repo.index.tree()

    with editor_main(["-i", "HEAD~~"]) as ed:
        with ed.next_file() as f:
            index = repo.index.commit()

            assert f.startswith_dedent(
                f"""\
                pick {prev.parent().oid.short()} commit two
                pick {prev.oid.short()} commit three
                # refs/heads/master
                index {index.oid.short()} <git index>
                """
            )
            f.replace_dedent(
                f"""\
                pick {prev.oid.short()} commit three
                fixup {index.oid.short()} <git index>
                pick {prev.parent().oid.short()} commit two
                # refs/heads/master
                """
            )

    curr = repo.get_commit("HEAD")
    curr_u = curr.parent()
    curr_uu = curr_u.parent()

    assert curr != prev
    assert curr.tree() == index_tree
    assert curr_u.message == prev.message
    assert curr.message == prev_u.message
    assert curr_uu == prev_uu

    assert b"file2" in prev_u.tree().entries
    assert b"file2" not in curr_u.tree().entries

    assert b"file3" not in prev.tree().entries
    assert b"file3" not in prev_u.tree().entries
    assert b"file3" not in prev_uu.tree().entries

    assert b"file3" in curr.tree().entries
    assert b"file3" in curr_u.tree().entries
    assert b"file3" not in curr_uu.tree().entries

    assert curr.tree().entries[b"file3"].blob().body == b"extra\n"
    assert curr_u.tree().entries[b"file3"].blob().body == b"extra\n"

    assert prev_u.tree().entries[b"file2"] == curr.tree().entries[b"file2"]
    assert prev_u.tree().entries[b"file1"] == curr_uu.tree().entries[b"file1"]
    assert prev.tree().entries[b"file1"] == curr_u.tree().entries[b"file1"]


@pytest.mark.parametrize(
    "rebase_config,revise_config,expected",
    [
        (None, None, False),
        ("1", "0", False),
        ("0", "1", True),
        ("1", None, True),
        (None, "1", True),
    ],
)
def test_autosquash_config(
    repo: Repository,
    rebase_config: Optional[str],
    revise_config: Optional[str],
    expected: bool,
) -> None:
    bash(
        """
        echo "hello, world" > file1
        git add file1
        git commit -m "commit one"

        echo "second file" > file2
        git add file2
        git commit -m "commit two"

        echo "new line!" >> file1
        git add file1
        git commit -m "commit three"

        echo "extra line" >> file2
        git add file2
        git commit --fixup=HEAD~
        """
    )

    if rebase_config is not None:
        bash(f"git config rebase.autoSquash '{rebase_config}'")
    if revise_config is not None:
        bash(f"git config revise.autoSquash '{revise_config}'")

    head = repo.get_commit("HEAD")
    headu = head.parent()
    headuu = headu.parent()

    disabled = f"""\
        pick {headuu.oid.short()} commit two
        pick {headu.oid.short()} commit three
        pick {head.oid.short()} fixup! commit two
        # refs/heads/master

        """
    enabled = f"""\
        pick {headuu.oid.short()} commit two
        fixup {head.oid.short()} fixup! commit two
        pick {headu.oid.short()} commit three

        """

    def subtest(args: Sequence[str], expected_todos: str) -> None:
        with editor_main((*args, "-i", "HEAD~3")) as ed:
            with ed.next_file() as f:
                assert f.startswith_dedent(expected_todos)
                f.replace_dedent(disabled)  # don't mutate state

        assert repo.get_commit("HEAD") == head

    subtest([], enabled if expected else disabled)
    subtest(["--autosquash"], enabled)
    subtest(["--no-autosquash"], disabled)


def test_interactive_reword(repo: Repository) -> None:
    bash(
        """
        echo "hello, world" > file1
        git add file1
        git commit -m "commit one" -m "extended1"

        echo "second file" > file2
        git add file2
        git commit -m "commit two" -m "extended2"

        echo "new line!" >> file1
        git add file1
        git commit -m "commit three" -m "extended3"
        """
    )

    prev = repo.get_commit("HEAD")
    prev_u = prev.parent()
    prev_uu = prev_u.parent()

    with editor_main(["-ie", "HEAD~~"]) as ed:
        with ed.next_file() as f:
            assert f.startswith_dedent(
                f"""\
                ++ pick {prev.parent().oid.short()}
                commit two

                extended2

                ++ pick {prev.oid.short()}
                commit three

                extended3
                """
            )
            f.replace_dedent(
                f"""\
                ++ pick {prev.oid.short()}
                updated commit three

                extended3 updated

                ++ pick {prev.parent().oid.short()}
                updated commit two

                extended2 updated
                """
            )

    curr = repo.get_commit("HEAD")
    curr_u = curr.parent()
    curr_uu = curr_u.parent()

    assert curr != prev
    assert curr.tree() == prev.tree()
    assert curr_u.message == b"updated commit three\n\nextended3 updated\n"
    assert curr.message == b"updated commit two\n\nextended2 updated\n"
    assert curr_uu == prev_uu

    assert b"file2" in prev_u.tree().entries
    assert b"file2" not in curr_u.tree().entries

    assert prev_u.tree().entries[b"file2"] == curr.tree().entries[b"file2"]
    assert prev_u.tree().entries[b"file1"] == curr_uu.tree().entries[b"file1"]
    assert prev.tree().entries[b"file1"] == curr_u.tree().entries[b"file1"]


@pytest.mark.parametrize("interactive_mode", ["-i", "-ie", "-e"])
def test_no_changes(repo: Repository, interactive_mode: str) -> None:
    bash("git commit --allow-empty -m empty")
    old = repo.get_commit("HEAD")
    assert old.message == b"empty\n"

    base = "--root" if interactive_mode != "-e" else "HEAD"

    outputs: List[bytes] = []
    with editor_main([interactive_mode, base], stdout_stderr_out=outputs) as ed:
        with ed.next_file() as f:
            f.replace_dedent(f.indata)

    normalized_outputs = [text.decode().replace("\r\n", "\n") for text in outputs]
    assert normalized_outputs == ["", "(warning) no changes performed\n"]
    new = repo.get_commit("HEAD")
    assert new.oid == old.oid


def test_editable_summary(repo: Repository) -> None:
    bash("git commit --allow-empty -m summary -m body")
    old = repo.get_commit("HEAD")
    assert old.message == b"summary\n\nbody\n"

    with editor_main(["-i", "--root"]) as ed:
        with ed.next_file() as f:
            lines = f.indata.splitlines()
            assert lines[0] == f"pick {old.oid.short()} summary".encode()
            lines[0] = f"pick {old.oid.short()} new summary".encode()
            f.replace_lines(lines)

    new = repo.get_commit("HEAD")
    assert new.message == b"new summary\n\nbody\n"


def test_bubbledrop(repo: Repository) -> None:
    bash(
        """
        echo "hello, world" > file1
        git add file1
        git commit -m "commit one"

        echo "second file" > file2
        git add file2
        git commit -m "commit two"

        echo "new line!" >> file1
        git add file1
        git commit -m "commit three"

        git commit --allow-empty -m "empty commit"
        git revert --no-edit HEAD~2
        git revert --no-edit HEAD~2

        echo "new line!" >> file2
        git add file2
        git commit -m "final state"
        """
    )

    def head(minus: int) -> Commit:
        commit = repo.get_commit("HEAD")
        for _ in range(minus):
            commit = commit.parent()
        return commit

    # There is now this bubble of commits that come back to the same tree.
    assert head(6).tree() == head(1).tree()

    # The user is allowed to drop bubbles, but all trees that come after shall be the same.
    holy_trees = [head(0).tree(), head(1).tree()]

    try:
        with editor_main(["--root", "-i"]) as ed:
            with ed.next_file() as f:
                lines = f.indata.splitlines()
                # Try dropping the wrong sequence
                lines = lines[:2] + lines[5:]
                f.replace_lines(lines)
        assert False, "Dropping a non-bubble shall be denied"
    except CalledProcessError:
        pass

    with editor_main(["--root", "-i"]) as ed:
        with ed.next_file() as f:
            lines = f.indata.splitlines()
            assert lines[:7] == [
                b"pick 594c1e1eee28 commit one",
                b"pick a6e7b8331024 commit two",
                b"pick 0e886bfadf4e commit three",
                b"pick f1b8a846a5fa empty commit",
                b'pick 19fbcbdfe198 Revert "commit two"',
                b'pick f58855baa550 Revert "commit three"',
                b"pick 0bfbcc8b272c final state",
            ]
            # Drop the bubble
            lines = lines[:1] + lines[6:]
            f.replace_lines(lines)

    assert [head(0).tree(), head(1).tree()] == holy_trees
