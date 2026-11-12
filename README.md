# Server Fleet Incident Detection & Recovery Lab

When two servers lose DNS at the same time, can the on-call person tell that one shared service failed? This lab breaks a few things on purpose, checks what Alertmanager sends, and verifies that each affected machine works again.

The incident drills run on two Alpine Linux VMs in QEMU on Windows. Prometheus collects the metrics, Grafana shows what changed, and a local webhook records each alert. A separate Python exporter simulates larger fleets for the ingestion benchmark.

## What happened

- Both alert routes detected all 20 incidents across four fault types. Both VMs passed the recovery checks after every trial.
- Grouping by the shared DNS dependency cut DNS notifications from 10 to 7 across five outages. The first grouped alert named both VMs each time.
- At 500 simulated hosts, Prometheus stored all 1.2 million expected samples in an hour. A one-minute check at 5,000 simulated hosts stored all 200,000 expected samples.

The full write-up includes alert timing, query latency, and links to the raw logs. [Measured results](RESULTS.md)

## Run the lab

Use PowerShell on Windows x64 with Python 3.12, 7-Zip, and outbound HTTPS for setup. The setup script downloads checksum-pinned archives, extracts the tools under the project's `.lab-work` directory, and builds the guest initramfs. QEMU uses software emulation.

```powershell
.\Run-Lab.ps1 -Stage Setup
.\Run-Lab.ps1 -Stage Drills
.\Run-Lab.ps1 -Stage Benchmark
```

Run these commands from the directory containing `Run-Lab.ps1`. The last two commands can run in separate terminals. They write each rerun to its own timestamped directory under `evidence-rerun`. The live lab opens [Grafana](http://127.0.0.1:13000/d/fleet-incident-lab) during the drills and stops its processes when the run ends. It uses loopback ports 53 UDP, 13000, 19091, 19093–19094, 19100–19101, 19200–19201 and 19300–19301. The benchmark uses port 19090 and an ephemeral exporter port. For a smoke test, use `-Stage Drills -Repetitions 1` or `-Stage Benchmark -Minutes 1`.

## Live VM runbook

The serial console stays available when the guest NIC loses connectivity. The runner saves the executed commands, guest output, notification payloads and recovery checks for each trial in `evidence`.

| Drill | Fault and diagnosis | Restore and verify |
|---|---|---|
| HTTP service failure | Stop the service PID from `/run/service.pid`. Run `ps` and `wget -T 2 -O - http://127.0.0.1:8080/` from the guest. | Restart BusyBox HTTP on guest port 8080. Verify the response body and `lab_service_up 1`. |
| Disk pressure | Write 7 MiB to the guest's 8 MiB scratch tmpfs. Run `df -P /scratch` and `du -k /scratch`. | Remove `/scratch/lab-fill`. Verify filesystem usage returns below 80%. The scratch filesystem is RAM-backed. |
| Shared DNS failure | Make the lab-owned UDP DNS responder stop answering. Run `cat /etc/resolv.conf` and `timeout 2 nslookup -type=A health.fleet.test 10.0.2.2` on both guests. | Resume DNS replies. Verify name resolution and `lab_dns_up 1` on both guests. The test domain resolves to the documentation address `192.0.2.10`. |
| Lost host connectivity | Set the VM's emulated NIC link down with QEMU `set_link`. Inspect `ip link show eth0`, `ip addr show eth0` and the external metrics request. | Restore the link, confirm Prometheus `up=1`, and repeat the guest HTTP and DNS checks. |

## Comparison and benchmark method

The experiment pairs both alerting configurations on each of 20 live fault injections, with five repetitions per type. The baseline groups by host. The other route groups DNS alerts by the shared dependency and keeps host-local alerts grouped by host. Both routes use the same rules and six-second Alertmanager group wait. A notification is counted when the local receiver records a firing webhook. Detection time is measured from fault injection to receipt of enough firing notifications to cover every expected affected host. The alert observation window is 22 seconds after diagnosis. Median and nearest-rank p95 use detected incidents. Resolved webhooks are retained as evidence.

By default, the benchmark scrapes 500 virtual identities with 20 series per identity every 30 seconds. That is 10,000 series and 1.2 million expected samples over one hour. Completeness counts stored `fleet_*` samples from Prometheus, excluding its internal scrape metrics. Three benchmark queries run every 30 seconds, and latency includes the local HTTP round trip and JSON decoding. Optional fan and temperature events are synthesized in Python and inspired by Redfish equipment telemetry. The physical SSD task was outside this workspace run.

Scale the simulator with `-HostCount`. The scrape interval stays at 30 seconds and each simulated host exports 20 series. For a one-minute test at ten times the default size, run:

```powershell
.\Run-Lab.ps1 -Stage Benchmark -HostCount 5000 -Minutes 1
```

This changes the number of identities served by one exporter. The live incident drills still use two Linux VMs.

## Evidence

[Measured results](RESULTS.md) include the alert comparison and fleet benchmark.

- `trials.jsonl` contains paired trials, diagnostic commands and both guests' recovery checks.
- `notifications.jsonl` contains the original Alertmanager firing and resolved webhook payloads with receipt times.
- `trial-summary.json` summarizes detection, notification counts and recovery.
- `grafana-dashboard.json` and `grafana-datasource-health.json` contain the dashboard and datasource check.
- `benchmark-query-times.jsonl` contains query measurements, and `benchmark-summary.json` contains the one-hour summary.

References include [Prometheus storage guidance](https://prometheus.io/docs/prometheus/latest/storage/), [Open Compute Project data center telemetry](https://www.opencompute.org/projects/data-center-facility/data-center-telemetry/), and [DMTF Redfish mockups](https://redfish.dmtf.org/redfish/mockups/v1).
