import argparse
import pandas as pd
import socket
import ipaddress
import json
import os
import platform
import struct
import time
import matplotlib.pyplot as plt

#linux struct tcp_info (include/uapi/linux/tcp.h), decoded up to tcpi_snd_wnd (232 bytes, kernel 5.4+)
#8 u8 header fields, 24 u32, then the u64/u32 extensions in kernel order. "=" = native byte order, no padding
TCP_INFO_FMT = "=8B24I4Q6IQ3Q2I2Q2I2I"
TCP_INFO_FIELDS = (
    "state ca_state retransmits probes backoff options wscale flags "
    "rto ato snd_mss rcv_mss unacked sacked lost retrans fackets "
    "last_data_sent last_ack_sent last_data_recv last_ack_recv "
    "pmtu rcv_ssthresh rtt rttvar snd_ssthresh snd_cwnd advmss reordering "
    "rcv_rtt rcv_space total_retrans "
    "pacing_rate max_pacing_rate bytes_acked bytes_received "
    "segs_out segs_in notsent_bytes min_rtt data_segs_in data_segs_out "
    "delivery_rate busy_time rwnd_limited sndbuf_limited delivered delivered_ce "
    "bytes_sent bytes_retrans dsack_dups reord_seen rcv_ooopack snd_wnd"
).split()
TCP_INFO_LEN = struct.calcsize(TCP_INFO_FMT)
assert len(TCP_INFO_FIELDS) == len(struct.unpack(TCP_INFO_FMT, bytes(TCP_INFO_LEN)))

#read and decode TCP_INFO from a live socket into a dict of raw kernel values
def tcp_info(sock) -> dict:
    raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, TCP_INFO_LEN)
    if len(raw) < TCP_INFO_LEN:
        raise RuntimeError(f"kernel tcp_info is {len(raw)} bytes, need {TCP_INFO_LEN}")
    return dict(zip(TCP_INFO_FIELDS, struct.unpack(TCP_INFO_FMT, raw[:TCP_INFO_LEN])))

#build one sample from two successive tcp_info reads taken dt seconds apart
#cumulative kernel counters are differenced to get per-interval values
def tcp_sample(info, prev, timestamp, t, dt) -> dict:
    retrans = info["total_retrans"] - prev["total_retrans"]
    segs_out = info["data_segs_out"] - prev["data_segs_out"]
    return {
        "timestamp_unix_s": timestamp,
        "time_s": t,
        "interval_s": dt,
        "goodput_bps": (info["bytes_acked"] - prev["bytes_acked"]) * 8 / dt,
        "snd_cwnd_segs": info["snd_cwnd"],
        "snd_mss_bytes": info["snd_mss"],
        "cwnd_bytes": info["snd_cwnd"] * info["snd_mss"],
        "snd_ssthresh_segs": info["snd_ssthresh"],
        "srtt_ms": info["rtt"] / 1000,
        "rttvar_ms": info["rttvar"] / 1000,
        "min_rtt_ms": info["min_rtt"] / 1000,
        "rto_ms": info["rto"] / 1000,
        #loss proxy: segments retransmitted during this interval, and as a fraction of data segments sent
        "retrans_segs": retrans,
        "retrans_rate": retrans / segs_out if segs_out else 0.0,
        "retrans_bytes": info["bytes_retrans"] - prev["bytes_retrans"],
        "total_retrans_segs": info["total_retrans"],
        "lost_segs": info["lost"],
        "rto_backoffs": info["retransmits"],
        "ca_state": info["ca_state"],
        "pacing_rate_bps": info["pacing_rate"] * 8,
        "delivery_rate_bps": info["delivery_rate"] * 8,
        "delivery_rate_app_limited": info["flags"] & 1,
        "bytes_acked": info["bytes_acked"],
        "bytes_sent": info["bytes_sent"],
        "unacked_segs": info["unacked"],
        "notsent_bytes": info["notsent_bytes"],
        "snd_wnd_bytes": info["snd_wnd"],
        #fraction of the interval the sender was limited by the receiver window / local send buffer instead of cwnd
        "rwnd_limited_frac": (info["rwnd_limited"] - prev["rwnd_limited"]) / (dt * 1e6),
        "sndbuf_limited_frac": (info["sndbuf_limited"] - prev["sndbuf_limited"]) / (dt * 1e6),
    }

