FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ARG GRADLE_VERSION=9.2.1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        ca-certificates \
        curl \
        git \
        unzip \
        gnupg \
        passwd \
        rclone && \
    curl -fsSL https://apt.corretto.aws/corretto.key \
        | gpg --dearmor -o /usr/share/keyrings/corretto-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/corretto-keyring.gpg] https://apt.corretto.aws stable main" \
        > /etc/apt/sources.list.d/corretto.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        java-25-amazon-corretto-jdk \
        maven && \
    curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
        -o /tmp/gradle.zip && \
    curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip.sha256" \
        -o /tmp/gradle.sha256 && \
    echo "$(cat /tmp/gradle.sha256)  /tmp/gradle.zip" | sha256sum -c - && \
    unzip -q /tmp/gradle.zip -d /opt && \
    ln -s "/opt/gradle-${GRADLE_VERSION}" /opt/gradle && \
    rm -f /tmp/gradle.zip /tmp/gradle.sha256 && \
    groupadd --system craftarr-builder && \
    useradd --system --gid craftarr-builder --home-dir /var/lib/craftarr-builder \
        --create-home --shell /usr/sbin/nologin craftarr-builder && \
    chmod 700 /var/lib/craftarr-builder && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/craftarr

COPY requirements.txt .
RUN python3 -m venv .venv && \
    .venv/bin/pip install --upgrade pip && \
    .venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY plugin-monitoring.yml ./app/bundled_plugin_monitoring.yml
COPY alembic.ini .

RUN mkdir -p \
    /var/lib/craftarr \
    /srv/minecraft \
    /etc/craftarr && \
    chmod 700 /var/lib/craftarr /srv/minecraft /etc/craftarr

ENV CRAFTARR_CONSOLE_HOST=0.0.0.0
ENV CRAFTARR_CONSOLE_PORT=8000
ENV CRAFTARR_CONSOLE_DATABASE=/var/lib/craftarr/craftarr.db
ENV CRAFTARR_CONSOLE_SERVER_ROOT=/srv/minecraft
ENV CRAFTARR_DEPLOYMENT_MODE=docker
ENV CRAFTARR_PLUGIN_BUILD_USER=craftarr-builder
ENV JAVA_HOME=/usr/lib/jvm/java-25-amazon-corretto
ENV PATH="${JAVA_HOME}/bin:/opt/gradle/bin:${PATH}"

EXPOSE 8000
EXPOSE 25565-25600

CMD ["/bin/bash", "-c", "\
    .venv/bin/python -c 'from app.migrations import upgrade_database; upgrade_database()' && \
    PASSWORD=$(.venv/bin/python -m app.admin_cli ensure-admin --username admin) && \
    if [ -n \"$PASSWORD\" ]; then \
      echo '============================================'; \
      echo 'Craftarr initial administrator'; \
      echo 'Username: admin'; \
      echo \"Temporary password: $PASSWORD\"; \
      echo '============================================'; \
    fi && \
    exec .venv/bin/python -m app.run \
"]
