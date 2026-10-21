# CS 422 HW2 Report

Oct 20, 2026

## Overview

A from-scratch Python iperf3 client completed 60 s uploads to 10 of 11 public iperf3 servers tried, sampling goodput and `TCP_INFO` from the data socket every 1 s. All Part 1 and Part 2 results below come from that single run (session `20261018T201911`).

| Item | Value |
| --- | --- |
| Repository | [CS422-Networks-Group/HW2](https://github.com/CS422-Networks-Group/HW2) (code links pin commit [`54b78a9`](https://github.com/CS422-Networks-Group/HW2/tree/54b78a96180478b7f7a17f0a46f5614d0260b5d9)) |
| Entry point | [`src/main.py`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py) |
| Host kernel | `6.6.87.2-microsoft-standard-WSL2` (Windows, WSL2) |
| Congestion control | `cubic` (kernel default, read back from the data socket with `TCP_CONGESTION`) |
| Dependencies | Python 3, `pandas==3.0.6`, `matplotlib==3.11.2` ([requirements.txt](../requirements.txt)) |
| Server list | [`data/listed_iperf3_servers.csv`](../data/listed_iperf3_servers.csv), from [iperf3serverlist.net](https://iperf3serverlist.net/) |
| Raw output | [`results/tcp_stats.csv`](../results/tcp_stats.csv) (576 samples), [`results/goodput.csv`](../results/goodput.csv), [`results/summary.csv`](../results/summary.csv), [`results/run_metadata.json`](../results/run_metadata.json) |

Reproduce on Linux or WSL2:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/main.py data/listed_iperf3_servers.csv --n 10 --time 60 --interval 1.0 --out results
```

Open item for the final submission: the AI-use acknowledgment still needs writing.

## Docker

The [`Dockerfile`](../Dockerfile) (`FROM ubuntu:24.04`) fixes the user-space environment: Python 3.12 from Ubuntu, the pinned `pandas` and `matplotlib` in a venv, `src/`, and `data/`. Its entry point is `python src/main.py`, so with no arguments the container runs the full pipeline: destination selection, CUBIC, Reno, and BBR tests, raw CSVs, summary tables, and PDF plots.

The container shares the host kernel, so TCP behavior and the `TCP_INFO` layout come from the host. The kernel version is recorded automatically: `platform.release()` inside the container returns the host kernel, which `main()` writes to `run_metadata.json` (`6.6.87.2-microsoft-standard-WSL2` for these results).

Host configuration, required once per boot on a Linux or WSL2 host (on WSL, `wsl -u root` opens a root shell):

```
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
```

The container cannot do either step itself. It cannot load kernel modules, and without `CAP_NET_ADMIN` an unprivileged socket may only select algorithms in `tcp_allowed_congestion_control`. Check with `cat /proc/sys/net/ipv4/tcp_allowed_congestion_control`; on the test host it reads `reno cubic bbr`.

Build and run from the repository root:

```
docker build -t hw2 .
docker run --rm --network host -v "$PWD/results:/hw2/results" hw2
docker run --rm --network host -v "$PWD/results:/hw2/results" hw2 \
    data/listed_iperf3_servers.csv --n 10 --time 60 --interval 1.0 --reps 3 --cc cubic reno bbr --out results
```

- `--network host` puts the sockets in the host's network namespace, so they use the host's TCP settings and route with no Docker NAT in the path.
- `-v` writes the results to `./results` on the host. Add `--user "$(id -u):$(id -g)"` so the files are not owned by root.
- Arguments after the image name replace the default command; the options are the same as in the README.

## Part 1: iPerf throughput application

### (a) iperf3-compatible client

The client speaks the iperf3 control protocol over raw sockets and never invokes the `iperf3` binary. [`run_test()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L98-L180) runs one test as the steps below.

| Requirement | What the client does | Code |
| --- | --- | --- |
| (i) Control connection | TCP connect, then send a 36-character random cookie (base32 alphabet) plus a null byte | [L99–L112](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L99-L112) |
| (ii) JSON parameter exchange | Wait for `PARAM_EXCHANGE` (9), send a 4-byte big-endian length then the JSON parameters (`tcp`, `time`, `len`=131072, `parallel`=1, ...) | [L114–L121](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L114-L121) |
| (iii) Data connection | Wait for `CREATE_STREAMS` (10), open a second TCP connection and send the same cookie; read the algorithm with `TCP_CONGESTION` | [L123–L132](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L123-L132) |
| (iv) Continuous transmission | After `TEST_START` (1) and `TEST_RUNNING` (2), `sendall()` 128 KiB blocks until `--time` seconds pass, sampling `TCP_INFO` every `--interval` | [L134–L149](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L134-L149) |
| (v) iperf3 termination | Send `TEST_END` (4), wait for `EXCHANGE_RESULTS` (13), exchange JSON results, read `DISPLAY_RESULTS` (14), send `IPERF_DONE` (16), close both sockets | [L151–L180](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L151-L180) |

Failure handling: every failure raises `OSError` or `RuntimeError`, which [`main()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L274-L302) logs before moving to the next server.

- **Non-responsive or overloaded servers:** a busy server answers with `ACCESS_DENIED` instead of 9, so the state check raises "server busy or closed connection".
- **Rejected parameters:** any reply other than `CREATE_STREAMS` raises "server rejected parameters".
- **Connection timeouts:** both sockets use a 10 s timeout, so a silent server raises `socket.timeout`. In the run, 66.35.20.123:30001 failed this way.
- **Premature termination:** a closed socket returns an empty read, which fails the next state check; a reset raises `ConnectionResetError`; a short results read raises `ConnectionError`.

### (b) Destination selection

`main()` shuffles the server list, then tests servers in that order until `--n` succeed or `--max-attempts` (default 30) are used. Every attempt is written to `run_metadata.json` with its run id, address, port, and status or error. A shortfall prints `shortfall: only k of n servers succeeded`. This run asked for 10 and reached 10 after 11 attempts, so there was no shortfall.

### (c) Goodput

Goodput uses the kernel's cumulative `tcpi_bytes_acked` from the data socket and the measured time between reads ([`tcp_sample()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L38-L45)), not the nominal interval:

