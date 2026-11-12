"""Run paired alerting drills on two disposable Linux guests."""
import argparse
import contextlib
import json
import socket
import socketserver
import statistics
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from benchmark import p95, request

PROM = "http://127.0.0.1:19091"
AM = "http://127.0.0.1:19093"
GRAFANA = "http://127.0.0.1:13000"
GUESTS = {"linux-01": 19100, "linux-02": 19101}
RULES = {
    "service": "lab_service_up == 0",
    "disk": "lab_disk_used_percent >= 80",
    "dns": "lab_dns_up == 0",
    "connectivity": 'up{job="linux_lab"} == 0',
}
DEPENDENCY = {"service": "local-http", "disk": "local-scratch",
              "dns": "shared-dns", "connectivity": "local-nic"}
MODES = ("per-host", "grouped")
STOP_SERVICE = 'kill "$(cat /run/service.pid)"'
START_SERVICE = "httpd -f -p 8080 -h /web </dev/null >/dev/null 2>&1 & echo $! > /run/service.pid"
FILL = "dd if=/dev/zero of=/scratch/lab-fill bs=1M count=7"
CLEAR = "rm -f /scratch/lab-fill"
HEALTH = (
    "uname -sr; ip link show eth0; df -P /scratch; "
    "timeout 2 nslookup -type=A health.fleet.test 10.0.2.2; "
    "wget -T 2 -q -O - http://127.0.0.1:8080/"
)
DIAGNOSE = {
    "service": "ps; wget -T 2 -O - http://127.0.0.1:8080/",
    "disk": "df -P /scratch; du -k /scratch",
    "dns": "cat /etc/resolv.conf; timeout 2 nslookup -type=A health.fleet.test 10.0.2.2",
    "connectivity": "ip link show eth0; ip addr show eth0",
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def until(check, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except OSError:
            pass
        time.sleep(.25)
    raise TimeoutError(description)


def fetch(url):
    with urlopen(url, timeout=3) as response:
        return response.read()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class Lab:
    def __init__(self, args):
        self.args = args
        self.stack = contextlib.ExitStack()
        self.notifications = []
        self.lock = threading.Lock()
        self.dns_online = threading.Event()
        self.dns_online.set()
        self.guests = {}

    def spawn(self, name, args, env=None):
        log = self.stack.enter_context((self.args.state / f"{name}.log").open("wb"))
        proc = subprocess.Popen(
            list(map(str, args)), stdout=log, stderr=subprocess.STDOUT,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.stack.callback(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)

    def servers(self):
        lab = self

        class DNS(socketserver.BaseRequestHandler):
            def handle(self):
                packet, conn = self.request
                if not lab.dns_online.is_set() or len(packet) < 17:
                    return
                name = b"\x06health\x05fleet\x04test\x00"
                question = name + b"\x00\x01\x00\x01"
                if not packet[12:].lower().startswith(question):
                    return
                header = packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
                answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4)
                conn.sendto(header + question + answer + socket.inet_aton("192.0.2.10"),
                            self.client_address)

        class Webhook(BaseHTTPRequestHandler):
            def do_POST(self):
                mode = self.path.removeprefix("/")
                if mode not in MODES:
                    self.send_error(404)
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                event = {"mode": mode, "utc": utc(), "monotonic": time.monotonic(),
                         "payload": payload}
                with lab.lock:
                    lab.notifications.append(event)
                    with (lab.args.output / "notifications.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps(event) + "\n")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        # QEMU's 10.0.2.2 host alias forwards to loopback.
        for server in (
            socketserver.UDPServer(("127.0.0.1", 53), DNS),
            ThreadingHTTPServer(("127.0.0.1", 19094), Webhook),
        ):
            self.stack.enter_context(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.stack.callback(server.shutdown)

    def boot(self):
        for index, (name, port) in enumerate(GUESTS.items()):
            serial, qmp = 19200 + index, 19300 + index
            proc = self.spawn(name, [
                self.args.qemu, "-m", "192", "-smp", "1", "-display", "none",
                "-monitor", "none", "-no-reboot", "-kernel", self.args.kernel,
                "-initrd", self.args.initrd,
                "-append", f"console=ttyS0 rdinit=/lab-init hostname={name} quiet",
                "-serial", f"tcp:127.0.0.1:{serial},server=on,wait=on",
                "-qmp", f"tcp:127.0.0.1:{qmp},server=on,wait=off",
                "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:9100",
                "-device", "e1000,id=nic0,netdev=net0",
            ])
            serial_socket = until(lambda: socket.create_connection(("127.0.0.1", serial), 1),
                                  20, f"{name} serial connection failed")
            self.stack.enter_context(serial_socket)
            serial_socket.settimeout(40)
            serial_stream = self.stack.enter_context(serial_socket.makefile("rb"))
            self.guests[name] = (serial_socket, serial_stream, qmp)
            while True:
                line = serial_stream.readline()
                if not line or proc.poll() is not None:
                    raise RuntimeError(f"{name} exited during boot")
                print(f"{name} boot: {line.decode(errors='replace').rstrip()}", flush=True)
                if line.strip() == b"LAB_READY":
                    break
            print(f"{name} {self.shell(name, 'uname -sr')}", flush=True)
            try:
                until(lambda: "lab_dns_up 1" in self.metrics(name), 35,
                      f"{name} DNS probe failed")
            except TimeoutError:
                print(f"{name} boot diagnostics: {self.shell(name, HEALTH)}", flush=True)
                raise

    def shell(self, name, command):
        conn, stream, _ = self.guests[name]
        conn.sendall((command + "; printf '\\nLAB_DONE:%s\\n' $?\n").encode())
        lines = []
        while True:
            raw = stream.readline()
            if not raw:
                raise ConnectionError(f"{name} serial closed")
            line = raw.decode(errors="replace").strip()
            if line.startswith("LAB_DONE:"):
                return {"command": command, "exit_code": int(line.split(":")[1]),
                        "output": "\n".join(lines)}
            if line:
                lines.append(line)

    def link(self, name, online):
        port = self.guests[name][2]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            with conn.makefile("rwb") as stream:
                json.loads(stream.readline())
                for command in (
                    {"execute": "qmp_capabilities"},
                    {"execute": "set_link", "arguments": {"name": "nic0", "up": online}},
                ):
                    stream.write(json.dumps(command).encode() + b"\n")
                    stream.flush()
                    while True:
                        response = json.loads(stream.readline())
                        if "return" in response:
                            break
                        if "error" in response:
                            raise RuntimeError(response["error"])
        return {"qmp": "set_link", "nic": "nic0", "up": online}

    def metrics(self, name):
        return fetch(f"http://127.0.0.1:{GUESTS[name]}/cgi-bin/metrics").decode()

    def monitoring(self):
        state = self.args.state.resolve()
        rules = [
            {"alert": "LabIncident", "expr": expression,
             "labels": {"mode": mode, "incident": incident, "dependency": DEPENDENCY[incident]}}
            for mode in MODES for incident, expression in RULES.items()
        ]
        write_json(state / "rules.yml", {"groups": [{"name": "lab", "rules": rules}]})
        write_json(state / "prometheus.yml", {
            "global": {"scrape_interval": "3s", "scrape_timeout": "2s", "evaluation_interval": "2s"},
            "rule_files": ["rules.yml"],
            "alerting": {"alertmanagers": [{"static_configs": [{"targets": ["127.0.0.1:19093"]}]}]},
            "scrape_configs": [{"job_name": "linux_lab", "metrics_path": "/cgi-bin/metrics",
                                "static_configs": [
                {"targets": [f"127.0.0.1:{port}"], "labels": {"host": name}}
                for name, port in GUESTS.items()
            ]}],
        })
        write_json(state / "alertmanager.yml", {
            "route": {
                "receiver": "grouped", "group_wait": "6s", "group_interval": "4s",
                "repeat_interval": "1h",
                "routes": [
                    {"receiver": "grouped", "matchers": ['mode="grouped"', 'incident="dns"'],
                     "group_by": ["mode", "incident", "dependency"]},
                    *[
                        {"receiver": mode, "matchers": [f'mode="{mode}"'],
                         "group_by": ["mode", "incident", "dependency", "host"]}
                        for mode in MODES
                    ],
                ],
            },
            "receivers": [
                {"name": mode, "webhook_configs": [
                    {"url": f"http://127.0.0.1:19094/{mode}", "send_resolved": True}
                ]}
                for mode in MODES
            ],
        })
        self.spawn("alertmanager", [self.args.alertmanager,
                                    f"--config.file={state / 'alertmanager.yml'}",
                                    f"--storage.path={state / 'alertmanager'}",
                                    "--web.listen-address=127.0.0.1:19093",
                                    "--cluster.listen-address="])
        self.spawn("prometheus", [self.args.prometheus,
                                  f"--config.file={state / 'prometheus.yml'}",
                                  f"--storage.tsdb.path={state / 'data'}",
                                  "--web.listen-address=127.0.0.1:19091"])
        until(lambda: fetch(AM + "/-/ready"), 20, "Alertmanager readiness")
        until(self.prom_healthy, 25, "Initial Prometheus recovery checks")
        if self.args.grafana:
            from dashboard import start_grafana
            start_grafana(self, GRAFANA, PROM)

    def prom_healthy(self):
        expressions = [
            'up{job="linux_lab"} == 1', "lab_service_up == 1",
            "lab_disk_used_percent < 80", "lab_dns_up == 1",
        ]
        return all({row["metric"]["host"] for row in request(PROM, expr)} == set(GUESTS)
                   for expr in expressions)

    def fault(self, incident, name, active):
        if incident == "service":
            return self.shell(name, STOP_SERVICE if active else START_SERVICE)
        if incident == "disk":
            return self.shell(name, FILL if active else CLEAR)
        if incident == "connectivity":
            return self.link(name, not active)
        if active:
            self.dns_online.clear()
        else:
            self.dns_online.set()
        return {"dependency": "shared UDP DNS listener", "responding": not active}

    def events(self, start, incident, status):
        with self.lock:
            return [event for event in self.notifications
                    if event["monotonic"] >= start and event["payload"]["status"] == status
                    and event["payload"]["groupLabels"].get("incident") == incident]

    @staticmethod
    def affected(events, mode):
        return {alert["labels"]["host"] for event in events if event["mode"] == mode
                for alert in event["payload"]["alerts"] if alert["status"] == "firing"}

    def trial(self, incident, repetition):
        affected = list(GUESTS) if incident == "dns" else [list(GUESTS)[(repetition - 1) % 2]]
        name = affected[0]
        started_utc = utc()
        start = time.monotonic()
        fault = self.fault(incident, name, True)
        try:
            diagnostics = {guest: self.shell(guest, DIAGNOSE[incident]) for guest in affected}
            if incident == "connectivity":
                try:
                    self.metrics(name)
                    diagnostics[name]["external_probe"] = "unexpected success"
                except OSError as exc:
                    diagnostics[name]["external_probe"] = str(exc)
            try:
                until(lambda: all(self.affected(self.events(start, incident, "firing"), mode)
                                  >= set(affected) for mode in MODES), 22, "Alert observation window")
            except TimeoutError:
                pass
        finally:
            recovery = self.fault(incident, name, False)
        until(self.prom_healthy, 25, "Prometheus recovery checks")
        checks = {}
        for guest in GUESTS:
            checks[guest] = self.shell(guest, HEALTH)
            metrics = self.metrics(guest)
            checks[guest]["metrics"] = metrics
            checks[guest]["passed"] = (
                checks[guest]["exit_code"] == 0
                and "fleet service healthy" in checks[guest]["output"]
                and "lab_service_up 1" in metrics
                and "lab_dns_up 1" in metrics
                and any(line.startswith("lab_disk_used_percent ")
                        and float(line.split()[1]) < 80 for line in metrics.splitlines())
            )
        until(lambda: not json.loads(fetch(AM + "/api/v2/alerts")), 25, "Alerts to clear")
        # Let the resolved notification flush and the Alertmanager group reset.
        time.sleep(5)
        firing = self.events(start, incident, "firing")
        results = {}
        for mode in MODES:
            events = [event for event in firing if event["mode"] == mode]
            covered = set()
            first = None
            for event in events:
                covered.update(self.affected([event], mode))
                if covered >= set(affected) and first is None:
                    first = event["monotonic"] - start
            results[mode] = {
                "actionable": first is not None, "detection_seconds": first,
                "notifications": len(events), "hosts_reported": sorted(covered),
            }
        return {
            "incident": incident, "repetition": repetition, "started_utc": started_utc,
            "finished_utc": utc(), "affected": affected, "fault": fault,
            "diagnostics": diagnostics, "recovery": recovery, "recovery_checks": checks,
            "alert_results": results,
        }


def summary(trials):
    result = {"trials": len(trials), "alerts": {}, "recovered_trials": sum(
        all(check["passed"] for check in trial["recovery_checks"].values()) for trial in trials)}
    for mode in MODES:
        rows = [trial["alert_results"][mode] for trial in trials]
        detected = [row["detection_seconds"] for row in rows if row["actionable"]]
        notifications = sum(row["notifications"] for row in rows)
        result["alerts"][mode] = {
            "actionable": len(detected), "missed": len(rows) - len(detected),
            "notifications": notifications, "notifications_per_incident": notifications / len(rows),
            "median_detection_seconds": statistics.median(detected) if detected else None,
            "p95_detection_seconds": p95(detected) if detected else None,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "kernel", "initrd", "prometheus", "alertmanager", "state", "output"):
        parser.add_argument(f"--{name}", required=True, type=lambda x: Path(x).resolve())
    parser.add_argument("--grafana", type=lambda x: Path(x).resolve())
    parser.add_argument("--repetitions", type=int, default=5, choices=range(1, 6))
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=False)
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "trials.jsonl").exists() or (args.output / "notifications.jsonl").exists():
        parser.error("Use a fresh output directory for drill evidence.")
    lab = Lab(args)
    trials = []
    with lab.stack:
        lab.servers()
        lab.boot()
        lab.monitoring()
        for repetition in range(1, args.repetitions + 1):
            for incident in RULES:
                trial = lab.trial(incident, repetition)
                trials.append(trial)
                with (args.output / "trials.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps(trial) + "\n")
                write_json(args.output / "trial-summary.json", summary(trials))
                print(f"{incident} #{repetition}: {trial['alert_results']}", flush=True)


if __name__ == "__main__":
    main()
