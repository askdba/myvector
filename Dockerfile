ARG MYSQL_VERSION=8.0
FROM ubuntu:22.04 AS libstdcxx
RUN apt-get update && apt-get install -y libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/libstdcxx && \
    libstdcxx_file="$(ls /usr/lib/x86_64-linux-gnu/libstdc++.so.6.* | head -n1)" && \
    cp -a "$libstdcxx_file" /opt/libstdcxx/ && \
    ln -s "$(basename "$libstdcxx_file")" /opt/libstdcxx/libstdc++.so.6

# Use MySQL as the base image
FROM mysql:${MYSQL_VERSION}

# Copy the MyVector plugin and installation script
# MySQL images use /usr/lib64/mysql/plugin on some distros.
COPY myvector.so /usr/lib/mysql/plugin/
RUN if [ -d /usr/lib64/mysql/plugin ]; then \
      cp /usr/lib/mysql/plugin/myvector.so /usr/lib64/mysql/plugin/; \
    fi
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /usr/lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6.* /usr/lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /usr/lib/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6.* /usr/lib/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6.* /lib64/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6 /lib/
COPY --from=libstdcxx /opt/libstdcxx/libstdc++.so.6.* /lib/
RUN libstdcxx_real="$(ls /lib64/libstdc++.so.6.* | grep -v gdb | head -n1)" && \
    ln -sf "$(basename "$libstdcxx_real")" /lib64/libstdc++.so.6 && \
    ln -sf "$(basename "$libstdcxx_real")" /usr/lib64/libstdc++.so.6 && \
    ln -sf "$(basename "$libstdcxx_real")" /usr/lib/libstdc++.so.6 && \
    ln -sf "$(basename "$libstdcxx_real")" /lib/libstdc++.so.6 && \
    rm -f /lib64/libstdc++.so.6.*gdb* /lib/libstdc++.so.6.*gdb* \
      /usr/lib64/libstdc++.so.6.*gdb* /usr/lib/libstdc++.so.6.*gdb* && \
    ldconfig

# The rest will be handled by the default MySQL entrypoint