#check whether a value is an ip address or a host name
def is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False

#processing the csv for use by the functions
def process_csv(csv_file: str) -> pd.DataFrame:
    #read the csv
    df = pd.read_csv(csv_file)

    #read each row in csv and resolve host names to ips
    for index, row in df.iterrows():
        ip_or_host = row["IP/HOST"]
        if not is_ip_address(ip_or_host):
            try:
                ip_or_host = socket.gethostbyname(ip_or_host)
            except socket.gaierror:
                continue
            #setting the df values
            df.loc[index, "IP/HOST"] = ip_or_host
    return df


def run_test(server, port, experiment_time, interval, cc=None):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(10)
    client.connect((server, port))
    params = {"tcp": True, "omit": 0, "time": experiment_time, "num": 0,
          "blockcount": 0, "parallel": 1, "len": 131072,
          "pacing_timer": 1000, "client_version": "3.22"}

    #step 1: generate and send cookie of 36 bytes + 1 null byte
    alphabet = b"abcdefghijklmnopqrstuvwxyz234567"
    cookie = b""
    for b in os.urandom(36):
        cookie += bytes([alphabet[b % 32]])
    cookie += b"\x00"
    client.sendall(cookie)

    #step 2: read PARAM_EXCHANGE (9)
    if client.recv(1) != bytes([9]):
        raise RuntimeError("server busy or closed connection")

    #step 3: send json string preview + json bytes
    body = json.dumps(params).encode()
    client.sendall(len(body).to_bytes(4, "big"))
    client.sendall(body)

    #step 4: read CREATE_STREAMS (10)
    if client.recv(1) != bytes([10]):
        raise RuntimeError("server rejected parameters")

    #step 5: open a second connection to send data, selecting the congestion control algorithm before connecting
    new_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    new_client.settimeout(10)
    if cc:
        new_client.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
    new_client.connect((server, port))
    new_client.sendall(cookie)
    algorithm = new_client.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).split(b"\0")[0].decode()
    if cc and algorithm != cc:
        raise RuntimeError(f"requested {cc} but data socket uses {algorithm}")

    #step 6: read TEST_START (1) and TEST_RUNNING (2)
    if client.recv(1) != bytes([1]) or client.recv(1) != bytes([2]):
        raise RuntimeError("test failed to start")

    #step 7: send data for the experiment duration, sampling goodput and tcp stats from the data socket every interval
    buf = bytes(131072)
    samples = []
    start = last_time = time.monotonic()
    prev = tcp_info(new_client)
    while time.monotonic() - start < experiment_time:
        new_client.sendall(buf)
        now = time.monotonic()
        if now - last_time >= interval:
            info = tcp_info(new_client)
            samples.append(tcp_sample(info, prev, time.time(), now - start, now - last_time))
            last_time, prev = now, info

    #step 8: send TEST_END (4), read EXCHANGE_RESULTS (13)
    client.sendall(bytes([4]))
    if client.recv(1) != bytes([13]):
        raise RuntimeError("server did not start results exchange")

    #step 9: send our results
    results = {"cpu_util_total": 0, "cpu_util_user": 0, "cpu_util_system": 0,
               "sender_has_retransmits": 1,
               "streams": [{"id": 1, "bytes": prev["bytes_acked"], "retransmits": prev["total_retrans"],
                            "jitter": 0, "errors": 0, "packets": 0}]}
    body = json.dumps(results).encode()
    client.sendall(len(body).to_bytes(4, "big"))
    client.sendall(body)

    #step 10: read the server's results (4-byte length, then that many bytes)
    n = int.from_bytes(client.recv(4), "big")
    server_results = b""
    while len(server_results) < n:
        chunk = client.recv(n - len(server_results))
        if not chunk:
            raise ConnectionError("server closed connection")
        server_results += chunk

    #step 11: read DISPLAY_RESULTS (14), send IPERF_DONE (16)
    client.recv(1)
    client.sendall(bytes([16]))

    new_client.close()
    client.close()
    return algorithm, samples


