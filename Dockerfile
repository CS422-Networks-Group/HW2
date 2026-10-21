#user-space environment for the hw2 pipeline. TCP behavior and TCP_INFO come from the host kernel,
#so run with --network host on a Linux / WSL2 host that has cubic, reno, and bbr available (see README)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

#ubuntu 24.04 blocks pip on the system python (PEP 668), so dependencies go in a venv
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /hw2
COPY src/ src/
COPY data/ data/

#arguments after the image name replace CMD, e.g. docker run ... hw2 --n 5 --time 30 --reps 2
ENTRYPOINT ["python", "src/main.py"]
CMD ["data/listed_iperf3_servers.csv", "--out", "results"]
