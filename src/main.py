import argparse
import pandas as pd
import socket
import ipaddress
import json
import os
import struct
import time
import matplotlib.pyplot as plt

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


def run_test(server, port, experiment_time, interval):
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

    #step 5: open a second connection to send data
    new_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    new_client.settimeout(10)
    new_client.connect((ip, port))
    new_client.sendall(cookie)
    algorithm = new_client.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).split(b"\0")[0].decode()

    #step 6: read TEST_START (1) and TEST_RUNNING (2)
    if client.recv(1) != bytes([1]) or client.recv(1) != bytes([2]):
        raise RuntimeError("test failed to start")

    #step 7: send data for the experiment duration, sampling goodput every interval
    buf = bytes(131072)
    samples = []
    start = last_time = time.time()
    info = new_client.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
    last_acked = struct.unpack("=8B24I4Q", info[:136])[34]   # tcpi_bytes_acked
    while time.time() - start < experiment_time:
        new_client.sendall(buf)
        now = time.time()
        if now - last_time >= interval:
            info = new_client.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
            acked = struct.unpack("=8B24I4Q", info[:136])[34]
            samples.append((now - start, (acked - last_acked) * 8 / (now - last_time)))
            last_time, last_acked = now, acked

    #step 8: send TEST_END (4), read EXCHANGE_RESULTS (13)
    client.sendall(bytes([4]))
    if client.recv(1) != bytes([13]):
        raise RuntimeError("server did not start results exchange")

    #step 9: send our results
    results = {"cpu_util_total": 0, "cpu_util_user": 0, "cpu_util_system": 0,
               "sender_has_retransmits": 0,
               "streams": [{"id": 1, "bytes": last_acked, "retransmits": 0,
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
    return parser.parse_args()

#main test runner
def main():
    args = parse_args()
    df = process_csv(args.input_csv)

    #try random servers until n succeed or we run out of attempts
    rows = []
    successes = 0
    for _, row in df.sample(frac=1).head(args.max_attempts).iterrows():
        if successes == args.n:
            break
        ip, port = row["IP/HOST"], int(str(row["PORT"]).split("-")[0])
        try:
            algorithm, samples = run_test(ip, port, args.time, args.interval)
            for t, goodput in samples:
                rows.append({"destination": f"{ip}:{port}", "algorithm": algorithm,
                             "time_s": t, "goodput_bps": goodput})
            successes += 1
            print(f"ok: {ip}:{port}")
        except (OSError, RuntimeError) as e:
            print(f"failed: {ip}:{port} ({e})")

    if successes < args.n:
        print(f"shortfall: only {successes} of {args.n} servers succeeded")
    if not rows:
        return

    os.makedirs(args.out, exist_ok=True)

    #save raw goodput samples
    data = pd.DataFrame(rows)
    data.to_csv(os.path.join(args.out, "goodput.csv"), index=False)


    #summary table (Mbit/s)
    g = data.groupby(["destination", "algorithm"])["goodput_bps"]
    summary = pd.DataFrame({"min": g.min(), "median": g.median(),
                            "mean": g.mean(), "p95": g.quantile(0.95)}) / 1e6
    summary.to_csv(os.path.join(args.out, "summary.csv"))
    print(summary)

    #plot goodput over time, one panel per destination
    destinations = data["destination"].unique()
    fig, axes = plt.subplots(len(destinations), 1, figsize=(8, 2.5 * len(destinations)), squeeze=False)
    for ax, d in zip(axes[:, 0], destinations):
        d_data = data[data["destination"] == d]
        ax.plot(d_data["time_s"], d_data["goodput_bps"] / 1e6)
        ax.set_title(d)
        ax.set_ylabel("Mbit/s")
    axes[-1, 0].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(os.path.join(args.out, "goodput.pdf")) 


if __name__ == "__main__":
    main()