```math
\text{goodput}_k = \frac{(\text{bytes\_acked}_k - \text{bytes\_acked}_{k-1}) \cdot 8}{t_k - t_{k-1}}
```

The throughput-over-time plot is [`results/goodput.pdf`](../results/goodput.pdf) (one panel per destination), made in [`main()` L325–L336](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L325-L336).

![Goodput over time, one panel per destination](figures/goodput.png)

*Goodput (Mbit/s) over 60 s for each of the 10 destinations, CUBIC, 1 s samples.*

Summary per destination, all CUBIC, Mbit/s, sorted by mean ([`results/summary.csv`](../results/summary.csv)):

| Destination | Algorithm | Min | Median | Mean | p95 |
| --- | --- | --- | --- | --- | --- |
| 157.250.201.61:5203 | cubic | 24.7 | 104.3 | 112.4 | 253.3 |
| 51.158.1.21:5200 | cubic | 63.8 | 89.6 | 100.4 | 186.7 |
| 23.81.207.31:5201 | cubic | 51.1 | 78.0 | 90.5 | 166.7 |
| 23.81.166.99:5201 | cubic | 48.4 | 68.7 | 82.9 | 162.0 |
| 23.105.41.222:5201 | cubic | 25.4 | 42.7 | 65.3 | 159.9 |
| 217.18.95.142:5201 | cubic | 28.5 | 57.0 | 60.4 | 82.2 |
| 45.147.210.189:5200 | cubic | 18.5 | 29.1 | 39.9 | 67.1 |
| 96.45.39.38:30001 | cubic | 9.2 | 29.4 | 32.5 | 93.1 |
| 185.246.208.67:5201 | cubic | 3.1 | 17.1 | 19.9 | 35.6 |
| 156.146.40.65:5201 | cubic | 2.9 | 4.6 | 17.7 | 64.5 |

Mean goodput spans 6x across paths, from 17.7 to 112.4 Mbit/s. On 156.146.40.65 the mean (17.7) is nearly 4x the median (4.6), so a few fast seconds carried most of its bytes.

## Part 2: TCP stats tracing

All Part 2 runs used `cubic`, the kernel default, read back from the data socket with `getsockopt(TCP_CONGESTION)` and stored in the `algorithm` column of every row.

### (a) Extracting TCP socket statistics

The client reads `TCP_INFO` from the **data** socket once before sending and then after each `sendall()` once at least `--interval` (1 s) has passed ([L140–L149](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L140-L149)). The same read produces that row's goodput, so goodput and TCP stats share one timestamp. Because a 128 KiB `sendall()` can block, the real gap between reads was 1.000–1.354 s (mean 1.033 s); every rate divides by that measured gap.

