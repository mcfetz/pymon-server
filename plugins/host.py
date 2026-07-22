#!/usr/bin/env python3
"""host.py — System information. No external deps."""
import json, platform, socket, sys, os




__schema__ = {'label': 'Host', 'description': 'System information (hostname, OS, CPU, RAM)', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 300, 'min': 5}]}

if __name__ == "__main__":
    config = json.load(sys.stdin)

    hostname = platform.node()
    os_name = platform.system()
    os_version = platform.version()

    uptime = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.readline().split()[0])
    except OSError:
        pass

    total_ram = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_ram = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass

    cpu_count = os.cpu_count() or 0
    cpu_model = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":")[1].strip()
                    break
    except OSError:
        pass

    swap_total = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("SwapTotal:"):
                    swap_total = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass

    metrics = {
        "hostname": hostname,
        "uptime": uptime,
        "os": os_name,
        "os_version": os_version,
        "total_ram": total_ram,
        "cpu_count": cpu_count,
        "cpu_model": cpu_model,
        "swap_total": swap_total,
    }

    # IP addresses per interface
    try:
        import fcntl, struct  # linux-specific
        SIOCGIFADDR = 0x8915
        with open("/proc/net/dev") as f:
            f.readline()
            f.readline()
            for line in f:
                ifname = line.strip().split(":")[0].strip()
                if ifname == "lo":
                    continue
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    ifr = struct.pack("16sH14s", ifname.encode()[:15], socket.AF_INET, b"\x00" * 14)
                    addr = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, ifr)
                    ip = socket.inet_ntoa(addr[20:24])
                    metrics[f"ip:{ifname}"] = ip
                    sock.close()
                except OSError:
                    pass
    except Exception:
        pass

    print(json.dumps(metrics))