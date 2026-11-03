FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 bubblewrap ripgrep \
    && rm -rf /var/lib/apt/lists/*
COPY target/debug/examples/exec-server-fixture /usr/local/bin/exec-server-fixture
ENTRYPOINT ["/usr/local/bin/exec-server-fixture"]