**Decoding format.** [`tcp_info()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L30-L35) decodes the buffer as Linux `struct tcp_info` from `include/uapi/linux/tcp.h`, through `tcpi_snd_wnd` (232 bytes, kernel 5.4 or newer). The format and field names are at [L12–L27](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L12-L27); an assert checks that the field count matches the format. `=` means native byte order with no added padding, which matches the naturally aligned kernel struct on x86_64. If the kernel returns fewer than 232 bytes, the test fails instead of misreading fields.

```
TCP_INFO_FMT = "=8B24I4Q6IQ3Q2I2Q2I2I"   # 232 bytes, host kernel 6.6.87.2-microsoft-standard-WSL2
```

| Format | Fields |
| --- | --- |
| `8B` | state, ca_state, retransmits, probes, backoff, options, wscale, flags (bit 0 = delivery_rate_app_limited) |
| `24I` | rto, ato, snd_mss, rcv_mss, unacked, sacked, lost, retrans, fackets, last_data_sent, last_ack_sent, last_data_recv, last_ack_recv, pmtu, rcv_ssthresh, rtt, rttvar, snd_ssthresh, snd_cwnd, advmss, reordering, rcv_rtt, rcv_space, total_retrans |
| `4Q` | pacing_rate, max_pacing_rate, bytes_acked, bytes_received |
| `6I` | segs_out, segs_in, notsent_bytes, min_rtt, data_segs_in, data_segs_out |
| `Q` + `3Q` | delivery_rate; busy_time, rwnd_limited, sndbuf_limited |
| `2I` + `2Q` | delivered, delivered_ce; bytes_sent, bytes_retrans |
| `2I` + `2I` | dsack_dups, reord_seen; rcv_ooopack, snd_wnd |

**Interval measurements.** [`tcp_sample()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L38-L70) differences cumulative kernel counters between read k−1 and read k, over the measured gap dt:

- `goodput_bps` = Δ`bytes_acked` × 8 / dt
- `retrans_segs` = Δ`total_retrans`; `retrans_rate` = `retrans_segs` / Δ`data_segs_out`; `retrans_bytes` = Δ`bytes_retrans`
- Instantaneous fields (cwnd, srtt, rttvar, min_rtt, pacing and delivery rate, lost, ca_state) are the values at read k.

**Loss proxy.** TCP cannot observe loss directly, so the proxy is retransmitted segments per interval (`retrans_segs`). It overcounts when a retransmission is spurious, for example after reordering or an early RTO. Three supporting signals are logged: `lost_segs` (segments the kernel currently marks lost), `rto_backoffs` (`tcpi_retransmits`, non-zero while an RTO is unrecovered), and `ca_state` (0 Open, 1 Disorder, 2 CWR, 3 Recovery, 4 Loss).

**Storage.** [`results/tcp_stats.csv`](../results/tcp_stats.csv) has one row per sample with `run_id`, `destination`, `algorithm`, and 27 stats columns. Units are in the column suffix: `_s` seconds, `_ms` milliseconds, `_bps` bits/s, `_bytes` bytes, `_segs` segments.

| Requirement | CSV columns |
| --- | --- |
| Timestamp | `timestamp_unix_s`, `time_s`, `interval_s` |
| snd_cwnd | `snd_cwnd_segs`, `cwnd_bytes`, `snd_ssthresh_segs` |
| RTT estimate | `srtt_ms`, `rttvar_ms`, `min_rtt_ms`, `rto_ms` |
| Loss signal | `retrans_segs`, `retrans_rate`, `retrans_bytes`, `total_retrans_segs`, `lost_segs`, `rto_backoffs`, `ca_state` |
| Goodput | `goodput_bps` |
| Recommended extras | `pacing_rate_bps`, `delivery_rate_bps`, `delivery_rate_app_limited`, `bytes_acked`, `bytes_sent`, `unacked_segs`, `notsent_bytes`, `snd_wnd_bytes` |

### (b) Visualization

