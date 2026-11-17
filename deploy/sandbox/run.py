"""Container-only entry point; stdout carries the result document to the executor."""

import base64
import json
import os
import resource
import stat
import subprocess
import sys
from pathlib import Path

LIMIT = 50 * 1024 * 1024
root = Path("/workspace")
outputs = root / "output"
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".csv", ".txt", ".pdf", ".xlsx", ".pptx", ".docx"}


def child_limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def collect_artifacts():
    files = []
    remaining = LIMIT
    if not outputs.is_dir() or outputs.is_symlink():
        return files
    for path in sorted(outputs.iterdir())[:30]:
        if path.suffix.lower() not in EXTENSIONS:
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= remaining:
                continue
            content = file.read(remaining + 1)
            if len(content) > remaining:
                break
            remaining -= len(content)
            files.append({"name": path.name, "content": base64.b64encode(content).decode("ascii")})
    return files


def main():
    data = json.load(sys.stdin)
    language = data["language"]
    executable, suffix = {"python": ("python", "py"), "javascript": ("node", "mjs")}[language]
    script = root / f"program.{suffix}"
    outputs.mkdir()
    script.write_text(data["code"], encoding="utf-8")
    with (root / "execution.log").open("w+b") as log:
        child = subprocess.run(
            [executable, str(script)], stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=55, check=False, preexec_fn=child_limits,
        )
        log.seek(0)
        output = log.read(8000).decode("utf-8", errors="replace")
    artifacts = collect_artifacts() if child.returncode == 0 else []
    json.dump({"exitCode": child.returncode, "output": output, "artifacts": artifacts}, sys.stdout)


if __name__ == "__main__":
    main()