def parse_args():
    parser = argparse.ArgumentParser(
        description="iperf3-compatible goodput client"
    )
    parser.add_argument(
        "input_csv",
        nargs="?",
        default="data/listed_iperf3_servers.csv",
        help="Path to a CSV file containing an IP/HOST column with the addresses to test "
             "(default: data/listed_iperf3_servers.csv)."
    )
    parser.add_argument(
        "--n",
        type=int,
        default=10,
        help="Number of servers to run the experiments on (default: 10)",
    )
    parser.add_argument(
        "--time",
        type=int,
        default=60,
        help="time to run each iperf3 test (default: 60s)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="goodput sampling interval in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=30,
        help="max number of servers to try (default: 30)"
    )
    parser.add_argument(
        "--out",
        default="results",
        help="directory for CSVs and PDF plots (default: results)"
    )
    parser.add_argument(
        "--representative",
        default=None,
        help="destination (ip:port) for the tcp stats plots (default: the one with median mean goodput)"
    )
    parser.add_argument(
        "--cc",
        nargs="+",
        default=["cubic", "reno", "bbr"],
        help="congestion control algorithms to compare (default: cubic reno bbr)"
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=3,
        help="repetitions of each destination x algorithm test (default: 3)"
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=3.0,
        help="seconds to wait between tests so servers accept the next one (default: 3)"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="extra attempts for a failed test before giving up on it (default: 1)"
    )
    return parser.parse_args()

#make sure every requested algorithm can be set on a socket by this user
def check_algorithms(algorithms):
    for cc in algorithms:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
        except OSError as e:
            raise SystemExit(f"cannot use congestion control '{cc}' ({e}). load it with 'sudo modprobe tcp_{cc}' and "
                             f"add it to net.ipv4.tcp_allowed_congestion_control")
        finally:
            s.close()

#rotate the algorithm order each repetition so no algorithm always runs first
def test_order(algorithms, rep):
    k = rep % len(algorithms)
    return algorithms[k:] + algorithms[:k]

#per-run aggregates used for the algorithm comparison and variability analysis
def summarize_runs(data: pd.DataFrame) -> pd.DataFrame:
    def one(r):
        min_rtt = r["min_rtt_ms"].iloc[-1]
        #bdp estimate: highest sustained delivery rate (p95) x propagation delay (min_rtt)
        bdp_bytes = r["delivery_rate_bps"].quantile(0.95) / 8 * min_rtt / 1000
        sent = r["bytes_sent"].iloc[-1]
        return pd.Series({
            "mean_goodput_mbps": r["goodput_bps"].mean() / 1e6,
            "median_goodput_mbps": r["goodput_bps"].median() / 1e6,
            "mean_srtt_ms": r["srtt_ms"].mean(),
            "min_rtt_ms": min_rtt,
            "mean_queue_delay_ms": (r["srtt_ms"] - r["min_rtt_ms"]).mean(),
            "mean_cwnd_segs": r["snd_cwnd_segs"].mean(),
            "mean_cwnd_bytes": r["cwnd_bytes"].mean(),
            "bdp_bytes": bdp_bytes,
            "cwnd_over_bdp": r["cwnd_bytes"].mean() / bdp_bytes if bdp_bytes else float("nan"),
            "total_retrans_segs": r["retrans_segs"].sum(),
            "retrans_pct_bytes": 100 * r["retrans_bytes"].sum() / sent if sent else 0.0,
            "rwnd_limited_frac": r["rwnd_limited_frac"].mean(),
            "sndbuf_limited_frac": r["sndbuf_limited_frac"].mean(),
        })
    keys = ["run_id", "rep", "order", "destination", "algorithm"]
    return data.groupby(keys, sort=False).apply(one, include_groups=False).reset_index()

