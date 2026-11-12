# Lab results

## Live incident drills

On November 12, 2026, Prometheus monitored two QEMU guests running Linux 6.18.60. The runner injected each fault five times and sent the same alerts through both Alertmanager routes. Grafana successfully queried telemetry for both VMs. Disk pressure used a dedicated RAM-backed Linux filesystem.

| Measure | Per-host route | Shared-dependency route |
|---|---:|---:|
| Actionable incidents | 20/20 | 20/20 |
| Missed incidents | 0 | 0 |
| Notifications with a firing alert | 25 | 22 |
| Notifications per incident | 1.25 | 1.10 |
| Median detection | 8.96 s | 8.83 s |
| p95 detection | 10.88 s | 10.88 s |

Every trial passed the post-recovery checks on both guests, including Prometheus reachability, the HTTP response, scratch filesystem usage, and DNS resolution. The local webhook represented the operator inbox.

Each shared DNS outage produced an initial grouped notification that named both guests. The per-host route sent 10 firing notifications across the five DNS trials. The grouped route sent seven, a 30% reduction. In two trials, one guest recovered before the other, so Alertmanager also sent an update that contained one resolved and one firing alert. Those updates are included in the table. Fully resolved messages are recorded in the evidence and excluded from the firing counts.

In DNS trial 1, the initial grouped notification arrived 8.88 seconds after fault injection. Both guests timed out resolving `health.fleet.test` during the outage and resolved it after the shared responder returned. The trial record includes the guest commands and output.

[Trial records](evidence/trials.jsonl) · [Alertmanager webhooks](evidence/notifications.jsonl) · [Detection summary](evidence/trial-summary.json) · [Grafana query check](evidence/grafana-query-check.json) · [Grafana dashboard](evidence/grafana-dashboard.json)

## 500-host fleet benchmark

Prometheus stored **1,200,000 of 1,200,000 expected samples** across 10,000 series over 3600.07 seconds. The observed rate was **333.33 samples per second** with **100.00% completeness**. The per-series count ranged from 120 to 120.

The 30-second scrape ran against a single Python exporter containing 500 simulated host identities. The exporter generated simulated fan and temperature alarms. At the target rate, the workload projects to 28.8 million samples per day. The measured window was one hour.

| Query | Runs | Median | p95 |
|---|---:|---:|---:|
| Fleet series count | 120 | 25.54 ms | 45.22 ms |
| Five-minute CPU average by rack | 120 | 2.84 ms | 16.99 ms |
| Maximum temperature by rack | 120 | 2.53 ms | 23.47 ms |

Latency includes the local HTTP request and JSON decoding. The original completeness query failed because PromQL removed metric names from series that shared the same labels. The corrected query counted each metric separately at the original window end using Prometheus's saved data. A separate one-minute run validated the corrected benchmark from start to finish.

[Benchmark summary](evidence/benchmark-summary.json) · [Query measurements](evidence/benchmark-query-times.jsonl)

## 5,000-host scale check

A one-minute run at ten times the original fleet size stored all **200,000 expected samples** across **100,000 series**. Every series had two samples. The observed rate was **3,333 samples per second**. The six timed query calls took 30–90 ms. The five-minute CPU query used the data available during warmup and the one-minute run.

[Scale summary](evidence/scale-5000/benchmark-summary.json) · [Query measurements](evidence/scale-5000/benchmark-query-times.jsonl)
