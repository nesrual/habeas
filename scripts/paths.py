"""Keep repository data separate from git metadata and host files."""
from pathlib import Path
import os
import stat
import subprocess


def within(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path outside {root}: {name}")
    return path


def output_path(repo, name):
    path = Path(repo) / name
    if path.is_symlink() or (path.exists() and not path.is_dir()
                             and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError(f"unsafe output path: {path}")
    return within(repo, name)


def proof_artifact(repo, name):
    return within(within(repo, ".audit/runs"), Path(repo).resolve() / name)


def source_path(repo, name):
    path = within(repo, name)
    parts = path.relative_to(Path(repo).resolve()).parts
    if any(p.casefold() in {".git", ".audit"} for p in parts):
        raise ValueError(f"reserved repository path: {name}")
    return path


def audit_workspace(repo):
    """A checkout must not supply Habeas state, output links or run scripts."""
    audit = Path(repo) / ".audit"
    if audit.is_symlink():
        raise ValueError(".audit must be a local directory")
    if not audit.exists():
        return
    if (Path(repo) / ".git").exists():
        tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z", "--", ".audit"])
        if set(tracked.split(b"\0")) - {b"", b".audit/config.yaml"}:
            raise ValueError("repository-controlled .audit state or artifacts")
    for directory, dirs, files in os.walk(audit):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                raise ValueError(f"unexpected link or special file in audit workspace: {path}")


def linked_worktree(repo, cwd):
    def rev(path, flag):
        return Path(subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", flag],
            text=True, stderr=subprocess.DEVNULL).strip()).resolve()

    cwd = Path(cwd).resolve()
    try:
        return (cwd != Path(repo).resolve() and (cwd / ".git").is_file()
                and rev(cwd, "--show-toplevel") == cwd
                and rev(cwd, "--git-common-dir") == rev(repo, "--git-common-dir")
                and rev(cwd, "--git-dir").parent == rev(cwd, "--git-common-dir") / "worktrees"
                and Path((rev(cwd, "--git-dir") / "gitdir").read_text().strip()).resolve()
                == cwd / ".git")
    except (OSError, subprocess.CalledProcessError):
        return False


if __name__ == "__main__":
    import sys
    try:
        if sys.argv[1] == "worktree":
            sys.exit(0 if linked_worktree(*sys.argv[2:]) else 1)
        elif sys.argv[1] == "audit":
            audit_workspace(sys.argv[2])
        else:
            print(source_path(*sys.argv[2:]))
    except (OSError, ValueError, subprocess.CalledProcessError) as err:
        sys.exit(str(err))
