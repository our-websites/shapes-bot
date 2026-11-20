"""How this process is doing: uptime, memory, CPU. No dependencies (reads /proc on Linux, falls back on the standard library).
The same file lives in the Yapin' bot, the Discord bot and the Shapes API, so /ping can show all three side by side."""
import os
import threading
import time

_BOOT = time.time()
_last = {"wall": time.time(), "cpu": time.process_time()}
_lock = threading.Lock()


def _started_at():
    """When the process started (not when this file was imported), so a restart in place still counts from the restart."""
    try:
        with open("/proc/self/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        return time.time() - (up - ticks / os.sysconf("SC_CLK_TCK"))
    except Exception:
        return _BOOT


STARTED_AT = _started_at()


def _memory_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return None


def _memory_limit_mb():
    """The container's memory limit, when it has one."""
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
            if v.isdigit() and int(v) < 1 << 50:
                return int(v) / 1048576
        except Exception:
            continue
    return None


def _cpu_percent():
    """Share of one CPU core used since the last call (since start the first time). Over 100 means more than one core."""
    with _lock:
        now, cpu = time.time(), time.process_time()
        dt, dc = now - _last["wall"], cpu - _last["cpu"]
        if dt < 1:                      # too soon to be meaningful: use the average since start
            dt, dc = now - STARTED_AT, cpu
        else:
            _last["wall"], _last["cpu"] = now, cpu
    return max(0.0, dc / dt * 100) if dt > 0 else 0.0


def snapshot():
    return {"uptime": max(0, int(time.time() - STARTED_AT)), "memory_mb": _memory_mb(), "memory_limit_mb": _memory_limit_mb(),
            "cpu": round(_cpu_percent(), 1), "threads": threading.active_count()}


def human_uptime(seconds):
    seconds = int(seconds or 0)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    mi, s = divmod(r, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h or d else "", f"{mi}m" if mi or h or d else "", f"{s}s") if p)


def describe(snap):
    """One readable block of lines for a stats dict (from snapshot() or from another service)."""
    if not snap:
        return "Not responding"
    mem = snap.get("memory_mb")
    lim = snap.get("memory_limit_mb")
    mem_s = "?" if mem is None else (f"{mem:.0f} / {lim:.0f} MB" if lim else f"{mem:.0f} MB")
    return f"Uptime: `{human_uptime(snap.get('uptime'))}`\nMemory: `{mem_s}`\nCPU: `{snap.get('cpu', 0):.1f}%`"