#pick the destination whose mean goodput is the median across destinations that have every algorithm
def pick_representative(data: pd.DataFrame) -> str:
    n_algorithms = data["algorithm"].nunique()
    per_dest = data.groupby("destination")["algorithm"].nunique()
    complete = data[data["destination"].isin(per_dest[per_dest == n_algorithms].index)]
    if not complete.empty:
        data = complete
    means = data.groupby("destination")["goodput_bps"].mean().sort_values()
    return means.index[(len(means) - 1) // 2]

TIMESERIES = [("snd_cwnd_segs", "snd_cwnd (segments)"), ("srtt_ms", "srtt (ms)"),
              ("retrans_segs", "retransmitted\nsegments / interval"), ("goodput_mbps", "goodput (Mbit/s)")]
SCATTER = [("snd_cwnd_segs", "snd_cwnd (segments)"), ("srtt_ms", "srtt (ms)"),
           ("retrans_segs", "retransmitted segments / interval")]
COLORS = plt.cm.tab10.colors
REP_COLORS = plt.cm.Dark2.colors

#q2(b)/q3(a): time series and scatter plots of tcp stats for one destination
#one column (time series) or row (scatter) per algorithm with shared axes, one color per repetition
def plot_tcp_stats(d_data: pd.DataFrame, out: str):
    dest = d_data["destination"].iloc[0]
    algorithms = list(dict.fromkeys(d_data["algorithm"]))
    reps = sorted(d_data["rep"].unique())
    d_data = d_data.assign(goodput_mbps=d_data["goodput_bps"] / 1e6)

    #(i) time series: rows = metric, columns = algorithm, same y scale across a row
    fig, axes = plt.subplots(len(TIMESERIES), len(algorithms), figsize=(5 * len(algorithms), 10),
                             sharex=True, sharey="row", squeeze=False)
    for j, cc in enumerate(algorithms):
        a = d_data[d_data["algorithm"] == cc]
        axes[0, j].set_title(cc)
        for rep in reps:
            r = a[a["rep"] == rep]
            for i, (col, _) in enumerate(TIMESERIES):
                axes[i, j].plot(r["time_s"], r[col], color=REP_COLORS[(rep - 1) % 8], lw=1, label=f"rep {rep}")
            axes[1, j].plot(r["time_s"], r["min_rtt_ms"], "--", color=REP_COLORS[(rep - 1) % 8], lw=0.8)
        axes[-1, j].set_xlabel("time (s)")
    for i, (_, label) in enumerate(TIMESERIES):
        axes[i, 0].set_ylabel(label)
    axes[0, -1].legend(loc="upper right", fontsize=8)
    fig.suptitle(f"TCP stats over time: {dest} (dashed = min_rtt)")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "tcp_stats_timeseries.pdf"))
    plt.close(fig)

    #(ii) scatter: rows = algorithm, columns = metric vs goodput, same x scale down a column and same y everywhere
    fig, axes = plt.subplots(len(algorithms), len(SCATTER), figsize=(14, 4 * len(algorithms)),
                             sharex="col", sharey=True, squeeze=False)
    for i, cc in enumerate(algorithms):
        a = d_data[d_data["algorithm"] == cc]
        for j, (col, label) in enumerate(SCATTER):
            for rep in reps:
                r = a[a["rep"] == rep]
                axes[i, j].scatter(r[col], r["goodput_mbps"], color=REP_COLORS[(rep - 1) % 8], s=12, alpha=0.7,
                                   label=f"rep {rep}")
            axes[-1, j].set_xlabel(label)
        axes[i, 0].set_ylabel(f"{cc}\ngoodput (Mbit/s)")
    axes[0, -1].legend(fontsize=8)
    fig.suptitle(f"TCP stats vs goodput: {dest}")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "tcp_stats_scatter.pdf"))
    plt.close(fig)

