"""Run an isolated Prometheus scrape benchmark and record measured results."""
import argparse
import json
import math
import random
import statistics
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

DEFAULT_HOSTS = 500
METRICS = (
    "cpu_percent", "memory_percent", "disk_percent", "temperature_celsius",
    "fan_rpm", "fan_alarm", "temperature_alarm", "power_watts", "uptime_seconds",
    "load_average", "read_iops", "write_iops", "rx_bytes_per_second",
    "tx_bytes_per_second", "http_seconds", "dns_seconds", "voltage_volts",
    "inlet_celsius", "outlet_celsius", "service_up",
)
INTERVAL = 30
START = time.monotonic()

QUERIES = {
    "fleet_series": 'count({__name__=~"fleet_.+"})',
    "rack_cpu_5m": "avg by (rack) (avg_over_time(fleet_cpu_percent[5m]))",
    "temperature_max": "max by (rack) (fleet_temperature_celsius)",
}

def payload(hardware_events, hosts):
    tick = int((time.monotonic() - START) / INTERVAL)
    rng = random.Random(tick)
    lines = []
    for host in range(hosts):
        hot = hardware_events and tick % 10 in (3, 4) and host < 20
        fan = hardware_events and tick % 10 in (6, 7) and 20 <= host < 40
        values = (
            rng.uniform(5, 85), rng.uniform(35, 85), 35 + host % 50,
            rng.uniform(82, 95) if hot else rng.uniform(42, 66),
            0 if fan else 2400 + host % 100, int(fan), int(hot),
            rng.uniform(110, 225), 86400 + tick * INTERVAL, rng.uniform(0, 3),
            rng.randrange(100, 400), rng.randrange(40, 150),
            rng.randrange(10000, 90000), rng.randrange(5000, 50000),
            rng.uniform(.02, .15), rng.uniform(.001, .02), 12,
            rng.uniform(18, 24), rng.uniform(25, 34), 1,
        )
        for metric, value in zip(METRICS, values):
            lines.append(f'fleet_{metric}{{host="sim-{host:03d}",rack="rack-{host // 25:02d}"}} {value:.6g}\n')
    return "".join(lines).encode()


def request(base, expression, evaluated_at=None):
    params = {"query": expression}
    if evaluated_at is not None:
        params["time"] = evaluated_at.isoformat()
    query = urlencode(params)
    with urlopen(f"{base}/api/v1/query?{query}", timeout=15) as response:
        return json.load(response)["data"]["result"]


def p95(values):
    return sorted(values)[math.ceil(.95 * len(values)) - 1]


def summarize(base, started, duration, finished, measurements, hardware_events, hosts=DEFAULT_HOSTS):
    end = started + timedelta(seconds=duration)
    counts = []
    # Count each metric separately so PromQL's removal of the metric name
    # cannot make series from different metrics share the same labels.
    for metric in METRICS:
        rows = request(base, f"count_over_time(fleet_{metric}[{duration}s])", end)
        counts.extend(int(row["value"][1]) for row in rows)
    expected_series = hosts * len(METRICS)
    expected = expected_series * (duration // INTERVAL)
    observed = sum(counts)
    return {
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "measurement_window_seconds": duration,
        "observed_wall_seconds": (finished - started).total_seconds(),
        "hardware_events": hardware_events,
        "simulated_hosts": hosts,
        "expected_samples": expected, "observed_samples": observed,
        "expected_series": expected_series, "observed_series": len(counts),
        "min_samples_per_series": min(counts) if len(counts) == expected_series else 0,
        "max_samples_per_series": max(counts, default=0),
        "samples_per_second": observed / duration,
        "completeness_percent": 100 * observed / expected,
        "queries": {
            name: {"expression": QUERIES[name], "count": len(values),
                   "median_ms": statistics.median(values), "p95_ms": p95(values)}
            for name, values in measurements.items()
        },
    }


def run(args):
    expected_series = args.hosts * len(METRICS)

    class Exporter(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            data = payload(args.hardware_events, args.hosts)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    args.state.mkdir(parents=True, exist_ok=False)
    args.output.mkdir(parents=True, exist_ok=True)
    config = args.state / "prometheus.yml"
    base = f"http://127.0.0.1:{args.prom_port}"
    measurements = {name: [] for name in QUERIES}
    with ThreadingHTTPServer(("127.0.0.1", 0), Exporter) as server:
        port = server.server_port
        config.write_text(f"""global:
  scrape_interval: 30s
  scrape_timeout: 10s
scrape_configs:
  - job_name: fleet
    static_configs:
      - targets: ['127.0.0.1:{port}']
""", encoding="utf-8")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with (args.state / "prometheus.log").open("wb") as log:
            prom = subprocess.Popen([
                str(args.prometheus.resolve()), f"--config.file={config.resolve()}",
                f"--storage.tsdb.path={(args.state / 'data').resolve()}",
                f"--web.listen-address=127.0.0.1:{args.prom_port}",
            ], stdout=log, stderr=subprocess.STDOUT)
            try:
                warmup = time.monotonic() + 60
                while time.monotonic() < warmup:
                    if prom.poll() is not None:
                        raise RuntimeError("Prometheus exited. See prometheus.log")
                    try:
                        series = request(base, QUERIES["fleet_series"])
                        if series and float(series[0]["value"][1]) == expected_series:
                            break
                    except OSError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError(f"{expected_series:,} series did not become available")
                started = datetime.now(timezone.utc)
                start = time.monotonic()
                print(f"Benchmark started {started.isoformat()}", flush=True)
                with (args.output / "benchmark-query-times.jsonl").open("w", encoding="utf-8") as raw:
                    for step in range(args.minutes * 2):
                        time.sleep(max(0, start + (step + 1) * INTERVAL - time.monotonic()))
                        for name, expression in QUERIES.items():
                            before = time.perf_counter()
                            result = request(base, expression)
                            elapsed = 1000 * (time.perf_counter() - before)
                            if not result:
                                raise RuntimeError(f"Empty query result for {name}")
                            measurements[name].append(elapsed)
                            raw.write(json.dumps({
                                "utc": datetime.now(timezone.utc).isoformat(),
                                "query": name, "latency_ms": elapsed, "rows": len(result),
                            }) + "\n")
                        raw.flush()
                        print(f"{(step + 1) * INTERVAL}s elapsed", flush=True)
                duration = args.minutes * 60
                finished = datetime.now(timezone.utc)
                summary = summarize(base, started, duration, finished, measurements,
                                    args.hardware_events, args.hosts)
                (args.output / "benchmark-summary.json").write_text(
                    json.dumps(summary, indent=2), encoding="utf-8")
                print(json.dumps(summary, indent=2), flush=True)
            finally:
                prom.terminate()
                prom.wait(timeout=15)
                server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--hosts", type=int, default=DEFAULT_HOSTS)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--prom-port", type=int, default=19090)
    parser.add_argument("--hardware-events", action="store_true")
    arguments = parser.parse_args()
    if arguments.minutes < 1:
        parser.error("--minutes must be at least 1")
    if arguments.hosts < 1:
        parser.error("--hosts must be at least 1")
    run(arguments)
