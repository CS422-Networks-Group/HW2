# CS 422 HW2: iPerf, TCP Statistics, and Congestion Control

## Running

Must run on Linux or WSL2 (`TCP_INFO` / `TCP_CONGESTION` are Linux socket options).

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/main.py [input_csv] [--n 10] [--time 60] [--interval 1.0] [--max-attempts 30] \
                    [--out results] [--representative IP:PORT]
```

| option | meaning |
|---|---|
| `input_csv` | server list with `IP/HOST` and `PORT` columns (default `data/listed_iperf3_servers.csv`) |
| `--n` | number of destinations that must complete a test |
| `--time` | transfer duration per destination (s) |
| `--interval` | sampling interval for goodput and TCP stats (s) |
| `--max-attempts` | max servers tried before reporting a shortfall |
| `--out` | output directory |
| `--representative` | destination used for the TCP stats plots (default: the one whose mean goodput is the median across destinations) |

### Outputs

| file | contents |
|---|---|
| `tcp_stats.csv` | one row per sample: run id, destination, algorithm, goodput, and decoded TCP stats |
| `goodput.csv` | goodput-only view of the same samples |
| `summary.csv` | min / median / mean / p95 goodput (Mbit/s) per destination and algorithm |
| `run_metadata.json` | session id, host kernel, duration, interval, and every destination attempted (ok / failed + error) |
| `goodput.pdf` | goodput over time, one panel per destination |
| `tcp_stats_timeseries.pdf` | Q2(b)(i): snd_cwnd, RTT, loss proxy, goodput over time for the representative destination |
| `tcp_stats_scatter.pdf` | Q2(b)(ii): snd_cwnd, srtt, retransmissions vs goodput, colored by time |

## TCP stats tracing (Q2)

### Decoding format

All stats come from `getsockopt(IPPROTO_TCP, TCP_INFO)` on the **data** socket (never the control socket).
The buffer is decoded as Linux `struct tcp_info` (`include/uapi/linux/tcp.h`) with the `struct` format

```
TCP_INFO_FMT = "=8B24I4Q6IQ3Q2I2Q2I2I"   # 232 bytes, kernel >= 5.4
```

| format | fields |
|---|---|
| `8B` | state, ca_state, retransmits, probes, backoff, options, wscale (snd:4/rcv:4), flags (bit 0 = delivery_rate_app_limited) |
| `24I` | rto, ato, snd_mss, rcv_mss, unacked, sacked, lost, retrans, fackets, last_data_sent, last_ack_sent, last_data_recv, last_ack_recv, pmtu, rcv_ssthresh, rtt, rttvar, snd_ssthresh, snd_cwnd, advmss, reordering, rcv_rtt, rcv_space, total_retrans |
| `4Q` | pacing_rate, max_pacing_rate, bytes_acked, bytes_received |
| `6I` | segs_out, segs_in, notsent_bytes, min_rtt, data_segs_in, data_segs_out |
| `Q` | delivery_rate |
| `3Q` | busy_time, rwnd_limited, sndbuf_limited |
| `2I` | delivered, delivered_ce |
| `2Q` | bytes_sent, bytes_retrans |
| `2I` | dsack_dups, reord_seen |
| `2I` | rcv_ooopack, snd_wnd |

`=` means native byte order with no implicit padding. The kernel struct is naturally aligned, so the offsets match on x86_64.
If the kernel returns fewer than 232 bytes, the test fails with an error and nothing is silently misread.
Tested on `6.6.87.2-microsoft-standard-WSL2` (default congestion control `cubic`).

### Interval measurements

The client reads `TCP_INFO` once before sending and then after each `sendall()` whenever at least `--interval` seconds
have elapsed (`time.monotonic()`). Each sample uses the **actual** elapsed time `dt` between two reads, not the nominal interval:

- `goodput_bps = (bytes_acked[k] - bytes_acked[k-1]) * 8 / dt`
- `retrans_segs = total_retrans[k] - total_retrans[k-1]`
- `retrans_rate = retrans_segs / (data_segs_out[k] - data_segs_out[k-1])`
- `retrans_bytes = bytes_retrans[k] - bytes_retrans[k-1]`

Instantaneous fields (cwnd, srtt, rttvar, min_rtt, pacing/delivery rate, lost, ca_state) are the values at the moment of the read.

### Loss estimate / proxy

TCP cannot see loss directly, so the loss proxy is **retransmitted segments per interval** (`retrans_segs`), normalized as
`retrans_rate`. This overcounts true loss when retransmissions are spurious (for example, reordering or a premature RTO).
Supporting signals in the CSV:
- `lost_segs`: segments the kernel currently marks as lost (RACK/SACK scoreboard)
- `rto_backoffs`: consecutive unrecovered RTO retransmissions (tcpi_retransmits); non-zero means a timeout is in progress
- `ca_state`: 0 Open, 1 Disorder, 2 CWR, 3 Recovery (fast retransmit), 4 Loss (RTO)

### CSV columns and units

Units are encoded in the column suffix: `_s` seconds, `_ms` milliseconds, `_bps` bits/s, `_bytes` bytes, `_segs` segments.
`srtt_ms`, `rttvar_ms`, `min_rtt_ms`, and `rto_ms` are converted from kernel microseconds. `pacing_rate_bps` and
`delivery_rate_bps` are converted from kernel bytes/s. `snd_ssthresh_segs = 2147483647` means ssthresh is unset (still in initial slow start).
