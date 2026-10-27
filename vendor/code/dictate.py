#!/usr/bin/env python3
"""Dictate a reply for Talpy Den: your voice to text, on this computer
only (whisper.cpp), for the chat pane's reply box.

    python3 -I dictate.py --status
        whether it can: {"ok", "tool" (whisper.cpp's whisper-cli), "model",
        "recorder", "models" (the ones you can download), "missing" (what to
        install)}
    python3 -I dictate.py --start ID
        start recording (the microphone, until --stop or --cancel; at most
        five minutes) into the widget's private cache
    python3 -I dictate.py --stop ID [--language L]
        stop and write it down: {"ok", "text"}; the recording is deleted
    python3 -I dictate.py --cancel ID
        stop, nothing written, the recording deleted
    python3 -I dictate.py --get-model NAME
        download a speech model (from huggingface.co/ggerganov/whisper.cpp,
        the only time anything goes over the network) into
        ~/.local/share/talpy-den/whisper

Speech never leaves the computer: whisper.cpp runs here. Standard library only.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TOOLS = ("whisper-cli", "whisper-cpp", "whisper.cpp")
# Models you can download: name -> (file, MB); base: fast, good for short replies
MODELS = {"base": ("ggml-base.bin", 142), "small": ("ggml-small.bin", 466), "base.en": ("ggml-base.en.bin", 142)}
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
LONGEST_S = 300
VALID_ID = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "talpy-den" / "whisper"


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = Path(base) / "talpy-den" / "dictation"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)   # (what you said)
    return path


def find_tool():
    return next((shutil.which(t) for t in TOOLS if shutil.which(t)), None)


def find_model():
    """A speech model: $WHISPER_MODEL, else the best one downloaded here or
    where whisper.cpp keeps them."""
    env = os.environ.get("WHISPER_MODEL")
    if env and os.path.isfile(env):
        return env
    folders = [data_dir(), Path.home() / ".local/share/whisper.cpp/models", Path("/usr/share/whisper.cpp/models"),
               Path("/usr/local/share/whisper.cpp/models")]
    for name in ("ggml-small.bin", "ggml-base.bin", "ggml-base.en.bin", "ggml-small.en.bin", "ggml-medium.bin",
                 "ggml-tiny.bin", "ggml-tiny.en.bin"):
        for folder in folders:
            if (folder / name).is_file():
                return str(folder / name)
    return None


def recorder(wav):
    """A command that records the microphone into wav (16 kHz mono, as
    whisper.cpp wants it), or None."""
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(wav)]
    if shutil.which("parecord"):
        return ["parecord", "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav", str(wav)]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav", str(wav)]
    return None


def status():
    tool, model = find_tool(), find_model()
    rec = recorder("x.wav")
    missing = []
    if tool is None:
        missing.append("whisper.cpp (Fedora: sudo dnf install whisper-cpp; elsewhere: build it from github.com/ggml-org/whisper.cpp)")
    if rec is None:
        missing.append("a recorder (pw-record, from PipeWire)")
    return {"ok": tool is not None and model is not None and rec is not None, "tool": tool, "model": model,
            "recorder": rec[0] if rec else None, "missing": missing,
            "models": [{"name": n, "file": f, "mb": mb} for n, (f, mb) in MODELS.items()]}


def paths(rid):
    if not rid or set(rid) - VALID_ID or len(rid) > 64:
        raise ValueError("Not a recording: " + str(rid))
    folder = cache_dir()
    return folder / (rid + ".wav"), folder / (rid + ".pid")


def start(rid):
    wav, pid_file = paths(rid)
    cmd = recorder(wav)
    if cmd is None:
        return {"ok": False, "message": "To dictate, install PipeWire's pw-record (or arecord)"}
    # (its own session: the widget's command ends now, the recording goes on; at most LONGEST_S)
    proc = subprocess.Popen(["timeout", "--signal=INT", str(LONGEST_S)] + cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    pid_file.write_text(str(proc.pid))
    time.sleep(0.3)
    if proc.poll() is not None:
        return {"ok": False, "message": "The microphone didn't start recording"}
    return {"ok": True, "message": "Recording"}


def stop_recording(pid_file):
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return
    try:
        os.killpg(pid, signal.SIGINT)   # (the recorder closes its file properly)
    except OSError:
        pass
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pid_file.unlink()
    except OSError:
        pass


def stop(rid, language="auto"):
    wav, pid_file = paths(rid)
    stop_recording(pid_file)
    try:
        tool, model = find_tool(), find_model()
        if tool is None or model is None:
            return {"ok": False, "message": "To write it down, install whisper.cpp and a speech model"}
        if not wav.is_file() or wav.stat().st_size < 3200:   # (under a tenth of a second)
            return {"ok": False, "message": "Nothing was recorded"}
        threads = str(max(1, min(8, (os.cpu_count() or 2) - 1)))
        try:
            r = subprocess.run([tool, "-m", model, "-f", str(wav), "-nt", "-np", "-l", language or "auto", "-t", threads],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "message": f"whisper.cpp didn't finish: {exc}"}
        if r.returncode != 0:
            return {"ok": False, "message": "whisper.cpp: " + (r.stderr.decode(errors="replace").strip().splitlines() or ["failed"])[-1]}
        # (whisper.cpp's marks for silence and sounds: [BLANK_AUDIO], (wind) …)
        lines = [line.strip() for line in r.stdout.decode("utf-8", "replace").splitlines()]
        text = " ".join(line for line in lines if line and not re.fullmatch(r"[\[(][^\])]*[\])]", line))
        return {"ok": bool(text), "text": text, "message": "" if text else "Nothing was said"}
    finally:
        try:
            wav.unlink()
        except OSError:
            pass


def cancel(rid):
    wav, pid_file = paths(rid)
    stop_recording(pid_file)
    try:
        wav.unlink()
    except OSError:
        pass
    return {"ok": True, "message": "Not written down"}


def get_model(name):
    if name not in MODELS:
        return {"ok": False, "message": "Not a model: " + str(name)}
    file, mb = MODELS[name]
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target, part = folder / file, folder / (file + ".part")
    try:
        with urllib.request.urlopen(MODEL_URL + file, timeout=60) as r, open(part, "wb") as out:
            shutil.copyfileobj(r, out, 1 << 20)
        if part.stat().st_size < mb * 2 ** 20 * 0.9:
            raise OSError("the download was cut short")
        os.replace(part, target)
    except (OSError, ValueError) as exc:
        try:
            part.unlink()
        except OSError:
            pass
        return {"ok": False, "message": f"Couldn't download the {name} model: {exc}"}
    return {"ok": True, "model": str(target), "message": f"The {name} speech model is here"}


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    language = "auto"
    if "--language" in args:
        i = args.index("--language")
        language = args[i + 1] if i + 1 < len(args) else "auto"
        del args[i:i + 2]
    try:
        if args == ["--status"]:
            result = status()
        elif len(args) == 2 and args[0] == "--start":
            result = start(args[1])
        elif len(args) == 2 and args[0] == "--stop":
            result = stop(args[1], language)
        elif len(args) == 2 and args[0] == "--cancel":
            result = cancel(args[1])
        elif len(args) == 2 and args[0] == "--get-model":
            result = get_model(args[1])
        else:
            sys.exit(__doc__)
    except ValueError as exc:
        result = {"ok": False, "message": str(exc)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
