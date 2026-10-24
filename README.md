# CS 422 HW2: iPerf, TCP Statistics, and Congestion Control

Runs on Linux or WSL2 (`TCP_INFO` / `TCP_CONGESTION` are Linux socket options) with Python 3.12.
Tested on host kernel `6.6.87.2-microsoft-standard-WSL2`.

## Host setup

The algorithms come from the host kernel. Load BBR and allow unprivileged sockets to select each algorithm:

```
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
```

## Running

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python src/main.py [input_csv] [--n 10] [--time 60] [--interval 1.0] [--reps 3] [--max-attempts 30] \
                    [--retries 1] [--seed SEED] [--out results] [--representative IP:PORT] [--algorithms cubic reno bbr]
```

| option | meaning |
|---|---|
| `input_csv` | server list with `IP/HOST` and `PORT` columns (default `data/listed_iperf3_servers.csv`) |
| `--n` | number of destinations that must complete every repetition of every algorithm (default 10) |
| `--time` | transfer duration per test in seconds (default 60) |
| `--interval` | sampling interval for goodput and TCP stats in seconds (default 1.0) |
| `--reps` | repetitions of each algorithm per destination (default 3) |
| `--max-attempts` | max servers tried before reporting a shortfall (default 30) |
| `--retries` | extra tries for a failed test before the destination is discarded (default 1) |
| `--seed` | seed for the server shuffle (default random; saved in `run_metadata.json`) |
| `--out` | output directory for CSVs, `run_metadata.json`, and PDF plots (default `results`) |
| `--representative` | destination for the TCP stats plots (default: median mean goodput) |
| `--algorithms` | algorithms to run (default `cubic reno bbr`) |

The report's results came from:

```
# Parts 1 and 2 (CUBIC only)
.venv/bin/python src/main.py data/listed_iperf3_servers.csv --algorithms cubic --reps 1 --out results/part1_2
# Part 3 (CUBIC, Reno, BBR)
.venv/bin/python src/main.py data/listed_iperf3_servers.csv --out results/part3
```

## Docker

After the host setup above (the container uses the host kernel):

```
docker build -t hw2 .
docker run --rm --network host -v "$PWD/results:/hw2/results" hw2 [same arguments as main.py]
```

With no arguments the container runs the Part 3 command.