[`pick_representative()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L231-L234) picks the destination whose mean goodput is the median of the 10: **217.18.95.142:5201** (60.4 Mbit/s mean, 93.0 ms min RTT). [`plot_tcp_stats()`](https://github.com/CS422-Networks-Group/HW2/blob/54b78a96180478b7f7a17f0a46f5614d0260b5d9/src/main.py#L236-L271) writes both PDFs automatically at the end of each run:

- [`results/tcp_stats_timeseries.pdf`](../results/tcp_stats_timeseries.pdf) (Figure 1): snd_cwnd, srtt with a ±rttvar band and min_rtt, retransmitted segments per interval, and goodput, on a shared time axis.
- [`results/tcp_stats_scatter.pdf`](../results/tcp_stats_scatter.pdf) (Figure 2): snd_cwnd, srtt, and retransmitted segments against goodput, colored by time.

![TCP stats over time for 217.18.95.142:5201](figures/tcp_stats_timeseries.png)

*Figure 1: snd_cwnd, RTT, retransmitted segments, and goodput over time for 217.18.95.142:5201 (CUBIC).*

![TCP stats vs goodput for 217.18.95.142:5201](figures/tcp_stats_scatter.png)

*Figure 2: snd_cwnd, srtt, and retransmitted segments vs goodput for 217.18.95.142:5201 (CUBIC), colored by time.*

### (c) Observations

On the representative path, goodput was set by the congestion window, not by queueing: after slow start goodput tracked cwnd closely (r = 0.94 over 54 samples) while srtt stayed within 3 ms of min_rtt.

| Metric | Meaning | Expected effect on goodput in congestion avoidance |
| --- | --- | --- |
| snd_cwnd | Most unacknowledged segments the sender may have in flight | goodput ≈ cwnd × MSS / RTT until cwnd reaches the BDP; beyond that, extra cwnd only fills the bottleneck queue |
| srtt | Smoothed RTT (EWMA of samples) | For a fixed cwnd, higher RTT means lower goodput; srtt rising above min_rtt signals queueing at the bottleneck |
| rttvar, min_rtt | RTT spread, and the lowest RTT seen (propagation estimate) | srtt − min_rtt estimates queueing delay; rttvar sets the RTO (srtt + 4 × rttvar) |
| Retransmitted segments | Loss proxy: segments sent again in the interval | Each loss event makes CUBIC cut cwnd to 0.7×, so goodput dips right after retransmissions |
| Goodput | Bytes newly acknowledged per second | The outcome the other metrics explain |

What the traces show:

1. **Slow-start overshoot.** cwnd reached 2172 segments at t = 1 s. 1061 of the run's 1105 retransmissions (96%) happened in the first 3 s, cwnd fell to 570, and goodput peaked at 124.3 Mbit/s at t = 2 s.
2. **CUBIC's multiplicative decrease is visible.** All 10 loss events after slow start set ssthresh to about 0.7 × the cwnd before the loss: 674→471, 518→364, 419→293, 293→205, 247→174. This is CUBIC's β = 0.7.
3. **CUBIC's regrowth matches theory.** After the loss at 9.3 s, cwnd climbed from 474 back to 673 by 15.5 s. CUBIC's time to return to W_max is K = (W_max(1 − β)/C)^(1/3) = (674 × 0.3 / 0.4)^(1/3) ≈ 8.0 s. Between 27 and 43 s cwnd flattened at 442–463, the concave plateau near W_max.
4. **Goodput ≈ cwnd × MSS / srtt.** At 8.2 s, 674 × 1448 B × 8 / 93.4 ms = 83.6 Mbit/s against a measured 82.5 Mbit/s. In Figure 2 the cwnd panel is a near-straight line.
5. **RTT stayed flat, so losses were not caused by our own queue.** srtt stayed between 93.4 and 96.0 ms against a 93.0 ms min_rtt, so queueing delay never exceeded 3 ms, and goodput was uncorrelated with srtt (r = −0.02). At 82 Mbit/s and 93 ms the BDP is about 0.96 MB (660 segments), close to the 450–670-segment cwnd. Losses arriving without RTT inflation point to a shallow bottleneck buffer, a policer, or cross traffic.
6. **The loss proxy is sparse.** After slow start each loss event retransmitted only 2–9 segments, yet each cost 30% of cwnd. In Figure 2 nearly all points sit at 0 retransmissions, so the per-interval count barely predicts goodput (r = −0.24).
7. **No timeouts.** `rto_backoffs` was 0 in every sample of all 10 traces. Recovery always ran through fast retransmit; at 54.8 s the trace shows `ca_state` = 3 (Recovery) with 2 segments marked lost.
8. **The flow was not receiver- or application-limited.** The receive window was 10.45 MB, far above the 3.1 MB peak cwnd. About 2 MB stayed queued in `notsent_bytes`, and the delivery rate was app-limited only in the first sample.

Anomalies:

- **Downward drift.** ssthresh ratcheted from 555 to 174 segments and goodput fell from 82 to 29 Mbit/s over the minute, with no RTT increase. Losses came every 1–14 s, often before cwnd regained its previous maximum, so CUBIC kept lowering W_max. The trace cannot tell whether competing traffic or a loss-rate limit caused this.
- **RTT spikes while cwnd was falling.** srtt jumped to 95.6–96.0 ms at 46, 51, and 58 s. Our cwnd was shrinking then, so the extra delay most likely came from other traffic at the bottleneck.
- **Other paths.** On 185.246.208.67:5201, srtt hit 308.6 ms at 54.3 s against a 122.0 ms minimum. On 156.146.40.65:5201, mean goodput (17.7 Mbit/s) was nearly 4× the median (4.6 Mbit/s), so a few fast seconds carried most of the transfer.

## Part 3: Algorithm comparison and design

Not written yet. The comparison run (`--n 5 --time 30 --reps 2 --cc cubic reno bbr`) is in `results/part3/` on the `part3` branch and still uncommitted.