#q1/q3: goodput over time, one panel per destination, one color per algorithm (every repetition drawn)
def plot_goodput(data: pd.DataFrame, out: str):
    destinations = list(dict.fromkeys(data["destination"]))
    algorithms = list(dict.fromkeys(data["algorithm"]))
    fig, axes = plt.subplots(len(destinations), 1, figsize=(8, 2.5 * len(destinations)), squeeze=False)
    for ax, d in zip(axes[:, 0], destinations):
        for k, cc in enumerate(algorithms):
            a = data[(data["destination"] == d) & (data["algorithm"] == cc)]
            for n, (_, r) in enumerate(a.groupby("run_id")):
                ax.plot(r["time_s"], r["goodput_bps"] / 1e6, color=COLORS[k], lw=1, alpha=0.8,
                        label=cc if n == 0 else None)
        ax.set_title(d)
        ax.set_ylabel("Mbit/s")
    axes[0, 0].legend(fontsize=8)
    axes[-1, 0].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "goodput.pdf"))
    plt.close(fig)

#q3(a): run-to-run variability of per-run mean goodput and mean srtt, grouped by destination and algorithm
def plot_variability(runs: pd.DataFrame, out: str):
    destinations = list(dict.fromkeys(runs["destination"]))
    algorithms = list(dict.fromkeys(runs["algorithm"]))
    width = 0.8 / len(algorithms)
    fig, axes = plt.subplots(2, 1, figsize=(max(8, 1.8 * len(destinations)), 8), sharex=True)
    for k, cc in enumerate(algorithms):
        a = runs[runs["algorithm"] == cc]
        x = [destinations.index(d) + (k - (len(algorithms) - 1) / 2) * width for d in a["destination"]]
        for ax, col in zip(axes, ["mean_goodput_mbps", "mean_srtt_ms"]):
            ax.scatter(x, a[col], color=COLORS[k], s=18, label=cc)
            means = a.groupby("destination")[col].mean()
            ax.scatter([destinations.index(d) + (k - (len(algorithms) - 1) / 2) * width for d in means.index],
                       means, color=COLORS[k], marker="_", s=300)
    axes[0].set_ylabel("per-run mean goodput (Mbit/s)")
    axes[1].set_ylabel("per-run mean srtt (ms)")
    axes[0].legend(fontsize=8)
    axes[1].set_xticks(range(len(destinations)), destinations, rotation=30, ha="right", fontsize=8)
    fig.suptitle("Run-to-run variability (dots = runs, bars = mean)")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "variability.pdf"))
    plt.close(fig)

