# CS 422 HW2: iPerf, TCP Statistics, and Congestion Control

## Running

Must run on Linux or WSL2 (`TCP_INFO` / `TCP_CONGESTION` are Linux socket options).

BBR is usually built as a kernel module. Load it and allow unprivileged sockets to select it
(both reset on reboot / WSL restart; on WSL, `wsl -u root` opens a root shell):

```
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
sudo sysctl -w net.ipv4.tcp_wmem="4096 16384 67108864"
```

The `tcp_wmem` max (default 4 MB) caps the sender's bytes in flight at about 2.7 MB. On a 100+ ms path that limits
every algorithm to about 2.7 MB / RTT (about 160-220 Mbit/s here) regardless of cwnd, hiding the differences
between algorithms. `results/part3_sndbuf4MB` is a run with the default limit kept for comparison. The script records
`tcp_wmem` in `run_metadata.json`, warns when the max is under 16 MB, and logs `sndbuf_limited_frac` /
`rwnd_limited_frac` (the fraction of each interval the sender was send-buffer / receive-window limited).

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/main.py [input_csv] [--n 10] [--time 60] [--interval 1.0] [--max-attempts 30] \
                    [--cc cubic reno bbr] [--reps 3] [--pause 3] [--retries 1] \
                    [--out results] [--representative IP:PORT]
```

Or with Docker (after the host setup above; the container uses the host kernel and cannot load modules itself):

```
docker build -t hw2 .
docker run --rm --network host -v "$PWD/results:/hw2/results" hw2 [same arguments as main.py]
```

With no arguments the container runs `src/main.py data/listed_iperf3_servers.csv --out results` with the defaults below.

| option | meaning |
|---|---|
| `input_csv` | server list with `IP/HOST` and `PORT` columns (default `data/listed_iperf3_servers.csv`) |
| `--n` | number of destinations; servers are probed in random order with a 2 s test until n succeed |
| `--time` | transfer duration per test (s) |
| `--interval` | sampling interval for goodput and TCP stats (s) |
| `--max-attempts` | max servers probed before reporting a shortfall |
| `--cc` | congestion control algorithms to compare, set on the data socket before connecting |
| `--reps` | repetitions of every destination x algorithm test |
| `--pause` | seconds between tests so servers accept the next connection |
| `--retries` | extra attempts for a failed test before it is recorded as failed |
| `--out` | output directory |
| `--representative` | destination for the TCP stats plots (default: among destinations with every algorithm, the one whose mean goodput is the median) |

Commands for the committed results:

```
.venv/bin/python src/main.py data/part3_servers.csv --n 5 --time 30 --reps 2 --out results/part3   # Q3 (results/part3)
.venv/bin/python src/main.py --n 10 --time 60 --cc cubic --reps 1           # equivalent of the Q1/Q2 run (results/)
```

`results/` was produced by an earlier version of the script (before `--cc` / `--reps`) using the default CUBIC,
so its files lack the `rep` / `order` columns and its scatter plot is colored by time instead of repetition.

### Test order

Every algorithm uses the same destinations, duration, and interval. Each repetition visits every destination,
and at each destination runs all algorithms back to back. The algorithm order rotates by one each repetition
(rep 1: cubic, reno, bbr; rep 2: reno, bbr, cubic; rep 3: bbr, cubic, reno; ...) so no algorithm always goes first.
The actual order, start time, attempts, and outcome of every test are in `run_metadata.json` under `schedule`.

### Outputs

| file | contents |
|---|---|
| `tcp_stats.csv` | one row per sample: run id, rep, order, destination, algorithm, goodput, and decoded TCP stats |
| `goodput.csv` | goodput-only view of the same samples |
| `summary.csv` | min / median / mean / p95 goodput (Mbit/s) per destination and algorithm |
| `runs.csv` | per-run aggregates: mean goodput, mean srtt, min_rtt, queueing delay, mean cwnd, BDP estimate, cwnd/BDP, retransmissions |
| `algorithm_comparison.csv` | mean and std across repetitions of the per-run aggregates, per destination and algorithm |
| `algorithm_overall.csv` | mean / median / std of the per-run aggregates per algorithm over all destinations |
| `run_metadata.json` | session id, host kernel, settings, destination selection attempts, and the full test schedule |
| `goodput.pdf` | goodput over time, one panel per destination, one color per algorithm |
| `variability.pdf` | per-run mean goodput and mean srtt for every destination and algorithm |
| `tcp_stats_timeseries.pdf` | Q2(b)(i)/Q3(a): snd_cwnd, srtt, loss proxy, goodput over time for the representative destination; one column per algorithm, shared y axes, one color per repetition |
| `tcp_stats_scatter.pdf` | Q2(b)(ii)/Q3(a): snd_cwnd, srtt, retransmissions vs goodput; one row per algorithm, shared axes |

### Per-run aggregates (`runs.csv`)

- `mean_queue_delay_ms` = mean(srtt - min_rtt): RTT inflation above the propagation delay, i.e. queueing
- `bdp_bytes` = p95(delivery_rate) x min_rtt: bandwidth-delay product estimated from the best sustained delivery rate
- `cwnd_over_bdp` = mean(cwnd_bytes) / bdp_bytes: above 1, the flow keeps more in flight than the pipe holds and the excess sits in queues
- `retrans_pct_bytes` = 100 x sum(retrans_bytes) / bytes_sent
- `sndbuf_limited_frac` / `rwnd_limited_frac` = mean fraction of each interval limited by the local send buffer / receive window (from tcpi_sndbuf_limited / tcpi_rwnd_limited, in µs)

`data/part3_servers.csv` lists the 5 destinations used for Q3, originally chosen at random from the full list.

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