#main test runner
def main():
    args = parse_args()
    check_algorithms(args.cc)
    df = process_csv(args.input_csv)
    session = time.strftime("%Y%m%dT%H%M%S")
    os.makedirs(args.out, exist_ok=True)

    #the send buffer max caps bytes in flight; if it is below the path bdp every algorithm hits the same ceiling
    with open("/proc/sys/net/ipv4/tcp_wmem") as f:
        tcp_wmem = [int(v) for v in f.read().split()]
    if tcp_wmem[2] < 16 * 2**20:
        print(f"warning: net.ipv4.tcp_wmem max is {tcp_wmem[2]} bytes; long fat paths will be send-buffer limited")
    meta = {"session": session, "kernel": platform.release(), "tcp_wmem": tcp_wmem,
            "algorithms": args.cc, "reps": args.reps,
            "time_s": args.time, "interval_s": args.interval, "pause_s": args.pause, "requested": args.n,
            "selection_attempts": [], "destinations": [], "schedule": []}

    def save():
        with open(os.path.join(args.out, "run_metadata.json"), "w") as f:
            json.dump(meta, f, indent=2)
        if rows:
            pd.DataFrame(rows).to_csv(os.path.join(args.out, "tcp_stats.csv"), index=False)

    #phase 1: try random servers with a short probe test until n succeed or we run out of attempts
    #the same destinations are then used for every algorithm and repetition
    rows = []
    destinations = []
    for _, row in df.sample(frac=1).head(args.max_attempts).iterrows():
        if len(destinations) == args.n:
            break
        ip, port = row["IP/HOST"], int(str(row["PORT"]).split("-")[0])
        try:
            run_test(ip, port, 2, args.interval)
            destinations.append((ip, port))
            meta["selection_attempts"].append({"destination": f"{ip}:{port}", "status": "ok"})
            print(f"selected: {ip}:{port}")
        except (OSError, RuntimeError) as e:
            meta["selection_attempts"].append({"destination": f"{ip}:{port}", "status": "failed", "error": str(e)})
            print(f"rejected: {ip}:{port} ({e})")
        time.sleep(args.pause)
    meta["destinations"] = [f"{ip}:{port}" for ip, port in destinations]
    meta["succeeded"] = len(destinations)
    if len(destinations) < args.n:
        print(f"shortfall: only {len(destinations)} of {args.n} servers succeeded")
    save()

    #phase 2: each repetition runs every destination with every algorithm, rotating the algorithm order per repetition
    total = args.reps * len(destinations) * len(args.cc)
    for rep in range(1, args.reps + 1):
        for ip, port in destinations:
            dest = f"{ip}:{port}"
            for order, cc in enumerate(test_order(args.cc, rep - 1), 1):
                run_id = f"{session}-r{rep}-{dest}-{cc}"
                entry = {"run_id": run_id, "rep": rep, "order": order, "destination": dest, "requested": cc,
                         "start_unix_s": time.time()}
                for attempt in range(1, args.retries + 2):
                    entry["attempts"] = attempt
                    try:
                        algorithm, samples = run_test(ip, port, args.time, args.interval, cc)
                        rows.extend({"run_id": run_id, "rep": rep, "order": order, "destination": dest,
                                     "algorithm": algorithm, **s} for s in samples)
                        entry.update(status="ok", algorithm=algorithm)
                        entry.pop("error", None)
                        break
                    except (OSError, RuntimeError) as e:
                        entry.update(status="failed", error=str(e))
                    finally:
                        time.sleep(args.pause)
                meta["schedule"].append(entry)
                save()
                print(f"[{len(meta['schedule'])}/{total}] rep {rep} {dest} {cc}: {entry['status']}"
                      + (f" ({entry['error']})" if entry["status"] == "failed" else ""))
    if not rows:
        return

    #save raw samples: full tcp stats, and the goodput-only view
    data = pd.DataFrame(rows)
    data[["run_id", "rep", "destination", "algorithm", "time_s", "goodput_bps"]].to_csv(
        os.path.join(args.out, "goodput.csv"), index=False)

    #summary table over all samples (Mbit/s)
    g = data.groupby(["destination", "algorithm"])["goodput_bps"]
    summary = pd.DataFrame({"min": g.min(), "median": g.median(),
                            "mean": g.mean(), "p95": g.quantile(0.95)}) / 1e6
    summary.to_csv(os.path.join(args.out, "summary.csv"))
    print(summary)

    #per-run aggregates, then mean and std across repetitions per destination x algorithm and per algorithm overall
    runs = summarize_runs(data)
    runs.to_csv(os.path.join(args.out, "runs.csv"), index=False)
    metrics = ["mean_goodput_mbps", "mean_srtt_ms", "mean_queue_delay_ms", "retrans_pct_bytes", "cwnd_over_bdp",
               "sndbuf_limited_frac", "rwnd_limited_frac"]
    comparison = runs.groupby(["destination", "algorithm"])[metrics].agg(["mean", "std"])
    comparison.to_csv(os.path.join(args.out, "algorithm_comparison.csv"))
    overall = runs.groupby("algorithm")[metrics].agg(["mean", "median", "std"])
    overall.to_csv(os.path.join(args.out, "algorithm_overall.csv"))
    print(overall.round(2))

    plot_goodput(data, args.out)
    plot_variability(runs, args.out)

    #tcp stats plots for a representative destination
    rep = args.representative or pick_representative(data)
    if rep not in data["destination"].values:
        print(f"representative {rep} has no samples, skipping tcp stats plots")
        return
    print(f"representative destination: {rep}")
    meta["representative"] = rep
    save()
    plot_tcp_stats(data[data["destination"] == rep], args.out)


if __name__ == "__main__":
    main()
